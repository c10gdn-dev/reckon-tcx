"""Synthetic TCX generation.

Branch coverage of the guards in `rescale` needs inputs that mostly do not occur
in real data — a missing distance stream, a zero total, an indoor activity, a
stream that goes backwards. Hand-writing a dozen XML files for that is
unmaintainable, so each guard gets a keyword argument here and a one-line case in
the tests.

The anonymised files in `tests/fixtures/` serve the opposite purpose: proving the
parser copes with what Fitbit actually emits. They arrive in phase 3.
"""

import datetime as dt
from collections.abc import Sequence

TCX_NS = "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"
AX_NS = "http://www.garmin.com/xmlschemas/ActivityExtension/v2"

EPOCH = dt.datetime(2024, 1, 1, 9, 0, 0, tzinfo=dt.UTC)
START_LAT = 51.5074
START_LON = -0.1278


def timestamp(offset_seconds: int) -> str:
    """A TCX timestamp: UTC with a Z suffix, as Fitbit writes them."""
    moment = EPOCH + dt.timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def trackpoint(
    *,
    offset_seconds: int,
    distance_m: float | None,
    with_position: bool = True,
    speed: float | None = None,
) -> str:
    parts = [f"<Time>{timestamp(offset_seconds)}</Time>"]
    if with_position:
        parts.append(
            "<Position>"
            f"<LatitudeDegrees>{START_LAT + offset_seconds * 1e-5}</LatitudeDegrees>"
            f"<LongitudeDegrees>{START_LON + offset_seconds * 1e-5}</LongitudeDegrees>"
            "</Position>"
        )
    parts.append("<AltitudeMeters>12.0</AltitudeMeters>")
    if distance_m is not None:
        parts.append(f"<DistanceMeters>{distance_m}</DistanceMeters>")
    parts.append("<HeartRateBpm><Value>148</Value></HeartRateBpm>")
    if speed is not None:
        parts.append(
            f'<Extensions><ns2:TPX xmlns:ns2="{AX_NS}">'
            f"<ns2:Speed>{speed}</ns2:Speed>"
            "<ns2:RunCadence>84</ns2:RunCadence>"
            "</ns2:TPX></Extensions>"
        )
    return "<Trackpoint>" + "".join(parts) + "</Trackpoint>"


def lap(
    *,
    start_offset: int = 0,
    trackpoints: Sequence[str] = (),
    total_time_seconds: float = 600.0,
    distance_m: float | None = None,
    max_speed: float | None = None,
    avg_speed: float | None = None,
) -> str:
    parts = [
        f"<TotalTimeSeconds>{total_time_seconds}</TotalTimeSeconds>",
    ]
    if distance_m is not None:
        parts.append(f"<DistanceMeters>{distance_m}</DistanceMeters>")
    if max_speed is not None:
        parts.append(f"<MaximumSpeed>{max_speed}</MaximumSpeed>")
    parts.append("<Calories>512</Calories>")
    parts.append("<Intensity>Active</Intensity>")
    parts.append("<TriggerMethod>Manual</TriggerMethod>")
    parts.append("<Track>" + "".join(trackpoints) + "</Track>")
    if avg_speed is not None:
        parts.append(
            f'<Extensions><ns2:LX xmlns:ns2="{AX_NS}">'
            f"<ns2:AvgSpeed>{avg_speed}</ns2:AvgSpeed>"
            "</ns2:LX></Extensions>"
        )
    return f'<Lap StartTime="{timestamp(start_offset)}">' + "".join(parts) + "</Lap>"


def activity(
    *,
    distances: Sequence[float | None] = (0.0, 500.0, 1000.0),
    with_position: bool = True,
    speeds: Sequence[float | None] | None = None,
    sport: str = "Running",
    activity_id: str | None = None,
    start_offset: int = 0,
    laps: int = 1,
    lap_distance_m: float | None = None,
    max_speed: float | None = None,
    avg_speed: float | None = None,
    include_id: bool = True,
) -> str:
    """One `<Activity>` element, with `distances` split evenly across `laps` laps."""
    if speeds is None:
        speeds = [None] * len(distances)
    points = [
        trackpoint(
            offset_seconds=start_offset + index * 10,
            distance_m=distance,
            with_position=with_position,
            speed=speed,
        )
        for index, (distance, speed) in enumerate(zip(distances, speeds, strict=True))
    ]

    per_lap = -(-len(points) // laps) if points else 0
    chunks = [points[i : i + per_lap] for i in range(0, len(points), per_lap)] if per_lap else [[]]
    chunks += [[]] * (laps - len(chunks))

    body = "".join(
        lap(
            start_offset=start_offset + index * 600,
            trackpoints=chunk,
            distance_m=lap_distance_m,
            max_speed=max_speed,
            avg_speed=avg_speed,
        )
        for index, chunk in enumerate(chunks)
    )
    header = ""
    if include_id:
        header = f"<Id>{activity_id or timestamp(start_offset)}</Id>"
    return f'<Activity Sport="{sport}">' + header + body + "</Activity>"


def document(*activities: str) -> bytes:
    """Wrap activity elements in a complete TCX document."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<TrainingCenterDatabase xmlns="{TCX_NS}" xmlns:ns2="{AX_NS}">'
        "<Activities>" + "".join(activities) + "</Activities>"
        "<Author><Name>Fitbit</Name></Author>"
        "</TrainingCenterDatabase>"
    ).encode()


def tcx(**kwargs: object) -> bytes:
    """The common case: a document holding a single activity."""
    return document(activity(**kwargs))  # type: ignore[arg-type]
