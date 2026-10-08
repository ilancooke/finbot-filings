"""Reusable SDK clients with bounded execution and per-attempt logging."""

import logging

from .execution import BlockingExecution

LOGGER = logging.getLogger(__name__)


def log_sdk_attempt(*, attempts, response=None, caught_exception=None, operation=None, **kwargs):
    status = response[0].status_code if response is not None else None
    if caught_exception is not None or (status is not None and status >= 300):
        LOGGER.warning("AWS request attempt failed", extra={
            "operation": operation.name if operation is not None else "unknown",
            "attempt_number": attempts, "http_status": status,
            "error_type": type(caught_exception).__name__ if caught_exception else "AWSResponseError"})
    elif attempts > 1:
        LOGGER.info("AWS request recovered", extra={"operation": operation.name, "attempt_number": attempts})


class AWSExecution(BlockingExecution):
    def __init__(self, client, config, *, executor=None):
        super().__init__(max_workers=config.max_workers, executor=executor, name="aws")
        self.client, self.config = client, config
        self._handler_id = f"finbot-attempts-{id(self)}"
        if hasattr(client, "meta"):
            self._event = "needs-retry." + client.meta.service_model.service_name
            client.meta.events.register(self._event, log_sdk_attempt, unique_id=self._handler_id)
        else:
            self._event = None

    @classmethod
    def from_config(cls, service, config, *, session=None):
        import boto3
        session = session if session is not None else boto3.Session(region_name=config.region)
        client = session.client(service, region_name=config.region, config=config.sdk_config())
        return cls(client, config)

    def close(self):
        if self._closed:
            return
        super().close()
        if self._event is not None:
            self.client.meta.events.unregister(self._event, unique_id=self._handler_id)
        self.client.close()
