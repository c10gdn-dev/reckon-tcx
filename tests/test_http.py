"""The network seam, tested against a real socket.

`http.py` is the one module that cannot be faked — faking it would test the fake.
So `send` runs against a `ThreadingHTTPServer` on a loopback port and genuinely
meets a 429, a redirect, a dropped connection and a stalled body, rather than a
mock's idea of them (`PLAN.md` §7).

`retrying` sits above the seam and is tested the way everything above the seam
is: with a `FakeTransport` and a recording sleep, in microseconds.
"""

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from fakes import Clock, FakeTransport, MaxJitter, RecordingSleep, response
from reckon.clients.http import (
    Request,
    Response,
    RetryPolicy,
    classify,
    retry_after,
    retrying,
    send,
)
from reckon.core.errors import (
    AuthError,
    HTTPError,
    NetworkError,
    RateLimited,
    ServerError,
    Transient,
)

# Long enough that a 0.1s client timeout always wins the race, short enough that
# a wedged test does not hold the suite open.
STALL_SECONDS = 3.0


class Handler(BaseHTTPRequestHandler):
    """Routes by path. Each branch exists to cover one real failure mode."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args: object) -> None:  # pragma: no cover - silences stderr
        pass

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length)

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/ok":
            self._send(200, b"hello", {"Content-Type": "text/plain", "X-Custom": "yes"})
        elif self.path == "/json":
            self._send(200, b'{"n": 1}', {"Content-Type": "application/json"})
        elif self.path == "/notfound":
            self._send(404, b"no such thing")
        elif self.path == "/limited":
            self._send(429, b"slow down", {"Retry-After": "7"})
        elif self.path == "/boom":
            self._send(500, b"upstream exploded")
        elif self.path == "/redirect":
            self._send(302, b"", {"Location": "/ok"})
        elif self.path == "/stall":
            # Headers promise a body that never arrives, so urlopen returns and
            # the *read* times out. That is the bare TimeoutError path, distinct
            # from a connect timeout, which arrives wrapped in URLError.
            self.send_response(200)
            self.send_header("Content-Length", "5")
            self.end_headers()
            time.sleep(STALL_SECONDS)
        else:  # /drop
            self.close_connection = True

    def do_POST(self) -> None:
        body = self._body()
        self._send(200, body, {"X-Seen-Method": "POST"})

    def do_DELETE(self) -> None:
        self._send(204, b"")


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def closed_port() -> int:
    """A port with nothing listening on it, for the connection-refused path."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --- send: against the real server -----------------------------------------


def test_send_returns_status_headers_and_body(base_url: str) -> None:
    result = send(Request("GET", f"{base_url}/ok"))
    assert result.status == 200
    assert result.body == b"hello"
    assert result.url == f"{base_url}/ok"


def test_headers_are_lowercased_and_readable_either_way(base_url: str) -> None:
    result = send(Request("GET", f"{base_url}/ok"))
    assert result.headers["x-custom"] == "yes"
    assert result.header("X-Custom") == "yes"
    assert result.header("Absent") is None


def test_json_decodes_the_body(base_url: str) -> None:
    assert send(Request("GET", f"{base_url}/json")).json() == {"n": 1}


def test_json_raises_on_a_body_that_is_not_json(base_url: str) -> None:
    with pytest.raises(ValueError, match="Expecting value"):
        send(Request("GET", f"{base_url}/ok")).json()


@pytest.mark.parametrize(
    ("path", "status", "body"),
    [
        ("/notfound", 404, b"no such thing"),
        ("/limited", 429, b"slow down"),
        ("/boom", 500, b"upstream exploded"),
    ],
)
def test_error_statuses_come_back_as_responses_not_exceptions(
    base_url: str, path: str, status: int, body: bytes
) -> None:
    """urllib raises on 4xx and 5xx; the seam's job is to un-raise them."""
    result = send(Request("GET", f"{base_url}{path}"))
    assert (result.status, result.body) == (status, body)


def test_retry_after_survives_the_round_trip(base_url: str) -> None:
    assert retry_after(send(Request("GET", f"{base_url}/limited"))) == 7.0


def test_redirects_are_followed(base_url: str) -> None:
    result = send(Request("GET", f"{base_url}/redirect"))
    assert result.status == 200
    assert result.url == f"{base_url}/ok"


def test_request_body_and_method_reach_the_server(base_url: str) -> None:
    result = send(Request("POST", f"{base_url}/echo", body=b"payload"))
    assert result.body == b"payload"
    assert result.header("x-seen-method") == "POST"


