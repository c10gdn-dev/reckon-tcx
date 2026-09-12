"""The local adapter: both stores in one JSON file.

`PLAN.md` §2 puts the token store and the dedupe store in the same file locally
and in the same DynamoDB table on AWS, so one object implements both protocols.
They stay separate *ports* because the AWS side keys them differently and because
mixing them up in the pipeline would be easy otherwise.

The file holds refresh tokens, so it is created 0600 and re-chmodded on every
open. A credential file that a stray `umask` left world-readable is not a
hypothetical.

Concurrency is real even locally — two terminals, or `reckon watch` alongside a
manual `reckon sync` — so every read-modify-write happens under `flock`, and the
compare-and-swap in `save` is checked inside that lock. POSIX only, which covers
macOS, Linux and the Lambda runtime; Windows would need `msvcrt.locking`.
"""

import fcntl
import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

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

DEFAULT_PATH = Path.home() / ".config" / "reckon" / "store.json"

# The file's own schema version, not a token version. Bumped only if the layout
# changes in a way an older Reckon could misread.
#
# 2 (2026-09-12) added `authorised_at` to a token record. The inventory arrived
# without a bump because an absent key reads correctly as "nothing yet"; this
# does not, because an absent `authorised_at` read by *older* code is invisible
# while a *newer* reader must know the difference between "granted at the epoch"
# and "never recorded". Reading a schema-1 document is therefore allowed and its
# tokens come back with `authorised_at=0.0`, which is the truth about them.
SCHEMA = 2
_READABLE = (1, 2)

_MODE = 0o600


