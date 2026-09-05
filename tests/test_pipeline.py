"""The pipeline: outcome routing, dedupe, sport types, and the CAS closure.

Built on the *real* clients over a `FakeTransport` rather than on fake clients.
The clients have their own tests; what this file has to catch is the two of them
disagreeing with the pipeline about a field name, which a hand-written fake
client would hide by construction.

The routing is the thing under test. `passed_through` versus `withheld` is the
distinction the whole design turns on: one reaches Strava uncorrected, the other
deliberately does not reach it at all.
"""

import json
import pathlib
from typing import Any

import pytest

import builders
from fakes import Clock, FakeLogStore, FakeTokenStore, FakeTransport, RecordingSleep, response
from reckon.clients.health import Exercise, GoogleHealth
from reckon.clients.oauth import TokenHolder, Tokens
from reckon.clients.strava import Strava
from reckon.core import tcx
from reckon.core.errors import AuthError, NetworkError, ReckonError
from reckon.core.rescale import ToleranceAction
from reckon.pipeline import (
    DEFAULT_SPORT_TYPE,
    PROJECT_URL,
    SPORT_TYPES,
    NotAuthorised,
    Outcome,
    Pipeline,
    _device,
    describe,
    summarise,
    token_holder,
)
from reckon.stores.base import LogEntry, Status, StoreError, VersionedTokens

LIVE = Tokens("live-access", "refresh", 10_000.0)

# The client filters the listing itself — the API rejects every documented
# spelling of its `filter` parameter for the exercise data type.
WINDOW = {"start_time": "2026-02-01T00:00:00Z", "end_time": "2026-03-01T00:00:00Z"}

# A track that measures long and a Lap total that says so: the ordinary case.
CORRECTABLE = builders.tcx(distances=[0.0, 500.0, 1000.0], lap_distance_m=930.0)
# Trackpoints with time and heart rate but no Position — the yoga shape.
NO_GPS = builders.tcx(distances=[None, None, None], with_position=False, lap_distance_m=0.0)


def exercise(
    point_id: str = "889672",
    exercise_type: str = "WALKING",
    display_name: str = "Morning Walk",
) -> Exercise:
    return Exercise(
        name=f"users/me/dataTypes/exercise/dataPoints/{point_id}",
        exercise_type=exercise_type,
        display_name=display_name,
        start_time="2026-02-23T13:10:00Z",
        end_time="2026-02-23T13:25:00Z",
        distance_m=930.0,
    )


def json_response(payload: Any) -> Any:
    return response(body=json.dumps(payload).encode())


def upload_response(**overrides: Any) -> Any:
    payload = {
        "id": 987,
        "external_id": "889672",
        "error": None,
        "status": "Your activity is still being processed.",
        "activity_id": None,
    }
    payload.update(overrides)
    return json_response(payload)


def holder(transport: FakeTransport | None = None) -> TokenHolder:
    return TokenHolder(
        transport or FakeTransport(),
        "https://token.example.test/",
        client_id="cid",
        client_secret="secret",
        tokens=LIVE,
        now=Clock(now=0.0).time,
    )


def pipeline(
    health_transport: FakeTransport,
    strava_transport: FakeTransport | None = None,
    logs: FakeLogStore | None = None,
    **settings: Any,
) -> Pipeline:
    settings.setdefault("sleep", RecordingSleep())
    # Off unless a test is about it, so every other test's FakeTransport does not
    # have to be scripted for the extra heart-rate call.
    settings.setdefault("merge_heart_rate", False)
    tokens = settings.pop("token_transport", None)
    return Pipeline(
        health=GoogleHealth(health_transport, holder(tokens), base_url="https://health.test/v4"),
        strava=Strava(
            strava_transport or FakeTransport(),
            holder(tokens),
            base_url="https://strava.test/v3",
        ),
        logs=logs or FakeLogStore(),
        now=Clock(now=1000.0).time,
        **settings,
    )


# --- the ordinary case ------------------------------------------------------


def test_a_correctable_activity_is_rescaled_and_uploaded() -> None:
    logs = FakeLogStore()
    subject = pipeline(
        FakeTransport(response(body=CORRECTABLE)),
        FakeTransport(upload_response(), upload_response(activity_id=555)),
        logs,
    )
    outcome = subject.process(exercise())
    assert outcome.status is Status.UPLOADED
    assert outcome.strava_activity_id == 555
    assert outcome.factor == pytest.approx(0.93)
    assert logs.recorded[0].status is Status.UPLOADED


