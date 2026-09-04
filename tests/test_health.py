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
    AccountNotLinked,
    Exercise,
    GoogleHealth,
    UnexpectedPayload,
    token_holder,
)
from reckon.clients.oauth import TokenHolder, Tokens
from reckon.core.errors import AuthError, HTTPError, ReckonError

BASE = "https://health.example.test/v4"


def json_response(payload: Any, status: int = 200) -> Any:
    return response(status=status, body=json.dumps(payload).encode())


WINDOW = {"start_time": "2026-02-01T00:00:00Z", "end_time": "2026-03-01T00:00:00Z"}


def exercise_payload(point_id: str = "889672", **exercise: Any) -> dict[str, Any]:
    body = {
        "interval": {"startTime": "2026-02-23T13:10:00Z", "endTime": "2026-02-23T13:25:00Z"},
        "exerciseType": "WALKING",
        "displayName": "Walk",
        "metricsSummary": {"distanceMillimeters": 1609344, "steps": 2000},
        "exerciseMetadata": {"hasGps": True},
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
    assert AUTHORIZE_EXTRA["access_type"] == "offline"
    assert "consent" in AUTHORIZE_EXTRA["prompt"]


def test_the_account_chooser_is_forced() -> None:
    """The project's owner account is routinely not the account holding the data."""
    assert "select_account" in AUTHORIZE_EXTRA["prompt"]


# --- listing exercises ------------------------------------------------------


def test_one_page_of_exercises_is_parsed() -> None:
    transport = FakeTransport(json_response({"dataPoints": [exercise_payload()]}))
    found = list(client(transport).exercises(**WINDOW))
    assert found == [
        Exercise(
            name="users/2515055/dataTypes/exercise/dataPoints/889672",
            exercise_type="WALKING",
            display_name="Walk",
            start_time="2026-02-23T13:10:00Z",
            end_time="2026-02-23T13:25:00Z",
            distance_m=1609.344,
            has_gps=True,
        )
    ]


def test_the_id_is_the_last_segment_of_the_resource_name() -> None:
    """It is what goes into Strava's external_id and the dedupe store."""
    assert Exercise("users/1/dataTypes/exercise/dataPoints/42", "", "", "", "", None).id == "42"


# --- the window is applied here, not by the API -----------------------------
#
# Every documented `filter` spelling is rejected for the exercise data type, so
# the client lists and compares. Confirmed against the live API on 2026-08-30.


def at(start: str, point_id: str = "1") -> dict[str, Any]:
    return exercise_payload(point_id, interval={"startTime": start, "endTime": start})


def test_no_filter_is_sent() -> None:
    """The API rejects every spelling of it; sending one fails the whole call."""
    transport = FakeTransport(json_response({"dataPoints": []}))
    list(client(transport).exercises(**WINDOW))
    assert "filter" not in query_of(transport.requests[0].url)


def test_points_outside_the_window_are_not_yielded() -> None:
    page = {
        "dataPoints": [
            at("2026-03-05T00:00:00Z", "after"),
            at("2026-02-15T00:00:00Z", "inside"),
            at("2026-01-05T00:00:00Z", "before"),
        ]
    }
    found = list(client(FakeTransport(json_response(page))).exercises(**WINDOW))
    assert [e.id for e in found] == ["inside"]


def test_the_end_of_the_window_is_exclusive() -> None:
    """So adjacent windows cannot double-count a session on the boundary."""
    page = {"dataPoints": [at("2026-03-01T00:00:00Z", "edge"), at("2026-02-01T00:00:00Z", "start")]}
    found = list(client(FakeTransport(json_response(page))).exercises(**WINDOW))
    assert [e.id for e in found] == ["start"]


def test_pagination_stops_once_a_page_ends_before_the_window() -> None:
    """The listing is newest-first, so there is nothing newer further on."""
    first = {"dataPoints": [at("2026-01-20T00:00:00Z", "old")], "nextPageToken": "more"}
    transport = FakeTransport(json_response(first), json_response({"dataPoints": []}))
    list(client(transport).exercises(**WINDOW))
    assert transport.calls == 1


def test_pagination_continues_while_the_page_is_still_inside_the_window() -> None:
    first = {"dataPoints": [at("2026-02-20T00:00:00Z", "a")], "nextPageToken": "more"}
    second = {"dataPoints": [at("2026-02-10T00:00:00Z", "b")]}
    transport = FakeTransport(json_response(first), json_response(second))
    found = list(client(transport).exercises(**WINDOW))
    assert [e.id for e in found] == ["a", "b"]


def test_a_point_whose_timestamp_cannot_be_read_is_yielded_not_dropped() -> None:
    """Over-reporting is recoverable and the dedupe store catches it. Losing one is not."""
    page = {"dataPoints": [at("not a timestamp", "unreadable"), at("2026-02-15T00:00:00Z", "ok")]}
    found = list(client(FakeTransport(json_response(page))).exercises(**WINDOW))
    assert [e.id for e in found] == ["unreadable", "ok"]


def test_an_unreadable_timestamp_does_not_decide_when_to_stop() -> None:
    first = {"dataPoints": [at("nonsense", "x")], "nextPageToken": "more"}
    second = {"dataPoints": [at("2026-02-15T00:00:00Z", "y")]}
    transport = FakeTransport(json_response(first), json_response(second))
    assert [e.id for e in client(transport).exercises(**WINDOW)] == ["x", "y"]


@pytest.mark.parametrize("bound", ["start_time", "end_time"])
def test_an_unreadable_window_bound_is_refused(bound: str) -> None:
    window = {**WINDOW, bound: "whenever"}
    with pytest.raises(ReckonError, match=f"{bound} is not an RFC 3339 timestamp"):
        list(client(FakeTransport()).exercises(**window))


# --- hasGps -----------------------------------------------------------------


def test_has_gps_is_carried_through() -> None:
    """Explains a pass-through before the file is even opened."""
    found = next(
        iter(
            client(
                FakeTransport(json_response({"dataPoints": [at("2026-02-15T00:00:00Z")]}))
            ).exercises(**WINDOW)
        )
    )
    assert found.has_gps is True


@pytest.mark.parametrize("metadata", [{"hasGps": False}, {}, "nope", None, {"hasGps": "yes"}])
def test_missing_or_odd_gps_metadata_does_not_raise(metadata: Any) -> None:
    raw = exercise_payload(exerciseMetadata=metadata)
    found = next(
        iter(client(FakeTransport(json_response({"dataPoints": [raw]}))).exercises(**WINDOW))
    )
    assert found.has_gps in (False, None)


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
    assert query_of(sent.url)["pageSize"] == "25"


def test_the_page_size_is_capped_at_the_apis_own_maximum() -> None:
    transport = FakeTransport(json_response({"dataPoints": []}))
    list(client(transport).exercises(page_size=500, **WINDOW))
    assert query_of(transport.requests[0].url)["pageSize"] == "25"


def test_pagination_follows_the_next_page_token() -> None:
    transport = FakeTransport(
        json_response({"dataPoints": [exercise_payload("1")], "nextPageToken": "more"}),
        json_response({"dataPoints": [exercise_payload("2")]}),
    )
    found = list(client(transport).exercises(**WINDOW))
    assert [e.id for e in found] == ["1", "2"]
    assert "pageToken" not in query_of(transport.requests[0].url)
    assert query_of(transport.requests[1].url)["pageToken"] == "more"


def test_an_empty_next_page_token_ends_the_walk() -> None:
    transport = FakeTransport(
        json_response({"dataPoints": [exercise_payload()], "nextPageToken": ""})
    )
    assert len(list(client(transport).exercises(**WINDOW))) == 1


def test_a_missing_data_points_key_yields_nothing() -> None:
    transport = FakeTransport(json_response({}))
    assert list(client(transport).exercises(**WINDOW)) == []


def test_endless_pagination_is_refused_rather_than_run_forever() -> None:
    pages = [
        json_response({"dataPoints": [], "nextPageToken": "again"}) for _ in range(MAX_PAGES + 1)
    ]
    transport = FakeTransport(*pages)
    with pytest.raises(UnexpectedPayload, match="still paginating"):
        list(client(transport).exercises(**WINDOW))
    assert transport.calls == MAX_PAGES


# --- payload shape guards ---------------------------------------------------


def test_a_non_object_response_is_reported() -> None:
    transport = FakeTransport(json_response([1, 2, 3]))
    with pytest.raises(UnexpectedPayload, match="expected an object"):
        list(client(transport).exercises(**WINDOW))


def test_a_non_list_data_points_is_reported() -> None:
    transport = FakeTransport(json_response({"dataPoints": {"oops": 1}}))
    with pytest.raises(UnexpectedPayload, match="expected a list"):
        list(client(transport).exercises(**WINDOW))


@pytest.mark.parametrize(
    "raw",
    [{"exercise": {}}, {"name": "n"}, {"name": 7, "exercise": {}}, {"name": "n", "exercise": 7}],
)
def test_a_data_point_missing_its_name_or_exercise_is_reported(raw: dict[str, Any]) -> None:
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    with pytest.raises(UnexpectedPayload, match="name/exercise"):
        list(client(transport).exercises(**WINDOW))


def test_an_exercise_without_an_interval_is_reported() -> None:
    transport = FakeTransport(json_response({"dataPoints": [{"name": "n", "exercise": {}}]}))
    with pytest.raises(UnexpectedPayload, match="no interval"):
        list(client(transport).exercises(**WINDOW))


def test_missing_optional_fields_default_rather_than_raise() -> None:
    raw = {"name": "users/1/dataTypes/exercise/dataPoints/9", "exercise": {"interval": {}}}
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    (found,) = list(client(transport).exercises(**WINDOW))
    assert (found.exercise_type, found.display_name, found.start_time) == ("", "", "")
    assert found.distance_m is None


def test_the_corrected_spelling_of_the_distance_field_is_also_accepted() -> None:
    """Google's own example spells it `distanceMillimiters`. Survive them fixing it."""
    raw = exercise_payload(metricsSummary={"distanceMillimeters": 5000})
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    (found,) = list(client(transport).exercises(**WINDOW))
    assert found.distance_m == 5.0


def test_a_summary_that_is_not_an_object_gives_no_distance() -> None:
    raw = exercise_payload(metricsSummary="none")
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    found = next(iter(client(transport).exercises(**WINDOW)))
    assert found.distance_m is None


def test_a_summary_with_no_distance_field_at_all_gives_none() -> None:
    raw = exercise_payload(metricsSummary={"steps": 2000, "caloriesKcal": 120})
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    found = next(iter(client(transport).exercises(**WINDOW)))
    assert found.distance_m is None


def test_a_non_numeric_distance_is_reported() -> None:
    raw = exercise_payload(metricsSummary={"distanceMillimiters": "far"})
    transport = FakeTransport(json_response({"dataPoints": [raw]}))
    with pytest.raises(UnexpectedPayload, match="not a number"):
        list(client(transport).exercises(**WINDOW))


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


# --- an account with no Google Health data ----------------------------------
#
# Found on the very first live call: the OAuth client, both scopes and the token
# were all correct, and the API still refused, because authorising an account and
# that account having health data are different things.


def not_linked(url: str | None = "https://fitbit.google.com/auth/signup") -> HTTPError:
    metadata = {} if url is None else {"redirect_uri": url}
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": "The account is not linked to Google Health.",
                "status": "FAILED_PRECONDITION",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "ACCOUNT_NOT_LINKED",
                        "domain": "health.googleapis.com",
                        "metadata": metadata,
                    }
                ],
            }
        }
    ).encode()
    return HTTPError(400, "GET", "https://health.googleapis.com/v4/x", body)


