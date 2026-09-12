"""The AWS adapter: both stores in one DynamoDB table.

The only module besides `aws/` permitted to import boto3, and
`tests/test_layering.py` enforces that. The client is constructed lazily rather
than at import, because `boto3.client(...)` at module scope needs credentials and
a region, fails in CI, and distorts coverage.

Deliberately the same three ports as `stores/file.py`, keyed the same way and
returning the same types, so `pipeline.py` cannot tell them apart. Single table,
partition key only: `TOKEN#google`, `TOKEN#strava`, `LOG#{activityId}`,
`INV#{activityId}`.

Inventory records additionally carry `kind` and `starts`, which are the key of a
global secondary index. That index exists so "every activity in this window,
oldest first" is a query rather than a table scan — a scan would read the tokens
and the whole processed log to answer a question about neither.

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
    InventoryEntry,
    LogEntry,
    MatchKind,
    Status,
    StoreError,
    TokenConflict,
    VersionedTokens,
    chronological,
)

# How long a processed-activity record lives. Long enough that a webhook
# redelivered after a very long outage is still recognised as done, short enough
# that the table does not grow without bound. Tokens carry no TTL: expiring them
# would silently deauthorise the whole pipeline.
LOG_TTL_DAYS = 90

_TOKEN_PREFIX = "TOKEN#"
_LOG_PREFIX = "LOG#"
_INV_PREFIX = "INV#"

# The secondary index that makes a windowed listing a query. Its partition key is
# a constant, which is normally a design smell — one hot partition — and is right
# here: the whole point is to read the inventory in time order, the table holds one
# person's activities, and the alternative is scanning every token and log record
# to answer a question about neither.
INVENTORY_INDEX = "kind-starts-index"
_INVENTORY_KIND = "inventory"


class DynamoStore:
    """A `TokenStore`, a `ProcessedLogStore` and an `InventoryStore` over one table."""

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
                    authorised_at=float(item.get("authorised_at", {}).get("N", 0.0)),
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
                    "authorised_at": {"N": repr(tokens.authorised_at)},
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

    # --- InventoryStore -----------------------------------------------------

    def put(self, entry: InventoryEntry) -> None:
        item: dict[str, Any] = {
            "pk": {"S": f"{_INV_PREFIX}{entry.activity_id}"},
            "kind": {"S": _INVENTORY_KIND},
            "starts": {"S": chronological(entry.start_time)},
            "start_time": {"S": entry.start_time},
            "match": {"S": str(entry.match)},
            "seen_at": {"N": repr(entry.seen_at or self._now())},
            "checked_at": {"N": repr(entry.checked_at)},
        }
        for name, value in (
            ("end_time", entry.end_time),
            ("exercise_type", entry.exercise_type),
            ("display_name", entry.display_name),
        ):
            if value:
                item[name] = {"S": value}
        if entry.distance_m is not None:
            item["distance_m"] = {"N": repr(entry.distance_m)}
        if entry.strava_activity_id is not None:
            item["strava_activity_id"] = {"N": str(entry.strava_activity_id)}
        # No `ttl` attribute, deliberately, where log records carry one. Expiring
        # the inventory would make Reckon forget an activity exists and upload it
        # again — the table's TTL is enabled, and an item without the attribute is
        # simply never expired, so the omission is the mechanism.
        self.client.put_item(TableName=self.table_name, Item=item)

    def inventory(self, activity_id: str) -> InventoryEntry | None:
        item = self._get(f"{_INV_PREFIX}{activity_id}")
        return None if item is None else _inventory(activity_id, item)

    def between(self, start_time: str, end_time: str) -> list[InventoryEntry]:
        found: list[InventoryEntry] = []
        arguments: dict[str, Any] = {
            "TableName": self.table_name,
            "IndexName": INVENTORY_INDEX,
            "KeyConditionExpression": "kind = :kind AND starts BETWEEN :low AND :high",
            "ExpressionAttributeValues": {
                ":kind": {"S": _INVENTORY_KIND},
                ":low": {"S": chronological(start_time)},
                # BETWEEN is inclusive at both ends and the port is half-open, so
                # the upper bound is excluded below rather than here — DynamoDB
                # has no exclusive form and silently including it would make the
                # two adapters disagree on a boundary nobody would think to test.
                ":high": {"S": chronological(end_time)},
            },
        }
        while True:
            page = self.client.query(**arguments)
            for item in page.get("Items", []):
                found.append(_inventory(item["pk"]["S"].removeprefix(_INV_PREFIX), item))
            if not (token := page.get("LastEvaluatedKey")):
                return [e for e in found if chronological(e.start_time) < chronological(end_time)]
            arguments["ExclusiveStartKey"] = token

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


def _inventory(activity_id: str, item: Mapping[str, Any]) -> InventoryEntry:
    try:
        return InventoryEntry(
            activity_id=activity_id,
            start_time=item["start_time"]["S"],
            end_time=item.get("end_time", {}).get("S", ""),
            exercise_type=item.get("exercise_type", {}).get("S", ""),
            display_name=item.get("display_name", {}).get("S", ""),
            distance_m=_optional_float(item.get("distance_m")),
            seen_at=float(item.get("seen_at", {}).get("N", 0.0)),
            strava_activity_id=_optional_int(item.get("strava_activity_id")),
            match=MatchKind(item["match"]["S"]),
            checked_at=float(item.get("checked_at", {}).get("N", 0.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreError(f"stored inventory record for {activity_id} is unreadable: {exc}") from exc
