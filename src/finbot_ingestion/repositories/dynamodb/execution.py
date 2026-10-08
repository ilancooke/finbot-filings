"""Bounded SDK execution, reusable injected client, and explicit lifecycle."""

from botocore.config import Config

from .config import DynamoDBConfig
from finbot_ingestion.aws_execution import AWSExecution


class DynamoDBExecution(AWSExecution):
    def __init__(self, client, config: DynamoDBConfig, *, executor=None):
        super().__init__(client, config, executor=executor)

    @classmethod
    def from_config(cls, config: DynamoDBConfig, *, session=None):
        """Explicit opt-in client construction; never invoked on import or parsing."""
        import boto3

        session = session or boto3.Session(region_name=config.region)
        client = session.client("dynamodb", region_name=config.region, config=Config(
            connect_timeout=config.connect_timeout_seconds,
            read_timeout=config.read_timeout_seconds,
            retries={"mode": "standard", "total_max_attempts": config.max_attempts},
            max_pool_connections=config.max_workers,
        ))
        return cls(client, config)