def test_an_unlinked_account_is_named_as_such_with_the_signup_url() -> None:
    with pytest.raises(AccountNotLinked) as caught:
        client(FakeTransport(not_linked())).tcx("42")
    assert caught.value.signup_url == "https://fitbit.google.com/auth/signup"
    assert "not linked to Google Health" in str(caught.value)


def test_the_message_suggests_the_other_account_too() -> None:
    """The usual cause is authorising the account that owns the Cloud project."""
    with pytest.raises(AccountNotLinked, match="the account your Fitbit data actually lives under"):
        client(FakeTransport(not_linked())).tcx("42")


def test_a_missing_signup_url_still_produces_a_usable_message() -> None:
    with pytest.raises(AccountNotLinked) as caught:
        client(FakeTransport(not_linked(url=None))).tcx("42")
    assert caught.value.signup_url is None
    assert " at " not in str(caught.value)


def test_metadata_that_is_not_an_object_is_survived() -> None:
    body = json.dumps(
        {"error": {"details": [{"reason": "ACCOUNT_NOT_LINKED", "metadata": "nope"}]}}
    ).encode()
    with pytest.raises(AccountNotLinked) as caught:
        client(FakeTransport(HTTPError(400, "GET", "u", body))).tcx("42")
    assert caught.value.signup_url is None


