import json
import os
import uuid
import logging

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")

OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]  # e.g., amazon.nova-pro-v1:0 OR anthropic....


DOC_TYPES = [
    "HCFA1500",
    "AutoPolicyDeclarations",
    "DriverLicense", 
    "CarDamagePhoto",
    "Invoice",
    "W2",
]

SYSTEM_PROMPT = (
    "You are a document classifier in an Intelligent Document Processing pipeline. "
    "Return STRICT JSON ONLY with keys: doc_type (string), confidence (number 0..1), "
    "reasoning for your choice (string). "
    f"Allowed doc_type values: {DOC_TYPES}. "
    "No extra keys. No explanation."
)


def extract_text(textract_json: dict, max_chars: int = 12000) -> str:
    blocks = textract_json.get("blocks", [])
    lines = []
    total = 0
    for b in blocks:
        if b.get("BlockType") == "LINE" and b.get("Text"):
            t = b["Text"]
            lines.append(t)
            total += len(t) + 1
            if total >= max_chars:
                break
    return "\n".join(lines)[:max_chars]

def rule_based_override(text: str):
    t = text.upper()
    if "POLICY DECLARATIONS" in t:
        return {
            "doc_type": "AutoPolicyDeclarations",
            "confidence": 0.99,
            "reason": "keyword: POLICY DECLARATIONS",
        }
    return None

def invoke_nova_old(prompt: str) -> str:
    body = {
        "inputText": prompt,
        "textGenerationConfig": {
            "maxTokenCount": 200,
            "temperature": 0,
            "topP": 1,
        },
    }
    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )
    payload = json.loads(resp["body"].read())
    # Nova usually: results[0].outputText
    if "results" in payload and payload["results"]:
        return payload["results"][0].get("outputText", "")
    return payload.get("outputText", "")


def invoke_nova(prompt: str) -> str:
    """
    Use Bedrock Runtime Converse API for Nova.
    Returns the model's raw text output (expected to be JSON).
    """
    resp = bedrock.converse(
        modelId=MODEL_ID,  # <-- inference profile ARN/ID works here too
        system=[{"text": SYSTEM_PROMPT}],
        messages=[
            {
                "role": "user",
                "content": [{"text": prompt}],
            }
        ],
        inferenceConfig={
            "maxTokens": 200,
            "temperature": 0.0,
            "topP": 1.0,
        },
    )

    # Bedrock Converse response shape
    return resp["output"]["message"]["content"][0]["text"]


def invoke_claude(prompt: str) -> str:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 200,
        "temperature": 0,
        "system": SYSTEM_PROMPT,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
    }
    resp = bedrock.invoke_model(
        modelId=MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )
    payload = json.loads(resp["body"].read())
    return (payload.get("content") or [{}])[0].get("text", "")


def parse_json_strict(raw: str) -> dict:
    raw = (raw or "").strip()
    # strip any wrapper text
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]
    try:
        obj = json.loads(raw)
    except Exception:
        logger.warning("Could not parse model JSON. Raw=%s", raw)
        return {"doc_type": "Other", "confidence": 0.0}

    doc_type = str(obj.get("doc_type", "Other"))
    conf = obj.get("confidence", 0.0)
    try:
        conf = float(conf)
    except Exception:
        conf = 0.0

    conf = max(0.0, min(1.0, conf))
    if doc_type not in DOC_TYPES:
        return {"doc_type": "Other", "confidence": min(conf, 0.3)}

    return {"doc_type": doc_type, "confidence": conf}


def lambda_handler(event, context):
    """
    Input (from SFN after Textract):
      {
        "status": "SUCCEEDED",
        "bucket": "<raw bucket>",
        "key": "<raw key>",
        "textractJobId": "...",
        "textractResultKey": "intermediate/textract/<uuid>.json"
      }
    """
    logger.info("Event: %s", json.dumps(event))

    if event.get("status") != "SUCCEEDED":
        return {**event, "classification": {"doc_type": "Other", "confidence": 0.0}}

    textract_key = event["textractResultKey"]

    obj = s3.get_object(Bucket=OUTPUT_BUCKET, Key=textract_key)
    textract_json = json.loads(obj["Body"].read())

    text = extract_text(textract_json)

    prompt = f"Document text:\n{text}\n\nReturn JSON now."

    override = rule_based_override(text)
    if override:
        classification = override
    else:
        if MODEL_ID.startswith("anthropic."):
            raw = invoke_claude(prompt)
        else:
            raw = invoke_nova(prompt)

        classification = parse_json_strict(raw)

    out_key = f"classification/{uuid.uuid4()}.json"
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=out_key,
        Body=json.dumps(
            {
                "source": {"bucket": event["bucket"], "key": event["key"]},
                "textractResultKey": textract_key,
                "modelId": MODEL_ID,
                "classification": classification,
            },
            indent=2,
        ).encode("utf-8"),
        ContentType="application/json",
    )

    logger.info("Classification written to s3://%s/%s", OUTPUT_BUCKET, out_key)

    return {**event, "classification": classification, "classificationResultKey": out_key}
