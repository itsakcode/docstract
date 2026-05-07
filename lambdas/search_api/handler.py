import os
import json
import time
from typing import Any, Dict, List, Optional

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth

AOSS_ENDPOINT = os.environ["AOSS_ENDPOINT"]
INDEX_NAME = os.environ.get("INDEX_NAME", "ak-idp-global-chunks")
EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))

DEFAULT_SIZE = int(os.environ.get("DEFAULT_SIZE", "10"))
DEFAULT_K = int(os.environ.get("DEFAULT_K", "50"))

# Optional: enrich results from DynamoDB docs table
DOCS_TABLE = os.environ.get("DOCS_TABLE")  # if not set, enrichment is skipped
DOCS_PK = os.environ.get("DOCS_PK", "doc_id")  # assume pk attribute name is doc_id

bedrock = boto3.client("bedrock-runtime")
dynamodb = boto3.resource("dynamodb") if DOCS_TABLE else None


def _cors_headers() -> Dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Methods": "OPTIONS,GET,POST",
        "Content-Type": "application/json",
    }


def _normalize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    Supports:
    - direct invoke: { "q": "...", "size": 10, "filters": {...} }
    - API GW/Function URL: { "body": "{...json...}" } or queryStringParameters
    """
    if event is None:
        return {}

    # Preflight
    if event.get("requestContext") and event.get("httpMethod") == "OPTIONS":
        return {"__preflight__": True}

    body = event.get("body")
    if isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            # fall back to raw event
            pass

    qsp = event.get("queryStringParameters") or {}
    if isinstance(qsp, dict) and qsp:
        # Allow /search?q=...&size=... etc.
        out: Dict[str, Any] = {}
        if "q" in qsp:
            out["q"] = qsp.get("q")
        if "size" in qsp:
            out["size"] = qsp.get("size")
        if "k" in qsp:
            out["k"] = qsp.get("k")
        # filters can be passed as JSON string
        if "filters" in qsp:
            try:
                out["filters"] = json.loads(qsp["filters"])
            except Exception:
                out["filters"] = {}
        return out

    return event


def _get_aoss_client() -> OpenSearch:
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise RuntimeError("AWS_REGION/AWS_DEFAULT_REGION not set")

    credentials = boto3.Session().get_credentials()
    auth = AWSV4SignerAuth(credentials, region, "aoss")

    host = AOSS_ENDPOINT.replace("https://", "").replace("http://", "")
    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=auth,
        use_ssl=True,
        verify_certs=True,
        connection_class=RequestsHttpConnection,
        timeout=20,
        max_retries=3,
        retry_on_timeout=True,
    )


def _bedrock_embed(text: str) -> List[float]:
    body = {"inputText": text, "dimensions": EMBED_DIM}
    resp = bedrock.invoke_model(
        modelId=EMBED_MODEL_ID,
        body=json.dumps(body).encode("utf-8"),
        accept="application/json",
        contentType="application/json",
    )
    payload = json.loads(resp["body"].read())
    emb = payload.get("embedding")
    if not isinstance(emb, list) or not emb:
        raise RuntimeError(f"Unexpected embedding response: {payload}")
    return emb


def _build_query(
    q: str,
    query_vector: List[float],
    size: int,
    k: int,
    doc_types: Optional[List[str]] = None,
    min_confidence: Optional[float] = None,
) -> Dict[str, Any]:
    filters: List[Dict[str, Any]] = []
    if doc_types:
        filters.append({"terms": {"doc_type": doc_types}})
    if min_confidence is not None:
        filters.append({"range": {"confidence": {"gte": float(min_confidence)}}})

    return {
        "size": size,
        "_source": [
            "doc_id",
            "chunk_id",
            "doc_type",
            "confidence",
            "pdf_s3_uri",
            "page_num",
            "chunk_text",
            "extracted_fields",
        ],
        "query": {
            "bool": {
                "filter": filters,
                "should": [
                    {
                        "multi_match": {
                            "query": q,
                            "fields": ["chunk_text^2", "extracted_fields.*"],
                            "type": "best_fields",
                        }
                    },
                    {"knn": {"embedding": {"vector": query_vector, "k": k}}},
                ],
                "minimum_should_match": 1,
            }
        },
    }


def _enrich_docs(doc_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    Returns mapping doc_id -> doc metadata from DynamoDB.
    Assumes table PK attribute name is DOCS_PK (default: doc_id).
    """
    if not DOCS_TABLE or not dynamodb or not doc_ids:
        return {}

    table = dynamodb.Table(DOCS_TABLE)

    # BatchGet via low-level client for performance + simplicity
    client = boto3.client("dynamodb")
    keys = [{DOCS_PK: {"S": d}} for d in set(doc_ids) if d]

    # DynamoDB BatchGet max 100 keys
    out: Dict[str, Dict[str, Any]] = {}
    for i in range(0, len(keys), 100):
        req = {DOCS_TABLE: {"Keys": keys[i : i + 100]}}
        resp = client.batch_get_item(RequestItems=req)
        items = resp.get("Responses", {}).get(DOCS_TABLE, []) or []
        for it in items:
            # minimal unmarshalling (string-ish fields)
            doc_id = it.get(DOCS_PK, {}).get("S")
            if not doc_id:
                continue
            # store the raw dynamodb json; you can prettify later if you want
            out[doc_id] = it
    return out


