"""OAuth 2.0: URL building, code exchange, refresh, and the expiry clock.

Everything here runs above the network seam, so it is a `FakeTransport` and a
`Clock` throughout — no sockets, no waiting, no patching.
"""

import json
import urllib.parse
from typing import Any

import pytest

from fakes import Clock, FakeTransport, response
from reckon.clients.http import HTTPError
from reckon.clients.oauth import (
    EXPIRY_SKEW,
    AuthorisationExpired,
    OAuthError,
    TokenHolder,
    Tokens,
    authorization_url,
    code_from_redirect,
    exchange_code,
    new_state,
    read_client_credentials,
    refresh,
    service_of,
)
from reckon.core.errors import Transient

TOKEN_URL = "https://oauth2.example.test/token"


def token_response(**overrides: object) -> object:
    payload = {"access_token": "fresh", "refresh_token": "rolling", "expires_in": 3600}
    payload.update(overrides)
    return response(body=json.dumps(payload).encode())


def query_of(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


# --- authorization_url ------------------------------------------------------


def test_authorization_url_carries_every_required_parameter() -> None:
    url = authorization_url(
        "https://accounts.example.test/auth",
        client_id="cid",
        redirect_uri="http://localhost:8721/callback",
        scopes=["read", "write"],
        state="nonce",
    )
    assert url.startswith("https://accounts.example.test/auth?")
    assert query_of(url) == {
        "client_id": "cid",
        "redirect_uri": "http://localhost:8721/callback",
        "response_type": "code",
        "scope": "read write",
        "state": "nonce",
    }


def test_extra_parameters_are_merged_in() -> None:
    url = authorization_url(
        "https://accounts.example.test/auth",
        client_id="cid",
        redirect_uri="http://localhost/cb",
        scopes=["read"],
        state="nonce",
        extra={"access_type": "offline", "prompt": "consent"},
    )
    assert query_of(url)["access_type"] == "offline"
    assert query_of(url)["prompt"] == "consent"


def test_the_scope_separator_can_be_a_comma_for_strava() -> None:
    url = authorization_url(
        "https://www.strava.test/oauth/authorize",
        client_id="cid",
        redirect_uri="http://localhost/cb",
        scopes=["activity:write", "activity:read"],
        state="nonce",
        scope_separator=",",
    )
    assert query_of(url)["scope"] == "activity:write,activity:read"


# --- exchange_code ----------------------------------------------------------


def test_exchange_code_posts_a_form_and_returns_tokens() -> None:
    transport = FakeTransport(token_response())
    clock = Clock(now=1000.0)
    tokens = exchange_code(
        transport,
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        code="one-time",
        redirect_uri="http://localhost/cb",
        now=clock.time,
    )
    assert tokens == Tokens("fresh", "rolling", 4600.0)

    sent = transport.requests[0]
    assert sent.method == "POST"
    assert sent.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert dict(urllib.parse.parse_qsl(sent.body.decode())) == {
        "client_id": "cid",
        "client_secret": "secret",
        "code": "one-time",
        "grant_type": "authorization_code",
        "redirect_uri": "http://localhost/cb",
    }


def test_expiry_is_measured_from_before_the_request_not_after() -> None:
    """Overstating a token's life risks using an expired one; understating costs a refresh."""
    clock = Clock(now=500.0)

    def slow(request: object) -> object:
        clock.advance(30.0)
        return token_response(expires_in=100)

    tokens = exchange_code(
        slow,  # type: ignore[arg-type]
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        code="c",
        redirect_uri="http://localhost/cb",
        now=clock.time,
    )
    assert tokens.expires_at == 600.0


# --- refresh ----------------------------------------------------------------


def test_refresh_sends_the_refresh_grant() -> None:
    transport = FakeTransport(token_response())
    clock = Clock(now=0.0)
    refresh(
        transport,
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("stale", "rolling", 0.0),
        now=clock.time,
    )
    assert dict(urllib.parse.parse_qsl(transport.requests[0].body.decode())) == {
        "client_id": "cid",
        "client_secret": "secret",
        "grant_type": "refresh_token",
        "refresh_token": "rolling",
    }


def test_a_response_without_a_refresh_token_keeps_the_existing_one() -> None:
    """Google omits it on refresh, and the old one stays valid. Losing it forces a re-auth."""
    transport = FakeTransport(token_response(refresh_token=None))
    tokens = refresh(
        transport,
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("stale", "keep-me", 0.0),
        now=Clock(now=0.0).time,
    )
    assert tokens.refresh_token == "keep-me"


def test_a_rotated_refresh_token_replaces_the_old_one() -> None:
    transport = FakeTransport(token_response(refresh_token="rotated"))
    tokens = refresh(
        transport,
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("stale", "old", 0.0),
        now=Clock(now=0.0).time,
    )
    assert tokens.refresh_token == "rotated"


def test_expires_at_is_converted_to_a_lifetime() -> None:
    """Strava sends both; preferring the duration avoids trusting two clocks to agree."""
    transport = FakeTransport(token_response(expires_in=None, expires_at=5000.0))
    tokens = refresh(
        transport,
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("stale", "r", 0.0),
        now=Clock(now=1000.0).time,
    )
    assert tokens.expires_at == 5000.0


def test_an_expires_at_already_in_the_past_does_not_go_negative() -> None:
    transport = FakeTransport(token_response(expires_in=None, expires_at=10.0))
    tokens = refresh(
        transport,
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("stale", "r", 0.0),
        now=Clock(now=1000.0).time,
    )
    assert tokens.expires_at == 1000.0


def test_a_response_without_any_expiry_gets_a_short_assumed_life() -> None:
    transport = FakeTransport(token_response(expires_in=None))
    tokens = refresh(
        transport,
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("stale", "r", 0.0),
        now=Clock(now=0.0).time,
    )
    assert 0 < tokens.expires_at <= 600.0


def call_refresh(transport: FakeTransport) -> Tokens:
    return refresh(
        transport,
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("stale", "r", 0.0),
        now=Clock(now=0.0).time,
    )


def test_a_non_json_body_is_an_oauth_error() -> None:
    with pytest.raises(OAuthError, match="not JSON"):
        call_refresh(FakeTransport(response(body=b"<html>maintenance</html>")))


def test_a_missing_access_token_is_an_oauth_error() -> None:
    with pytest.raises(OAuthError, match="no access_token"):
        call_refresh(FakeTransport(response(body=b'{"error": "invalid_grant"}')))


def test_a_missing_refresh_token_with_nothing_to_fall_back_on_is_an_error() -> None:
    transport = FakeTransport(response(body=b'{"access_token": "a", "expires_in": 60}'))
    with pytest.raises(OAuthError, match="offline access"):
        exchange_code(
            transport,
            TOKEN_URL,
            client_id="cid",
            client_secret="secret",
            code="c",
            redirect_uri="http://localhost/cb",
            now=Clock(now=0.0).time,
        )


def test_a_non_numeric_expiry_is_an_oauth_error() -> None:
    with pytest.raises(OAuthError, match="not a number"):
        call_refresh(FakeTransport(token_response(expires_in="soon")))


# --- Tokens.expired ---------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "expected"),
    [(0.0, False), (1000.0 - EXPIRY_SKEW - 1, False), (1000.0 - EXPIRY_SKEW, True), (2000.0, True)],
)
def test_expiry_includes_a_skew_margin(now: float, expected: bool) -> None:
    assert Tokens("a", "r", 1000.0).expired(now) is expected