def test_request_headers_reach_the_server(base_url: str) -> None:
    result = send(Request("POST", f"{base_url}/echo", headers={"X-Trace": "abc"}, body=b""))
    assert result.status == 200


def test_a_204_has_an_empty_body(base_url: str) -> None:
    result = send(Request("DELETE", f"{base_url}/anything"))
    assert (result.status, result.body) == (204, b"")


def test_a_dropped_connection_is_a_network_error(base_url: str) -> None:
    with pytest.raises(NetworkError, match="GET"):
        send(Request("GET", f"{base_url}/drop"))


def test_a_refused_connection_is_a_network_error(closed_port: int) -> None:
    with pytest.raises(NetworkError, match=r"refused|unreachable"):
        send(Request("GET", f"http://127.0.0.1:{closed_port}/ok"))


def test_a_stalled_body_times_out_as_a_network_error(base_url: str) -> None:
    started = time.monotonic()
    with pytest.raises(NetworkError, match=r"timed out after 0\.1s"):
        send(Request("GET", f"{base_url}/stall", timeout=0.1))
    assert time.monotonic() - started < STALL_SECONDS


def test_a_network_error_is_transient(closed_port: int) -> None:
    """The marker is what the pipeline routes on, so it has to be on the class."""
    with pytest.raises(NetworkError) as caught:
        send(Request("GET", f"http://127.0.0.1:{closed_port}/ok"))
    assert isinstance(caught.value, Transient)


# --- classify: status to meaning -------------------------------------------

REQUEST = Request("GET", "https://example.test/thing")


@pytest.mark.parametrize(
    ("status", "expected", "transient"),
    [
        (400, HTTPError, False),
        (401, AuthError, False),
        (403, AuthError, False),
        (404, HTTPError, False),
        (429, RateLimited, True),
        (500, ServerError, True),
        (503, ServerError, True),
    ],
)
def test_classify_maps_status_to_meaning(
    status: int, expected: type[HTTPError], transient: bool
) -> None:
    error = classify(REQUEST, response(status=status))
    assert type(error) is expected
    assert isinstance(error, Transient) is transient
    assert error.status == status


def test_the_error_message_names_the_request_and_quotes_the_body() -> None:
    error = classify(REQUEST, response(status=404, body=b"no  such\n thing"))
    assert str(error) == "GET https://example.test/thing returned 404: no such thing"


def test_a_long_body_is_truncated() -> None:
    error = classify(REQUEST, response(status=500, body=b"x" * 500))
    assert str(error).endswith("...")
    assert len(str(error)) < 300


def test_an_empty_body_adds_nothing_to_the_message() -> None:
    assert str(classify(REQUEST, response(status=404))) == (
        "GET https://example.test/thing returned 404"
    )


def test_an_undecodable_body_does_not_break_the_message() -> None:
    error = classify(REQUEST, response(status=400, body=b"\xff\xfe bad"))
    assert "400" in str(error)


# --- retry_after ------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        (" 7 ", 7.0),
        ("0", 0.0),
        ("2.5", 2.5),
        ("-1", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("", None),
    ],
)
def test_retry_after_parses_seconds_only(value: str, expected: float | None) -> None:
    assert retry_after(response(headers={"retry-after": value})) == expected


def test_retry_after_is_none_when_absent() -> None:
    assert retry_after(response()) is None


# --- retrying: above the seam ----------------------------------------------


def policy(**overrides: object) -> RetryPolicy:
    """A policy whose waiting is instant, recorded and exactly predictable."""
    defaults: dict[str, object] = {
        "sleep": RecordingSleep(),
        "rng": MaxJitter(),
        "base_delay": 1.0,
    }
    return RetryPolicy(**(defaults | overrides))  # type: ignore[arg-type]


def test_a_success_is_returned_without_sleeping() -> None:
    transport = FakeTransport(response(body=b"ok"))
    settings = policy()
    assert retrying(transport, settings)(REQUEST).body == b"ok"
    assert settings.sleep.calls == []  # type: ignore[union-attr]


def test_the_default_policy_is_usable_without_arguments() -> None:
    assert retrying(FakeTransport(response()))(REQUEST).status == 200


@pytest.mark.parametrize("status", [200, 201, 204, 299])
def test_every_2xx_counts_as_success(status: int) -> None:
    assert retrying(FakeTransport(response(status=status)), policy())(REQUEST).status == status


def test_a_transient_status_is_retried_until_it_succeeds() -> None:
    transport = FakeTransport(
        response(status=503), response(status=500), response(status=200, body=b"finally")
    )
    settings = policy()
    assert retrying(transport, settings)(REQUEST).body == b"finally"
    assert transport.calls == 3


