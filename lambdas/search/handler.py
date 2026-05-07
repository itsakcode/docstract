import os, json, re
import boto3
from boto3.dynamodb.conditions import Key


DOCS_TABLE = os.environ["DOCS_TABLE"]
SEARCH_MODE_DEFAULT = os.getenv("SEARCH_MODE", "auto")
OPENSEARCH_ENABLED = os.getenv("OPENSEARCH_ENABLED", "false").lower() == "true"
OPENSEARCH_ENDPOINT = os.getenv("OPENSEARCH_ENDPOINT", "")

ssm = boto3.client("ssm")
ddb = boto3.resource("dynamodb")
table = ddb.Table(DOCS_TABLE)

def get_opensearch_endpoint() -> str | None:
    param = os.getenv("SSM_OS_ENDPOINT_PARAM")
    if not param:
        return None
    try:
        resp = ssm.get_parameter(Name=param)
        return resp["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound:
        return None

def _is_structured_query(q: str) -> bool:
    if not q:
        return False
    q = q.strip()
    # policy number / claim id style: mostly digits or digits+dash
    if re.fullmatch(r"[A-Za-z0-9\-]{6,}", q) and (any(c.isdigit() for c in q)):
        return True
    # VIN is 17 chars alnum (excluding I,O,Q often, but keep simple)
    if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", q.upper()):
        return True
    return False

def dynamo_search(q: str, doc_type: str | None, limit: int = 20):
    # Cost-safe strategy:
    # 1) If doc_type provided -> Query GSI by docType+createdAt desc, then filter locally by q in searchText
    # 2) If q looks like exact id (policyNumber/VIN) -> attempt exact match via GSI (recommended)
    # NOTE: You should create these GSIs in CDK:
    # - GSI_DocTypeCreatedAt: PK=docType, SK=createdAt
    # - GSI_PolicyNumber: PK=policyNumber
    # - GSI_VIN: PK=vin (optional)
    items = []

    if q and _is_structured_query(q):
        # try policyNumber exact match first
        try:
            resp = table.query(
                IndexName="GSI_PolicyNumber",
                KeyConditionExpression=Key("policyNumber").eq(q),
                Limit=limit,
            )
            items = resp.get("Items", [])
        except Exception:
            items = []

    if not items and doc_type:
        resp = table.query(
            IndexName="GSI_DocTypeCreatedAt",
            KeyConditionExpression=Key("docType").eq(doc_type),
            ScanIndexForward=False,
            Limit=200,  # pull a small window, then filter in code
        )
        pool = resp.get("Items", [])
        if q:
            ql = q.lower()
            pool = [it for it in pool if ql in (it.get("searchText","").lower())]
        items = pool[:limit]

    # If no doc_type, avoid full table scan in prod. For dev you can allow a limited scan:
    if not items and q and os.getenv("ALLOW_DDB_SCAN", "false").lower() == "true":
        resp = table.scan(Limit=200)
        pool = resp.get("Items", [])
        ql = q.lower()
        pool = [it for it in pool if ql in (it.get("searchText","").lower())]
        items = pool[:limit]

    return [
        {
            "docId": it["docId"],
            "title": it.get("title", it["docId"]),
            "docType": it.get("docType", "Unknown"),
            "createdAt": it.get("createdAt"),
            "score": 0.75,  # Dynamo is not ranked; use a constant or simple heuristic
            "highlights": [],
            "fields": it.get("fields", {}),
            "source": "dynamo",
        }
        for it in items
    ]

def opensearch_search(q: str, doc_type: str | None, limit: int = 20):
    # Placeholder: implement your current OpenSearch query code here
    # Return the SAME shape as dynamo_search.
    # If you already have requests signing / aws4auth logic, reuse it.
    raise NotImplementedError("OpenSearch provider not wired yet")

def lambda_handler(event, context):
    params = event.get("queryStringParameters") or {}
    q = (params.get("q") or "").strip()
    doc_type = (params.get("docType") or "").strip() or None
    backend = (params.get("backend") or SEARCH_MODE_DEFAULT).lower()
    limit = int(params.get("limit") or "20")

    endpoint = get_opensearch_endpoint()
    opensearch_available = bool(endpoint)

    if backend == "auto":
        # smart routing: structured -> dynamo; otherwise opensearch if available else dynamo
        if _is_structured_query(q) or doc_type:
            backend = "dynamo"
        else:
            backend = "opensearch" if opensearch_available else "dynamo"

    if backend == "opensearch" and not opensearch_available:
        backend = "dynamo"

    if backend == "opensearch":
        results = opensearch_search(q, doc_type, limit)
    elif backend == "dynamo":
        results = dynamo_search(q, doc_type, limit)
    else:
        results = []

    body = {
        "backend": backend,
        "query": q,
        "filters": {"docType": doc_type},
        "results": results
    }
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }
