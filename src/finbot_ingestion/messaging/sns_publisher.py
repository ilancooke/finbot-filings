from .publisher import bounded_json_message, validate_message_id


class SNSArtifactEventPublisher:
    def __init__(self, execution, config):
        if execution.config.region != config.region:
            raise ValueError("SNS client and topic regions must match")
        self.execution, self.config = execution, config

    async def publish_artifact_ready(self, event):
        response = await self.execution.call(self.execution.client.publish,
            TopicArn=self.config.topic_arn, Message=bounded_json_message(event.to_json()))
        validate_message_id(response)
