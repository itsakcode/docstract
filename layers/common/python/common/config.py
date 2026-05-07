import os

def required(name: str) -> str:
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v

OUTPUT_BUCKET = required("OUTPUT_BUCKET")
CONFIG_BUCKET = required("CONFIG_BUCKET")

SCHEMA_PREFIX = os.getenv("SCHEMA_PREFIX", "schema_registry")
VALIDATE_SCHEMA_PREFIX = os.getenv("VALIDATE_SCHEMA_PREFIX", "schemas")
REPORT_PREFIX = os.getenv("REPORT_PREFIX", "validation_reports")

BEDROCK_MODEL_ID = required("BEDROCK_MODEL_ID")

CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.8"))
