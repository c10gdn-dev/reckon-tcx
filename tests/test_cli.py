"""The command line surface: argument parsing, exit codes and where output goes.

`main` takes its streams as arguments precisely so these tests need no capsys
juggling and no patching of `sys.stdout`.
"""

import argparse
import io

import pytest

import builders
from reckon import __version__
from reckon.cli import main, parse_distance


class BinaryStream(io.BytesIO):
    """A text stream standing in front of a binary one, as `sys.stdout` is."""

    @property
    def buffer(self) -> "BinaryStream":
        return self


def run(*argv: str, stdout=None, stderr=None, transport=None):
    """Invoke the CLI, returning (exit code, stdout bytes, stderr text)."""
    out = io.BytesIO() if stdout is None else stdout
    err = io.StringIO() if stderr is None else stderr
    code = main(list(argv), stdout=out, stderr=err, transport=transport)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def tcx_file(tmp_path):
    path = tmp_path / "run.tcx"
    path.write_bytes(builders.tcx(distances=(0.0, 500.0, 1000.0)))
    return path


@pytest.fixture
def self_describing(tmp_path):
    """A file carrying its own Lap/DistanceMeters, as every real export does."""
    path = tmp_path / "real.tcx"
    path.write_bytes(builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0))
    return path


# --- parse_distance ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10200", 10200.0),
        ("10.2km", 10200.0),
        ("10.2KM", 10200.0),
        ("1mi", 1609.344),
        ("6.3Mi", 6.3 * 1609.344),
        ("900.5", 900.5),
    ],
)
def test_parse_distance_accepts_metres_kilometres_and_miles(text, expected):
    assert parse_distance(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "banana", "10km ", "-5", "10 km", "km", "1e3", "10.2m"])
def test_parse_distance_rejects_anything_else(text):
    with pytest.raises(argparse.ArgumentTypeError, match="expected a distance"):
        parse_distance(text)


@pytest.mark.parametrize("text", ["0", "0km", "0.0mi"])
def test_parse_distance_rejects_zero(text):
    with pytest.raises(argparse.ArgumentTypeError, match="greater than zero"):
        parse_distance(text)


# --- rescale -----------------------------------------------------------------


def test_writes_the_rescaled_file_to_stdout_by_default(tcx_file):
    code, out, err = run("rescale", str(tcx_file), "--distance", "900")

    assert code == 0
    assert b"<DistanceMeters>900</DistanceMeters>" in out
    assert "factor 0.900000" in err
    assert "3 trackpoints" in err
    assert "gps 1000.0 m" in err
    assert "target 900.0 m" in err
    assert "result 900.0 m" in err


def test_writes_to_a_file_when_output_is_given(tcx_file, tmp_path):
    destination = tmp_path / "fixed.tcx"

    code, out, _ = run("rescale", str(tcx_file), "--distance", "900", "-o", str(destination))

    assert code == 0
    assert out == b""
    assert b"<DistanceMeters>900</DistanceMeters>" in destination.read_bytes()


def test_writes_through_the_buffer_of_a_text_stream(tcx_file):
    stdout = BinaryStream()

    code, out, _ = run("rescale", str(tcx_file), "--distance", "900", stdout=stdout)

    assert code == 0
    assert b"<DistanceMeters>900</DistanceMeters>" in out


def test_distance_accepts_kilometres_on_the_command_line(tcx_file):
    code, _, err = run("rescale", str(tcx_file), "--distance", "0.9km")

    assert code == 0
    assert "target 900.0 m" in err


def test_warnings_are_reported_on_stderr(tmp_path):
    path = tmp_path / "indoor.tcx"
    path.write_bytes(builders.tcx(with_position=False))

    code, out, err = run("rescale", str(path), "--distance", "900")

    assert code == 0
    assert "warning: activity" in err
    assert "no GPS positions" in err
    assert "3 trackpoints, nothing to rescale" in err
    assert out == path.read_bytes()