def test_it_is_detected_on_a_listing_as_well_as_a_download() -> None:
    with pytest.raises(AccountNotLinked):
        list(client(FakeTransport(not_linked())).exercises(**WINDOW))


def test_it_is_detected_after_a_token_refresh_too() -> None:
    tokens = FakeTransport(
        json_response({"access_token": "second", "refresh_token": "r", "expires_in": 3600})
    )
    api = FakeTransport(AuthError(401, "GET", "u", b""), not_linked())
    with pytest.raises(AccountNotLinked):
        GoogleHealth(api, refreshing_tokens(tokens), base_url=BASE).tcx("42")


@pytest.mark.parametrize(
    "body",
    [
        b"<html>gateway</html>",
        b"[]",
        json.dumps({"error": {"details": [{"reason": "SOMETHING_ELSE"}]}}).encode(),
        json.dumps({"error": {"details": ["not an object"]}}).encode(),
        json.dumps({"error": {}}).encode(),
    ],
)
def test_other_bad_requests_are_not_mistaken_for_an_unlinked_account(body: bytes) -> None:
    with pytest.raises(HTTPError) as caught:
        client(FakeTransport(HTTPError(400, "GET", "u", body))).tcx("42")
    assert not isinstance(caught.value, AccountNotLinked)


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


