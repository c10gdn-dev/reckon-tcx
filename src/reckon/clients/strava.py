"""Strava client — the one place an activity leaves Reckon.

Strava is unaffected by the Fitbit retirement, so this follows `PLAN.md` §8 as
written. Two details there are load-bearing and easy to get silently wrong:

- **`external_id` carries the source activity's id.** Strava dedupes on it, which
  gives idempotency a second layer independent of Reckon's own processed-log
  store. A redelivered webhook then costs one rejected upload, not a duplicate
  activity in someone's feed.
- **Upload is asynchronous.** The POST returns an upload id, not an activity.
  Polling is the caller's job on purpose: locally that is a bounded loop, but in
  Lambda it must be a delayed SQS re-enqueue, because `sleep` in a handler is
  billed wall-clock time (`PLAN.md` §9).

`sport_type` has to be set explicitly. The corpus showed Fitbit exports walks and
yoga alike as `Sport="Other"`, which is information-free, and the native
integration evidently types them from outside the file. A Reckon upload has only
what it sends, so the value comes from the source activity's own type — mapping
one vocabulary to the other belongs to the pipeline, not here.
"""

import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from reckon.clients.http import Request, Response, Transport
from reckon.clients.oauth import TokenHolder
from reckon.core.errors import AuthError, ReckonError

BASE_URL = "https://www.strava.com/api/v3"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"

SCOPES = ("activity:write",)

# Strava separates scopes with commas where the specification says spaces, and
# spells "ask again even though this user already said yes" as `approval_prompt`.
# Both matter only when re-running the flow to widen a grant, which is exactly
# when a silent no-op is hardest to notice.
SCOPE_SEPARATOR = ","
AUTHORIZE_EXTRA = {"approval_prompt": "force"}

# Strava reports a rejected duplicate as an ordinary upload error whose text
# names the existing activity. There is no machine-readable field for it, so
# matching the prose is the only option available; it is checked case-insensitively
# and treated as success rather than failure, since the activity is on Strava
# either way. If Strava rewords this, Reckon re-uploads and Strava rejects it
# again — noisy, never wrong.
_DUPLICATE_MARKER = "duplicate"


@dataclass(frozen=True)
class Upload:
    """The state of one upload, as Strava last reported it."""

    id: int
    external_id: str | None
    activity_id: int | None
    error: str | None
    status: str

    @property
    def done(self) -> bool:
        """True once Strava has finished, successfully or not."""
        return self.activity_id is not None or self.error is not None

    @property
    def duplicate(self) -> bool:
        return self.error is not None and _DUPLICATE_MARKER in self.error.lower()


class Strava:
    """Uploads a TCX and reports what became of it."""

    def __init__(
        self,
        transport: Transport,
        tokens: TokenHolder,
        *,
        base_url: str = BASE_URL,
        rng: random.Random | None = None,
    ) -> None:
        self._transport = transport
        self._tokens = tokens
        self._base_url = base_url.rstrip("/")
        self._rng = random.Random() if rng is None else rng

    def upload(
        self,
        tcx: bytes,
        *,
        name: str,
        external_id: str,
        sport_type: str,
        description: str = "",
        filename: str = "activity.tcx",
    ) -> Upload:
        """Post a TCX file. Returns immediately with an upload to poll."""
        boundary = self._boundary()
        body = _multipart(
            boundary,
            fields={
                "data_type": "tcx",
                "name": name,
                "description": description,
                "external_id": external_id,
                "sport_type": sport_type,
            },
            filename=filename,
            content=tcx,
        )
        response = self._send(
            "POST",
            "uploads",
            body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        return _upload(response.json())

    def upload_status(self, upload_id: int) -> Upload:
        """Re-read one upload. Poll this until `done`."""
        return _upload(self._send("GET", f"uploads/{upload_id}").json())

    def _boundary(self) -> str:
        # Injected RNG, never the module-level functions (`PLAN.md` §7). 16 bytes
        # of hex is far more than enough to not collide with a TCX document's
        # contents, which is the only property a boundary needs.
        return f"----reckon{self._rng.randbytes(16).hex()}"

    def _send(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> Response:
        """One authenticated call, retrying once through a fresh access token.

        Strava access tokens last six hours, so an unattended worker will meet an
        expired one routinely; the holder refreshes on schedule and this covers
        the case where it expired anyway.
        """
        try:
            return self._transport(self._request(method, path, body, content_type))
        except AuthError:
            self._tokens.force_refresh()
            return self._transport(self._request(method, path, body, content_type))

    def _request(
        self, method: str, path: str, body: bytes | None, content_type: str | None
    ) -> Request:
        headers = {"Authorization": f"Bearer {self._tokens.access_token()}"}
        if content_type is not None:
            headers["Content-Type"] = content_type
        return Request(method, f"{self._base_url}/{path}", headers=headers, body=body)


def _multipart(boundary: str, *, fields: Mapping[str, str], filename: str, content: bytes) -> bytes:
    """Encode a multipart/form-data body.

    Hand-rolled because the stdlib encodes multipart in `email` and parses it in
    `cgi`, but offers nothing that writes an HTTP one, and adding `requests` for
    twenty lines would cost the zero-dependency property the whole deployment
    rests on (`PLAN.md` §3).
    """
    marker = f"--{boundary}".encode()
    parts: list[bytes] = []
    for key, value in fields.items():
        parts += [
            marker,
            f'Content-Disposition: form-data; name="{key}"'.encode(),
            b"",
            value.encode(),
        ]
    parts += [
        marker,
        f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
        b"Content-Type: application/octet-stream",
        b"",
        content,
        f"--{boundary}--".encode(),
        b"",
    ]
    return b"\r\n".join(parts)


def _upload(payload: Any) -> Upload:
    if not isinstance(payload, dict):
        raise ReckonError(f"Strava returned {type(payload).__name__}, expected an upload object")
    return Upload(
        id=int(payload.get("id", 0)),
        external_id=payload.get("external_id"),
        activity_id=payload.get("activity_id"),
        error=payload.get("error"),
        status=str(payload.get("status", "")),
    )


def token_holder(
    transport: Transport,
    *,
    client_id: str,
    client_secret: str,
    tokens: Any,
    now: Callable[[], float] = time.time,
    on_refresh: Callable[[Any], Any] | None = None,
) -> TokenHolder:
    """A `TokenHolder` already pointed at Strava's token endpoint."""
    return TokenHolder(
        transport,
        TOKEN_URL,
        client_id=client_id,
        client_secret=client_secret,
        tokens=tokens,
        now=now,
        on_refresh=on_refresh,
    )
