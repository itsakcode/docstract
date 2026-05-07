import json
import os
import time
import logging

import boto3

from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ddb = boto3.resource("dynamodb")
sfn = boto3.client("stepfunctions")

TABLE_NAME = os.environ["REVIEW_TABLE_NAME"]

def to_ddb_types(obj):
    """Recursively convert Python floats to Decimal for DynamoDB writes."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: to_ddb_types(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_ddb_types(v) for v in obj]
    return obj

def to_jsonable(obj):
    """Recursively convert DynamoDB Decimals to JSON-safe floats."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    return obj


def _response(code: int, body: dict):
    return {
        "statusCode": code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(to_jsonable(body)),
    }


def lambda_handler(event, context):
    """
    HTTP POST body:
      {
        "reviewId": "...",
        "action": "APPROVE" | "REJECT",
        "doc_type": "OptionalCorrectedType",
        "confidence": 0.9
      }
    """
    logger.info("Event: %s", json.dumps(event))

    try:
        body = json.loads(event.get("body") or "{}")
    except Exception:
        return _response(400, {"ok": False, "error": "Invalid JSON body"})

    review_id = body.get("reviewId")
    action = (body.get("action") or "").upper()

    if not review_id or action not in ["APPROVE", "REJECT"]:
        return _response(400, {"ok": False, "error": "reviewId and action(APPROVE/REJECT) required"})

    table = ddb.Table(TABLE_NAME)
    item = table.get_item(Key={"reviewId": review_id}).get("Item")
    if not item:
        return _response(404, {"ok": False, "error": "ReviewId not found"})

    if item.get("status") != "PENDING":
        return _response(409, {"ok": False, "error": f"Review already {item.get('status')}"})

    task_token = item["taskToken"]
    now = int(time.time())

    if action == "REJECT":
        # Fail the waiting task
        sfn.send_task_failure(taskToken=task_token, error="HumanRejected", cause="Rejected by reviewer")
        table.update_item(
            Key={"reviewId": review_id},
            UpdateExpression="SET #s=:s, decidedAt=:t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "REJECTED", ":t": now},
        )
        return _response(200, {"ok": True, "reviewId": review_id, "status": "REJECTED"})

    # APPROVE: build final classification = original + optional corrections
    original = (item.get("payload") or {}).get("classification") or {}

    final_classification = dict(original)  # copy
    if body.get("doc_type"):
        final_classification["doc_type"] = str(body["doc_type"])

    if body.get("confidence") is not None:
        try:
            final_classification["confidence"] = float(body["confidence"])
        except Exception:
            pass

    # stamp source so you know it was human-reviewed
    final_classification["source"] = "human"

    output = {
        "reviewId": review_id,
        "reviewStatus": "APPROVED",
        "classification": to_jsonable(final_classification),
    }

    sfn.send_task_success(taskToken=task_token, output=json.dumps(output))

    table.update_item(
    Key={"reviewId": review_id},
    UpdateExpression="SET #s=:s, decidedAt=:t, decision=:d",
    ExpressionAttributeNames={"#s": "status"},
    ExpressionAttributeValues=to_ddb_types({
        ":s": "APPROVED",
        ":t": now,
        ":d": output,   # output has floats -> will be converted for DDB
    }),
)

    return _response(200, {"ok": True, "reviewId": review_id, "status": "APPROVED", "decision": output})
