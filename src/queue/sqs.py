import os
import json
import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

class SQSClient:
    def __init__(self):
        self.endpoint_url = os.getenv("SQS_ENDPOINT_URL", "http://localstack:4566")
        self.queue_name = os.getenv("SQS_QUEUE_NAME", "inference-requests")
        self.region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        self.sqs = boto3.client(
            "sqs",
            region_name=self.region,
            endpoint_url=self.endpoint_url,
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
        )
        self.queue_url: str = None

    def init_queue(self):
        """Create the queue if it doesn't exist and cache its URL."""
        try:
            response = self.sqs.get_queue_url(QueueName=self.queue_name)
            self.queue_url = response["QueueUrl"]
        except ClientError:
            response = self.sqs.create_queue(QueueName=self.queue_name)
            self.queue_url = response["QueueUrl"]
        print(f"SQS queue ready: {self.queue_url}")
        return self.queue_url

    def send_message(self, body: dict) -> str:
        response = self.sqs.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(body),
        )
        return response["MessageId"]

    def poll_messages(self, max_messages: int = 10, wait_seconds: int = 1):
        """Long-poll for messages. Returns list of raw SQS messages."""
        response = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
            VisibilityTimeout=30,
        )
        return response.get("Messages", [])

    def delete_message(self, receipt_handle: str):
        """Acknowledge / delete a message after successful processing."""
        self.sqs.delete_message(
            QueueUrl=self.queue_url,
            ReceiptHandle=receipt_handle,
        )

    def purge_queue(self):
        self.sqs.purge_queue(QueueUrl=self.queue_url)

sqs_client = SQSClient()
