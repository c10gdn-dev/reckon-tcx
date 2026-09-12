"""The Strava client: multipart encoding, upload polling, and idempotency.

The multipart body is hand-rolled (`PLAN.md` §3 forbids `requests`), so it is
parsed back with the stdlib `email` package here rather than compared as a blob.
A test that only asserted on bytes would pass on a body no server could read.
"""

import datetime as dt
import json
import random
from email import message_from_bytes
from email.message import Message
from typing import Any

import pytest

from fakes import Clock, FakeTransport, response
from reckon.clients.oauth import TokenHolder, Tokens
from reckon.clients.strava import (
    AUTHORIZE_EXTRA,
    MAX_PAGES,
    SCOPE_SEPARATOR,
    SCOPES,
    Strava,
    Upload,
    token_holder,
)
from reckon.core.errors import AuthError, ReckonError

BASE = "https://strava.example.test/api/v3"
TCX = b'<?xml version="1.0"?><TrainingCenterDatabase>route</TrainingCenterDatabase>'


def json_response(payload: Any) -> Any:
    return response(body=json.dumps(payload).encode())


def upload_response(**overrides: Any) -> Any:
    payload = {
        "id": 987,
        "id_str": "987",
        "external_id": "889672",
        "error": None,
        "status": "Your activity is still being processed.",
        "activity_id": None,
    }
    payload.update(overrides)
    return json_response(payload)


def live_tokens(
    transport: FakeTransport | None = None, expires_at: float = 10_000.0
) -> TokenHolder:
    return token_holder(
        transport or FakeTransport(),
        client_id="cid",
        client_secret="secret",
        tokens=Tokens("live-access", "r", expires_at),
        now=Clock(now=0.0).time,
    )


def client(transport: FakeTransport, tokens: TokenHolder | None = None) -> Strava:
    return Strava(transport, tokens or live_tokens(), base_url=BASE, rng=random.Random(0))


def send_upload(subject: Strava) -> Upload:
    return subject.upload(TCX, name="Morning Walk", external_id="889672", sport_type="Walk")


def parsed_body(request: Any) -> Message:
    """Reassemble the multipart body the way an HTTP server would."""
    raw = f"Content-Type: {request.headers['Content-Type']}\r\n\r\n".encode() + request.body
    return message_from_bytes(raw)


def parts_of(request: Any) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for part in parsed_body(request).get_payload():
        name = part.get_param("name", header="content-disposition")
        found[str(name)] = part.get_payload(decode=True)
    return found


# --- scopes -----------------------------------------------------------------


def test_the_scopes_are_write_and_read_all_and_nothing_wider() -> None:
    """`activity:read_all`, not `activity:read`: the narrower one cannot see an
    activity set to "Only You", and one reconcile cannot see is one catch-up
    uploads again. Pinned because widening a scope here is invisible until
    someone re-authorises, and narrowing it breaks reconcile silently.
    """
    assert SCOPES == ("activity:write", "activity:read_all")


def test_stravas_non_standard_authorisation_dialect_is_declared() -> None:
    """Commas for scopes, and its own spelling of "ask again"."""
    assert SCOPE_SEPARATOR == ","
    assert AUTHORIZE_EXTRA == {"approval_prompt": "force"}


# --- the multipart body -----------------------------------------------------


def test_the_upload_is_a_readable_multipart_body() -> None:
    transport = FakeTransport(upload_response())
    send_upload(client(transport))
    message = parsed_body(transport.requests[0])
    assert message.is_multipart()
    assert message.get_content_type() == "multipart/form-data"


def test_every_documented_field_is_sent() -> None:
    transport = FakeTransport(upload_response())
    send_upload(client(transport))
    parts = parts_of(transport.requests[0])
    assert parts["data_type"] == b"tcx"
    assert parts["name"] == b"Morning Walk"
    assert parts["sport_type"] == b"Walk"
    assert parts["file"] == TCX


def test_the_external_id_is_the_source_activity_id() -> None:
    """Strava dedupes on it — idempotency independent of Reckon's own store."""
    transport = FakeTransport(upload_response())
    send_upload(client(transport))
    assert parts_of(transport.requests[0])["external_id"] == b"889672"


def test_the_description_defaults_to_empty_and_is_still_sent() -> None:
    transport = FakeTransport(upload_response())
    send_upload(client(transport))
    assert parts_of(transport.requests[0])["description"] == b""


def test_a_description_is_carried_through() -> None:
    transport = FakeTransport(upload_response())
    client(transport).upload(
        TCX, name="n", external_id="1", sport_type="Run", description="Corrected by Reckon"
    )
    assert parts_of(transport.requests[0])["description"] == b"Corrected by Reckon"


def test_the_boundary_in_the_header_matches_the_body() -> None:
    transport = FakeTransport(upload_response())
    send_upload(client(transport))
    request = transport.requests[0]
    boundary = request.headers["Content-Type"].partition("boundary=")[2]
    assert request.body.startswith(f"--{boundary}\r\n".encode())
    assert request.body.endswith(f"--{boundary}--\r\n".encode())


