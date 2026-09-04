"""Google Health API client — the source of activities and their TCX.

This replaces the Fitbit Web API that `PLAN.md` §8 was written against. Google
retired the standalone Fitbit app, new Fitbit developer accounts are no longer
issued, and the legacy Web API is deprecated as of September 2026, so building
against it would have produced something unbuildable and then dead. The Google
Health API is its documented successor and carries the one endpoint this project
cannot do without: an exercise exported as TCX, complete with the GPS route.

What changed for Reckon, beyond the hostname:

- **Auth is Google OAuth 2.0.** The refresh token does not rotate on use, so the
  single-use-token race that `PLAN.md` §8 makes the centrepiece of phase 5 is no
  longer a correctness problem. The compare-and-swap write is still worth having
  for the access token, but it is now belt-and-braces rather than load-bearing.
- **The notification does carry a time range**, unlike Fitbit's, so the worker
  can query the interval directly instead of listing a whole day and diffing.
  The processed-log store still dedupes, because webhooks are at-least-once.
- **TCX export needs the location scope on top of the activity scope.** Without
  it the export succeeds and the route is simply absent, which is exactly the
  kind of silent failure §8 exists to catch: `SCOPES` requests both.
"""

import datetime as dt
import json
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from reckon.clients.http import Request, Response, Transport
from reckon.clients.oauth import TokenHolder
from reckon.core.errors import AuthError, HTTPError, ReckonError

BASE_URL = "https://health.googleapis.com/v4"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# The first two are required for `exportExerciseTcx`. Dropping the location scope
# does not fail the call — it silently returns a route-less file.
#
# The third is for heart rate, which lives under a different scope entirely and
# is *not* included in the TCX the API exports: the same walk exported from the
# app carries heart rate on 193 trackpoints and fetched here carries none. Reckon
# fetches it separately and merges it back, so without this scope every upload
# loses a heart-rate trace the device recorded.
SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.location.readonly",
    "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly",
)

# Sampled far more often than a trackpoint, and on its own clock. Merging is
# nearest-sample-within-tolerance rather than exact matching for that reason.
HEART_RATE_TYPE = "heart-rate"

# Google issues a refresh token only when `access_type=offline` is asked for, and
# `prompt=consent` is needed on every re-authorisation, not just the first:
# without it a user who has already granted access gets an access token and no
# refresh token, and the unattended half of the pipeline quietly stops working an
# hour later.
#
# `select_account` forces the account chooser even when only one Google session
# is active. It costs a click, and it is worth it: the account that owns the
# Cloud project is routinely not the account the health data lives under, and
# authorising the wrong one succeeds completely — right up to the first API call,
# which fails with ACCOUNT_NOT_LINKED and no hint that the cause was a choice
# made several screens earlier.
AUTHORIZE_EXTRA = {"access_type": "offline", "prompt": "select_account consent"}

# The API's own maximum for exercise, which is far lower than for other data
# types. Asking for more is not an error; it just does not give you more.
MAX_PAGE_SIZE = 25

# Heart rate is sampled far more often than exercises are recorded, and its data
# type is not subject to exercise's much lower cap.
MAX_SAMPLE_PAGE_SIZE = 1000

# Guards the pagination loop against a server that keeps handing back a token.
# One page is 25 exercises, so this is thousands of activities — far past any
# real backfill, and the alternative is a Lambda that runs until it is killed.
MAX_PAGES = 200


class AccountNotLinked(ReckonError):
    """The credentials are valid, but the Google account holds no Health data.

    Found on the very first live call. Everything about the request was correct —
    the OAuth client, both scopes, the token, the URL — and the API still refuses,
    because authorising an account and that account having health data are
    different things. Every data type fails identically, so it is not about
    exercise.

    Deterministic, and no amount of retrying or re-authorising fixes it: someone
    has to link the account, or authorise a different one. The API returns the
    address to send them to, so pass it on rather than making them search.
    """

    def __init__(self, signup_url: str | None) -> None:
        self.signup_url = signup_url
        where = f" at {signup_url}" if signup_url else ""
        super().__init__(
            f"this Google account is not linked to Google Health, so it has no "
            f"activities to read; link it{where}, or re-run "
            f"`python scripts/authorize.py google` and sign in with the account "
            f"your Fitbit data actually lives under"
        )


