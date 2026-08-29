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


def run(*argv: str, stdout=None, stderr=None):
    """Invoke the CLI, returning (exit code, stdout bytes, stderr text)."""
    out = io.BytesIO() if stdout is None else stdout
    err = io.StringIO() if stderr is None else stderr
    code = main(list(argv), stdout=out, stderr=err)
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