def test_a_file_with_no_gps_needs_no_distance_and_passes_through(tmp_path):
    """The yoga case: uncorrectable, but it must still come out the other side."""
    path = tmp_path / "yoga.tcx"
    path.write_bytes(builders.tcx(with_position=False, distances=(None,) * 5))

    code, out, err = run("rescale", str(path))

    assert code == 0
    assert out == path.read_bytes()
    assert "5 trackpoints, nothing to rescale" in err


def test_unreadable_input_exits_one_without_a_traceback(tmp_path):
    missing = tmp_path / "nope.tcx"

    code, out, err = run("rescale", str(missing), "--distance", "900")

    assert code == 1
    assert out == b""
    assert "cannot read" in err
    assert "No such file" in err


def test_tolerance_breach_exits_one_with_the_guidance_message(tcx_file):
    code, out, err = run("rescale", str(tcx_file), "--distance", "100")

    assert code == 1
    assert out == b""
    assert "outside tolerance" in err
    assert "--on-tolerance clamp|proceed" in err


def test_malformed_input_exits_one(tmp_path):
    path = tmp_path / "bad.tcx"
    path.write_bytes(b"<NotATCX/>")

    code, _, err = run("rescale", str(path), "--distance", "900")

    assert code == 1
    assert "root element is" in err


def test_on_tolerance_clamp_is_accepted_from_the_command_line(tcx_file):
    code, _, err = run("rescale", str(tcx_file), "--distance", "500", "--on-tolerance", "clamp")

    assert code == 0
    assert "factor 0.600000" in err


def test_explicit_tolerance_widens_the_guard(tcx_file):
    code, _, err = run("rescale", str(tcx_file), "--distance", "500", "--tolerance", "0.6")

    assert code == 0
    assert "factor 0.500000" in err


# --- parser surface ----------------------------------------------------------


def test_version_is_reported(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["--version"])

    assert caught.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_a_subcommand_is_required(capsys):
    with pytest.raises(SystemExit) as caught:
        main([])

    assert caught.value.code == 2
    assert "required" in capsys.readouterr().err


def test_distance_is_optional_and_read_from_the_file(self_describing):
    code, out, err = run("rescale", str(self_describing))

    assert code == 0
    assert b"<DistanceMeters>900</DistanceMeters>" in out
    assert "target 900.0 m (from file)" in err


def test_explicit_distance_is_reported_as_given(self_describing):
    code, _, err = run("rescale", str(self_describing), "--distance", "800")

    assert code == 0
    assert "target 800.0 m (given)" in err


def test_missing_target_exits_one_with_guidance(tcx_file):
    code, out, err = run("rescale", str(tcx_file))

    assert code == 1
    assert out == b""
    assert "pass an explicit distance" in err


def test_main_defaults_to_the_real_streams(tcx_file, capsysbinary):
    assert main(["rescale", str(tcx_file), "--distance", "900"]) == 0

    captured = capsysbinary.readouterr()
    assert b"<DistanceMeters>900</DistanceMeters>" in captured.out
    assert b"factor 0.900000" in captured.err


# --- analyse -----------------------------------------------------------------


@pytest.fixture
def corpus(tmp_path):
    """Two correctable files and one indoor one, as a miniature training-data/."""
    directory = tmp_path / "corpus"
    directory.mkdir()
    (directory / "a.tcx").write_bytes(
        builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)
    )
    (directory / "b.tcx").write_bytes(
        builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=800.0)
    )
    (directory / "c.tcx").write_bytes(builders.tcx(with_position=False))
    return directory


def analyse(*argv: str):
    """Invoke `analyse`, whose stdout is text rather than the rescaled bytes."""
    out, err = io.StringIO(), io.StringIO()
    code = main(["analyse", *argv], stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_analyse_reports_every_file_and_a_summary(corpus):
    code, report, _ = analyse("--corpus", str(corpus))

    assert code == 0
    for name in ("a", "b", "c"):
        assert name in report
    assert "2 of 3 corrected" in report
    assert "0.8000-0.9000" in report
    assert "mean 0.8500" in report
    assert "no_gps" in report


def test_analyse_reports_a_single_file_without_a_stdev(tmp_path):
    directory = tmp_path / "one"
    directory.mkdir()
    (directory / "a.tcx").write_bytes(
        builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=900.0)
    )

    code, report, _ = analyse("--corpus", str(directory))

    assert code == 0
    assert "stdev" not in report