# --- heart rate -------------------------------------------------------------
#
# Fetched separately because `:exportExerciseTcx` omits it entirely. The response
# shape here is documentation-derived — the scope was not granted when this was
# written — so the parsing is deliberately tolerant and the spellings it accepts
# are listed rather than assumed.

HR_WINDOW = {"start_time": "2026-02-15T10:00:00Z", "end_time": "2026-02-15T11:00:00Z"}


def samples(*points: dict[str, Any], token: str | None = None) -> Any:
    body: dict[str, Any] = {"dataPoints": list(points)}
    if token:
        body["nextPageToken"] = token
    return json_response(body)


def test_the_heart_rate_scope_is_requested() -> None:
    """Without it every call is a 403 and no heart rate is ever merged."""
    assert any("health_metrics_and_measurements" in scope for scope in SCOPES)


def test_samples_inside_the_window_are_returned_oldest_first() -> None:
    page = samples(
        {"sampleTime": "2026-02-15T10:30:00Z", "beatsPerMinute": 120},
        {"sampleTime": "2026-02-15T10:10:00Z", "beatsPerMinute": 100},
    )
    found = client(FakeTransport(page)).heart_rate(**HR_WINDOW)
    assert [bpm for _, bpm in found] == [100, 120]


def test_the_request_targets_the_heart_rate_collection() -> None:
    transport = FakeTransport(samples())
    client(transport).heart_rate(**HR_WINDOW)
    assert "/dataTypes/heart-rate/dataPoints" in transport.requests[0].url
    assert query_of(transport.requests[0].url)["pageSize"] == "1000"


def test_no_filter_is_sent_here_either() -> None:
    transport = FakeTransport(samples())
    client(transport).heart_rate(**HR_WINDOW)
    assert "filter" not in query_of(transport.requests[0].url)


def test_samples_outside_the_window_are_dropped() -> None:
    page = samples(
        {"sampleTime": "2026-02-15T09:00:00Z", "beatsPerMinute": 60},
        {"sampleTime": "2026-02-15T10:30:00Z", "beatsPerMinute": 120},
        {"sampleTime": "2026-02-15T12:00:00Z", "beatsPerMinute": 90},
    )
    assert [b for _, b in client(FakeTransport(page)).heart_rate(**HR_WINDOW)] == [120]


