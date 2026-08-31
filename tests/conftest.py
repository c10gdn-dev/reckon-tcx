"""Shared fixtures.

The AWS ones use `moto`, which mocks in process — no Docker, no LocalStack, no
credentials and no cost. Dummy credentials are forced into the environment so a
mistake in a fixture cannot reach a real account.
"""

import boto3
import pytest
from moto import mock_aws

TABLE = "reckon-test"
REGION = "eu-west-2"


@pytest.fixture
def aws_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deliberately invalid credentials, so nothing can escape to a real account."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def dynamo(aws_credentials: None):
    """A mocked DynamoDB client with the single table already created.

    Partition key only, matching `PLAN.md` §9: one table holding both
    `TOKEN#{service}` and `LOG#{activityId}` items.
    """
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        client.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield client


@pytest.fixture
def sqs(aws_credentials: None):
    """A mocked SQS queue, yielding (client, queue_url)."""
    with mock_aws():
        client = boto3.client("sqs", region_name=REGION)
        url = client.create_queue(QueueName="reckon-test")["QueueUrl"]
        yield client, url


@pytest.fixture
def aws(aws_credentials: None):
    """DynamoDB and SQS together, for the handler entry points."""
    with mock_aws():
        dynamo = boto3.client("dynamodb", region_name=REGION)
        dynamo.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        sqs_client = boto3.client("sqs", region_name=REGION)
        url = sqs_client.create_queue(QueueName="reckon-test")["QueueUrl"]
        yield dynamo, sqs_client, url
