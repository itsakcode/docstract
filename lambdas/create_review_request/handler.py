import json
import os
import uuid
import time
import logging

import boto3
from decimal import Decimal

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ddb = boto3.resource("dynamodb")
sns = boto3.client("sns")

TABLE_NAME = os.environ["REVIEW_TABLE_NAME"]
TOPIC_ARN = os.environ.get("REVIEW_TOPIC_ARN")  # optional

def to_ddb_types(obj):
    """
    Recursively convert Python floats to Decimal for DynamoDB.
    """
    if isinstance(obj, float):
        # Convert via string to avoid binary float surprises
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: to_ddb_types(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_ddb_types(v) for v in obj]
    return obj

def lambda_handler(event, context):
    """
    Expected input from SFN includes a taskToken plus context:
      {
        "taskToken": "...",
        "bucket": "...",
        "key": "...",
        "textractResultKey": "...",
        "classification": {...},
        ...
      }
    """
    logger.info("Event: %s", json.dumps(event))

    task_token = event["taskToken"]
    review_id = str(uuid.uuid4())
    now = int(time.time())

    table = ddb.Table(TABLE_NAME)
    item = {
        "reviewId": review_id,
        "status": "PENDING",
        "createdAt": now,
        "taskToken": task_token,
        "payload": event,  # store full context for reviewer/audit
    }

    # convert floats to Decimal before writing
    table.put_item(Item=to_ddb_types(item))

    if TOPIC_ARN:
        sns.publish(
            TopicArn=TOPIC_ARN,
            Subject="IDP Document Review Needed",
            Message=json.dumps(
                {
                    "reviewId": review_id,
                    "status": "PENDING",
                    "bucket": event.get("bucket"),
                    "key": event.get("key"),
                    "classification": event.get("classification"),
                },
                indent=2,
            ),
        )

    # For callback pattern, Step Functions will wait for SendTaskSuccess/Failure.
    return {"reviewId": review_id, "status": "PENDING"}
