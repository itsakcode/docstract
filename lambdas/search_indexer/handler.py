import os
import json
import time
import hashlib
from typing import Any, Dict, List, Tuple, Optional

import boto3
from opensearchpy import OpenSearch, RequestsHttpConnection, AWSV4SignerAuth, helpers


# ----------------------------
# Config (env vars)
# ----------------------------
AOSS_ENDPOINT = os.environ["AOSS_ENDPOINT"]  # e.g. https://xxxx.us-east-2.aoss.amazonaws.com
INDEX_NAME = os.environ.get("INDEX_NAME", "ak-idp-global-chunks")

EMBED_MODEL_ID = os.environ.get("EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))

# Where Textract JSON is stored (your output bucket).
# merged_doc has textractResultKey but not bucket, so we supply bucket via env.
TEXTRACT_BUCKET = os.environ.get("TEXTRACT_BUCKET")  # strongly recommended

# Chunking parameters (tweak anytime)
MAX_CHARS = int(os.environ.get("CHUNK_MAX_CHARS", "1200"))
OVERLAP = int(os.environ.get("CHUNK_OVERLAP", "150"))


s3 = boto3.client("s3")
bedrock = boto3.client("bedrock-runtime")


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def _flatten_to_text(obj: Any, prefix: str = "") -> List[str]:
    """
    Flattens a JSON object into key: value lines for embedding.
    """
    lines: List[str] = []
    if obj is None:
        return lines
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            lines.extend(_flatten_to_text(v, key))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}[{i}]"
            lines.extend(_flatten_to_text(v, key))
    else:
        # scalar
        val = str(obj).strip()
        if val:
            lines.append(f"{prefix}: {val}" if prefix else val)
    return lines

def _chunk_text(text: str, max_chars: int, overlap: int) -> List[str]:
    """
    Simple sliding-window chunker on characters.
    """
    text = (text or "").strip()
    if not text:
        return []

    chunks: List[str] = []
    start = 0
    n = len(text)

    while start < n:
        end = min(start + max_chars, n)
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end == n:
            break
        start = max(0, end - overlap)
    return chunks


def _get_textract_lines_by_page(textract_json: Dict[str, Any]) -> Dict[int, List[str]]:
    """
    Extracts LINE text from Textract and groups by Page (if present).
    """
    by_page: Dict[int, List[str]] = {}
    blocks = textract_json.get("blocks", []) or []

    print("Blocks in Textract JSON:", len(blocks))
    print("Blocks in Textract JSON:", blocks)

    for b in blocks:
        if b.get("BlockType") not in {"LINE", "WORD"}:
            continue
        txt = (b.get("Text") or "").strip()
        if not txt:
            continue
        page = b.get("Page")
        if isinstance(page, int) and page > 0:
            by_page.setdefault(page, []).append(txt)
        else:
            by_page.setdefault(1, []).append(txt)

    print("number of pages with text extracted:", len(by_page))
    
    return by_page

def _bedrock_embed(text: str) -> List[float]:
    """
    Calls Bedrock Titan Text Embeddings v2.
    """
    body = {
        "inputText": text,
        # Titan v2 supports dimensions; keep consistent with your index mapping.
        "dimensions": EMBED_DIM
    }

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


def _get_aoss_client() -> OpenSearch:
    """
    OpenSearch Serverless uses SigV4.
    """
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
        timeout=30,
        max_retries=3,
        retry_on_timeout=True,
    )


