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
screen** in Google Health and in Strava, read off by hand. (Google retired the
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

Across the current corpus the second is 6–11% larger than the first, and it
tracks the raw haversine sum of the coordinates to within 0.1%. So the target
total the transform rescales *to* is already present in the file — no activity
summary fetch is needed to compute the factor. `reference.json` records both
totals so that assumption stays testable against what Google Health displays.

Real exports also carry no `Extensions` at all — no `TPX/Speed`, no `LX/AvgSpeed`.
Those tags stay in `_SCALED_TAGS` for correctness, but they never fire on Fitbit
data.

## What consumes it

- `tests/test_corpus.py` — parametrised over whatever is present, skipped when
  empty so CI and forkers stay green.
- `reckon analyse` — factor mean, stdev and range across the corpus, plus
  start-time lag and moving-time delta.

Both arrive in phase 3. Drop files in whenever you have them.

## Promoting a file to a test fixture

`scripts/anonymise.py` (phase 3) shifts all coordinates by a constant offset,
rebases timestamps to an epoch, and strips device serial and user ID. That is how
`tests/fixtures/` gets populated with something committable.
