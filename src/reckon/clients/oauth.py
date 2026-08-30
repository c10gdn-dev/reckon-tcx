"""OAuth 2.0, the parts both services share.

Google Health and Strava are both plain authorization-code OAuth 2.0, so the URL
building, the code exchange and the refresh live here once. What differs between
them is *policy*, not protocol, and that stays in each client:

- Strava's refresh token never changes. Google's does not rotate on use either,
  but it is only issued once, with `access_type=offline&prompt=consent`, and a
  client still in "Testing" publishing status gets one that expires after seven
  days. Neither resembles the legacy Fitbit single-use rotation this project was
  originally planned around (`PLAN.md` §8).
- Only Google requires the `location` scope for the TCX route to be present.
"""

import hmac
import json
import random
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from reckon.clients.http import HTTPError, Request, Transport
from reckon.core.errors import ReckonError

# Treat a token as expired this long before it really is. Covers clock skew and
# the flight time of the request the token is about to be used on.
EXPIRY_SKEW = 60.0


class OAuthError(ReckonError):
    """The token endpoint answered, but not with a usable token.

    Deterministic on purpose: asking again cannot change the answer. HTTP faults
    on the way to the endpoint are classified by `http.py` as usual.
    """


class AuthorisationExpired(OAuthError):
    """The refresh token is gone: revoked, expired, or already superseded.

    Split out from `OAuthError` because it is the one OAuth failure with a known
    fix that a human can carry out, and because it is *expected* rather than
    exceptional on Google: a client whose consent screen is still in "Testing"
    publishing status is issued refresh tokens that expire after seven days, so
    an unattended deployment meets this on a schedule until the client is
    published (`PLAN.md` §8).

    Nothing automated can recover from it. Renewing the grant needs the browser
    consent flow — a signed-in session and a click — so the only correct response
    is to say plainly which service died and what command fixes it.
    """

    def __init__(self, service: str, detail: str) -> None:
        self.service = service
        super().__init__(
            f"the {service} authorisation is no longer valid ({detail}); "
            f"re-run `python scripts/authorize.py {service}`"
        )


@dataclass(frozen=True)
class Tokens:
    """One service's credentials, and when the access half stops working."""

    access_token: str
    refresh_token: str
    expires_at: float

    def expired(self, now: float, skew: float = EXPIRY_SKEW) -> bool:
        return now >= self.expires_at - skew


@dataclass(frozen=True)
class ClientCredentials:
    """What Google Cloud hands you when you create an OAuth client."""

    client_id: str
    client_secret: str
    redirect_uris: tuple[str, ...] = ()


def read_client_credentials(text: str) -> ClientCredentials:
    """Parse the credentials JSON downloaded from the Google Cloud console.

    Exists so the client secret can reach Reckon through a 0600 file rather than
    a command line, where it would land in shell history and in `ps` output for
    every other user on the machine. Strava has no equivalent download, so its
    credentials still come from flags or the environment.

    The interesting key is `web` for a Web application client and `installed` for
    a Desktop one. Both shapes are otherwise identical, and which you get depends
    on a dropdown chosen minutes earlier, so accept either rather than making
    someone re-read the console.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OAuthError(f"credentials file is not JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise OAuthError("credentials file does not contain a JSON object")

    for key in ("web", "installed"):
        section = document.get(key)
        if isinstance(section, dict):
            break
    else:
        raise OAuthError(
            f"credentials file has no 'web' or 'installed' section, only {sorted(document)}; "
            f"download it again from the Credentials page of the Google Cloud console"
        )

    client_id = section.get("client_id")
    client_secret = section.get("client_secret")
    if not client_id or not client_secret:
        raise OAuthError(f"credentials file is missing client_id or client_secret under {key!r}")
    redirect_uris = section.get("redirect_uris") or []
    return ClientCredentials(
        client_id=str(client_id),
        client_secret=str(client_secret),
        redirect_uris=tuple(str(uri) for uri in redirect_uris),
    )


def authorization_url(
    authorize_url: str,
    *,
    client_id: str,
    redirect_uri: str,
    scopes: Sequence[str],
    state: str,
    extra: Mapping[str, str] | None = None,
    scope_separator: str = " ",
) -> str:
    """The URL a human opens to grant access.

    Lives here rather than in `scripts/authorize.py` so it is covered: §7 keeps
    scripts to argparse plumbing precisely so that logic like this is testable.

    `scope_separator` exists because Strava joins scopes with commas where the
    specification says spaces, and sends back a narrower grant rather than an
    error when given spaces.
    """
    query = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope_separator.join(scopes),
        "state": state,
        **(extra or {}),
    }
    return f"{authorize_url}?{urllib.parse.urlencode(query)}"


def code_from_redirect(redirect: str, *, expected_state: str) -> str:
    """Pull the authorisation code out of the URL the browser was redirected to.

    Lives here rather than in `scripts/authorize.py` for the same reason as
    `authorization_url`: it is the step where a mistake is silent. Comparing
    `state` is the only thing standing between this flow and an attacker-supplied
    code, and it is compared with `compare_digest` because it is a secret being
    checked against a value someone else controls.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlparse(redirect).query)
    if error := query.get("error"):
        raise OAuthError(f"authorisation was refused: {error[0]}")
    state = (query.get("state") or [""])[0]
    if not hmac.compare_digest(state, expected_state):
        raise OAuthError(
            f"state mismatch: expected {expected_state!r}, got {state!r}; "
            f"this response belongs to a different authorisation attempt"
        )
    code = (query.get("code") or [""])[0]
    if not code:
        raise OAuthError(f"no authorisation code in {redirect!r}")
    return code