def _ensure_index(client: OpenSearch) -> None:
    """
    Create index if missing. Safe to call every run.
    """
    if client.indices.exists(index=INDEX_NAME):
        return

    mapping = {
        "settings": {"index": {"knn": True}},
        "mappings": {
            "properties": {
                "doc_id": {"type": "keyword"},
                "chunk_id": {"type": "keyword"},
                "doc_type": {"type": "keyword"},
                "confidence": {"type": "float"},
                "pdf_s3_uri": {"type": "keyword"},
                "textract_s3_uri": {"type": "keyword"},
                "page_num": {"type": "integer"},
                "ingested_at": {"type": "date"},
                "chunk_text": {"type": "text"},
                "embedding": {"type": "knn_vector", "dimension": EMBED_DIM},
                "extracted_fields": {"type": "object", "enabled": True},
            }
        },
    }

    client.indices.create(index=INDEX_NAME, body=mapping)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    event:
      - pdf_bucket: str
      - pdf_key: str
      - merged_doc: dict
    """
    t0 = time.time()

    pdf_bucket = event.get("pdf_bucket")
    pdf_key = event.get("pdf_key")
    merged = event.get("merged_doc")

    if not isinstance(pdf_bucket, str) or not pdf_bucket:
        raise ValueError("Missing/invalid pdf_bucket")
    if not isinstance(pdf_key, str) or not pdf_key:
        raise ValueError("Missing/invalid pdf_key")
    if not isinstance(merged, dict):
        raise ValueError("Missing/invalid merged_doc")

    # Pull useful metadata from merged JSON
    classification = merged.get("classification", {}) or {}
    doc_type = classification.get("doc_type") or "Unknown"
    confidence = float(classification.get("confidence") or 0.0)

    textract_key = merged.get("textractResultKey")
    if not isinstance(textract_key, str) or not textract_key:
        textract_key = None

    extraction = merged.get("extraction", {}) or {}
    extraction_result = extraction.get("result", {}) or {}
    summary = (extraction_result.get("summary") or "").strip()

    # extracted_fields: everything inside extraction.result except meta-ish keys
    extracted_fields = dict(extraction_result)
    # remove redundant fields if you like
    extracted_fields.pop("summary", None)

    pdf_s3_uri = f"s3://{pdf_bucket}/{pdf_key}"

    # Load Textract JSON (recommended for best semantic search)
    page_texts: Dict[int, str] = {}
    textract_s3_uri: Optional[str] = None

    if textract_key:
        if not TEXTRACT_BUCKET:
            raise RuntimeError(
                "merged_doc has textractResultKey but TEXTRACT_BUCKET env var is not set"
            )
        textract_s3_uri = f"s3://{TEXTRACT_BUCKET}/{textract_key}"
        obj = s3.get_object(Bucket=TEXTRACT_BUCKET, Key=textract_key)
        print("Fetched Textract JSON from S3:", textract_s3_uri)
        textract_json = json.loads(obj["Body"].read())
        by_page_lines = _get_textract_lines_by_page(textract_json)
        for page, lines in by_page_lines.items():
            page_texts[page] = "\n".join(lines).strip()

    # Build base context text to include in each chunk
    flattened = "\n".join(_flatten_to_text(extracted_fields))
    base_context_parts = [
        f"Document Type: {doc_type}",
        f"PDF: {pdf_s3_uri}",
    ]
    if summary:
        base_context_parts.append(f"Summary:\n{summary}")
    if flattened:
        base_context_parts.append(f"Extracted Fields:\n{flattened}")

    base_context = "\n\n".join(base_context_parts).strip()

    # Decide chunk sources:
    # - Prefer Textract per-page text if present
    # - Otherwise embed the base_context only
    chunks: List[Tuple[int, str]] = []  # (page_num, chunk_text)

    if page_texts:
        for page_num, page_text in sorted(page_texts.items()):
            full_text = f"{base_context}\n\nOCR (Page {page_num}):\n{page_text}".strip()
            for i, c in enumerate(_chunk_text(full_text, MAX_CHARS, OVERLAP)):
                chunks.append((page_num, c))
    else:
        for i, c in enumerate(_chunk_text(base_context, MAX_CHARS, OVERLAP)):
            chunks.append((1, c))

    # Deterministic doc_id so re-index overwrites instead of duplicating
    doc_id_seed = f"{pdf_bucket}/{pdf_key}|{doc_type}|{textract_key or ''}"
    doc_id = _sha1(doc_id_seed)

    client = _get_aoss_client()

    print("AOSS_ENDPOINT:", AOSS_ENDPOINT)
    print("INDEX_NAME:", INDEX_NAME)
    print("AWS_REGION:", os.environ.get("AWS_REGION"))

    _ensure_index(client)

    # Bulk index actions
    actions = []
    embedded_count = 0

    for idx, (page_num, chunk_text) in enumerate(chunks):
        # Embedding call per chunk (for demo scale this is fine)
        emb = _bedrock_embed(chunk_text)
        embedded_count += 1

        chunk_id = f"{doc_id}#p{page_num}#c{idx}"
        actions.append({
            "_op_type": "index",
            "_index": INDEX_NAME,
            "_source": {
                "doc_id": doc_id,
                "chunk_id": chunk_id,
                "doc_type": doc_type,
                "confidence": confidence,
                "pdf_s3_uri": pdf_s3_uri,
                "textract_s3_uri": textract_s3_uri,
                "page_num": page_num,
                "ingested_at": int(time.time() * 1000),
                "chunk_text": chunk_text,
                "embedding": emb,
                "extracted_fields": extracted_fields,
            }
        })

    success, failed = helpers.bulk(client, actions, raise_on_error=False, request_timeout=60)

    # do the bulk index

    # NEW LOGGING
    print("Bulk index success count:", success)
    print("Bulk index failed count:", len(failed) if failed else 0)

    # If there are failed items, show the full response
    if failed:
        print("Failed items detail:")
        for item in failed:
            print(item)

    return {
        "ok": True,
        "doc_id": doc_id,
        "doc_type": doc_type,
        "pdf_s3_uri": pdf_s3_uri,
        "textract_s3_uri": textract_s3_uri,
        "chunks_total": len(chunks),
        "embedded": embedded_count,
        "indexed_success": success,
        "indexed_failed": len(failed) if failed else 0,
        "elapsed_ms": int((time.time() - t0) * 1000),
    }
