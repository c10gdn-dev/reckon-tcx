"""The Google Health client: pagination, payload shape, TCX, and 401 recovery.

Field names are the whole risk here. `PLAN.md` §8 exists because getting one
wrong fails silently — a renamed `metricsSummary` does not raise, it just makes
every distance None — so these tests assert on the exact wire spellings observed
in Google's published examples.
"""

import json
from typing import Any

import pytest

from fakes import Clock, FakeTransport, response
from reckon.clients.health import (
    AUTHORIZE_EXTRA,
    MAX_PAGES,
    SCOPES,
    Exercise,
    GoogleHealth,
    UnexpectedPayload,
    token_holder,
)
from reckon.clients.oauth import TokenHolder, Tokens
from reckon.core.errors import AuthError

BASE = "https://health.example.test/v4"


def json_response(payload: Any, status: int = 200) -> Any:
    return response(status=status, body=json.dumps(payload).encode())


def exercise_payload(point_id: str = "889672", **exercise: Any) -> dict[str, Any]:
    body = {
        "interval": {"startTime": "2026-02-23T13:10:00Z", "endTime": "2026-02-23T13:25:00Z"},
        "exerciseType": "WALKING",
        "displayName": "Walk",
        "metricsSummary": {"distanceMillimiters": 1609344, "steps": 2000},
    }
    body.update(exercise)
    return {"name": f"users/2515055/dataTypes/exercise/dataPoints/{point_id}", "exercise": body}


def client(transport: FakeTransport, tokens: TokenHolder | None = None) -> GoogleHealth:
    return GoogleHealth(transport, tokens or live_tokens(FakeTransport()), base_url=BASE)


def live_tokens(transport: FakeTransport) -> TokenHolder:
    return token_holder(
        transport,
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("live-access", "r", 10_000.0),
        now=Clock(now=0.0).time,
    )


def query_of(url: str) -> dict[str, str]:
    import urllib.parse

    return dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))


# --- scopes and authorisation ----------------------------------------------


def test_both_scopes_are_requested() -> None:
    """Without the location scope the export succeeds and the route is simply absent."""
    assert any("activity_and_fitness" in scope for scope in SCOPES)
    assert any("location" in scope for scope in SCOPES)


def test_offline_access_and_a_forced_consent_are_requested() -> None:
    """Without both, a returning user gets no refresh token and the worker dies silently."""
    assert AUTHORIZE_EXTRA == {"access_type": "offline", "prompt": "consent"}


# --- listing exercises ------------------------------------------------------


def test_one_page_of_exercises_is_parsed() -> None:
    transport = FakeTransport(json_response({"dataPoints": [exercise_payload()]}))
    found = list(client(transport).exercises(start_time="2026-02-23T00:00:00Z", end_time="X"))
    assert found == [
        Exercise(
            name="users/2515055/dataTypes/exercise/dataPoints/889672",
            exercise_type="WALKING",
            display_name="Walk",
            start_time="2026-02-23T13:10:00Z",
            end_time="2026-02-23T13:25:00Z",
            distance_m=1609.344,
        )
    ]


def test_the_id_is_the_last_segment_of_the_resource_name() -> None:
    """It is what goes into Strava's external_id and the dedupe store."""
    assert Exercise("users/1/dataTypes/exercise/dataPoints/42", "", "", "", "", None).id == "42"


def test_the_request_targets_the_exercise_collection_with_a_time_filter() -> None:
    transport = FakeTransport(json_response({"dataPoints": []}))
    list(
        client(transport).exercises(
            start_time="2026-02-23T00:00:00Z", end_time="2026-02-24T00:00:00Z"
        )
    )
    sent = transport.requests[0]
    assert sent.url.startswith(f"{BASE}/users/me/dataTypes/exercise/dataPoints?")
    assert sent.headers["Authorization"] == "Bearer live-access"
    query = query_of(sent.url)
    assert query["filter"] == (
        'exercise.interval.start_time >= "2026-02-23T00:00:00Z" '
        'AND exercise.interval.start_time < "2026-02-24T00:00:00Z"'
    )
    assert query["pageSize"] == "25"


def test_the_page_size_is_capped_at_the_apis_own_maximum() -> None:
    transport = FakeTransport(json_response({"dataPoints": []}))
    list(client(transport).exercises(start_time="a", end_time="b", page_size=500))
    assert query_of(transport.requests[0].url)["pageSize"] == "25"