def test_the_uploaded_bytes_are_the_corrected_ones() -> None:
    strava = FakeTransport(upload_response(activity_id=555))
    pipeline(FakeTransport(response(body=CORRECTABLE)), strava).process(exercise())
    assert b"930" in strava.requests[0].body
    assert strava.requests[0].body.count(b"1000.0") == 0, "the raw GPS total should be gone"


def test_the_external_id_is_the_activity_id() -> None:
    strava = FakeTransport(upload_response(activity_id=555))
    pipeline(FakeTransport(response(body=CORRECTABLE)), strava).process(exercise("12345"))
    assert b"12345" in strava.requests[0].body


def test_the_decision_is_recorded_with_the_clock():
    logs = FakeLogStore()
    pipeline(
        FakeTransport(response(body=CORRECTABLE)),
        FakeTransport(upload_response(activity_id=555)),
        logs,
    ).process(exercise())
    assert logs.recorded[0].recorded_at == 1000.0


# --- passed_through: uncorrectable, and uploaded anyway ---------------------


def test_an_activity_with_no_gps_is_uploaded_unchanged() -> None:
    """Yoga. Uncorrectable, and it must still reach Strava."""
    strava = FakeTransport(upload_response(activity_id=555))
    outcome = pipeline(FakeTransport(response(body=NO_GPS)), strava).process(
        exercise(exercise_type="YOGA", display_name="Yoga")
    )
    assert outcome.status is Status.PASSED_THROUGH
    assert outcome.status.on_strava is True
    assert outcome.reason == "no_gps"
    assert outcome.factor is None
    assert NO_GPS in strava.requests[0].body, "byte-identical, not merely similar"


def test_a_file_with_no_activities_at_all_is_passed_through() -> None:
    """Google listed it, the export came back empty. Still not Reckon's to drop."""
    strava = FakeTransport(upload_response(activity_id=555))
    outcome = pipeline(FakeTransport(response(body=builders.document())), strava).process(
        exercise()
    )
    assert outcome.status is Status.PASSED_THROUGH
    assert outcome.reason == "nothing to rescale"


def test_a_passed_through_activity_is_still_recorded() -> None:
    logs = FakeLogStore()
    pipeline(
        FakeTransport(response(body=NO_GPS)), FakeTransport(upload_response(activity_id=1)), logs
    ).process(exercise())
    assert logs.recorded[0].status is Status.PASSED_THROUGH


def test_a_passed_through_upload_carries_no_correction_note() -> None:
    strava = FakeTransport(upload_response(activity_id=555))
    pipeline(FakeTransport(response(body=NO_GPS)), strava).process(exercise())
    assert b"corrected by Reckon" not in strava.requests[0].body


def test_a_corrected_upload_says_what_changed() -> None:
    strava = FakeTransport(upload_response(activity_id=555))
    pipeline(FakeTransport(response(body=CORRECTABLE)), strava).process(exercise())
    body = strava.requests[0].body
    assert b"Reckon" in body
    assert "corrected 1.00 \u2192 0.93 km".encode() in body
    assert PROJECT_URL.encode() in body


def test_the_description_names_the_device_that_recorded_it() -> None:
    """A file self-identifies, so the activity can say which watch it came from."""
    from reckon.core.rescale import rescale_tcx

    data = pathlib.Path("training-data/6772877356585089904.tcx").read_bytes()
    assert describe(rescale_tcx(data), _device(data)).startswith("Reckon \u00b7 Charge 5 \u00b7")


def test_a_file_without_a_creator_omits_the_device() -> None:
    """Synthetic fixtures have no Creator; the separator must not be left dangling."""
    from reckon.core.rescale import rescale_tcx

    line = describe(rescale_tcx(CORRECTABLE), None).splitlines()[0]
    assert line == "Reckon \u00b7 corrected 1.00 \u2192 0.93 km"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (NO_GPS, "not corrected \u2014 no GPS recorded"),
        (
            builders.tcx(
                distances=tuple(float(i * 100) for i in range(10)),
                lap_distance_m=909.0,
                positions=[True] * 4 + [False] + [True] * 5,
            ),
            "not corrected \u2014 GPS incomplete",
        ),
    ],
)
def test_each_uncorrected_reason_has_its_own_wording(document: bytes, expected: str) -> None:
    """The reason is stated, because "not corrected" alone invites the question."""
    from reckon.core.rescale import rescale_tcx

    assert expected in describe(rescale_tcx(document), None)


