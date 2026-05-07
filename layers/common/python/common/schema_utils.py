import json
import os
import time
import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")

_SCHEMA_CACHE = {"expires_at": 0, "registry": None, "docs": {}}

def _now() -> int:
    return int(time.time())

def _get_env(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v if v is not None and v != "" else default

def _s3_get_json(bucket: str, key: str) -> dict:
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except ClientError as e:
        raise RuntimeError(f"Failed to read s3://{bucket}/{key}: {e}")
    
def _s3_get_txt(bucket: str, key: str) -> str:
    """
    Read a text (.txt) file from S3 and return its full content as a string.
    """
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
        text = obj["Body"].read().decode("utf-8")
        return text
    except ClientError as e:
        #raise RuntimeError(f"Failed to read s3://{bucket}/{key}: {e}")
        return None

def load_schema_bundle(doc_type: str, event: dict | None = None) -> dict:
    """
    Returns:
      {
        "doc_type": "...",
        "version": "v1",
        "extraction_prompt": "...",
        "base_schema": {...},
        "typed_schema": {...},
        "mapping": {...}
      }
    """
    bucket = _get_env("CONFIG_BUCKET")
    prefix = _get_env("SCHEMA_PREFIX", "schema_registry").strip("/")  # align with your dash convention
    ttl = int(_get_env("SCHEMA_CACHE_TTL_SECONDS", "300"))

    if not bucket:
        raise RuntimeError("CONFIG_BUCKET env var is required")

    # Cache registry
    if _SCHEMA_CACHE["registry"] is None or _SCHEMA_CACHE["expires_at"] < _now():
        registry_key = f"{prefix}/registry.json"
        _SCHEMA_CACHE["registry"] = _s3_get_json(bucket, registry_key)
        _SCHEMA_CACHE["docs"] = {}
        _SCHEMA_CACHE["expires_at"] = _now() + ttl

    registry = _SCHEMA_CACHE["registry"]

    # Resolve doc type config with fallback
    cfg = registry.get(doc_type) or registry.get("Other")
    if not cfg:
        raise RuntimeError(f"doc_type '{doc_type}' not found and no 'Other' fallback in registry.json")

    # Optional override from the running event
    override = None
    if event:
        override = (event.get("classification") or {}).get("schema_version_override")
        if override:
            override = str(override).strip()

    version = override or cfg.get("defaultVersion", "v1")

    cache_key = f"{doc_type}:{version}"
    if cache_key in _SCHEMA_CACHE["docs"]:
        return _SCHEMA_CACHE["docs"][cache_key]

    base_key  = f"{prefix}/doc_types/{doc_type}/base.schema.json"
    extraction_prompt_key  = f"{prefix}/doc_types/{doc_type}/{version}/extraction_prompt.txt"
    typed_key = f"{prefix}/doc_types/{doc_type}/{version}/typed.schema.json"
    map_key   = f"{prefix}/doc_types/{doc_type}/{version}/mapping.json"

    try:
        bundle = {
            "doc_type": doc_type,
            "version": version,
            "extraction_prompt": _s3_get_txt(bucket, extraction_prompt_key),
            "base_schema": _s3_get_json(bucket, base_key),
            "typed_schema": _s3_get_json(bucket, typed_key),
            "mapping": _s3_get_json(bucket, map_key),
        }
    except Exception as e:
        # If override was requested but doesn't exist, fall back to defaultVersion
        if override:
            fallback_version = cfg.get("defaultVersion", "v1")
            prompt_key_fb   = f"{prefix}/doc_types/{doc_type}/{fallback_version}/extraction_prompt.txt"
            typed_key_fb = f"{prefix}/doc_types/{doc_type}/{fallback_version}/typed.schema.json"
            map_key_fb   = f"{prefix}/doc_types/{doc_type}/{fallback_version}/mapping.json"

            bundle = {
                "doc_type": doc_type,
                "version": fallback_version,
                "extraction_prompt": _s3_get_txt(bucket, prompt_key_fb),
                "base_schema": _s3_get_json(bucket, base_key),
                "typed_schema": _s3_get_json(bucket, typed_key_fb),
                "mapping": _s3_get_json(bucket, map_key_fb),
                "_meta": {
                    "override_requested": override,
                    "override_failed": True,
                    "error": str(e),
                },
            }
        else:
            raise

    _SCHEMA_CACHE["docs"][cache_key] = bundle
    return bundle