# --- TokenHolder ------------------------------------------------------------


def holder(transport: FakeTransport, tokens: Tokens, clock: Clock, **kwargs: object) -> TokenHolder:
    return TokenHolder(
        transport,
        TOKEN_URL,
        client_id="cid",
        client_secret="secret",
        tokens=tokens,
        now=clock.time,
        **kwargs,  # type: ignore[arg-type]
    )


def test_a_live_token_is_returned_without_a_network_call() -> None:
    transport = FakeTransport()
    subject = holder(transport, Tokens("live", "r", 10_000.0), Clock(now=0.0))
    assert subject.access_token() == "live"
    assert transport.calls == 0


def test_an_expired_token_is_refreshed_first() -> None:
    transport = FakeTransport(token_response())
    subject = holder(transport, Tokens("stale", "r", 0.0), Clock(now=1000.0))
    assert subject.access_token() == "fresh"
    assert subject.tokens.refresh_token == "rolling"


def test_a_forced_refresh_ignores_the_expiry() -> None:
    transport = FakeTransport(token_response())
    subject = holder(transport, Tokens("live", "r", 10_000.0), Clock(now=0.0))
    assert subject.force_refresh() == "fresh"
    assert transport.calls == 1


def test_on_refresh_is_told_about_the_new_pair() -> None:
    seen: list[Tokens] = []
    transport = FakeTransport(token_response())
    subject = holder(
        transport,
        Tokens("stale", "r", 0.0),
        Clock(now=1000.0),
        on_refresh=lambda pair: (seen.append(pair), pair)[1],
    )
    subject.access_token()
    assert seen == [Tokens("fresh", "rolling", 4600.0)]