def test_the_wording_avoids_this_projects_vocabulary() -> None:
    """ "Passed through" is precise here and meaningless in a Strava feed."""
    from reckon.core.rescale import rescale_tcx

    text = describe(rescale_tcx(NO_GPS), None)
    assert "passed through" not in text.lower()
    assert "skip" not in text.lower()


# --- withheld: deliberately not uploaded ------------------------------------


def test_a_malformed_file_is_withheld_and_never_uploaded() -> None:
    strava = FakeTransport()
    outcome = pipeline(FakeTransport(response(body=b"<not-tcx/>")), strava).process(exercise())
    assert outcome.status is Status.WITHHELD
    assert outcome.status.on_strava is False
    assert strava.calls == 0, "withheld means it does not reach Strava"


def test_a_factor_outside_tolerance_is_withheld_under_abort() -> None:
    absurd = builders.tcx(distances=[0.0, 500.0, 1000.0], lap_distance_m=10.0)
    outcome = pipeline(FakeTransport(response(body=absurd)), FakeTransport()).process(exercise())
    assert outcome.status is Status.WITHHELD
    assert "tolerance" in outcome.reason


def test_the_same_file_uploads_when_tolerance_is_relaxed() -> None:
    """Withholding is a policy, not a property of the file."""
    absurd = builders.tcx(distances=[0.0, 500.0, 1000.0], lap_distance_m=10.0)
    outcome = pipeline(
        FakeTransport(response(body=absurd)),
        FakeTransport(upload_response(activity_id=555)),
        on_tolerance=ToleranceAction.PROCEED,
    ).process(exercise())
    assert outcome.status is Status.UPLOADED


def test_a_withheld_activity_is_recorded_so_it_is_not_retried() -> None:
    logs = FakeLogStore()
    pipeline(FakeTransport(response(body=b"<not-tcx/>")), FakeTransport(), logs).process(exercise())
    assert logs.recorded[0].status is Status.WITHHELD


# --- transient faults propagate ---------------------------------------------


def test_a_network_fault_propagates_and_records_nothing() -> None:
    """SQS must redeliver. A recorded outcome would mean it never retries."""
    logs = FakeLogStore()
    with pytest.raises(NetworkError):
        pipeline(FakeTransport(NetworkError("reset")), FakeTransport(), logs).process(exercise())
    assert logs.recorded == []


def test_an_auth_fault_that_survives_a_refresh_propagates() -> None:
    strava = FakeTransport(AuthError(401, "POST", "u", b""), AuthError(401, "POST", "u", b""))
    tokens = FakeTransport(
        json_response({"access_token": "second", "refresh_token": "r", "expires_in": 3600})
    )
    with pytest.raises(AuthError):
        pipeline(FakeTransport(response(body=CORRECTABLE)), strava, token_transport=tokens).process(
            exercise()
        )


# --- dedupe -----------------------------------------------------------------


def test_an_already_recorded_activity_is_not_fetched_again() -> None:
    health = FakeTransport()
    logs = FakeLogStore(LogEntry("889672", Status.UPLOADED, strava_activity_id=42))
    outcome = pipeline(health, FakeTransport(), logs).process(exercise())
    assert outcome.fresh is False
    assert outcome.status is Status.UPLOADED
    assert outcome.strava_activity_id == 42
    assert health.calls == 0, "the decision was already made"


def test_a_withheld_activity_stays_withheld_on_a_second_pass() -> None:
    logs = FakeLogStore(LogEntry("889672", Status.WITHHELD, reason="malformed"))
    outcome = pipeline(FakeTransport(), FakeTransport(), logs).process(exercise())
    assert (outcome.status, outcome.fresh, outcome.reason) == (
        Status.WITHHELD,
        False,
        "malformed",
    )


def test_replaying_a_decision_does_not_record_it_again() -> None:
    logs = FakeLogStore(LogEntry("889672", Status.UPLOADED))
    pipeline(FakeTransport(), FakeTransport(), logs).process(exercise())
    assert logs.recorded == []


# --- the asynchronous upload ------------------------------------------------


def test_polling_continues_until_strava_finishes() -> None:
    strava = FakeTransport(upload_response(), upload_response(), upload_response(activity_id=555))
    sleeper = RecordingSleep()
    subject = pipeline(FakeTransport(response(body=CORRECTABLE)), strava, sleep=sleeper)
    assert subject.process(exercise()).strava_activity_id == 555
    assert len(sleeper.calls) == 2


