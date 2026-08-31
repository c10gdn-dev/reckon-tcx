"""Moving what a store holds into another store.

Needed once, when a local deployment becomes an AWS one: the tokens live in
`~/.config/reckon/store.json` and DynamoDB starts empty, so the first webhook
would arrive at a worker with no credentials.

Direction-agnostic, because both adapters satisfy the same two ports — the same
property `test_store_contract.py` exists to hold. Copying DynamoDB back to a file
to debug a deployment works with the same function and no extra code.

Nothing here uses boto3, so this stays importable from anywhere.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from reckon.stores.base import LogEntry, ProcessedLogStore, TokenStore

# Both services, in the order a reader would expect to see them reported.
SERVICES = ("google", "strava")


@dataclass(frozen=True)
class Transferred:
    """What a migration did, and what it deliberately left alone."""

    copied: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    logs: int = 0
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def empty(self) -> bool:
        return not self.copied and not self.logs


def copy_tokens(
    source: TokenStore,
    destination: TokenStore,
    *,
    services: Sequence[str] = SERVICES,
    overwrite: bool = False,
) -> Transferred:
    """Copy each service's token pair, refusing to clobber by default.

    A destination that already holds tokens is left alone unless `overwrite` is
    asked for. That is the cautious default because the destination may be
    *ahead*: a running worker refreshes on its own schedule, and replacing its
    pair with an older one from a laptop would discard the newer access token for
    no gain. The refresh half is identical either way — neither service rotates —
    so overwriting is recoverable rather than destructive, which is why it is
    offered at all.
    """
    copied: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []

    for service in services:
        stored = source.load(service)
        if stored is None:
            warnings.append(f"{service}: nothing to copy; the source holds no tokens")
            continue

        current = destination.load(service)
        if current is not None and not overwrite:
            skipped.append(service)
            warnings.append(
                f"{service}: the destination already holds tokens at version "
                f"{current.version}; pass --overwrite to replace them"
            )
            continue

        destination.save(
            service,
            stored.tokens,
            expected_version=0 if current is None else current.version,
        )
        copied.append(service)

    return Transferred(
        copied=tuple(copied), skipped=tuple(skipped), logs=0, warnings=tuple(warnings)
    )


def copy_logs(entries: Iterable[LogEntry], destination: ProcessedLogStore) -> int:
    """Copy processed-activity records, so already-synced activities stay done.

    Worth doing rather than skipping. Without it the first notification would
    re-process every activity the local tool had already handled. Strava's
    `external_id` deduplication would catch the resulting uploads, so nothing
    would be duplicated in the end — but it would spend the API budget and fill
    the log with rejections to read through.

    Takes an iterable rather than a store because `ProcessedLogStore` has no
    enumeration method, deliberately: the pipeline only ever asks about one
    activity, and widening the port for a one-off migration would be the wrong
    trade. `FileStore.entries()` supplies these.
    """
    count = 0
    for entry in entries:
        destination.record(entry)
        count += 1
    return count