def test_the_loser_of_a_race_continues_with_the_winners_tokens() -> None:
    """`PLAN.md` §8's rule, as a return value: the store's write decides, not this object."""
    winner = Tokens("winner-access", "winner-refresh", 9999.0)
    transport = FakeTransport(token_response())
    subject = holder(
        transport, Tokens("stale", "r", 0.0), Clock(now=1000.0), on_refresh=lambda _: winner
    )
    assert subject.access_token() == "winner-access"
    assert subject.tokens == winner


# --- code_from_redirect -----------------------------------------------------


def test_the_code_is_extracted_from_the_redirect() -> None:
    url = "http://localhost:8721/callback?code=abc123&state=nonce&scope=read"
    assert code_from_redirect(url, expected_state="nonce") == "abc123"


def test_a_refusal_is_reported_rather_than_parsed() -> None:
    url = "http://localhost:8721/callback?error=access_denied&state=nonce"
    with pytest.raises(OAuthError, match="access_denied"):
        code_from_redirect(url, expected_state="nonce")


def test_a_mismatched_state_is_refused() -> None:
    """The only thing between this flow and an attacker-supplied code."""
    url = "http://localhost:8721/callback?code=abc123&state=someone-elses"
    with pytest.raises(OAuthError, match="state mismatch"):
        code_from_redirect(url, expected_state="nonce")


def test_a_missing_state_is_refused() -> None:
    with pytest.raises(OAuthError, match="state mismatch"):
        code_from_redirect("http://localhost/cb?code=abc", expected_state="nonce")


def test_a_redirect_with_no_code_is_refused() -> None:
    with pytest.raises(OAuthError, match="no authorisation code"):
        code_from_redirect("http://localhost/cb?state=nonce", expected_state="nonce")


def test_state_is_unguessable_and_fresh() -> None:
    import random

    assert len(new_state()) == 32
    assert new_state(random.Random(1)) != new_state(random.Random(2))
    assert new_state(random.Random(1)) == new_state(random.Random(1))


# --- a grant that is gone ---------------------------------------------------
#
# Expected rather than exceptional on Google: a client still in "Testing"
# publishing status is issued refresh tokens that expire after seven days, so an
# unattended deployment meets this on a schedule until the client is published.


def rejected(status: int = 400, payload: object = None) -> Any:
    body = b"" if payload is None else json.dumps(payload).encode()
    return HTTPError(status, "POST", TOKEN_URL, body)


def refresh_against(outcome: object) -> Tokens:
    return refresh(
        FakeTransport(outcome),
        "https://oauth2.googleapis.com/token",
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("stale", "dead", 0.0),
        now=Clock(now=0.0).time,
    )


def test_an_expired_grant_names_the_service_and_the_fix() -> None:
    outcome = rejected(
        payload={
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        }
    )
    with pytest.raises(AuthorisationExpired) as caught:
        refresh_against(outcome)
    assert caught.value.service == "google"
    assert "authorize.py google" in str(caught.value)
    assert "expired or revoked" in str(caught.value)


def test_an_expired_grant_is_deterministic_not_transient() -> None:
    """Nothing automated can recover: renewing the grant needs a browser and a click."""
    with pytest.raises(AuthorisationExpired) as caught:
        refresh_against(rejected(payload={"error": "invalid_grant"}))
    assert not isinstance(caught.value, Transient)
    assert isinstance(caught.value, OAuthError)


