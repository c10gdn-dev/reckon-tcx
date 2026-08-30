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

import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from reckon.clients.http import Request, Response, Transport
from reckon.clients.oauth import TokenHolder
from reckon.core.errors import AuthError, ReckonError

BASE_URL = "https://health.googleapis.com/v4"
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# Both are required for `exportExerciseTcx`. Dropping the location scope does not
# fail the call — it silently returns a route-less file.
SCOPES = (
    "https://www.googleapis.com/auth/googlehealth.activity_and_fitness.readonly",
    "https://www.googleapis.com/auth/googlehealth.location.readonly",
)

# Google issues a refresh token only when both are asked for. `prompt=consent`
# is needed on every re-authorisation, not just the first: without it a user who
# has already granted access gets an access token and no refresh token, and the
# unattended half of the pipeline quietly stops working a hour later.
AUTHORIZE_EXTRA = {"access_type": "offline", "prompt": "consent"}

# The API's own maximum for exercise, which is far lower than for other data
# types. Asking for more is not an error; it just does not give you more.
MAX_PAGE_SIZE = 25

# Guards the pagination loop against a server that keeps handing back a token.
# One page is 25 exercises, so this is thousands of activities — far past any
# real backfill, and the alternative is a Lambda that runs until it is killed.
MAX_PAGES = 200


class UnexpectedPayload(ReckonError):
    """The API answered 200 with a body this client cannot read.

    Deterministic: the same request will return the same unreadable body. Being
    told about it loudly is the point — a renamed field is the failure mode
    `PLAN.md` §8 is entirely about.
    """


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
        """Every exercise starting within `[start_time, end_time)`.

        Times are RFC 3339, the format the webhook notification already uses, so
        a caller can hand the notification's interval straight through. The end
        is exclusive to keep adjacent windows from double-counting a session on
        the boundary — the dedupe store would catch it, but silently.
        """
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            payload = self._get_json(
                f"{self._collection()}/dataPoints",
                {
                    "pageSize": str(min(page_size, MAX_PAGE_SIZE)),
                    "filter": (
                        f'exercise.interval.start_time >= "{start_time}" '
                        f'AND exercise.interval.start_time < "{end_time}"'
                    ),
                    **({"pageToken": page_token} if page_token else {}),
                },
            )
            for raw in _sequence(payload, "dataPoints"):
                yield _exercise(raw)
            page_token = payload.get("nextPageToken") or None
            if page_token is None:
                return
        raise UnexpectedPayload(f"still paginating after {MAX_PAGES} pages; refusing to continue")

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
            return self._transport(self._request(path, params, accept=accept, token=token))

    def _request(self, path: str, params: Mapping[str, str], *, accept: str, token: str) -> Request:
        url = f"{self._base_url}/{path}?{urllib.parse.urlencode(dict(params))}"
        headers = {"Authorization": f"Bearer {token}", "Accept": accept}
        if self._timeout is None:
            return Request("GET", url, headers=headers)
        return Request("GET", url, headers=headers, timeout=self._timeout)


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
    )


def _distance_m(summary: Any) -> float | None:
    """Metres from the summary's millimetre field, or None when it carries none.

    Advisory only. The target the transform rescales *to* comes from the file's
    own `Lap/DistanceMeters`, which the corpus showed matches what the app
    displays to within 0.06%, so no API call is needed to correct an activity.
    This exists to log against, and to notice if that ever stops being true.

    The field is spelled `distanceMillimiters` in Google's published example —
    a typo carried into the wire format, not a transcription error here. Both
    spellings are accepted so that a fix on their side does not break Reckon.
    """
    if not isinstance(summary, dict):
        return None
    for key in ("distanceMillimiters", "distanceMillimeters"):
        raw = summary.get(key)
        if raw is not None:
            try:
                return float(raw) / 1000.0
            except (TypeError, ValueError) as exc:
                raise UnexpectedPayload(f"{key} is not a number: {raw!r}") from exc
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
