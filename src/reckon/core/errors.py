"""Exception hierarchy for the pure layer.

Every error raised by `core` is a `ReckonError`, so callers — the CLI now, the
pipeline in phase 5 — have exactly one thing to catch at the boundary.
"""


class ReckonError(Exception):
    """Base class for every deterministic failure Reckon raises."""


class MalformedTCX(ReckonError):
    """The input is not a TCX document we can read."""


class MissingTarget(ReckonError):
    """No target distance was given and the file carries none of its own.

    Raised only when the caller asked Reckon to take the target from the file.
    An explicit target is never second-guessed.
    """


class ToleranceExceeded(ReckonError):
    """The rescale factor is further from 1 than the caller allowed.

    A large discrepancy is more likely a bad Fitbit summary than a bad track, so
    the default is to refuse rather than to apply a correction nobody asked for.
    """

    def __init__(
        self, factor: float, gps_total_m: float, target_m: float, tolerance: float
    ) -> None:
        self.factor = factor
        self.gps_total_m = gps_total_m
        self.target_m = target_m
        self.tolerance = tolerance
        super().__init__(
            f"factor {factor:.4f} is outside tolerance {tolerance:.4f} "
            f"(GPS total {gps_total_m:.1f} m, target {target_m:.1f} m); "
            f"check the target distance, or pass --on-tolerance clamp|proceed"
        )
