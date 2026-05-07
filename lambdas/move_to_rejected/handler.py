import json
import os
import logging
from urllib.parse import unquote_plus

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

INPUT_BUCKET = os.environ["INPUT_BUCKET"]
SOURCE_PREFIX = os.environ.get("SOURCE_PREFIX", "incoming/")
DEST_PREFIX = os.environ.get("DEST_PREFIX", "rejected/")


def lambda_handler(event, context):
    """
    Input event should include:
      - bucket: (input bucket name) (optional; we use INPUT_BUCKET)
      - key: original key, usually incoming/...

    Moves object:
      incoming/<file> -> processed/<file>
    """
    logger.info("Event: %s", json.dumps(event))

    bucket = event.get("bucket") or INPUT_BUCKET
    key = unquote_plus(event["key"])

    if not key.startswith(SOURCE_PREFIX):
        # If the key isn't under incoming/, don't move it
        logger.warning("Key does not start with %s, skipping move: %s", SOURCE_PREFIX, key)
        return {**event, "moved": False, "reason": "not_in_source_prefix"}

    dest_key = DEST_PREFIX + key[len(SOURCE_PREFIX):]

    # Copy
    s3.copy_object(
        Bucket=bucket,
        Key=dest_key,
        CopySource={"Bucket": bucket, "Key": key},
    )

    # Delete original
    s3.delete_object(Bucket=bucket, Key=key)

    logger.info("Moved s3://%s/%s -> s3://%s/%s", bucket, key, bucket, dest_key)

    return {**event, "moved": True, "processedKey": dest_key}
