"""Hand-emitted SVG. A histogram is a few dozen rectangles.

Pulling in a plotting library for this would cost the zero-dependency property
that lets Terraform zip `src/` with no build step, in exchange for drawing shapes
this project already knows how to draw. `xml.etree` writes it, the same module
that reads the TCX.
"""

import xml.etree.ElementTree as ET

_WIDTH = 720
_HEIGHT = 360
_MARGIN_LEFT = 56
_MARGIN_RIGHT = 20
_MARGIN_TOP = 48
_MARGIN_BOTTOM = 56

# Deliberately readable on both a white and a dark background: mid-tone fills
# with an explicit light background rect behind them.
_BACKGROUND = "#fbfbfd"
_AXIS = "#5b6169"
_BAR = "#4a7fb5"
_BAR_EDGE = "#2f5c8a"
_TEXT = "#22262b"
_MUTED = "#6b7178"


def histogram(
    values: list[float],
    *,
    bins: int = 12,
    title: str = "",
    x_label: str = "",
) -> bytes:
    """Render `values` as a histogram, returned as SVG bytes.

    Raises `ValueError` on an empty input rather than emitting an empty chart —
    a plot of nothing is a bug in the caller, not a picture.
    """
    if not values:
        raise ValueError("cannot plot an empty set of values")
    if bins < 1:
        raise ValueError(f"bins must be at least 1, got {bins}")

    low, high = min(values), max(values)
    if high == low:
        # A single distinct value would give a zero-width axis; give it one bin's
        # worth of room either side so the bar has somewhere to stand.
        low, high = low - 0.5, high + 0.5
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        index = min(int((value - low) / width), bins - 1)
        counts[index] += 1
    tallest = max(counts)

    plot_w = _WIDTH - _MARGIN_LEFT - _MARGIN_RIGHT
    plot_h = _HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM
    root = ET.Element(
        "svg",
        xmlns="http://www.w3.org/2000/svg",
        viewBox=f"0 0 {_WIDTH} {_HEIGHT}",
        width=str(_WIDTH),
        height=str(_HEIGHT),
        role="img",
    )
    ET.SubElement(root, "title").text = title or "Histogram"
    ET.SubElement(
        root, "rect", x="0", y="0", width=str(_WIDTH), height=str(_HEIGHT), fill=_BACKGROUND
    )

    if title:
        _text(root, _MARGIN_LEFT, 28, title, size=16, weight="600")

    # Horizontal gridlines, one per count, so bar heights can be read off.
    for count in range(tallest + 1):
        y = _MARGIN_TOP + plot_h - (count / tallest) * plot_h
        ET.SubElement(
            root,
            "line",
            x1=str(_MARGIN_LEFT),
            y1=f"{y:.1f}",
            x2=str(_MARGIN_LEFT + plot_w),
            y2=f"{y:.1f}",
            stroke="#e3e6ea" if count else _AXIS,
            **{"stroke-width": "1"},
        )
        _text(root, _MARGIN_LEFT - 10, y + 4, str(count), size=11, fill=_MUTED, anchor="end")

    bar_w = plot_w / bins
    for index, count in enumerate(counts):
        if not count:
            continue
        height = (count / tallest) * plot_h
        ET.SubElement(
            root,
            "rect",
            x=f"{_MARGIN_LEFT + index * bar_w + 1:.1f}",
            y=f"{_MARGIN_TOP + plot_h - height:.1f}",
            width=f"{bar_w - 2:.1f}",
            height=f"{height:.1f}",
            fill=_BAR,
            stroke=_BAR_EDGE,
            **{"stroke-width": "1"},
        )

    # X axis ticks at the ends and the middle: more would crowd at this width.
    for fraction in (0.0, 0.5, 1.0):
        x = _MARGIN_LEFT + fraction * plot_w
        value = low + fraction * (high - low)
        _text(root, x, _MARGIN_TOP + plot_h + 20, f"{value:.3f}", size=11, fill=_MUTED)
    if x_label:
        _text(root, _MARGIN_LEFT + plot_w / 2, _HEIGHT - 14, x_label, size=12, fill=_TEXT)

    return ET.tostring(root, encoding="UTF-8", xml_declaration=True)


def _text(
    parent: ET.Element,
    x: float,
    y: float,
    content: str,
    *,
    size: int = 12,
    fill: str = _TEXT,
    weight: str = "400",
    anchor: str = "middle",
) -> None:
    element = ET.SubElement(
        parent,
        "text",
        x=f"{x:.1f}",
        y=f"{y:.1f}",
        fill=fill,
        **{
            "font-family": "system-ui, -apple-system, Segoe UI, Helvetica, Arial, sans-serif",
            "font-size": str(size),
            "font-weight": weight,
            "text-anchor": anchor,
        },
    )
    element.text = content
