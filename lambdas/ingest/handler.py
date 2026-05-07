import json
import os
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sfn = boto3.client("stepfunctions")
STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]


def lambda_handler(event, context):
    """
    Triggered directly by S3 event notification.
    Starts a Step Functions execution with { bucket, key }.
    """
    logger.info("Event: %s", json.dumps(event))

    records = event.get("Records", [])
    if not records:
        logger.error("No Records in event")
        return {"ok": False, "reason": "no records"}

    record = records[0]
    s3_info = record.get("s3", {})
    bucket = s3_info.get("bucket", {}).get("name")
    key = s3_info.get("object", {}).get("key")

    if not bucket or not key:
        logger.error("Missing bucket or key in S3 event")
        return {"ok": False, "reason": "missing bucket/key"}

    input_payload = json.dumps({"bucket": bucket, "key": key})
    logger.info("Starting state machine with input: %s", input_payload)

    sfn.start_execution(
        stateMachineArn=STATE_MACHINE_ARN,
        input=input_payload,
    )

    return {"ok": True, "bucket": bucket, "key": key}
