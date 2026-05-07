import os, json, base64, uuid, datetime
import boto3

s3 = boto3.client("s3")
brt = boto3.client("bedrock-runtime")

OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
CLAUDE_MODEL_ID = os.environ.get("CLAUDE_MODEL_ID")  # set in Lambda env

# ---- your existing helpers (assumed) ----
# _s3_get_json(bucket, key) -> dict
# _s3_get_txt(bucket, key) -> str
# load_registry_bundle(bucket, prefix, doc_type, version, override=False, cfg={...}) -> dict
# jsonschema_validate(payload, schema) -> raise/return

def _s3_get_bytes(bucket, key) -> bytes:
    return s3.get_object(Bucket=bucket, Key=key)["Body"].read()

def _s3_put_json(bucket, key, payload):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

def _guess_media_type(key: str) -> str:
    k = key.lower()
    if k.endswith(".png"): return "image/png"
    if k.endswith(".webp"): return "image/webp"
    if k.endswith(".gif"): return "image/gif"
    return "image/jpeg"

def call_claude_with_image(prompt_text: str, image_bytes: bytes, media_type: str, max_tokens: int = 900) -> str:
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": base64.b64encode(image_bytes).decode("utf-8"),
                        },
                    },
                    {"type": "text", "text": prompt_text},
                ],
            }
        ],
    }

    resp = brt.invoke_model(
        modelId=CLAUDE_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(body),
    )
    raw = json.loads(resp["body"].read())
    # Collect all text blocks
    out = []
    for block in raw.get("content", []):
        if block.get("type") == "text":
            out.append(block.get("text", ""))
    return "\n".join(out).strip()

def _safe_json_loads(text: str) -> dict | None:
    try:
        return json.loads(text)
    except Exception:
        # Sometimes model may wrap JSON in extra text; try to extract first {...}
        try:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                return json.loads(text[start : end + 1])
        except Exception:
            pass
    return None

# ---- template builder from typed schema ----
def build_template_from_schema(schema: dict) -> dict:
    def _resolve_type(t):
        if isinstance(t, list):
            non_null = [x for x in t if x != "null"]
            return non_null[0] if non_null else "null"
        return t

    def _build(node: dict):
        if "default" in node:
            return node["default"]
        t = _resolve_type(node.get("type"))
        if t == "object":
            props = node.get("properties", {})
            return {k: _build(v) for k, v in props.items()}
        if t == "array":
            return []
        return None

    return _build(schema)

def lambda_handler(event, context):
    """
    Event example:
    {
      "bucket": "my-incoming-bucket",
      "key": "incoming/photos/car_001.jpg",
      "doc_type": "CarDamagePhoto",
      "version": "v1",
      "document_id": "optional"
    }
    """
    in_bucket = event["bucket"]
    in_key = event["key"]

    doc_type = event.get("doc_type", "CarDamagePhoto")
    version = event.get("version", "v1")
    document_id = event.get("document_id") or str(uuid.uuid4())

    # Registry location
    REGISTRY_BUCKET = os.environ["CONFIG_BUCKET"]
    REGISTRY_PREFIX = os.environ.get("CONFIG_PREFIX", "schema_registry")

    # Load registry bundle (prompt optional)
    bundle = load_registry_bundle(
        bucket=REGISTRY_BUCKET,
        prefix=REGISTRY_PREFIX,
        doc_type=doc_type,
        version=version,
        override=False,
        cfg={"defaultVersion": "v1"},
    )

    typed_schema = bundle["typed_schema"]
    # Build schema_template from schema defaults (so prompt stays aligned with typed schema)
    schema_template = build_template_from_schema(typed_schema)

    # Ensure required identity fields are set in template (in case schema defaults don't cover them)
    schema_template["document_id"] = document_id
    schema_template["doc_type"] = doc_type
    schema_template["schema_version"] = schema_template.get("schema_version") or version

    # Use doc-type prompt if present, else default prompt text you keep in code/config
    default_prompt_text = os.environ.get("DEFAULT_IMAGE_PROMPT", "")
    prompt_template = bundle.get("extraction_prompt") or default_prompt_text

    # If you want placeholders in prompt like your other doc types:
    # {doc_type} {schema_template}
    prompt_text = prompt_template.format(
        doc_type=doc_type,
        schema_template=json.dumps(schema_template, indent=2)
    )

    # Read image and call Claude
    media_type = _guess_media_type(in_key)
    image_bytes = _s3_get_bytes(in_bucket, in_key)

    model_text = call_claude_with_image(prompt_text, image_bytes, media_type)
    extracted = _safe_json_loads(model_text) or schema_template

    # Enforce identity keys
    extracted["document_id"] = document_id
    extracted["doc_type"] = doc_type
    extracted["schema_version"] = extracted.get("schema_version") or version

    # Validate against typed schema (recommended)
    try:
        jsonschema_validate(extracted, typed_schema)
        valid = True
        validation_error = None
    except Exception as e:
        valid = False
        validation_error = str(e)
        # Keep output, but mark confidence low if invalid
        conf = extracted.get("confidence") or {}
        extracted["confidence"] = conf
        extracted["confidence"]["overall"] = conf.get("overall") if conf.get("overall") is not None else 0.0
        extracted["confidence"]["rationale"] = (conf.get("rationale") or "") + " | Schema validation failed."

    # Write output similar to your other pipeline outputs
    out_key = f"data_extraction/{document_id}.json"
    payload = {
        "document_id": document_id,
        "doc_type": doc_type,
        "schema_version": version,
        "source": {"bucket": in_bucket, "key": in_key},
        "ingested_at": datetime.datetime.utcnow().isoformat() + "Z",
        "validation": {"valid": valid, "error": validation_error},
        "extraction": extracted,
    }
    _s3_put_json(OUTPUT_BUCKET, out_key, payload)

    return {"document_id": document_id, "output_bucket": OUTPUT_BUCKET, "output_key": out_key, "valid": valid}