def lambda_handler(event: Dict[str, Any], context: Any) -> Any:
    t0 = time.time()

    payload = _normalize_event(event)

    # Preflight for API GW / Function URL
    if isinstance(payload, dict) and payload.get("__preflight__"):
        return {"statusCode": 200, "headers": _cors_headers(), "body": ""}

    q = (payload.get("q") or "").strip() if isinstance(payload, dict) else ""
    if not q:
        body = {"ok": False, "error": "Missing query 'q'."}
        # Return API-style if this looks like an HTTP invoke
        if isinstance(event, dict) and ("requestContext" in event or "rawPath" in event or "headers" in event):
            return {"statusCode": 400, "headers": _cors_headers(), "body": json.dumps(body)}
        return body

    size = int(payload.get("size") or DEFAULT_SIZE)
    size = max(1, min(size, 25))

    filters = payload.get("filters") or {}
    doc_types = filters.get("doc_types")
    if doc_types is not None and not isinstance(doc_types, list):
        body = {"ok": False, "error": "filters.doc_types must be a list of strings."}
        return {"statusCode": 400, "headers": _cors_headers(), "body": json.dumps(body)} if "requestContext" in (event or {}) else body

    min_confidence = filters.get("min_confidence")
    if min_confidence is not None:
        try:
            min_confidence = float(min_confidence)
        except Exception:
            body = {"ok": False, "error": "filters.min_confidence must be a number."}
            return {"statusCode": 400, "headers": _cors_headers(), "body": json.dumps(body)} if "requestContext" in (event or {}) else body

    k = int(payload.get("k") or DEFAULT_K)
    k = max(size, min(k, 500))

    query_vector = _bedrock_embed(q)

    client = _get_aoss_client()
    body = _build_query(q=q, query_vector=query_vector, size=size, k=k, doc_types=doc_types, min_confidence=min_confidence)
    resp = client.search(index=INDEX_NAME, body=body)

    hits_obj = resp.get("hits", {})
    hits = hits_obj.get("hits", []) or []
    total = hits_obj.get("total", {})
    total_value = total.get("value") if isinstance(total, dict) else len(hits)

    doc_ids = [(h.get("_source", {}) or {}).get("doc_id") for h in hits]
    doc_meta = _enrich_docs([d for d in doc_ids if d])

    results = []
    for h in hits:
        src = h.get("_source", {}) or {}
        doc_id = src.get("doc_id")
        results.append({
            "score": h.get("_score"),
            "doc_id": doc_id,
            "chunk_id": src.get("chunk_id"),
            "doc_type": src.get("doc_type"),
            "confidence": src.get("confidence"),
            "pdf_s3_uri": src.get("pdf_s3_uri"),
            "page_num": src.get("page_num"),
            "snippet": (src.get("chunk_text") or "")[:600],
            "doc_meta": doc_meta.get(doc_id),  # only present when DOCS_TABLE is set
        })

    out = {
        "ok": True,
        "took_ms": int((time.time() - t0) * 1000),
        "total_hits": total_value,
        "results": results,
    }

    # If invoked over HTTP, return API GW-style response
    if isinstance(event, dict) and ("requestContext" in event or "rawPath" in event or "headers" in event):
        return {"statusCode": 200, "headers": _cors_headers(), "body": json.dumps(out)}
    return out