def test_polling_backs_off_rather_than_hammering() -> None:
    strava = FakeTransport(*[upload_response()] * 6)
    sleeper = RecordingSleep()
    pipeline(
        FakeTransport(response(body=CORRECTABLE)), strava, sleep=sleeper, poll_delay=1.0
    ).process(exercise())
    assert sleeper.calls == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_an_upload_that_never_finishes_is_a_failure_not_a_retry() -> None:
    """A slow Strava is not a poisoned message and does not belong in the DLQ."""
    strava = FakeTransport(*[upload_response()] * 6)
    outcome = pipeline(FakeTransport(response(body=CORRECTABLE)), strava).process(exercise())
    assert outcome.status is Status.FAILED
    assert "still processing after 5 checks" in outcome.reason


def test_an_upload_that_is_finished_immediately_is_not_polled() -> None:
    strava = FakeTransport(upload_response(activity_id=555))
    sleeper = RecordingSleep()
    pipeline(FakeTransport(response(body=CORRECTABLE)), strava, sleep=sleeper).process(exercise())
    assert sleeper.calls == []


def test_a_rejected_upload_is_a_failure() -> None:
    strava = FakeTransport(upload_response(error="Invalid file type"))
    outcome = pipeline(FakeTransport(response(body=CORRECTABLE)), strava).process(exercise())
    assert outcome.status is Status.FAILED
    assert outcome.reason == "Invalid file type"


def test_a_duplicate_counts_as_uploaded() -> None:
    """Strava dedupes on external_id; the activity is there, which is what matters."""
    strava = FakeTransport(upload_response(error="duplicate of activity 42"))
    outcome = pipeline(FakeTransport(response(body=CORRECTABLE)), strava).process(exercise())
    assert outcome.status is Status.UPLOADED
    assert "already on Strava" in outcome.reason


def test_a_duplicate_of_a_passed_through_activity_keeps_its_reason() -> None:
    strava = FakeTransport(upload_response(error="duplicate of activity 42"))
    outcome = pipeline(FakeTransport(response(body=NO_GPS)), strava).process(exercise())
    assert outcome.status is Status.PASSED_THROUGH
    assert outcome.reason == "already on Strava: no_gps"


# --- sport types ------------------------------------------------------------


@pytest.mark.parametrize(("exercise_type", "sport_type"), sorted(SPORT_TYPES.items()))
def test_every_mapped_exercise_type_reaches_strava(exercise_type: str, sport_type: str) -> None:
    strava = FakeTransport(upload_response(activity_id=1))
    outcome = pipeline(FakeTransport(response(body=CORRECTABLE)), strava).process(
        exercise(exercise_type=exercise_type)
    )
    assert f'name="sport_type"\r\n\r\n{sport_type}'.encode() in strava.requests[0].body
    assert outcome.warnings == ()


def test_weight_training_is_mapped() -> None:
    """Found live: without this, every gym session uploads as a run."""
    assert SPORT_TYPES["WEIGHTS"] == "WeightTraining"


def test_the_default_asserts_nothing_untrue() -> None:
    """`Workout` is as editable as `Run` and does not claim an activity happened."""
    assert DEFAULT_SPORT_TYPE == "Workout"


def test_an_unmapped_type_uploads_with_a_default_and_a_warning() -> None:
    """Refusing to upload would be the dropping the whole design says never to do."""
    strava = FakeTransport(upload_response(activity_id=1))
    outcome = pipeline(FakeTransport(response(body=CORRECTABLE)), strava).process(
        exercise(exercise_type="ROLLER_SKIING")
    )
    assert f'name="sport_type"\r\n\r\n{DEFAULT_SPORT_TYPE}'.encode() in strava.requests[0].body
    assert "ROLLER_SKIING" in outcome.warnings[0]


def test_the_mapping_can_be_replaced_wholesale() -> None:
    strava = FakeTransport(upload_response(activity_id=1))
    pipeline(
        FakeTransport(response(body=CORRECTABLE)), strava, sport_types={"WALKING": "Hike"}
    ).process(exercise())
    assert b'name="sport_type"\r\n\r\nHike' in strava.requests[0].body