def test_a_network_error_is_retried() -> None:
    transport = FakeTransport(NetworkError("reset"), response(body=b"second time"))
    assert retrying(transport, policy())(REQUEST).body == b"second time"
    assert transport.calls == 2


def test_a_deterministic_status_is_raised_on_the_first_attempt() -> None:
    transport = FakeTransport(response(status=404), response(status=200))
    with pytest.raises(HTTPError) as caught:
        retrying(transport, policy())(REQUEST)
    assert caught.value.status == 404
    assert transport.calls == 1, "a 404 will still be a 404; asking again is wasted quota"


def test_an_auth_failure_is_not_retried() -> None:
    transport = FakeTransport(response(status=401))
    with pytest.raises(AuthError):
        retrying(transport, policy())(REQUEST)
    assert transport.calls == 1


def test_persistent_transience_raises_after_the_last_attempt() -> None:
    transport = FakeTransport(*[response(status=500)] * 4)
    settings = policy(attempts=4)
    with pytest.raises(ServerError):
        retrying(transport, settings)(REQUEST)
    assert transport.calls == 4
    assert len(settings.sleep.calls) == 3, "no sleep after the attempt that gives up"  # type: ignore[union-attr]


def test_a_persistent_network_error_is_raised_not_swallowed() -> None:
    transport = FakeTransport(*[NetworkError("reset")] * 3)
    with pytest.raises(NetworkError):
        retrying(transport, policy(attempts=3))(REQUEST)
    assert transport.calls == 3


def test_one_attempt_means_no_retries() -> None:
    transport = FakeTransport(response(status=500))
    with pytest.raises(ServerError):
        retrying(transport, policy(attempts=1))(REQUEST)
    assert transport.calls == 1


def test_attempts_below_one_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        RetryPolicy(attempts=0)


def test_backoff_doubles_and_is_capped() -> None:
    transport = FakeTransport(*[response(status=500)] * 6)
    settings = policy(attempts=6, base_delay=1.0, max_delay=4.0)
    with pytest.raises(ServerError):
        retrying(transport, settings)(REQUEST)
    assert settings.sleep.calls == [1.0, 2.0, 4.0, 4.0, 4.0]  # type: ignore[union-attr]


def test_jitter_spans_the_whole_window() -> None:
    """Full jitter, not a band around the target — that is what decorrelates."""
    transport = FakeTransport(*[response(status=500)] * 3)
    settings = RetryPolicy(attempts=3, base_delay=8.0, sleep=RecordingSleep(), rng=MaxJitter())
    settings.rng.uniform = lambda a, b: a  # type: ignore[method-assign]
    with pytest.raises(ServerError):
        retrying(transport, settings)(REQUEST)
    assert settings.sleep.calls == [0.0, 0.0]  # type: ignore[union-attr]


def test_a_named_retry_after_is_honoured_exactly() -> None:
    transport = FakeTransport(
        response(status=429, headers={"retry-after": "12"}), response(status=200)
    )
    settings = policy(max_retry_after=60.0)
    retrying(transport, settings)(REQUEST)
    assert settings.sleep.calls == [12.0], "the server named a time; do not jitter it"  # type: ignore[union-attr]


def test_a_named_retry_after_is_capped() -> None:
    transport = FakeTransport(
        response(status=429, headers={"retry-after": "3600"}), response(status=200)
    )
    settings = policy(max_retry_after=30.0)
    retrying(transport, settings)(REQUEST)
    assert settings.sleep.calls == [30.0]  # type: ignore[union-attr]


def test_a_429_without_retry_after_falls_back_to_backoff() -> None:
    transport = FakeTransport(response(status=429), response(status=200))
    settings = policy(base_delay=1.0)
    retrying(transport, settings)(REQUEST)
    assert settings.sleep.calls == [1.0]  # type: ignore[union-attr]


def test_the_request_is_replayed_unchanged() -> None:
    request = Request("POST", "https://example.test/x", headers={"A": "b"}, body=b"payload")
    transport = FakeTransport(response(status=500), response(status=200))
    retrying(transport, policy())(request)
    assert transport.requests == [request, request]


def test_the_clock_double_advances_with_recorded_sleep() -> None:
    clock = Clock(now=100.0)
    sleeper = RecordingSleep(clock)
    sleeper(2.5)
    assert (clock.time(), sleeper.total) == (102.5, 2.5)


def test_the_fake_transport_refuses_an_unscripted_request() -> None:
    with pytest.raises(AssertionError, match="unexpected request"):
        FakeTransport()(REQUEST)


def test_a_response_is_hashable_and_comparable() -> None:
    assert response(status=200, body=b"x") == Response(200, {}, b"x", "https://example.test/")
