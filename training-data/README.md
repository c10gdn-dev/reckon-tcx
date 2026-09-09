# training-data/

Your own raw Fitbit exports, used to calibrate and validate the transform.

**Gitignored, and it must stay that way.** A raw TCX is a precise record of where
you live and when you leave the house. The files are also large.

## Layout

The raw exports keep whatever filename Fitbit gave them (the log ID):

```
<logid>.tcx
```

Alongside them, one shared manifest:

```
reference.json
```

`reference.json` holds, per run, the distance and duration **as displayed on
screen** in Google Health and in Strava, read off by hand. Files that reached
Strava only through Reckon have no uncorrected Strava reading to record, and
their `strava` block is null with a note saying why — a derived stand-in would
be a fabrication in the one file that exists to be ground truth. (Google retired the
standalone Fitbit app, so Google Health is the only first-party surface; the
device's own stride-fused total comes from the TCX itself, not from an app
screen.) That is the ground truth the rescale factor and its tolerance band
are calibrated against. It is gitignored along with everything else here: it carries no
coordinates, but it still says when you were out and for how long.

Each run has two blocks:

- `derived_from_tcx` — computed from the file itself. Regenerated, never
  hand-edited.
- `reported` — filled in by hand. `null` means not yet supplied.

Record the unit the app actually showed (`"km"` or `"mi"`) rather than
converting. A silent unit assumption is the one error this corpus cannot
survive.

## Note on Fitbit's two totals

A real Fitbit TCX carries *two* different distances, and they do not agree:

- `Lap/DistanceMeters` — the stride-fused total.
- the final `Trackpoint/DistanceMeters` — the cumulative GPS stream.

Across the corpus files that carry a usable stream, the second runs from 0.6% to
38.3% larger than the first, and it tracks the raw haversine sum of the
coordinates to within 0.1%. Two files measure *short* instead, and they are the
two partial-GPS cases: one by 24.8%, where the watch lost its lock mid-route, and
one by 9.1%, where it recorded nothing at all for the first six minutes. So the target total the transform rescales *to* is already present
in the file — no activity summary fetch is needed to compute the factor.
`reference.json` records both totals so that assumption stays testable against
what Google Health displays.

Real exports also carry no `Extensions` at all — no `TPX/Speed`, no `LX/AvgSpeed`.
Those tags stay in `_SCALED_TAGS` for correctness, but they never fire on Fitbit
data.

## What consumes it

- `tests/test_corpus.py` — parametrised over whatever is present, skipped when
  empty so CI and forkers stay green.
- `reckon analyse` — factor mean, stdev and range across the corpus, plus
  GPS coverage, recording gaps, wiggle, start-time lag and moving-time delta.

Drop files in whenever you have them; both pick them up automatically.

## Promoting a file to a test fixture

`scripts/anonymise.py` shifts all coordinates by a constant offset,
rebases timestamps to an epoch, and strips device serial and user ID. That is how
`tests/fixtures/` gets populated with something committable.