def test_the_boundary_comes_from_the_injected_rng() -> None:
    """Never the module-level functions, so a test can pin the whole request."""
    first = FakeTransport(upload_response())
    second = FakeTransport(upload_response())
    send_upload(Strava(first, live_tokens(), base_url=BASE, rng=random.Random(7)))
    send_upload(Strava(second, live_tokens(), base_url=BASE, rng=random.Random(7)))
    assert first.requests[0].body == second.requests[0].body


def test_a_default_rng_still_produces_a_usable_boundary() -> None:
    transport = FakeTransport(upload_response())
    send_upload(Strava(transport, live_tokens(), base_url=BASE))
    assert "boundary=----reckon" in transport.requests[0].headers["Content-Type"]


def test_the_filename_can_be_set() -> None:
    transport = FakeTransport(upload_response())
    client(transport).upload(
        TCX, name="n", external_id="1", sport_type="Run", filename="corrected.tcx"
    )
    body = transport.requests[0].body
    assert b'filename="corrected.tcx"' in body


def test_a_tcx_containing_the_boundary_text_would_still_be_delimited() -> None:
    """Sixteen random bytes is the only property a boundary needs."""
    transport = FakeTransport(upload_response())
    subject = client(transport)
    send_upload(subject)
    boundary = transport.requests[0].headers["Content-Type"].partition("boundary=")[2]
    assert boundary not in TCX.decode()


# --- the upload lifecycle ---------------------------------------------------


def test_the_post_targets_the_uploads_endpoint() -> None:
    transport = FakeTransport(upload_response())
    send_upload(client(transport))
    assert transport.requests[0].method == "POST"
    assert transport.requests[0].url == f"{BASE}/uploads"
    assert transport.requests[0].headers["Authorization"] == "Bearer live-access"


def test_the_upload_is_returned_unfinished() -> None:
    """The POST returns an upload id, never an activity — processing is asynchronous."""
    result = send_upload(client(FakeTransport(upload_response())))
    assert result == Upload(
        id=987,
        external_id="889672",
        activity_id=None,
        error=None,
        status="Your activity is still being processed.",
    )
    assert result.done is False


def test_polling_reads_the_upload_back() -> None:
    transport = FakeTransport(upload_response(activity_id=555, status="Your activity is ready."))
    result = client(transport).upload_status(987)
    assert transport.requests[0].method == "GET"
    assert transport.requests[0].url == f"{BASE}/uploads/987"
    assert result.activity_id == 555
    assert result.done is True


def test_an_errored_upload_is_done_too() -> None:
    transport = FakeTransport(upload_response(error="Invalid file type"))
    result = client(transport).upload_status(987)
    assert result.done is True
    assert result.duplicate is False


def test_a_duplicate_is_recognised_and_is_not_a_failure() -> None:
    """The activity is on Strava either way; re-uploading would be the wrong move."""
    transport = FakeTransport(upload_response(error="1234.tcx duplicate of activity 987654"))
    result = client(transport).upload_status(987)
    assert result.duplicate is True
    assert result.done is True


def test_duplicate_detection_is_case_insensitive() -> None:
    transport = FakeTransport(upload_response(error="Duplicate of activity 1"))
    assert client(transport).upload_status(987).duplicate is True


def test_no_error_is_not_a_duplicate() -> None:
    assert client(FakeTransport(upload_response())).upload_status(987).duplicate is False


def test_a_response_that_is_not_an_object_is_reported() -> None:
    transport = FakeTransport(json_response(["nope"]))
    with pytest.raises(ReckonError, match="expected an upload object"):
        client(transport).upload_status(987)


def test_missing_fields_default_rather_than_raise() -> None:
    result = client(FakeTransport(json_response({}))).upload_status(1)
    assert result == Upload(id=0, external_id=None, activity_id=None, error=None, status="")


# --- authentication recovery ------------------------------------------------


def test_a_401_is_retried_once_through_a_forced_refresh() -> None:
    token_transport = FakeTransport(
        json_response({"access_token": "second", "refresh_token": "r", "expires_in": 3600})
    )
    api = FakeTransport(AuthError(401, "POST", "u", b""), upload_response())
    subject = Strava(api, live_tokens(token_transport), base_url=BASE, rng=random.Random(0))
    assert send_upload(subject).id == 987
    assert api.requests[0].headers["Authorization"] == "Bearer live-access"
    assert api.requests[1].headers["Authorization"] == "Bearer second"


def test_a_second_401_propagates() -> None:
    token_transport = FakeTransport(
        json_response({"access_token": "second", "refresh_token": "r", "expires_in": 3600})
    )
    api = FakeTransport(AuthError(401, "POST", "u", b""), AuthError(401, "POST", "u", b""))
    subject = Strava(api, live_tokens(token_transport), base_url=BASE, rng=random.Random(0))
    with pytest.raises(AuthError):
        send_upload(subject)
    assert api.calls == 2


