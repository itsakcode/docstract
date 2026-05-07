import json
import os
import uuid
import logging
from decimal import Decimal
from common.schema_utils import load_schema_bundle
from datetime import datetime

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")

OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
MODEL_ID = os.environ["BEDROCK_MODEL_ID"]  # inference profile ARN/ID works

# --- Helpers ---
def to_jsonable(obj):
    # If any Decimal slips in from DDB or elsewhere
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    return obj

def invoke_nova_converse(system_prompt: str, user_prompt: str) -> str:
    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": 1200, "temperature": 0.0, "topP": 1.0},
    )
    return resp["output"]["message"]["content"][0]["text"]

def parse_json_strict(raw: str) -> dict:
    raw = (raw or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end+1]
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("Could not parse JSON from model output. Raw=%s", raw)
        return {"error": "MODEL_OUTPUT_NOT_JSON", "raw": raw[:2000]}

DATE_FORMAT_MAP = {
    "MM/DD/YYYY": "%m/%d/%Y",
    "YYYY-MM-DD": "%Y-%m-%d",
    "MMM DD, YYYY": "%b %d, %Y",
    "MMMM DD, YYYY": "%B %d, %Y",
}

def normalize_date_with_mapping(value: str, formats: list[str]):
    if not value:
        return value

    value = value.strip()
    value = value.replace(".", "") # to remove . if it is in the date
    value = " ".join(value.split())

    for fmt in formats:
        py_fmt = DATE_FORMAT_MAP.get(fmt)
        if not py_fmt:
            continue
        try:
            return datetime.strptime(value, py_fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue

    return value  # fallback: leave unchanged

#--- Schema/template helpers ---
def prune_template(template: dict, remove_keys=None) -> dict:
    """
    Remove top-level keys from template so the model only fills extraction fields.
    """
    if remove_keys is None:
        remove_keys = {"document_id", "doc_type", "schema_version", "_meta"}
    if not isinstance(template, dict):
        return template
    return {k: v for k, v in template.items() if k not in remove_keys}

def _index_blocks(blocks):
    return {b["Id"]: b for b in blocks if "Id" in b}

def _get_child_text(block_map, block):
    texts = []
    for rel in block.get("Relationships", []):
        if rel.get("Type") == "CHILD":
            for cid in rel.get("Ids", []):
                cb = block_map.get(cid)
                if not cb:
                    continue
                if cb.get("BlockType") == "WORD":
                    texts.append(cb.get("Text", ""))
                elif cb.get("BlockType") == "SELECTION_ELEMENT":
                    if cb.get("SelectionStatus") == "SELECTED":
                        texts.append("SELECTED")
    return " ".join([t for t in texts if t]).strip()

def extract_kv_pairs(textract_json: dict, max_pairs: int = 80):
    """
    Returns list of {key, value, key_confidence, value_confidence}
    Uses KEY_VALUE_SET blocks.
    """
    blocks = textract_json.get("Blocks") or textract_json.get("blocks") or []
    block_map = _index_blocks(blocks)

    key_blocks = []
    value_blocks = []

    for b in blocks:
        if b.get("BlockType") == "KEY_VALUE_SET":
            ent = b.get("EntityTypes", [])
            if "KEY" in ent:
                key_blocks.append(b)
            elif "VALUE" in ent:
                value_blocks.append(b)

    value_by_id = {vb["Id"]: vb for vb in value_blocks if "Id" in vb}

    pairs = []
    for kb in key_blocks:
        key_text = _get_child_text(block_map, kb)
        if not key_text:
            continue

        # Find linked VALUE blocks
        val_text = ""
        val_conf = None
        for rel in kb.get("Relationships", []):
            if rel.get("Type") == "VALUE":
                for vid in rel.get("Ids", []):
                    vb = value_by_id.get(vid)
                    if vb:
                        val_text = _get_child_text(block_map, vb)
                        val_conf = vb.get("Confidence")
                        break

        pairs.append({
            "key": key_text,
            "value": val_text,
            "key_confidence": kb.get("Confidence"),
            "value_confidence": val_conf
        })

        if len(pairs) >= max_pairs:
            break

    # Keep the most confident pairs first
    pairs.sort(key=lambda x: (x.get("key_confidence") or 0) + (x.get("value_confidence") or 0), reverse=True)
    return pairs[:max_pairs]


#--- Prompt builders ---
def extract_lines(textract_json: dict, max_lines: int = 400):
    blocks = textract_json.get("Blocks") or textract_json.get("blocks") or []
    lines = []
    for b in blocks:
        if b.get("BlockType") == "LINE" and b.get("Text"):
            lines.append(b["Text"])
            if len(lines) >= max_lines:
                break
    return lines

def anchor_snippets(lines, anchors, window: int = 2, max_snippets: int = 60):
    """
    Find anchors in lines and return snippets +/- window lines.
    """
    if not lines or not anchors:
        return []

    anchors_lc = [a.lower() for a in anchors if a]
    seen = set()
    snippets = []

    for i, line in enumerate(lines):
        ll = line.lower()
        if any(a in ll for a in anchors_lc):
            start = max(0, i - window)
            end = min(len(lines), i + window + 1)
            snippet = "\n".join(lines[start:end]).strip()
            if snippet and snippet not in seen:
                seen.add(snippet)
                snippets.append(snippet)
                if len(snippets) >= max_snippets:
                    break

    return snippets

def build_evidence_pack(textract_json: dict, bundle: dict) -> str:
    mapping = bundle.get("mapping", {}) or {}
    mapping_fields = mapping.get("fields", {}) or {}

    # Gather anchors across all fields (cap to keep prompt small)
    anchors = []
    for cfg in mapping_fields.values():
        for a in (cfg.get("anchors") or []):
            if a and a not in anchors:
                anchors.append(a)
        if len(anchors) >= 80:
            break

    lines = extract_lines(textract_json, max_lines=500)
    snippets = anchor_snippets(lines, anchors, window=2, max_snippets=50)
    kvs = extract_kv_pairs(textract_json, max_pairs=60)

    evidence = []
    if kvs:
        evidence.append("TEXTRACT_KEY_VALUES (most confident):")
        evidence.append(json.dumps(kvs[:60], indent=2))

    if snippets:
        evidence.append("\nANCHOR_SNIPPETS (lines around key anchors):")
        # Join snippets, but cap size
        joined = "\n\n---\n\n".join(snippets)
        evidence.append(joined[:12000])

    # small fallback context
    if lines:
        evidence.append("\nFIRST_LINES (fallback context):")
        evidence.append("\n".join(lines[:120])[:6000])

    return "\n".join(evidence)

def schema_to_template(schema: dict) -> dict:
    if not schema:
        return {}

    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        if "object" in schema_type:
            schema_type = "object"
        else:
            schema_type = next((t for t in schema_type if t != "null"), schema_type[0])

    if schema_type == "object":
        props = schema.get("properties", {}) or {}
        out = {}
        for k, v in props.items():
            if "default" in v:
                out[k] = v["default"]
            else:
                out[k] = schema_to_template(v)
        return out

    if schema_type == "array":
        items = schema.get("items", {}) or {}
        items_type = items.get("type")
        if items_type == "object" or (isinstance(items_type, list) and "object" in items_type):
            return [schema_to_template(items)]
        return []

    if "default" in schema:
        return schema["default"]

    return None

def build_prompts_from_registry(doc_type: str, bundle: dict, evidence_pack: str):
    typed_schema = bundle["typed_schema"]
    template = schema_to_template(typed_schema)

    # Remove metadata keys from template to avoid confusion
    template = prune_template(template, remove_keys={"document_id", "doc_type", "schema_version", "_meta"})

    system = (
        "You are an information extraction engine. "
        "Return STRICT JSON only. No markdown, no commentary. "
        "You MUST output JSON matching the TEMPLATE exactly: "
        "For 'summary', write 2-3 sentences describing what the document is and key facts "
        "same keys and nesting. Use null when unknown. Do not add new keys."
    )

    default_prompt = (
        f"Document type: {doc_type}\n\n"
        "TEMPLATE (output must match exactly):\n"
        f"{json.dumps(template, indent=2)}\n\n"
        "EVIDENCE (use this; do not guess beyond it):\n"
        f"{evidence_pack}\n"
    )

    user_prompt_template = bundle.get("extraction_prompt") 
    
    if user_prompt_template is not None:
        user_prompt = user_prompt_template.format(
            doc_type=doc_type,
            schema_template=json.dumps(template, indent=2),
            evidence_pack=evidence_pack
        )
    else:
        user_prompt = default_prompt

    print("default_prompt:", default_prompt[:500])
    print("user_prompt:", user_prompt[:500])

    user = user_prompt

    return system, user

# --- Lambda handler ---
def lambda_handler(event, context):
    logger.info("Event: %s", json.dumps(to_jsonable(event)))

    textract_key = event["textractResultKey"]
    doc_type = (event.get("classification") or {}).get("doc_type", "Other")

    # Load schema bundle FIRST (drives extraction)
    bundle = load_schema_bundle(doc_type, event)

    obj = s3.get_object(Bucket=OUTPUT_BUCKET, Key=textract_key)
    textract_json = json.loads(obj["Body"].read())

    MAX_EVIDENCE_CHARS = int(os.getenv("MAX_EVIDENCE_CHARS", "18000"))

    evidence_pack = build_evidence_pack(textract_json, bundle)
    evidence_pack = evidence_pack[:MAX_EVIDENCE_CHARS]

    system_prompt, user_prompt = build_prompts_from_registry(doc_type, bundle, evidence_pack)

    logger.info("EvidencePack chars=%d, userPrompt chars=%d", len(evidence_pack), len(user_prompt))
    logger.info("UserPrompt: %s", user_prompt[:1000])
    logger.info("EvidencePackPreview: %s", evidence_pack[:800])

    raw = invoke_nova_converse(system_prompt, user_prompt)
    extracted = parse_json_strict(raw)

    # Attach schema metadata to extracted output (still good)
    extracted["doc_type"] = doc_type
    extracted["schema_version"] = bundle["version"]
    extracted.setdefault("_meta", {})
    extracted["_meta"]["schema"] = {
        "doc_type": doc_type,
        "version": bundle["version"],
        "id": bundle["typed_schema"].get("$id"),
    }

    mapping = bundle.get("mapping", {})
    fields = mapping.get("fields", {})

    for field_path, cfg in fields.items():
        if "date_formats" not in cfg:
            continue

        formats = cfg["date_formats"]

        # Walk into extracted dict using dotted path
        parts = field_path.split(".")
        target = extracted
        for p in parts[:-1]:
            target = target.get(p)
            if not isinstance(target, dict):
                target = None
                break

        if target and parts[-1] in target:
            before = target.get(parts[-1])
            after = normalize_date_with_mapping(before, formats)
            if before != after:
                logger.info("Normalized date %s: '%s' -> '%s' using %s", field_path, before, after, formats)
            target[parts[-1]] = after

    out_key = f"extraction/{uuid.uuid4()}.json"
    payload = {
        "source": {"bucket": event.get("bucket"), "key": event.get("key")},
        "textractResultKey": textract_key,
        "classification": event.get("classification"),
        "modelId": MODEL_ID,
        "extraction": {
            "result": extracted,
        },
    }

    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=out_key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    logger.info("Extraction written to s3://%s/%s", OUTPUT_BUCKET, out_key)
    return {**event, "extractionResultKey": out_key}