def test_pagination_follows_the_next_page_token() -> None:
    transport = FakeTransport(
        json_response({"dataPoints": [exercise_payload("1")], "nextPageToken": "more"}),
        json_response({"dataPoints": [exercise_payload("2")]}),
    )
    found = list(client(transport).exercises(start_time="a", end_time="b"))
    assert [e.id for e in found] == ["1", "2"]
    assert "pageToken" not in query_of(transport.requests[0].url)
    assert query_of(transport.requests[1].url)["pageToken"] == "more"


def test_an_empty_next_page_token_ends_the_walk() -> None:
    transport = FakeTransport(
        json_response({"dataPoints": [exercise_payload()], "nextPageToken": ""})
    )
    assert len(list(client(transport).exercises(start_time="a", end_time="b"))) == 1


def test_a_missing_data_points_key_yields_nothing() -> None:
    transport = FakeTransport(json_response({}))
    assert list(client(transport).exercises(start_time="a", end_time="b")) == []


def test_endless_pagination_is_refused_rather_than_run_forever() -> None:
    pages = [
        json_response({"dataPoints": [], "nextPageToken": "again"}) for _ in range(MAX_PAGES + 1)
    ]
    transport = FakeTransport(*pages)
    with pytest.raises(UnexpectedPayload, match="still paginating"):
        list(client(transport).exercises(start_time="a", end_time="b"))
    assert transport.calls == MAX_PAGES


# --- payload shape guards ---------------------------------------------------


def test_a_non_object_response_is_reported() -> None:
    transport = FakeTransport(json_response([1, 2, 3]))
    with pytest.raises(UnexpectedPayload, match="expected an object"):
        list(client(transport).exercises(start_time="a", end_time="b"))


def test_a_non_list_data_points_is_reported() -> None:
    transport = FakeTransport(json_response({"dataPoints": {"oops": 1}}))
    with pytest.raises(UnexpectedPayload, match="expected a list"):
        list(client(transport).exercises(start_time="a", end_time="b"))


@pytest.mark.parametrize(
    "raw",
    [{"exercise": {}}, {"name": "n"}, {"name": 7, "exercise": {}}, {"name": "n", "exercise": 7}],
)
def test_a_data_point_missing_its_name_or_exercise_is_reported(raw: dict[str, Any]) -> None:
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    with pytest.raises(UnexpectedPayload, match="name/exercise"):
        list(client(transport).exercises(start_time="a", end_time="b"))


def test_an_exercise_without_an_interval_is_reported() -> None:
    transport = FakeTransport(json_response({"dataPoints": [{"name": "n", "exercise": {}}]}))
    with pytest.raises(UnexpectedPayload, match="no interval"):
        list(client(transport).exercises(start_time="a", end_time="b"))


def test_missing_optional_fields_default_rather_than_raise() -> None:
    raw = {"name": "users/1/dataTypes/exercise/dataPoints/9", "exercise": {"interval": {}}}
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    (found,) = list(client(transport).exercises(start_time="a", end_time="b"))
    assert (found.exercise_type, found.display_name, found.start_time) == ("", "", "")
    assert found.distance_m is None


def test_the_corrected_spelling_of_the_distance_field_is_also_accepted() -> None:
    """Google's own example spells it `distanceMillimiters`. Survive them fixing it."""
    raw = exercise_payload(metricsSummary={"distanceMillimeters": 5000})
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    (found,) = list(client(transport).exercises(start_time="a", end_time="b"))
    assert found.distance_m == 5.0


def test_a_summary_that_is_not_an_object_gives_no_distance() -> None:
    raw = exercise_payload(metricsSummary="none")
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    found = next(iter(client(transport).exercises(start_time="a", end_time="b")))
    assert found.distance_m is None


def test_a_summary_with_no_distance_field_at_all_gives_none() -> None:
    raw = exercise_payload(metricsSummary={"steps": 2000, "caloriesKcal": 120})
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    found = next(iter(client(transport).exercises(start_time="a", end_time="b")))
    assert found.distance_m is None


def test_a_non_numeric_distance_is_reported() -> None:
    raw = exercise_payload(metricsSummary={"distanceMillimiters": "far"})
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    with pytest.raises(UnexpectedPayload, match="not a number"):
        list(client(transport).exercises(start_time="a", end_time="b"))


# --- TCX export -------------------------------------------------------------

TCX = b'<?xml version="1.0"?><TrainingCenterDatabase/>'