def test_an_activity_with_no_display_name_still_gets_one() -> None:
    strava = FakeTransport(upload_response(activity_id=1))
    pipeline(FakeTransport(response(body=CORRECTABLE)), strava).process(exercise(display_name=""))
    assert b'name="name"\r\n\r\nActivity' in strava.requests[0].body


# --- dry run ----------------------------------------------------------------


def test_a_dry_run_uploads_nothing_and_records_nothing() -> None:
    strava = FakeTransport()
    logs = FakeLogStore()
    outcome = pipeline(
        FakeTransport(response(body=CORRECTABLE)), strava, logs, dry_run=True
    ).process(exercise())
    assert strava.calls == 0
    assert logs.recorded == []
    assert outcome.status is Status.UPLOADED
    assert outcome.reason == "dry run"


def test_a_dry_run_still_reports_the_factor_it_would_have_used() -> None:
    outcome = pipeline(
        FakeTransport(response(body=CORRECTABLE)), FakeTransport(), dry_run=True
    ).process(exercise())
    assert outcome.factor == pytest.approx(0.93)


def test_a_dry_run_names_the_pass_through_reason_too() -> None:
    outcome = pipeline(FakeTransport(response(body=NO_GPS)), FakeTransport(), dry_run=True).process(
        exercise()
    )
    assert outcome.reason == "dry run: no_gps"


# --- sync -------------------------------------------------------------------


def test_sync_processes_every_activity_in_the_window() -> None:
    listing = json_response(
        {
            "dataPoints": [
                {
                    "name": f"users/me/dataTypes/exercise/dataPoints/{i}",
                    "exercise": {
                        "interval": {"startTime": "2026-02-23T13:10:00Z"},
                        "exerciseType": "WALKING",
                        "displayName": "Walk",
                    },
                }
                for i in ("1", "2")
            ]
        }
    )
    health = FakeTransport(listing, response(body=CORRECTABLE), response(body=CORRECTABLE))
    strava = FakeTransport(upload_response(activity_id=11), upload_response(activity_id=22))
    outcomes = pipeline(health, strava).sync(**WINDOW)
    assert [o.activity_id for o in outcomes] == ["1", "2"]
    assert [o.strava_activity_id for o in outcomes] == [11, 22]


def test_sync_over_an_empty_window_is_an_empty_list() -> None:
    outcomes = pipeline(FakeTransport(json_response({"dataPoints": []}))).sync(**WINDOW)
    assert outcomes == []


def test_summarise_counts_by_status_and_separates_replays() -> None:
    counts = summarise(
        [
            Outcome("1", Status.UPLOADED),
            Outcome("2", Status.UPLOADED),
            Outcome("3", Status.PASSED_THROUGH),
            Outcome("4", Status.UPLOADED, fresh=False),
        ]
    )
    assert counts == {"uploaded": 2, "passed_through": 1, "already done": 1}


def test_summarise_of_nothing_is_empty() -> None:
    assert summarise([]) == {}


# --- fetch ------------------------------------------------------------------


def test_fetch_returns_the_corrected_file() -> None:
    data = pipeline(FakeTransport(response(body=CORRECTABLE))).fetch("889672")
    assert b"930" in data
    assert data != CORRECTABLE


def test_fetch_raw_returns_googles_bytes_untouched() -> None:
    assert pipeline(FakeTransport(response(body=CORRECTABLE))).fetch("889672", raw=True) == (
        CORRECTABLE
    )


def test_fetch_needs_no_second_request_for_the_target() -> None:
    """The target is already in the file — still true on the new API."""
    health = FakeTransport(response(body=CORRECTABLE))
    pipeline(health).fetch("889672")
    assert health.calls == 1


# --- the token CAS closure --------------------------------------------------


def bind(store: FakeTokenStore, transport: FakeTransport) -> TokenHolder:
    return token_holder(
        store,  # type: ignore[arg-type]
        "google",
        transport=transport,
        token_url="https://token.example.test/",
        client_id="cid",
        client_secret="secret",
        now=Clock(now=20_000.0).time,
    )


def refreshed(access: str = "second") -> Any:
    return json_response({"access_token": access, "refresh_token": "refresh", "expires_in": 3600})


def test_an_unauthorised_service_is_named_with_the_fix() -> None:
    with pytest.raises(NotAuthorised, match=r"authorize\.py google"):
        bind(FakeTokenStore(), FakeTransport())