class UnexpectedPayload(ReckonError):
    """The API answered 200 with a body this client cannot read.

    Deterministic: the same request will return the same unreadable body. Being
    told about it loudly is the point — a renamed field is the failure mode
    `PLAN.md` §8 is entirely about.
    """


def _moment(text: str) -> "dt.datetime | None":
    """An RFC 3339 timestamp, or None when it cannot be read."""
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        return None


def _required_moment(text: str, name: str) -> dt.datetime:
    moment = _moment(text)
    if moment is None:
        raise ReckonError(f"{name} is not an RFC 3339 timestamp: {text!r}")
    return moment


@dataclass(frozen=True)
class Exercise:
    """One workout session, as much of it as Reckon needs.

    `name` is the full resource path, not a bare id, because that is what every
    subsequent call wants. `id` is its last segment, which is what goes into
    Strava's `external_id` and into the processed-log store.
    """

    name: str
    exercise_type: str
    display_name: str
    start_time: str
    end_time: str
    distance_m: float | None
    # `exerciseMetadata.hasGps`, absent on activities recorded without a route.
    # Advisory: the file is still fetched and still uploaded either way, since an
    # activity Reckon cannot correct must reach Strava regardless. It is worth
    # carrying because it explains a pass-through before the file is opened.
    has_gps: bool | None = None

    @property
    def id(self) -> str:
        return self.name.rpartition("/")[2]


