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

QA_MODEL_ID = os.environ["QA_MODEL_ID"]

DEFAULT_SIZE = int(os.environ.get("DEFAULT_SIZE", "6"))
DEFAULT_K = int(os.environ.get("DEFAULT_K", "50"))

bedrock = boto3.client("bedrock-runtime")


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
        ],
        "query": {
            "bool": {
                "filter": filters,
                "should": [
                    {
                        "multi_match": {
                            "query": q,
                            "fields": ["chunk_text^2"],
                            "type": "best_fields",
                        }
                    },
                    {"knn": {"embedding": {"vector": query_vector, "k": k}}},
                ],
                "minimum_should_match": 1,
            }
        },
    }


def _invoke_textgen(prompt: str) -> str:
    # Nova expects "messages" format. Converse API returns:
    # response["output"]["message"]["content"][0]["text"]
    resp = bedrock.converse(
        modelId=QA_MODEL_ID,  # e.g. "amazon.nova-pro-v1:0"
        messages=[
            {
                "role": "user",
                "content": [{"text": prompt}],
            }
        ],
        inferenceConfig={
            "maxTokens": 700,
            "temperature": 0.2,
            "topP": 0.9,
        },
    )
    return resp["output"]["message"]["content"][0]["text"]

def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    t0 = time.time()

    question = (event.get("question") or "").strip()
    if not question:
        return {"ok": False, "error": "Missing 'question'."}

    size = int(event.get("size") or DEFAULT_SIZE)
    size = max(1, min(size, 12))

    filters = event.get("filters") or {}
    doc_types = filters.get("doc_types")
    if doc_types is not None and not isinstance(doc_types, list):
        return {"ok": False, "error": "filters.doc_types must be a list of strings."}

    min_confidence = filters.get("min_confidence")
    if min_confidence is not None:
        try:
            min_confidence = float(min_confidence)
        except Exception:
            return {"ok": False, "error": "filters.min_confidence must be a number."}

    k = int(event.get("k") or DEFAULT_K)
    k = max(size, min(k, 200))

    # 1) Retrieve
    qv = _bedrock_embed(question)
    client = _get_aoss_client()
    body = _build_query(question, qv, size=size, k=k, doc_types=doc_types, min_confidence=min_confidence)
    resp = client.search(index=INDEX_NAME, body=body)

    hits = (resp.get("hits") or {}).get("hits") or []
    chunks = []
    for i, h in enumerate(hits, start=1):
        src = (h.get("_source") or {})
        chunks.append(
            {
                "n": i,
                "score": h.get("_score"),
                "doc_id": src.get("doc_id"),
                "chunk_id": src.get("chunk_id"),
                "page_num": src.get("page_num"),
                "text": src.get("chunk_text") or "",
                "pdf_s3_uri": src.get("pdf_s3_uri"),
            }
        )

    print(f"Retrieved {len(chunks)} chunks for question.")

    context_text = "\n\n".join(
        [f"[{c['n']}] doc={c['doc_id']} page={c['page_num']}:\n{c['text']}" for c in chunks]
    )

    prompt = f"""You are a document Q&A assistant.
Answer ONLY using the context below. If the answer is not present, say "I don't have enough information in the retrieved documents."

Question:
{question}

Context:
{context_text}

Return a concise answer, then list citations as [n] references you used.
"""

    # 2) Generate answer
    answer = _invoke_textgen(prompt)

    return {
        "ok": True,
        "took_ms": int((time.time() - t0) * 1000),
        "question": question,
        "answer": answer,
        "citations": [{"n": c["n"], "doc_id": c["doc_id"], "page_num": c["page_num"], "chunk_id": c["chunk_id"]} for c in chunks[:5]],
        "top_chunks": chunks,  # keep for demo/debug; remove later
    }