def test_a_refresh_is_persisted_before_the_new_token_is_used() -> None:
    store = FakeTokenStore(google=Tokens("stale", "refresh", 0.0))
    transport = FakeTransport(refreshed())
    assert bind(store, transport).access_token() == "second"
    assert store.records["google"] == VersionedTokens(
        Tokens("second", "refresh", 23_600.0), version=2
    )


def test_a_live_token_is_used_without_touching_the_store() -> None:
    store = FakeTokenStore(google=Tokens("live-access", "refresh", 99_000.0))
    assert bind(store, FakeTransport()).access_token() == "live-access"
    assert store.saves == []


def test_the_loser_of_a_race_continues_with_the_winners_tokens() -> None:
    """The branch settled in docs/diagrams/token-refresh.puml."""
    store = FakeTokenStore(google=Tokens("stale", "refresh", 0.0))
    holder_under_test = bind(store, FakeTransport(refreshed("mine")))

    # Someone else refreshes and writes first, taking the version to 2.
    winner = Tokens("winner", "refresh", 99_000.0)
    store.save("google", winner, expected_version=1)

    assert holder_under_test.access_token() == "winner"
    assert holder_under_test.tokens == winner


def test_the_refresh_is_not_repeated_after_a_lost_race() -> None:
    """A conflict means the work was already done. Refreshing again unbounds it."""
    store = FakeTokenStore(google=Tokens("stale", "refresh", 0.0))
    transport = FakeTransport(refreshed("mine"))
    holder_under_test = bind(store, transport)
    store.save("google", Tokens("winner", "refresh", 99_000.0), expected_version=1)
    holder_under_test.access_token()
    assert transport.calls == 1


def test_the_loser_tracks_the_winners_version_for_its_next_write() -> None:
    store = FakeTokenStore(google=Tokens("stale", "refresh", 0.0))
    transport = FakeTransport(refreshed("mine"), refreshed("later"))
    holder_under_test = bind(store, transport)
    store.save("google", Tokens("winner", "refresh", 0.0), expected_version=1)
    holder_under_test.access_token()
    assert holder_under_test.force_refresh() == "later"
    assert store.records["google"].version == 3


def test_tokens_vanishing_mid_refresh_is_reported_not_guessed_at() -> None:
    """A conflict says someone else won. If nobody did, something is badly wrong."""
    from reckon.stores.base import TokenConflict

    store = FakeTokenStore(google=Tokens("stale", "refresh", 0.0))
    holder_under_test = bind(store, FakeTransport(refreshed()))

    def conflict(service: str, tokens: object, *, expected_version: int) -> object:
        raise TokenConflict(service, expected_version, 99)

    store.save = conflict  # type: ignore[method-assign]
    store.records.clear()

    with pytest.raises(StoreError, match="vanished mid-refresh"):
        holder_under_test.access_token()


def test_a_reckon_error_is_what_the_cli_catches() -> None:
    assert issubclass(NotAuthorised, ReckonError)


# --- adopting an account that already syncs by another route ----------------


def test_mark_done_records_without_fetching_or_uploading() -> None:
    """The whole point: a first sync must not re-upload a history already there."""
    listing = json_response(
        {
            "dataPoints": [
                {
                    "name": f"users/me/dataTypes/exercise/dataPoints/{i}",
                    "exercise": {
                        "interval": {"startTime": "2026-02-15T00:00:00Z"},
                        "exerciseType": "WALKING",
                        "displayName": "Walk",
                    },
                }
                for i in ("1", "2")
            ]
        }
    )
    health = FakeTransport(listing)
    strava = FakeTransport()
    logs = FakeLogStore()
    outcomes = pipeline(health, strava, logs).mark_done(reason="already there", **WINDOW)

    assert [o.activity_id for o in outcomes] == ["1", "2"]
    assert health.calls == 1, "the listing only; no TCX was downloaded"
    assert strava.calls == 0
    assert [e.status for e in logs.recorded] == [Status.UPLOADED, Status.UPLOADED]
    assert logs.recorded[0].reason == "already there"


def test_mark_done_leaves_an_existing_decision_alone() -> None:
    listing = json_response(
        {
            "dataPoints": [
                {
                    "name": "users/me/dataTypes/exercise/dataPoints/1",
                    "exercise": {
                        "interval": {"startTime": "2026-02-15T00:00:00Z"},
                        "exerciseType": "WALKING",
                        "displayName": "Walk",
                    },
                }
            ]
        }
    )
    logs = FakeLogStore(LogEntry("1", Status.WITHHELD, reason="malformed"))
    (outcome,) = pipeline(FakeTransport(listing), FakeTransport(), logs).mark_done(
        reason="already there", **WINDOW
    )
    assert (outcome.status, outcome.fresh, outcome.reason) == (
        Status.WITHHELD,
        False,
        "malformed",
    )
    assert logs.recorded == []