def test_the_error_code_alone_is_enough_of_a_detail() -> None:
    with pytest.raises(AuthorisationExpired, match="invalid_grant"):
        refresh_against(rejected(payload={"error": "invalid_grant", "error_description": ""}))


def test_the_strava_endpoint_is_named_when_it_is_the_one_that_died() -> None:
    with pytest.raises(AuthorisationExpired, match=r"authorize\.py strava"):
        refresh(
            FakeTransport(rejected(payload={"error": "invalid_grant"})),
            "https://www.strava.com/oauth/token",
            client_id="cid",
            client_secret="secret",
            tokens=Tokens("stale", "dead", 0.0),
            now=Clock(now=0.0).time,
        )


def test_an_unrecognised_host_is_reported_as_itself() -> None:
    assert service_of("https://tokens.example.test/oauth") == "tokens.example.test"


@pytest.mark.parametrize(
    "outcome",
    [
        # A wrong client secret, which needs a different fix entirely.
        rejected(status=401, payload={"error": "invalid_client"}),
        # A 500 from the token endpoint is transient and must stay retryable.
        rejected(status=500, payload={"error": "invalid_grant"}),
        # An HTML error page rather than JSON.
        rejected(payload=None),
    ],
)
def test_other_token_failures_are_not_reported_as_an_expired_grant(outcome: Any) -> None:
    """Saying "re-authorise" when the real fix is "fix your secret" wastes an evening."""
    with pytest.raises(HTTPError) as caught:
        refresh_against(outcome)
    assert not isinstance(caught.value, AuthorisationExpired)


def test_a_json_body_that_is_not_an_object_falls_through() -> None:
    with pytest.raises(HTTPError):
        refresh_against(rejected(payload=["invalid_grant"]))


# --- the credentials file ---------------------------------------------------
#
# Exists so the client secret reaches Reckon through a 0600 file rather than a
# command line, where it lands in shell history and in `ps` for every other user
# on the machine.


def credentials_json(section: str = "web", **overrides: Any) -> str:
    body = {
        "client_id": "123.apps.googleusercontent.com",
        "project_id": "reckon-471203",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_secret": "GOCSPX-secret",
        "redirect_uris": ["http://localhost:8721/callback"],
    }
    body.update(overrides)
    return json.dumps({section: body})


@pytest.mark.parametrize("section", ["web", "installed"])
def test_either_client_type_is_accepted(section: str) -> None:
    """Which key you get depends on a dropdown chosen minutes earlier."""
    client = read_client_credentials(credentials_json(section))
    assert client.client_id == "123.apps.googleusercontent.com"
    assert client.client_secret == "GOCSPX-secret"
    assert client.redirect_uris == ("http://localhost:8721/callback",)


def test_a_file_with_no_redirect_uris_still_parses() -> None:
    client = read_client_credentials(credentials_json(redirect_uris=None))
    assert client.redirect_uris == ()


def test_a_file_that_is_not_json_says_so() -> None:
    with pytest.raises(OAuthError, match="not JSON"):
        read_client_credentials("{oops")


def test_a_json_document_that_is_not_an_object_says_so() -> None:
    with pytest.raises(OAuthError, match="does not contain a JSON object"):
        read_client_credentials("[]")


def test_the_wrong_kind_of_json_file_names_what_it_found() -> None:
    """Downloading the service-account key instead is an easy mistake to make."""
    with pytest.raises(OAuthError, match="no 'web' or 'installed' section"):
        read_client_credentials(json.dumps({"type": "service_account", "private_key": "..."}))


@pytest.mark.parametrize("missing", ["client_id", "client_secret"])
def test_a_section_missing_either_half_is_refused(missing: str) -> None:
    with pytest.raises(OAuthError, match="missing client_id or client_secret"):
        read_client_credentials(credentials_json(**{missing: ""}))


def test_a_section_that_is_not_an_object_is_not_mistaken_for_one() -> None:
    with pytest.raises(OAuthError, match="no 'web' or 'installed' section"):
        read_client_credentials(json.dumps({"web": "nope"}))
