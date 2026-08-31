"""The SQS seam.

Sits alongside `clients/http.py` in spirit: one place that talks to the queue, so
everything above it takes a plain callable and is tested with a list. boto3 is
permitted here because `aws/` is one of the two directories `test_layering.py`
allows it in.
"""

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

import boto3


class Sqs:
    """Sends messages to one queue. The client is built on first use, not at import."""

    def __init__(
        self,
        queue_url: str,
        *,
        client: Any = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.queue_url = queue_url
        self._injected = client
        self._now = now

    @property
    def client(self) -> Any:
        if self._injected is None:
            self._injected = boto3.client("sqs")
        return self._injected

    def send(self, message: Mapping[str, Any], *, delay_seconds: int = 0) -> None:
        """Enqueue one message, optionally delayed.

        `DelaySeconds` is how the worker waits for a slow Strava upload without
        sleeping: a sleeping Lambda is billed wall-clock time (`PLAN.md` §9).
        """
        self.client.send_message(
            QueueUrl=self.queue_url,
            MessageBody=json.dumps(message, sort_keys=True),
            **({"DelaySeconds": delay_seconds} if delay_seconds else {}),
        )