def test_a_marked_activity_is_then_skipped_by_sync() -> None:
    """Proves the seeding actually stops the re-upload."""
    point = {
        "name": "users/me/dataTypes/exercise/dataPoints/1",
        "exercise": {
            "interval": {"startTime": "2026-02-15T00:00:00Z"},
            "exerciseType": "WALKING",
            "displayName": "Walk",
        },
    }
    logs = FakeLogStore()
    pipeline(
        FakeTransport(json_response({"dataPoints": [point]})), FakeTransport(), logs
    ).mark_done(reason="already there", **WINDOW)
    strava = FakeTransport()
    outcomes = pipeline(FakeTransport(json_response({"dataPoints": [point]})), strava, logs).sync(
        **WINDOW
    )
    assert [o.fresh for o in outcomes] == [False]
    assert strava.calls == 0


# --- putting back the heart rate the API's export leaves out ----------------


def hr_page(*samples: tuple[str, int]) -> Any:
    return json_response(
        {"dataPoints": [{"sampleTime": t, "beatsPerMinute": bpm} for t, bpm in samples]}
    )


def timed_exercise() -> Exercise:
    """An activity whose window actually contains the builder's trackpoints."""
    return Exercise(
        name="users/me/dataTypes/exercise/dataPoints/889672",
        exercise_type="WALKING",
        display_name="Morning Walk",
        start_time="2024-01-01T08:59:00Z",
        end_time="2024-01-01T09:01:00Z",
        distance_m=930.0,
    )


def timed_tcx() -> bytes:
    """A track with no heart rate, as the API delivers one."""
    return builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=930.0, with_heart_rate=False)


def heart_rates(data: bytes) -> list[int | None]:
    from reckon.core import heartrate as hr

    root = tcx.parse(data)
    out: list[int | None] = []
    for a in tcx.activities(root):
        for p in tcx.trackpoints(a):
            e = p.find(hr.HEART_RATE_BPM)
            out.append(None if e is None else int(e.find(hr.VALUE).text))
    return out


def test_heart_rate_is_fetched_and_merged_before_upload() -> None:
    health = FakeTransport(
        response(body=timed_tcx()),
        hr_page(
            ("2024-01-01T09:00:00Z", 100),
            ("2024-01-01T09:00:10Z", 110),
            ("2024-01-01T09:00:20Z", 120),
        ),
    )
    strava = FakeTransport(upload_response(activity_id=1))
    outcome = pipeline(health, strava, merge_heart_rate=True).process(timed_exercise())
    assert outcome.warnings == ()
    assert b"HeartRateBpm" in strava.requests[0].body


def test_the_merged_values_reach_strava() -> None:
    health = FakeTransport(response(body=timed_tcx()), hr_page(("2024-01-01T09:00:10Z", 137)))
    strava = FakeTransport(upload_response(activity_id=1))
    pipeline(health, strava, merge_heart_rate=True).process(timed_exercise())
    body = strava.requests[0].body
    start = body.index(b"<?xml")
    assert heart_rates(body[start : body.index(b"\r\n--", start)]) == [137, 137, 137]


def test_no_samples_means_no_change_and_no_warning() -> None:
    """A weights session may genuinely have none in the window."""
    health = FakeTransport(response(body=timed_tcx()), hr_page())
    strava = FakeTransport(upload_response(activity_id=1))
    outcome = pipeline(health, strava, merge_heart_rate=True).process(timed_exercise())
    assert outcome.warnings == ()
    assert b"HeartRateBpm" not in strava.requests[0].body


def test_a_failed_heart_rate_fetch_warns_and_still_uploads() -> None:
    """Enrichment must never cost the activity. Arriving without it beats not arriving."""
    # A missing scope is a 403, so the client refreshes once and tries again —
    # two heart-rate calls, both refused, before it gives up.
    denied = AuthError(403, "GET", "u", b"scope missing")
    health = FakeTransport(response(body=timed_tcx()), denied, denied)
    strava = FakeTransport(upload_response(activity_id=1))
    tokens = FakeTransport(
        json_response({"access_token": "t", "refresh_token": "r", "expires_in": 3600})
    )
    outcome = pipeline(health, strava, merge_heart_rate=True, token_transport=tokens).process(
        timed_exercise()
    )
    assert outcome.status is Status.UPLOADED
    assert any(
        "health_metrics_and_measurements scope is not granted" in w for w in outcome.warnings
    )
    assert all(len(w) < 200 for w in outcome.warnings), "said once per activity, so keep it short"