def new_state(rng: random.Random | None = None) -> str:
    """A fresh, unguessable `state` value. Injected RNG, per `PLAN.md` §7."""
    return (rng or random.SystemRandom()).randbytes(16).hex()


def exchange_code(
    transport: Transport,
    token_url: str,
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    now: Callable[[], float] = time.time,
) -> Tokens:
    """Trade a one-time authorisation code for a token pair."""
    return _token_request(
        transport,
        token_url,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        now=now,
    )


def refresh(
    transport: Transport,
    token_url: str,
    *,
    client_id: str,
    client_secret: str,
    tokens: Tokens,
    now: Callable[[], float] = time.time,
) -> Tokens:
    """Exchange the refresh half for a fresh access token.

    A response that omits `refresh_token` keeps the existing one, which is what
    Google does and what Strava's documentation permits.
    """
    return _token_request(
        transport,
        token_url,
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": tokens.refresh_token,
        },
        now=now,
        fallback_refresh_token=tokens.refresh_token,
    )


# RFC 6749 §5.2: the grant is invalid, expired, revoked, or was issued to another
# client. Google returns exactly this for a lapsed refresh token.
#
# Only the standard code is matched. Strava's error body is a different,
# non-standard shape which has not been observed first-hand, so it falls through
# to the ordinary `HTTPError` — whose message already quotes the body. That costs
# little: Strava's refresh token does not expire, so this path is Google's in
# practice.
_GRANT_GONE = "invalid_grant"


def _raise_if_grant_is_gone(error: HTTPError, service: str) -> None:
    if error.status not in (400, 401):
        return
    try:
        payload = json.loads(error.body)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict) or payload.get("error") != _GRANT_GONE:
        return
    raise AuthorisationExpired(service, payload.get("error_description") or _GRANT_GONE) from error


def service_of(token_url: str) -> str:
    """A name for the service behind a token endpoint, for error messages.

    Derived from the URL rather than passed down from the caller so that every
    entry point — the CLI, a script, a Lambda handler — reports the same word
    without having to remember to.
    """
    host = urllib.parse.urlparse(token_url).hostname or token_url
    if "google" in host:
        return "google"
    if "strava" in host:
        return "strava"
    return host


def _token_request(
    transport: Transport,
    token_url: str,
    form: Mapping[str, str],
    *,
    now: Callable[[], float],
    fallback_refresh_token: str | None = None,
) -> Tokens:
    # Read the clock *before* the request. Overstating a token's life by the
    # round-trip time risks using an expired one; understating it costs a refresh.
    issued_at = now()
    request = Request(
        "POST",
        token_url,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        body=urllib.parse.urlencode(dict(form)).encode(),
    )
    try:
        response = transport(request)
    except HTTPError as exc:
        _raise_if_grant_is_gone(exc, service_of(token_url))
        raise
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise OAuthError(f"{token_url} returned a body that is not JSON: {exc}") from exc

    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token") or fallback_refresh_token
    if not access_token:
        raise OAuthError(f"{token_url} returned no access_token: {sorted(payload)}")
    if not refresh_token:
        raise OAuthError(
            f"{token_url} returned no refresh_token; "
            f"the authorisation was probably not requested with offline access"
        )
    return Tokens(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=issued_at + _lifetime(payload, token_url, issued_at),
    )


# Used when the token endpoint does not say how long the token lives. Both
# services do say, so this is a floor rather than a guess: short enough that a
# wrong assumption costs one extra refresh, not a run of 401s.
_ASSUMED_LIFETIME = 300.0


def _lifetime(payload: Mapping[str, object], token_url: str, issued_at: float) -> float:
    """Seconds of life, from `expires_in` or, failing that, `expires_at`.

    Strava reports `expires_at` as an absolute epoch and `expires_in` as a
    duration; Google reports only `expires_in`. Preferring the duration avoids
    depending on the two clocks agreeing.
    """
    for key in ("expires_in", "expires_at"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise OAuthError(f"{token_url} returned {key}={raw!r}, which is not a number") from exc
        return value if key == "expires_in" else max(0.0, value - issued_at)
    return _ASSUMED_LIFETIME


class TokenHolder:
    """Keeps a token pair current, and tells someone else when it changed.

    `on_refresh` is how persistence stays out of `clients/`: phase 5 passes the
    store's compare-and-swap write, which returns the pair that actually won.
    The loser of a race therefore continues with the winner's tokens rather than
    its own, which is the rule `PLAN.md` §8 sets out — expressed here as a return
    value so this class needs no lock, no store and no boto3.
    """

    def __init__(
        self,
        transport: Transport,
        token_url: str,
        *,
        client_id: str,
        client_secret: str,
        tokens: Tokens,
        now: Callable[[], float] = time.time,
        on_refresh: Callable[[Tokens], Tokens] | None = None,
    ) -> None:
        self._transport = transport
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._tokens = tokens
        self._now = now
        self._on_refresh = on_refresh

    @property
    def tokens(self) -> Tokens:
        return self._tokens

    def access_token(self) -> str:
        """The current access token, refreshed first if it is due to expire."""
        if self._tokens.expired(self._now()):
            self.force_refresh()
        return self._tokens.access_token

    def force_refresh(self) -> str:
        """Refresh regardless of expiry, for when a 401 arrived anyway.

        A token can stop working before its stated expiry — revoked, or the
        seven-day life of a Testing-status Google client running out early — and
        the only way to find that out is to be told 401.
        """
        fresh = refresh(
            self._transport,
            self._token_url,
            client_id=self._client_id,
            client_secret=self._client_secret,
            tokens=self._tokens,
            now=self._now,
        )
        self._tokens = fresh if self._on_refresh is None else self._on_refresh(fresh)
        return self._tokens.access_token