def test_tcx_returns_the_body_verbatim() -> None:
    transport = FakeTransport(response(body=TCX))
    assert client(transport).tcx("users/1/dataTypes/exercise/dataPoints/42") == TCX


def test_tcx_asks_for_media_and_partial_data() -> None:
    transport = FakeTransport(response(body=TCX))
    client(transport).tcx("users/1/dataTypes/exercise/dataPoints/42")
    sent = transport.requests[0]
    assert sent.url.startswith(
        f"{BASE}/users/me/dataTypes/exercise/dataPoints/42:exportExerciseTcx?"
    )
    assert query_of(sent.url) == {"alt": "media", "partialData": "true"}
    assert sent.headers["Accept"] == "application/vnd.garmin.tcx+xml"


def test_partial_data_defaults_on_so_a_dropout_still_reaches_strava() -> None:
    """Correcting must never mean dropping; the transform passes partial GPS through."""
    transport = FakeTransport(response(body=TCX))
    client(transport).tcx("42", partial=False)
    assert query_of(transport.requests[0].url)["partialData"] == "false"


def test_a_bare_id_works_as_well_as_a_full_resource_name() -> None:
    transport = FakeTransport(response(body=TCX))
    client(transport).tcx("42")
    assert ":exportExerciseTcx" in transport.requests[0].url


# --- authentication recovery ------------------------------------------------


def refreshing_tokens(token_transport: FakeTransport, expires_at: float = 10_000.0) -> TokenHolder:
    return token_holder(
        token_transport,
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("live-access", "r", expires_at),
        now=Clock(now=0.0).time,
    )


def test_a_401_is_retried_once_through_a_forced_refresh() -> None:
    """A Testing-status Google client's refresh token dies after seven days, mid-life."""
    token_transport = FakeTransport(
        json_response({"access_token": "second", "refresh_token": "r", "expires_in": 3600})
    )
    api = FakeTransport(AuthError(401, "GET", "u", b""), response(body=TCX))
    subject = GoogleHealth(api, refreshing_tokens(token_transport), base_url=BASE)
    assert subject.tcx("42") == TCX
    assert api.requests[0].headers["Authorization"] == "Bearer live-access"
    assert api.requests[1].headers["Authorization"] == "Bearer second"


def test_a_second_401_propagates_rather_than_looping() -> None:
    token_transport = FakeTransport(
        json_response({"access_token": "second", "refresh_token": "r", "expires_in": 3600})
    )
    api = FakeTransport(AuthError(401, "GET", "u", b""), AuthError(401, "GET", "u", b""))
    subject = GoogleHealth(api, refreshing_tokens(token_transport), base_url=BASE)
    with pytest.raises(AuthError):
        subject.tcx("42")
    assert api.calls == 2


def test_an_expired_token_is_refreshed_before_the_call_is_made() -> None:
    token_transport = FakeTransport(
        json_response({"access_token": "renewed", "refresh_token": "r", "expires_in": 3600})
    )
    api = FakeTransport(response(body=TCX))
    subject = GoogleHealth(api, refreshing_tokens(token_transport, expires_at=0.0), base_url=BASE)
    subject.tcx("42")
    assert api.requests[0].headers["Authorization"] == "Bearer renewed"


# --- construction -----------------------------------------------------------


def test_a_custom_timeout_reaches_the_request() -> None:
    transport = FakeTransport(response(body=TCX))
    GoogleHealth(transport, live_tokens(FakeTransport()), base_url=BASE, timeout=5.0).tcx("42")
    assert transport.requests[0].timeout == 5.0


def test_the_default_timeout_is_left_to_the_transport() -> None:
    from reckon.clients.http import DEFAULT_TIMEOUT

    transport = FakeTransport(response(body=TCX))
    client(transport).tcx("42")
    assert transport.requests[0].timeout == DEFAULT_TIMEOUT


def test_a_trailing_slash_on_the_base_url_does_not_double_up() -> None:
    transport = FakeTransport(response(body=TCX))
    GoogleHealth(transport, live_tokens(FakeTransport()), base_url=f"{BASE}/").tcx("42")
    assert "//users" not in transport.requests[0].url.removeprefix("https://")


def test_the_user_segment_is_configurable() -> None:
    transport = FakeTransport(response(body=TCX))
    GoogleHealth(transport, live_tokens(FakeTransport()), base_url=BASE, user="12345").tcx("42")
    assert "/users/12345/" in transport.requests[0].url