def test_samples_that_match_nothing_are_reported() -> None:
    health = FakeTransport(response(body=timed_tcx()), hr_page(("2024-01-01T09:00:55Z", 150)))
    strava = FakeTransport(upload_response(activity_id=1))
    outcome = pipeline(health, strava, merge_heart_rate=True).process(timed_exercise())
    assert any("none within" in w for w in outcome.warnings)
    assert b"HeartRateBpm" not in strava.requests[0].body


def test_merging_can_be_turned_off() -> None:
    health = FakeTransport(response(body=timed_tcx()))
    strava = FakeTransport(upload_response(activity_id=1))
    pipeline(health, strava, merge_heart_rate=False).process(timed_exercise())
    assert health.calls == 1, "no heart-rate call at all"


def with_average(bpm: int | None) -> Exercise:
    from dataclasses import replace as _replace

    return _replace(timed_exercise(), average_heart_rate=bpm)


def test_the_summary_average_is_written_when_the_file_lacks_one() -> None:
    """The only heart rate an account without the restricted scope will get."""
    from reckon.core import heartrate as hr

    health = FakeTransport(response(body=timed_tcx()), hr_page())
    strava = FakeTransport(upload_response(activity_id=1))
    outcome = pipeline(health, strava, merge_heart_rate=True).process(with_average(146))
    assert outcome.warnings == ()
    body = strava.requests[0].body
    start = body.index(b"<?xml")
    uploaded = body[start : body.index(b"\r\n--", start)]
    lap = next(tcx.activities(tcx.parse(uploaded))).find(tcx.LAP)
    assert int(lap.find(hr.AVERAGE_HEART_RATE_BPM).find(hr.VALUE).text) == 146


def test_the_average_is_written_even_when_the_series_is_refused() -> None:
    """The fallback exists precisely for the account that cannot read the series."""
    denied = AuthError(403, "GET", "u", b"scope missing")
    health = FakeTransport(response(body=timed_tcx()), denied, denied)
    strava = FakeTransport(upload_response(activity_id=1))
    tokens = FakeTransport(
        json_response({"access_token": "t", "refresh_token": "r", "expires_in": 3600})
    )
    outcome = pipeline(health, strava, merge_heart_rate=True, token_transport=tokens).process(
        with_average(146)
    )
    assert outcome.status is Status.UPLOADED
    assert b"AverageHeartRateBpm" in strava.requests[0].body
    assert any("scope is not granted" in w for w in outcome.warnings)


def test_an_activity_with_no_summary_average_is_left_alone() -> None:
    health = FakeTransport(response(body=timed_tcx()), hr_page())
    strava = FakeTransport(upload_response(activity_id=1))
    pipeline(health, strava, merge_heart_rate=True).process(with_average(None))
    assert b"AverageHeartRateBpm" not in strava.requests[0].body


def test_a_non_scope_auth_failure_is_reported_as_itself() -> None:
    """Only the expected, recurring one gets the friendly wording."""
    revoked = AuthError(401, "GET", "u", b"invalid credentials")
    health = FakeTransport(response(body=timed_tcx()), revoked, revoked)
    tokens = FakeTransport(
        json_response({"access_token": "t", "refresh_token": "r", "expires_in": 3600})
    )
    outcome = pipeline(
        health,
        FakeTransport(upload_response(activity_id=1)),
        merge_heart_rate=True,
        token_transport=tokens,
    ).process(with_average(146))
    assert any("invalid credentials" in w for w in outcome.warnings)
    assert not any("scope is not granted" in w for w in outcome.warnings)


def test_a_non_auth_failure_is_reported_plainly() -> None:
    health = FakeTransport(response(body=timed_tcx()), NetworkError("connection reset"))
    outcome = pipeline(
        health, FakeTransport(upload_response(activity_id=1)), merge_heart_rate=True
    ).process(with_average(146))
    assert any("series not merged" in w and "reset" in w for w in outcome.warnings)