def test_analyse_writes_a_plot_when_asked(corpus, tmp_path):
    destination = tmp_path / "out" / "factors.svg"

    code, report, _ = analyse("--corpus", str(corpus), "--plot", str(destination))

    assert code == 0
    assert destination.read_bytes().startswith(b"<?xml")
    assert str(destination) in report


def test_analyse_plot_has_a_default_destination(corpus, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    code, _, _ = analyse("--corpus", str(corpus), "--plot")

    assert code == 0
    assert (tmp_path / "docs" / "factor-distribution.svg").is_file()


def test_analyse_writes_no_plot_by_default(corpus, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    analyse("--corpus", str(corpus))

    assert not (tmp_path / "docs").exists()


def test_analyse_refuses_to_plot_when_nothing_was_corrected(tmp_path):
    directory = tmp_path / "indoor"
    directory.mkdir()
    (directory / "a.tcx").write_bytes(builders.tcx(with_position=False))

    code, _, err = analyse("--corpus", str(directory), "--plot", str(tmp_path / "p.svg"))

    assert code == 1
    assert "nothing to plot" in err


def test_analyse_on_an_empty_directory_exits_one(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    code, _, err = analyse("--corpus", str(empty))

    assert code == 1
    assert "no .tcx files" in err


def test_analyse_on_a_missing_directory_exits_one(tmp_path):
    code, _, err = analyse("--corpus", str(tmp_path / "nope"))

    assert code == 1
    assert "no .tcx files" in err


def test_analyse_reports_a_malformed_file_by_name(tmp_path):
    directory = tmp_path / "bad"
    directory.mkdir()
    (directory / "broken.tcx").write_bytes(b"<NotATCX/>")

    code, _, err = analyse("--corpus", str(directory))

    assert code == 1
    assert "broken.tcx" in err


# --- fetch and sync ---------------------------------------------------------
#
# These need both services authorised, so each test seeds a real `FileStore` in
# a temporary directory and injects a `FakeTransport`. `main` takes the transport
# for the same reason it takes its streams: so nothing has to be patched.

CORRECTABLE = builders.tcx(distances=(0.0, 500.0, 1000.0), lap_distance_m=930.0)
NO_GPS = builders.tcx(distances=(None, None, None), with_position=False, lap_distance_m=0.0)


def json_body(payload):
    import json

    return json.dumps(payload).encode()


def upload_body(**overrides):
    payload = {"id": 987, "external_id": "1", "error": None, "status": "processing"}
    payload.update(overrides)
    return json_body(payload)


def ago(days):
    """A timestamp `days` before now, so it lands inside sync's default window."""
    import datetime as dt

    moment = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def listing(*ids, exercise_type="WALKING", days_ago=1):
    """A page of activities. Newest first, as the live API returns them."""
    return json_body(
        {
            "dataPoints": [
                {
                    "name": f"users/me/dataTypes/exercise/dataPoints/{i}",
                    "exercise": {
                        "interval": {"startTime": ago(days_ago)},
                        "exerciseType": exercise_type,
                        "displayName": "Morning Walk",
                    },
                }
                for i in ids
            ]
        }
    )


def dated_listing(*pairs):
    """Activities at explicit ages, for exercising the window boundaries."""
    return json_body(
        {
            "dataPoints": [
                {
                    "name": f"users/me/dataTypes/exercise/dataPoints/{i}",
                    "exercise": {
                        "interval": {"startTime": ago(d)},
                        "exerciseType": "WALKING",
                        "displayName": "Walk",
                    },
                }
                for i, d in pairs
            ]
        }
    )


@pytest.fixture
def authorised(tmp_path, monkeypatch):
    """A store with both services authorised, and the credentials in the environment."""
    from reckon.clients.oauth import Tokens
    from reckon.stores.file import FileStore

    monkeypatch.setenv("RECKON_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("RECKON_GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.setenv("RECKON_STRAVA_CLIENT_ID", "sid")
    monkeypatch.setenv("RECKON_STRAVA_CLIENT_SECRET", "ssecret")

    path = tmp_path / "store.json"
    store = FileStore(path)
    live = Tokens("live-access", "refresh", 4_000_000_000.0)
    store.save("google", live, expected_version=0)
    store.save("strava", live, expected_version=0)
    return path


def sync(*argv: str, transport=None):
    """Invoke `sync`, whose stdout is a report rather than the rescaled bytes."""
    out, err = io.StringIO(), io.StringIO()
    code = main(["sync", *argv], stdout=out, stderr=err, transport=transport)
    return code, out.getvalue(), err.getvalue()


def transport_of(*bodies):
    from fakes import FakeTransport, response

    return FakeTransport(*[response(body=b) for b in bodies])


def test_fetch_writes_the_corrected_file_to_stdout(authorised):
    code, out, _ = run(
        "fetch", "889672", "--store", str(authorised), transport=transport_of(CORRECTABLE)
    )
    assert code == 0
    assert b"930" in out


def test_fetch_raw_writes_googles_bytes_untouched(authorised):
    code, out, _ = run(
        "fetch",
        "889672",
        "--raw",
        "--store",
        str(authorised),
        transport=transport_of(CORRECTABLE),
    )
    assert (code, out) == (0, CORRECTABLE)


def test_fetch_can_write_to_a_file(authorised, tmp_path):
    target = tmp_path / "out.tcx"
    code, _, err = run(
        "fetch",
        "889672",
        "-o",
        str(target),
        "--store",
        str(authorised),
        transport=transport_of(CORRECTABLE),
    )
    assert code == 0
    assert b"930" in target.read_bytes()
    assert str(target) in err


def test_fetch_without_authorisation_explains_the_fix(tmp_path, monkeypatch):
    monkeypatch.setenv("RECKON_GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setenv("RECKON_GOOGLE_CLIENT_SECRET", "gsecret")
    code, _, err = run(
        "fetch", "1", "--store", str(tmp_path / "empty.json"), transport=transport_of()
    )
    assert code == 1
    assert "authorize.py google" in err


@pytest.mark.parametrize(
    "unset",
    [
        "RECKON_GOOGLE_CLIENT_ID",
        "RECKON_GOOGLE_CLIENT_SECRET",
        "RECKON_STRAVA_CLIENT_ID",
        "RECKON_STRAVA_CLIENT_SECRET",
    ],
)
def test_a_missing_credential_is_named(authorised, monkeypatch, unset):
    monkeypatch.delenv(unset)
    code, _, err = run("fetch", "1", "--store", str(authorised), transport=transport_of())
    assert code == 1
    assert unset in err


def test_both_missing_credentials_are_named_at_once(authorised, monkeypatch):
    monkeypatch.delenv("RECKON_GOOGLE_CLIENT_ID")
    monkeypatch.delenv("RECKON_GOOGLE_CLIENT_SECRET")
    code, _, err = run("fetch", "1", "--store", str(authorised), transport=transport_of())
    assert code == 1
    assert "CLIENT_ID and RECKON_GOOGLE_CLIENT_SECRET are not set" in err


def test_an_expired_google_authorisation_says_how_to_fix_it(tmp_path, monkeypatch):
    """The one OAuth failure a human can act on, so the message has to name the act.

    Expected rather than exceptional until the OAuth client is published: a
    Testing-status client's refresh tokens expire after seven days.
    """
    import json as _json

    from fakes import FakeTransport
    from reckon.clients.http import HTTPError
    from reckon.clients.oauth import Tokens
    from reckon.stores.file import FileStore

    for name in ("GOOGLE", "STRAVA"):
        monkeypatch.setenv(f"RECKON_{name}_CLIENT_ID", "id")
        monkeypatch.setenv(f"RECKON_{name}_CLIENT_SECRET", "secret")

    path = tmp_path / "store.json"
    FileStore(path).save("google", Tokens("dead", "dead", 0.0), expected_version=0)
    FileStore(path).save("strava", Tokens("live", "r", 4_000_000_000.0), expected_version=0)

    body = _json.dumps(
        {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
    ).encode()
    transport = FakeTransport(HTTPError(400, "POST", "https://oauth2.googleapis.com/token", body))

    code, _, err = sync("--store", str(path), transport=transport)
    assert code == 1
    assert "google authorisation is no longer valid" in err
    assert "authorize.py google" in err


def test_sync_reports_one_line_per_activity(authorised):
    transport = transport_of(
        listing("1", "2"),
        CORRECTABLE,
        upload_body(activity_id=11),
        CORRECTABLE,
        upload_body(activity_id=22),
    )
    code, report, _ = sync("--store", str(authorised), transport=transport)
    assert code == 0
    assert "889672" not in report
    assert "2 uploaded" in report


def test_since_excludes_anything_older(authorised):
    """The window is applied by the client, so assert on what comes through."""
    import datetime as dt

    cutoff = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=5)).strftime("%Y-%m-%d")
    transport = transport_of(
        dated_listing(("recent", 2), ("ancient", 40)), CORRECTABLE, upload_body(activity_id=1)
    )
    code, report, _ = sync("--since", cutoff, "--store", str(authorised), transport=transport)
    assert code == 0
    assert "recent" in report
    assert "ancient" not in report


def test_a_full_timestamp_with_an_offset_is_accepted(authorised):
    from reckon.cli import _timestamp

    assert _timestamp("2026-08-01T09:30:00+01:00") == "2026-08-01T08:30:00Z"
    assert _timestamp("2026-08-01") == "2026-08-01T00:00:00Z"

    transport = transport_of(listing())
    code, _, _ = sync(
        "--since", "2026-08-01T09:30:00+01:00", "--store", str(authorised), transport=transport
    )
    assert code == 0


def test_an_unparseable_date_is_rejected_with_the_accepted_forms():
    with pytest.raises(SystemExit):
        sync("--since", "last tuesday")


def test_sync_defaults_to_the_last_week(authorised):
    transport = transport_of(
        dated_listing(("this-week", 3), ("last-month", 30)), CORRECTABLE, upload_body(activity_id=1)
    )
    code, report, _ = sync("--store", str(authorised), transport=transport)
    assert code == 0
    assert "this-week" in report
    assert "last-month" not in report


def test_sync_over_an_empty_window_says_so(authorised):
    code, report, _ = sync("--store", str(authorised), transport=transport_of(listing()))
    assert code == 0
    assert "nothing recorded" in report


def test_a_withheld_activity_makes_the_exit_code_nonzero(authorised):
    """It is not on Strava, and a human has to look."""
    transport = transport_of(listing("1"), b"<not-tcx/>")
    code, report, _ = sync("--store", str(authorised), transport=transport)
    assert code == 1
    assert "withheld" in report


def test_a_passed_through_activity_is_a_success(authorised):
    transport = transport_of(listing("1", exercise_type="YOGA"), NO_GPS, upload_body(activity_id=9))
    code, report, _ = sync("--store", str(authorised), transport=transport)
    assert code == 0
    assert "passed_through" in report
    assert "no_gps" in report


def test_a_dry_run_says_so_and_records_nothing(authorised):
    from reckon.stores.file import FileStore

    transport = transport_of(listing("1"), CORRECTABLE)
    code, _, err = sync("--dry-run", "--store", str(authorised), transport=transport)
    assert code == 0
    assert "dry run" in err
    assert FileStore(authorised).entries() == []


def test_a_second_sync_replays_the_stored_decision(authorised):
    first = transport_of(listing("1"), CORRECTABLE, upload_body(activity_id=11))
    sync("--store", str(authorised), transport=first)

    second = transport_of(listing("1"))
    code, report, _ = sync("--store", str(authorised), transport=second)
    assert code == 0
    assert "already done" in report
    assert second.calls == 1, "the TCX was not fetched again"


def test_a_transient_fault_fails_loudly_rather_than_silently(authorised):
    """Locally there is no retry (`PLAN.md` §2) — say what happened and exit 1.

    The pipeline itself lets transient faults propagate, which is what the SQS
    worker needs; the CLI is the boundary that turns one into a message.
    """
    from fakes import FakeTransport
    from reckon.core.errors import NetworkError

    code, _, err = sync("--store", str(authorised), transport=FakeTransport(NetworkError("reset")))
    assert code == 1
    assert "reset" in err
