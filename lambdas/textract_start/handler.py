import json
import logging
import boto3
from urllib.parse import unquote_plus

logger = logging.getLogger()
logger.setLevel(logging.INFO)

textract = boto3.client("textract")


def lambda_handler(event, context):
    """
    Input (from SFN): { bucket: "...", key: "..." }
    Output: { bucket, key, textractJobId }
    """
    logger.info("Event: %s", json.dumps(event))

    bucket = event["bucket"]
    key = unquote_plus(event["key"])

    logger.info("Key: %s", key)

    resp = textract.start_document_analysis(
        DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}},
        FeatureTypes=["TABLES", "FORMS"],
    )

    job_id = resp["JobId"]
    logger.info("Started Textract JobId=%s", job_id)

    return {"bucket": bucket, "key": key, "textractJobId": job_id}
