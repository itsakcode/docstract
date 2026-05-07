import json
import os
import uuid
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

textract = boto3.client("textract")
s3 = boto3.client("s3")

OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]


def lambda_handler(event, context):
    """
    Input: { bucket, key, textractJobId }
    Output (when IN_PROGRESS): { status: "IN_PROGRESS", ... }
    Output (when SUCCEEDED): { status: "SUCCEEDED", textractResultKey: "intermediate/textract/....json", ... }
    """
    logger.info("Event: %s", json.dumps(event))

    bucket = event["bucket"]
    key = event["key"]
    job_id = event["textractJobId"]

    # First call to check status
    first = textract.get_document_analysis(JobId=job_id, MaxResults=1000)
    status = first["JobStatus"]

    if status in ["IN_PROGRESS"]:
        return {"status": "IN_PROGRESS", "bucket": bucket, "key": key, "textractJobId": job_id}

    if status != "SUCCEEDED":
        # FAILED or PARTIAL_SUCCESS etc.
        msg = first.get("StatusMessage", "Textract job did not succeed")
        logger.error("Textract status=%s message=%s", status, msg)
        return {"status": status, "bucket": bucket, "key": key, "textractJobId": job_id, "error": msg}

    # SUCCEEDED: fetch all pages via NextToken
    blocks = first.get("Blocks", [])
    next_token = first.get("NextToken")

    while next_token:
        page = textract.get_document_analysis(JobId=job_id, MaxResults=1000, NextToken=next_token)
        blocks.extend(page.get("Blocks", []))
        next_token = page.get("NextToken")

    result = {
        "source": {"bucket": bucket, "key": key},
        "textractJobId": job_id,
        "jobStatus": status,
        "blocks": blocks,
    }

    out_key = f"textract/{uuid.uuid4()}.json"
    s3.put_object(
        Bucket=OUTPUT_BUCKET,
        Key=out_key,
        Body=json.dumps(result).encode("utf-8"),
        ContentType="application/json",
    )

    logger.info("Wrote Textract result to s3://%s/%s", OUTPUT_BUCKET, out_key)

    return {
        "status": "SUCCEEDED",
        "bucket": bucket,
        "key": key,
        "textractJobId": job_id,
        "textractResultKey": out_key,
    }