class FileStore:
    """A `TokenStore`, a `ProcessedLogStore` and an `InventoryStore` over one JSON document."""

    def __init__(self, path: Path = DEFAULT_PATH, *, now: Callable[[], float] = time.time) -> None:
        self.path = path
        self._now = now

    # --- TokenStore ---------------------------------------------------------

    def load(self, service: str) -> VersionedTokens | None:
        with self._transaction(write=False) as document:
            return _versioned(document["tokens"].get(service))

    def save(self, service: str, tokens: Tokens, *, expected_version: int) -> VersionedTokens:
        with self._transaction(write=True) as document:
            current = _versioned(document["tokens"].get(service))
            found = 0 if current is None else current.version
            if found != expected_version:
                raise TokenConflict(service, expected_version, found)
            saved = VersionedTokens(tokens, expected_version + 1)
            document["tokens"][service] = {**asdict(tokens), "version": saved.version}
            return saved

    # --- ProcessedLogStore --------------------------------------------------

    def get(self, activity_id: str) -> LogEntry | None:
        with self._transaction(write=False) as document:
            raw = document["logs"].get(activity_id)
            return None if raw is None else _entry(activity_id, raw)

    def record(self, entry: LogEntry) -> None:
        with self._transaction(write=True) as document:
            stored = asdict(entry)
            stored.pop("activity_id")
            stored["status"] = str(entry.status)
            stored["recorded_at"] = entry.recorded_at or self._now()
            document["logs"][entry.activity_id] = stored

    def entries(self) -> list[LogEntry]:
        """Everything recorded so far, oldest first. For `reckon sync --status`."""
        with self._transaction(write=False) as document:
            found = [_entry(key, raw) for key, raw in document["logs"].items()]
        return sorted(found, key=lambda entry: entry.recorded_at)

    # --- InventoryStore -----------------------------------------------------

    def put(self, entry: InventoryEntry) -> None:
        with self._transaction(write=True) as document:
            stored = asdict(entry)
            stored.pop("activity_id")
            stored["match"] = str(entry.match)
            stored["seen_at"] = entry.seen_at or self._now()
            document["inventory"][entry.activity_id] = stored

    def inventory(self, activity_id: str) -> InventoryEntry | None:
        with self._transaction(write=False) as document:
            raw = document["inventory"].get(activity_id)
            return None if raw is None else _inventory(activity_id, raw)

    def between(self, start_time: str, end_time: str) -> list[InventoryEntry]:
        low, high = chronological(start_time), chronological(end_time)
        with self._transaction(write=False) as document:
            found = [_inventory(key, raw) for key, raw in document["inventory"].items()]
        # Filtered and sorted on the normalised key, never the raw text: two
        # activities written with different UTC offsets compare wrongly as
        # strings, which is the trap local-mode matching already fell into.
        inside = [e for e in found if low <= chronological(e.start_time) < high]
        return sorted(inside, key=lambda e: chronological(e.start_time))

    # --- the file itself ----------------------------------------------------

    @contextmanager
    def _transaction(self, *, write: bool) -> Iterator[dict[str, Any]]:
        """Read the document under a lock, and write it back if asked.

        Rewritten in place rather than through a temporary file and `os.replace`,
        because replacing the file would drop the lock the next writer is waiting
        on. The cost is a crash window between `truncate` and `write`; the file is
        a few kilobytes and is fsynced, so that window is small, and the
        alternative trades it for a correctness bug under concurrency.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, _MODE)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), _MODE)
            fcntl.flock(handle, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            document = self._parse(handle.read())
            yield document
            if write:
                handle.seek(0)
                handle.truncate()
                json.dump(document, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _parse(self, text: str) -> dict[str, Any]:
        if not text.strip():
            return {"schema": SCHEMA, "tokens": {}, "logs": {}, "inventory": {}}
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise StoreError(f"{self.path} is not valid JSON: {exc}") from exc
        if not isinstance(document, dict):
            raise StoreError(f"{self.path} does not contain a JSON object")
        schema = document.get("schema")
        if schema not in _READABLE:
            raise StoreError(
                f"{self.path} is schema {schema!r}, this Reckon reads "
                f"{' and '.join(str(v) for v in _READABLE)}; move it aside and re-authorise"
            )
        # Upgraded in place on the next write rather than migrated by a separate
        # step: every schema-1 field means the same thing in schema 2, so there
        # is nothing to convert and a migration script would only be a thing to
        # forget to run.
        document["schema"] = SCHEMA
        document.setdefault("tokens", {})
        document.setdefault("logs", {})
        # Added with the inventory port and deliberately *not* a schema bump: an
        # older document simply has no inventory yet, which is true, and an older
        # Reckon reading a newer document ignores a key it does not know. A bump
        # would force every existing store to be moved aside and re-authorised
        # for a change that costs an existing reader nothing.
        document.setdefault("inventory", {})
        return document


def _versioned(raw: Any) -> VersionedTokens | None:
    if raw is None:
        return None
    try:
        return VersionedTokens(
            Tokens(
                access_token=raw["access_token"],
                refresh_token=raw["refresh_token"],
                expires_at=float(raw["expires_at"]),
                # Absent in a schema-1 document, where it means the grant's age
                # was never recorded — not that it was granted at the epoch.
                authorised_at=float(raw.get("authorised_at", 0.0)),
            ),
            version=int(raw["version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreError(f"stored token record is unreadable: {exc}") from exc


def _entry(activity_id: str, raw: Any) -> LogEntry:
    try:
        return LogEntry(
            activity_id=activity_id,
            status=Status(raw["status"]),
            reason=raw.get("reason", ""),
            strava_activity_id=raw.get("strava_activity_id"),
            factor=raw.get("factor"),
            recorded_at=float(raw.get("recorded_at", 0.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreError(f"stored log record for {activity_id} is unreadable: {exc}") from exc


def _inventory(activity_id: str, raw: Any) -> InventoryEntry:
    try:
        return InventoryEntry(
            activity_id=activity_id,
            start_time=raw["start_time"],
            end_time=raw.get("end_time", ""),
            exercise_type=raw.get("exercise_type", ""),
            display_name=raw.get("display_name", ""),
            distance_m=raw.get("distance_m"),
            seen_at=float(raw.get("seen_at", 0.0)),
            strava_activity_id=raw.get("strava_activity_id"),
            match=MatchKind(raw.get("match", MatchKind.NONE)),
            checked_at=float(raw.get("checked_at", 0.0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StoreError(f"stored inventory record for {activity_id} is unreadable: {exc}") from exc
