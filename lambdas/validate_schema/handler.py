import json
import os
import boto3
import logging
from jsonschema import Draft202012Validator

s3 = boto3.client("s3")

logger = logging.getLogger()
logger.setLevel(logging.INFO)

from common.config import (
    CONFIG_BUCKET,
    OUTPUT_BUCKET,
    VALIDATE_SCHEMA_PREFIX,
    BEDROCK_MODEL_ID,
    REPORT_PREFIX,
)

def _read_json(bucket: str, key: str):
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))

def _put_json(bucket: str, key: str, payload: dict):
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

def lambda_handler(event, context):
    """
    Log-only schema validation:
    - Loads schema from CONFIG_BUCKET/{VALIDATE_SCHEMA_PREFIX}/{doc_type}.schema.json (example)
    - Loads extracted JSON either from event or from OUTPUT_BUCKET + key
    - Writes a validation report to OUTPUT_BUCKET/{REPORT_PREFIX}/...
    - Always returns success
    """
    doc_type = (
        event.get("doc_type")
        or event.get("classification", {}).get("doc_type")
        or "Unknown"
    )

    doc_type = doc_type.strip().lower()

    # Decide where extracted JSON is coming from
    logger.info(f"Validating schema event: {event}")
    extracted = event.get("extraction", {}).get("result")
    extracted_s3_key = event.get("extractionResultKey")

    if extracted is None and extracted_s3_key:
        extracted = _read_json(OUTPUT_BUCKET, extracted_s3_key)
    elif extracted is None:
        extracted = event.get("extracted")  # optional fallback

    if isinstance(extracted, str):
        extracted = json.loads(extracted)
    logger.info(f"Validating schema extracted json: {extracted}")

    # If we loaded the full pipeline wrapper JSON, validate the actual extracted payload
    if isinstance(extracted, dict) and isinstance(extracted.get("extraction"), dict):
        extracted = extracted["extraction"].get("result", extracted)

    # If still missing, log-only “could not validate”
    if extracted is None:
        report = {
            "doc_type": doc_type,
            "status": "SKIPPED",
            "reason": "No extracted JSON found in event (extraction.result/extraction.output_key).",
            "errors": [],
        }
        report_key = f"{REPORT_PREFIX}/{context.aws_request_id}.json"
        _put_json(OUTPUT_BUCKET, report_key, report)
        event["validation"] = {"status": "SKIPPED", "report_key": report_key}
        return event

    schema_key = f"{VALIDATE_SCHEMA_PREFIX}/{doc_type}.schema.json"
    try:
        schema = _read_json(CONFIG_BUCKET, schema_key)
        validator = Draft202012Validator(schema)
        errors = []
        for e in sorted(validator.iter_errors(extracted), key=lambda x: tuple(x.path)):
            errors.append({
                "path": "/".join([str(p) for p in e.path]),
                "message": e.message,
                "validator": e.validator,
            })

        status = "PASS" if not errors else "FAIL"
        report = {
            "doc_type": doc_type,
            "schema_key": schema_key,
            "status": status,
            "error_count": len(errors),
            "errors": errors[:200],  # keep reports bounded
            "request_id": context.aws_request_id,
        }
    except Exception as ex:
        # Still log-only: schema load issues, invalid schema, etc.
        report = {
            "doc_type": doc_type,
            "schema_key": schema_key,
            "status": "ERROR",
            "error": str(ex),
            "errors": [],
            "request_id": context.aws_request_id,
        }

    # Pick a stable report key if you have doc_id; otherwise request_id
    doc_id = event.get("doc_id") or event.get("document_id") or context.aws_request_id
    report_key = f"{REPORT_PREFIX}/{doc_type}/{doc_id}.json"

    _put_json(OUTPUT_BUCKET, report_key, report)

    # Attach summary into state (small)
    event["validation"] = {
        "status": report["status"],
        "error_count": report.get("error_count", 0),
        "report_key": report_key,
        "schema_key": report.get("schema_key"),
    }
    return event