def test_heart_rate_pagination_stops_once_a_page_is_before_the_window() -> None:
    first = samples({"sampleTime": "2026-02-15T09:00:00Z", "beatsPerMinute": 60}, token="more")
    transport = FakeTransport(first, samples())
    client(transport).heart_rate(**HR_WINDOW)
    assert transport.calls == 1


def test_heart_rate_pagination_continues_while_inside_the_window() -> None:
    first = samples({"sampleTime": "2026-02-15T10:40:00Z", "beatsPerMinute": 130}, token="more")
    second = samples({"sampleTime": "2026-02-15T10:20:00Z", "beatsPerMinute": 110})
    found = client(FakeTransport(first, second)).heart_rate(**HR_WINDOW)
    assert [b for _, b in found] == [110, 130]


@pytest.mark.parametrize(
    "point",
    [
        {"sampleTime": "2026-02-15T10:30:00Z", "beatsPerMinute": 120},
        {"sampleTime": "2026-02-15T10:30:00Z", "bpm": 120},
        {"sampleTime": "2026-02-15T10:30:00Z", "value": 120},
        {"time": "2026-02-15T10:30:00Z", "beatsPerMinute": 120},
        {"heartRate": {"sampleTime": "2026-02-15T10:30:00Z", "beatsPerMinute": 120}},
    ],
)
def test_every_plausible_spelling_is_accepted(point: dict[str, Any]) -> None:
    """The live shape is unverified, so the documented variants are all tried."""
    assert [b for _, b in client(FakeTransport(samples(point))).heart_rate(**HR_WINDOW)] == [120]


@pytest.mark.parametrize(
    "point",
    [
        {"beatsPerMinute": 120},
        {"sampleTime": "not a time", "beatsPerMinute": 120},
        {"sampleTime": "2026-02-15T10:30:00Z"},
        {"sampleTime": "2026-02-15T10:30:00Z", "beatsPerMinute": "fast"},
        {"heartRate": "nope"},
        "not an object",
    ],
)
def test_an_unreadable_sample_costs_that_sample_and_nothing_else(point: Any) -> None:
    """Tolerant on purpose: heart rate is enrichment, and one bad reading must
    not cost an upload. `_exercise` is strict for the opposite reason."""
    page = samples(point, {"sampleTime": "2026-02-15T10:31:00Z", "beatsPerMinute": 99})
    assert [b for _, b in client(FakeTransport(page)).heart_rate(**HR_WINDOW)] == [99]


def test_a_float_reading_is_rounded_to_an_integer() -> None:
    page = samples({"sampleTime": "2026-02-15T10:30:00Z", "beatsPerMinute": 120.7})
    assert [b for _, b in client(FakeTransport(page)).heart_rate(**HR_WINDOW)] == [120]


@pytest.mark.parametrize("bound", ["start_time", "end_time"])
def test_an_unreadable_window_bound_is_refused_here_too(bound: str) -> None:
    with pytest.raises(ReckonError, match=f"{bound} is not an RFC 3339 timestamp"):
        client(FakeTransport()).heart_rate(**{**HR_WINDOW, bound: "whenever"})


def test_endless_pagination_is_refused() -> None:
    page = samples({"sampleTime": "2026-02-15T10:30:00Z", "beatsPerMinute": 120}, token="again")
    transport = FakeTransport(*[page] * (MAX_PAGES + 1))
    client(transport).heart_rate(**HR_WINDOW)
    assert transport.calls == MAX_PAGES


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("146", 146), (146, 146), (146.9, 146), (None, None), ("fast", None), ({}, None)],
)
def test_the_summary_average_heart_rate_is_parsed_permissively(raw: Any, expected: Any) -> None:
    """Google serialises int64 as a string. Advisory, so a bad value costs the value."""
    summary = (
        {"distanceMillimeters": 1000} if raw is None else {"averageHeartRateBeatsPerMinute": raw}
    )
    page = json_response({"dataPoints": [exercise_payload(metricsSummary=summary)]})
    found = next(iter(client(FakeTransport(page)).exercises(**WINDOW)))
    assert found.average_heart_rate == expected


def test_a_summary_that_is_not_an_object_gives_no_average() -> None:
    page = json_response({"dataPoints": [exercise_payload(metricsSummary="none")]})
    found = next(iter(client(FakeTransport(page)).exercises(**WINDOW)))
    assert found.average_heart_rate is None
