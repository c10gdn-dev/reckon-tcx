"""The hand-emitted histogram.

Asserted as parsed XML rather than as a string: the point of writing SVG by hand
is that it is real XML, so the tests should hold it to that.
"""

import xml.etree.ElementTree as ET

import pytest

from reckon.core.svg import histogram

SVG_NS = "{http://www.w3.org/2000/svg}"


def render(values, **kwargs) -> ET.Element:
    return ET.fromstring(histogram(values, **kwargs))


def bars(root: ET.Element) -> list[ET.Element]:
    # The first rect is the background; the rest are bars.
    return root.findall(f"{SVG_NS}rect")[1:]


def test_emits_well_formed_svg():
    root = render([0.9, 0.95, 1.0])

    assert root.tag == f"{SVG_NS}svg"
    assert root.get("viewBox") == "0 0 720 360"


def test_every_value_lands_in_a_bar():
    values = [0.7, 0.8, 0.9, 0.95, 0.99]

    root = render(values, bins=5)

    assert 1 <= len(bars(root)) <= 5


def test_the_largest_value_is_included_rather_than_falling_off_the_end():
    """The top of the range is inclusive, so the maximum must be counted."""
    root = render([0.0, 1.0], bins=2)

    assert len(bars(root)) == 2


def test_bars_are_taller_where_more_values_fall():
    root = render([0.1, 0.9, 0.9, 0.9], bins=2)
    heights = sorted(float(b.get("height")) for b in bars(root))

    assert heights[1] > heights[0]


def test_a_title_is_rendered():
    root = render([0.9, 1.0], title="Correction factor")

    texts = [e.text for e in root.iter(f"{SVG_NS}text")]
    assert "Correction factor" in texts
    assert root.find(f"{SVG_NS}title").text == "Correction factor"


def test_an_axis_label_is_rendered():
    root = render([0.9, 1.0], x_label="factor")

    assert "factor" in [e.text for e in root.iter(f"{SVG_NS}text")]


def test_labels_are_omitted_when_not_asked_for():
    root = render([0.9, 1.0])

    assert root.find(f"{SVG_NS}title").text == "Histogram"


def test_identical_values_still_render_a_bar():
    """A zero-width axis would divide by zero; it must not."""
    root = render([0.9, 0.9, 0.9])

    assert len(bars(root)) == 1


def test_a_single_value_still_renders():
    assert len(bars(render([0.9]))) == 1


def test_an_empty_input_is_refused():
    with pytest.raises(ValueError, match="empty set of values"):
        histogram([])


def test_a_nonsensical_bin_count_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        histogram([0.9, 1.0], bins=0)


def test_output_is_bytes_with_an_xml_declaration():
    out = histogram([0.9, 1.0])

    assert out.startswith(b"<?xml")