class GoogleHealth:
    """Reads exercises and their TCX. Holds no state beyond its tokens."""

    def __init__(
        self,
        transport: Transport,
        tokens: TokenHolder,
        *,
        base_url: str = BASE_URL,
        user: str = "me",
        timeout: float | None = None,
    ) -> None:
        self._transport = transport
        self._tokens = tokens
        self._base_url = base_url.rstrip("/")
        self._user = user
        self._timeout = timeout

    def exercises(
        self,
        *,
        start_time: str,
        end_time: str,
        page_size: int = MAX_PAGE_SIZE,
    ) -> Iterator[Exercise]:
        """Every exercise starting within `[start_time, end_time)`, newest first.

        Times are RFC 3339, the format the webhook notification already uses, so
        a caller can hand the notification's interval straight through. The end
        is exclusive to keep adjacent windows from double-counting a session on
        the boundary — the dedupe store would catch it, but silently.

        **Filtered here rather than by the API.** The documented `filter`
        parameter — `steps.interval.start_time >= "..."` — is rejected outright
        for the exercise data type. Every variant tried against the live API on
        2026-08-30 failed: an `exercise.` prefix gives
        `INVALID_DATA_POINT_FILTER_DATA_TYPE_MEMBER`, a bare `interval.` gives
        `INVALID_DATA_POINT_FILTER_DATA_TYPE_RESTRICTION`, and the
        `startTime`/`endTime` query parameters some documentation mentions are
        not bound at all. Rather than keep guessing at an undocumented grammar,
        list and compare.

        That costs little. The listing comes back ordered newest first, so
        pagination stops as soon as a page ends before the window; a week's sync
        is normally a single request of 25.

        The ordering is *observed*, not promised, so it decides only when to
        stop, never what to yield. Each point is checked against the window on
        its own merits, and one whose timestamp cannot be read is yielded rather
        than dropped — over-reporting is recoverable, and the dedupe store
        catches it. Silently losing an activity is not.
        """
        window_start = _required_moment(start_time, "start_time")
        window_end = _required_moment(end_time, "end_time")
        page_token: str | None = None

        for _ in range(MAX_PAGES):
            payload = self._get_json(
                f"{self._collection()}/dataPoints",
                {
                    "pageSize": str(min(page_size, MAX_PAGE_SIZE)),
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            oldest: dt.datetime | None = None
            for raw in _sequence(payload, "dataPoints"):
                found = _exercise(raw)
                started = _moment(found.start_time)
                if started is None or window_start <= started < window_end:
                    yield found
                if started is not None:
                    oldest = started

            page_token = payload.get("nextPageToken") or None
            if page_token is None or (oldest is not None and oldest < window_start):
                return
        raise UnexpectedPayload(f"still paginating after {MAX_PAGES} pages; refusing to continue")

    def heart_rate(self, *, start_time: str, end_time: str) -> list[tuple[dt.datetime, int]]:
        """Heart-rate samples covering `[start_time, end_time)`, oldest first.

        Fetched separately because `:exportExerciseTcx` does not include heart
        rate — the same walk exported from the app carries it on 193 trackpoints
        and fetched from here carries none. `core.heartrate` merges these back in.

        Filtered client-side for the same reason `exercises` is: the API rejects
        the documented `filter` spellings. This data type is sampled far more
        often than exercises are recorded, so the page size is the general
        maximum rather than exercise's 25.

        Requires `health_metrics_and_measurements.readonly`, which is a different
        scope from the activity and location ones. Without it every call is a 403
        and no heart rate is merged — which is why it is in `SCOPES`.
        """
        window_start = _required_moment(start_time, "start_time")
        window_end = _required_moment(end_time, "end_time")
        samples: list[tuple[dt.datetime, int]] = []
        page_token: str | None = None

        for _ in range(MAX_PAGES):
            payload = self._get_json(
                f"users/{self._user}/dataTypes/{HEART_RATE_TYPE}/dataPoints",
                {
                    "pageSize": str(MAX_SAMPLE_PAGE_SIZE),
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            oldest: dt.datetime | None = None
            for raw in _sequence(payload, "dataPoints"):
                sample = _heart_rate_sample(raw)
                if sample is None:
                    continue
                moment, bpm = sample
                if window_start <= moment < window_end:
                    samples.append((moment, bpm))
                oldest = moment

            page_token = payload.get("nextPageToken") or None
            if page_token is None or (oldest is not None and oldest < window_start):
                break
        samples.sort()
        return samples

    def tcx(self, name: str, *, partial: bool = True) -> bytes:
        """The exercise's route as a TCX document.

        `partial=True` is the default deliberately, and mirrors the legacy
        `includePartialTCX`. A file whose GPS dropped out mid-activity is not
        useless to Reckon — the transform detects that structurally and passes it
        through unchanged so the activity still reaches Strava (`PLAN.md`
        §"Partial GPS"). Refusing to download it here would be the dropping that
        the whole design says never to do.
        """
        response = self._send(
            f"{self._collection()}/dataPoints/{_segment(name)}:exportExerciseTcx",
            {"alt": "media", "partialData": "true" if partial else "false"},
            accept="application/vnd.garmin.tcx+xml",
        )
        return response.body

    def _collection(self) -> str:
        return f"users/{self._user}/dataTypes/exercise"

    def _get_json(self, path: str, params: Mapping[str, str]) -> Mapping[str, Any]:
        payload = self._send(path, params, accept="application/json").json()
        if not isinstance(payload, dict):
            raise UnexpectedPayload(f"{path} returned {type(payload).__name__}, expected an object")
        return payload

    def _send(self, path: str, params: Mapping[str, str], *, accept: str) -> Response:
        """One authenticated GET, retrying once through a fresh access token.

        A 401 on a token this client believed was live means it was revoked or
        cut short — a Google client still in Testing status is issued refresh
        tokens that expire after seven days. One forced refresh distinguishes
        that from credentials that are genuinely gone, and the second 401 is
        allowed to propagate.
        """
        request = self._request(path, params, accept=accept, token=self._tokens.access_token())
        try:
            return self._transport(request)
        except AuthError:
            token = self._tokens.force_refresh()
            try:
                return self._transport(self._request(path, params, accept=accept, token=token))
            except HTTPError as exc:
                _raise_if_account_unlinked(exc)
                raise
        except HTTPError as exc:
            _raise_if_account_unlinked(exc)
            raise

    def _request(self, path: str, params: Mapping[str, str], *, accept: str, token: str) -> Request:
        url = f"{self._base_url}/{path}?{urllib.parse.urlencode(dict(params))}"
        headers = {"Authorization": f"Bearer {token}", "Accept": accept}
        if self._timeout is None:
            return Request("GET", url, headers=headers)
        return Request("GET", url, headers=headers, timeout=self._timeout)


# The API reports this as a 400 with a machine-readable reason, and hands back
# the address the account has to be linked at. Both are worth surfacing verbatim.
_NOT_LINKED = "ACCOUNT_NOT_LINKED"


def _raise_if_account_unlinked(error: HTTPError) -> None:
    try:
        payload = json.loads(error.body)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict):
        return
    for detail in payload.get("error", {}).get("details", []):
        if isinstance(detail, dict) and detail.get("reason") == _NOT_LINKED:
            metadata = detail.get("metadata")
            url = metadata.get("redirect_uri") if isinstance(metadata, dict) else None
            raise AccountNotLinked(url) from error


def _exercise(raw: Mapping[str, Any]) -> Exercise:
    name = raw.get("name")
    exercise = raw.get("exercise")
    if not isinstance(name, str) or not isinstance(exercise, dict):
        raise UnexpectedPayload(f"data point has no name/exercise pair: {sorted(raw)}")
    interval = exercise.get("interval")
    if not isinstance(interval, dict):
        raise UnexpectedPayload(f"{name} has no interval")
    return Exercise(
        name=name,
        exercise_type=str(exercise.get("exerciseType", "")),
        display_name=str(exercise.get("displayName", "")),
        start_time=str(interval.get("startTime", "")),
        end_time=str(interval.get("endTime", "")),
        distance_m=_distance_m(exercise.get("metricsSummary")),
        has_gps=_has_gps(exercise.get("exerciseMetadata")),
    )


def _has_gps(metadata: Any) -> bool | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("hasGps")
    return value if isinstance(value, bool) else None


def _distance_m(summary: Any) -> float | None:
    """Metres from the summary's millimetre field, or None when it carries none.

    Advisory only. The target the transform rescales *to* comes from the file's
    own `Lap/DistanceMeters`, which the corpus showed matches what the app
    displays to within 0.06%, so no API call is needed to correct an activity.
    This exists to log against, and to notice if that ever stops being true.

    The live API spells it `distanceMillimeters`, correctly — confirmed against
    real data on 2026-08-30. Google's *published example* carries the typo
    `distanceMillimiters`, so that spelling is still accepted second, in case any
    version of the service matches its own documentation.
    """
    if not isinstance(summary, dict):
        return None
    for key in ("distanceMillimeters", "distanceMillimiters"):
        raw = summary.get(key)
        if raw is not None:
            try:
                return float(raw) / 1000.0
            except (TypeError, ValueError) as exc:
                raise UnexpectedPayload(f"{key} is not a number: {raw!r}") from exc
    return None


def _heart_rate_sample(raw: Any) -> tuple[dt.datetime, int] | None:
    """One reading, or None when the shape is not one this can read.

    Tolerant rather than strict, and deliberately: heart rate is an enrichment,
    so a reading that cannot be parsed should cost that reading and not the
    upload. `_exercise` is strict for the opposite reason — a data point that
    cannot be read there means an activity is silently skipped.

    The wrapper key and the value spelling are documentation-derived and have not
    met the live API, because the scope was not granted when this was written.
    """
    if not isinstance(raw, dict):
        return None
    # Either branch yields a dict, `raw` having been checked above — so there is
    # no third case to defend against, and writing one would be unreachable.
    body = raw.get("heartRate") if isinstance(raw.get("heartRate"), dict) else raw
    moment = _moment(str(body.get("sampleTime") or body.get("time") or ""))
    if moment is None:
        return None
    for key in ("beatsPerMinute", "bpm", "value"):
        value = body.get(key)
        if value is None:
            continue
        try:
            return moment, int(float(value))
        except (TypeError, ValueError):
            return None
    return None


def _sequence(payload: Mapping[str, Any], key: str) -> list[Any]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise UnexpectedPayload(f"{key} is {type(value).__name__}, expected a list")
    return value


def _segment(name: str) -> str:
    """The bare data point id, whether given a full resource name or just an id."""
    return name.rpartition("/")[2]


def token_holder(
    transport: Transport,
    *,
    client_id: str,
    client_secret: str,
    tokens: Any,
    now: Callable[[], float] = time.time,
    on_refresh: Callable[[Any], Any] | None = None,
) -> TokenHolder:
    """A `TokenHolder` already pointed at Google's token endpoint."""
    return TokenHolder(
        transport,
        TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        tokens=tokens,
        now=now,
        on_refresh=on_refresh,
    )
