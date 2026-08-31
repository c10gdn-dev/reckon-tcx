"""The AWS adapter: both stores in one DynamoDB table.

The only module besides `aws/` permitted to import boto3, and
`tests/test_layering.py` enforces that. The client is constructed lazily rather
than at import, because `boto3.client(...)` at module scope needs credentials and
a region, fails in CI, and distorts coverage.

Deliberately the same two ports as `stores/file.py`, keyed the same way and
returning the same types, so `pipeline.py` cannot tell them apart. Single table,
partition key only: `TOKEN#google`, `TOKEN#strava`, `LOG#{activityId}`.

The compare-and-swap is a `ConditionExpression` on a version attribute. That is
the mechanism `PLAN.md` §8 specifies, and it is worth keeping even though the
race it was designed for — Fitbit's single-use refresh tokens — no longer exists:
neither Google nor Strava rotates, so a lost race now costs a wasted refresh
rather than a destroyed credential. What it still buys is a store whose contents
cannot silently diverge from what a caller believes it wrote.
"""

import time
from collections.abc import Callable, Mapping
from typing import Any

import boto3
from botocore.exceptions import ClientError

from reckon.clients.oauth import Tokens
from reckon.stores.base import (
    LogEntry,
    Status,
    StoreError,
    TokenConflict,
    VersionedTokens,
)

# How long a processed-activity record lives. Long enough that a webhook
# redelivered after a very long outage is still recognised as done, short enough
# that the table does not grow without bound. Tokens carry no TTL: expiring them
# would silently deauthorise the whole pipeline.
LOG_TTL_DAYS = 90

_TOKEN_PREFIX = "TOKEN#"
_LOG_PREFIX = "LOG#"


class DynamoStore:
    """A `TokenStore` and a `ProcessedLogStore` over one DynamoDB table."""

    def __init__(
        self,
        table_name: str,
        *,
        client: Any = None,
        now: Callable[[], float] = time.time,
        log_ttl_days: int = LOG_TTL_DAYS,
    ) -> None:
        self.table_name = table_name
        self._injected = client
        self._now = now
        self._log_ttl_days = log_ttl_days

    @property
    def client(self) -> Any:
        """The DynamoDB client, built on first use.

        Lazy on purpose: constructing it at import time needs credentials and a
        region, which CI has neither of.
        """
        if self._injected is None:
            self._injected = boto3.client("dynamodb")
        return self._injected

    # --- TokenStore ---------------------------------------------------------

    def load(self, service: str) -> VersionedTokens | None:
        item = self._get(f"{_TOKEN_PREFIX}{service}")
        if item is None:
            return None
        try:
            return VersionedTokens(
                Tokens(
                    access_token=item["access_token"]["S"],
                    refresh_token=item["refresh_token"]["S"],
                    expires_at=float(item["expires_at"]["N"]),
                ),
                version=int(item["version"]["N"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError(f"stored token record for {service} is unreadable: {exc}") from exc

    def save(self, service: str, tokens: Tokens, *, expected_version: int) -> VersionedTokens:
        """Persist `tokens`, or raise `TokenConflict` if someone else got there first.

        Version 0 means "there should be no record yet", which is a different
        condition from "the record is at version N" and has to be expressed as
        one — `version = 0` would match nothing on a first write.
        """
        saved = VersionedTokens(tokens, expected_version + 1)
        condition = "attribute_not_exists(pk)" if expected_version == 0 else "version = :expected"
        values: dict[str, Any] = {}
        if expected_version != 0:
            values[":expected"] = {"N": str(expected_version)}
        try:
            self.client.put_item(
                TableName=self.table_name,
                Item={
                    "pk": {"S": f"{_TOKEN_PREFIX}{service}"},
                    "access_token": {"S": tokens.access_token},
                    "refresh_token": {"S": tokens.refresh_token},
                    "expires_at": {"N": repr(tokens.expires_at)},
                    "version": {"N": str(saved.version)},
                },
                ConditionExpression=condition,
                **({"ExpressionAttributeValues": values} if values else {}),
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Re-read to report what actually won. Costs a round trip on the
            # losing branch only, and the caller needs the real version to
            # continue rather than a guess.
            current = self.load(service)
            raise TokenConflict(
                service, expected_version, 0 if current is None else current.version
            ) from exc
        return saved

    # --- ProcessedLogStore --------------------------------------------------

    def get(self, activity_id: str) -> LogEntry | None:
        item = self._get(f"{_LOG_PREFIX}{activity_id}")
        if item is None:
            return None
        try:
            return LogEntry(
                activity_id=activity_id,
                status=Status(item["status"]["S"]),
                reason=item.get("reason", {}).get("S", ""),
                strava_activity_id=_optional_int(item.get("strava_activity_id")),
                factor=_optional_float(item.get("factor")),
                recorded_at=float(item.get("recorded_at", {}).get("N", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StoreError(f"stored log record for {activity_id} is unreadable: {exc}") from exc

    def record(self, entry: LogEntry) -> None:
        recorded_at = entry.recorded_at or self._now()
        item: dict[str, Any] = {
            "pk": {"S": f"{_LOG_PREFIX}{entry.activity_id}"},
            "status": {"S": str(entry.status)},
            "recorded_at": {"N": repr(recorded_at)},
            "ttl": {"N": str(int(recorded_at + self._log_ttl_days * 86400))},
        }
        if entry.reason:
            item["reason"] = {"S": entry.reason}
        if entry.strava_activity_id is not None:
            item["strava_activity_id"] = {"N": str(entry.strava_activity_id)}
        if entry.factor is not None:
            item["factor"] = {"N": repr(entry.factor)}
        # No condition. A decision is final, but a redelivery re-deciding the same
        # way must not fail — and the pipeline never records a second, different
        # outcome for one activity, because it consults the store first.
        self.client.put_item(TableName=self.table_name, Item=item)

    # --- the table ----------------------------------------------------------

    def _get(self, key: str) -> Mapping[str, Any] | None:
        response = self.client.get_item(
            TableName=self.table_name,
            Key={"pk": {"S": key}},
            # The token path reads immediately after a conditional write from
            # another worker; an eventually-consistent read could hand back the
            # pair that just lost.
            ConsistentRead=True,
        )
        return response.get("Item")


def _optional_int(value: Mapping[str, Any] | None) -> int | None:
    return None if value is None else int(value["N"])


def _optional_float(value: Mapping[str, Any] | None) -> float | None:
    return None if value is None else float(value["N"])