def test_an_expired_access_token_is_refreshed_before_the_upload() -> None:
    """Strava access tokens last six hours, so an unattended worker meets this routinely."""
    token_transport = FakeTransport(
        json_response({"access_token": "renewed", "refresh_token": "r", "expires_in": 21600})
    )
    api = FakeTransport(upload_response())
    subject = Strava(
        api, live_tokens(token_transport, expires_at=0.0), base_url=BASE, rng=random.Random(0)
    )
    send_upload(subject)
    assert api.requests[0].headers["Authorization"] == "Bearer renewed"


def test_a_trailing_slash_on_the_base_url_does_not_double_up() -> None:
    transport = FakeTransport(upload_response())
    Strava(transport, live_tokens(), base_url=f"{BASE}/").upload_status(1)
    assert transport.requests[0].url == f"{BASE}/uploads/1"


# --- listing what Strava already has ----------------------------------------


def activities_response(*items: object) -> Any:
    return response(body=json.dumps(list(items)).encode())


def an_activity(**overrides: Any) -> dict[str, Any]:
    payload = {"id": 77, "start_date": "2026-02-23T13:10:00Z", "name": "Walk", "external_id": "1"}
    payload.update(overrides)
    return payload


WINDOW = {"after": "2026-02-23T00:00:00Z", "before": "2026-02-24T00:00:00Z"}


def test_activities_reads_the_fields_reconciling_needs() -> None:
    transport = FakeTransport(activities_response(an_activity()))

    (found,) = list(client(transport).activities(**WINDOW))

    assert found.id == 77
    assert found.name == "Walk"
    assert found.external_id == "1"
    assert found.started_at == dt.datetime(2026, 2, 23, 13, 10, tzinfo=dt.UTC).timestamp()


def test_activities_widens_the_window_because_stravas_bounds_are_exclusive() -> None:
    """Both `after` and `before` exclude their own second.

    Not widening drops an activity starting exactly on the boundary — which, for
    a caller reconciling a day at a time, is a duplicate upload at midnight.
    """
    transport = FakeTransport(activities_response())

    list(client(transport).activities(**WINDOW))

    url = transport.requests[0].url
    low = dt.datetime(2026, 2, 23, tzinfo=dt.UTC).timestamp()
    high = dt.datetime(2026, 2, 24, tzinfo=dt.UTC).timestamp()
    assert f"after={int(low) - 1}" in url
    assert f"before={int(high) + 1}" in url


def test_activities_trims_what_the_widened_window_let_in() -> None:
    """Half-open, matching the inventory port, so adjacent windows tile."""
    transport = FakeTransport(
        activities_response(
            an_activity(id=1, start_date="2026-02-22T23:59:59Z"),
            an_activity(id=2, start_date="2026-02-23T00:00:00Z"),
            an_activity(id=3, start_date="2026-02-24T00:00:00Z"),
        )
    )

    assert [a.id for a in client(transport).activities(**WINDOW)] == [2]


def test_activities_pages_until_a_short_page() -> None:
    full = activities_response(*[an_activity(id=i) for i in range(200)])
    transport = FakeTransport(full, activities_response(an_activity(id=999)))

    found = list(client(transport).activities(**WINDOW))

    assert len(found) == 201
    assert "page=2" in transport.requests[1].url


def test_activities_stops_at_the_page_cap() -> None:
    """A bounded loop rather than a trusting one; the cap is the only guarantee."""
    full = activities_response(*[an_activity(id=i) for i in range(200)])
    transport = FakeTransport(*[full] * (MAX_PAGES + 2))

    list(client(transport).activities(**WINDOW))

    assert transport.calls == MAX_PAGES


def test_activities_rejects_a_payload_that_is_not_a_list() -> None:
    transport = FakeTransport(response(body=b'{"message":"Authorization Error"}'))

    with pytest.raises(ReckonError, match="expected a list of activities"):
        list(client(transport).activities(**WINDOW))


def test_activities_rejects_an_entry_that_is_not_an_object() -> None:
    transport = FakeTransport(activities_response("not an activity"))

    with pytest.raises(ReckonError, match="expected an activity object"):
        list(client(transport).activities(**WINDOW))


def test_activities_rejects_an_entry_with_no_start_date() -> None:
    transport = FakeTransport(activities_response({"id": 77}))

    with pytest.raises(ReckonError, match="missing id or start_date"):
        list(client(transport).activities(**WINDOW))


def test_activities_rejects_a_window_that_is_not_a_timestamp() -> None:
    with pytest.raises(ReckonError, match="not an RFC 3339 timestamp"):
        list(client(FakeTransport()).activities(after="whenever", before="2026-02-24T00:00:00Z"))
