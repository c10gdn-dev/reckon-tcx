# Reckon

**Corrects Fitbit's GPS distance inflation before uploading to Strava.**

![Python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue)
![License](https://img.shields.io/badge/license-MIT-blue)

<!-- CI and coverage badges go here once the repository has a remote. They are
     deliberately omitted rather than pointed at a guessed URL. -->

## The problem

A wrist GPS records a noisy track. Every jitter in that noise *adds* length and
none subtracts, so summing the raw distance stream overstates how far you
actually went. Fitbit's own display avoids this by fusing GPS with stride
cadence — but the TCX export still carries the inflated stream, and Strava
believes it. The result is that the same run reads differently in two apps.

Real activities from a Fitbit Charge 5:

| Activity | Google Health shows | Strava shows | Inflation | Reckon produces |
|----------|--------------------:|-------------:|----------:|----------------:|
| run, 116 min | 21.46 km | 24.06 km | +12.1% | 21.46 km |
| run, 83 min | 15.23 km | 16.14 km | +6.0% | 15.23 km |
| run, 57 min | 8.18 km | 8.94 km | +9.3% | 8.18 km |
| walk, 24 min | 2.40 km | 2.54 km | +6.1% | 2.40 km |
| walk, 53 min | 5.26 km | 5.41 km | +3.0% | 5.26 km |
| walk, 67 min | 6.47 km | 6.54 km | +1.1% | 6.47 km |
| cycle, 30 min | 9.57 km | 9.76 km | +2.1% | 9.57 km |
| cycle, 31 min | 9.32 km | 9.37 km | +0.6% | 9.32 km |

Reckon rescales the distance stream so the total matches the stride-fused
figure, leaving the GPS geometry and every timestamp untouched.

## Quickstart

No credentials, no config, no network:

```console
$ uv tool install reckon-tcx
$ reckon rescale run.tcx -o fixed.tcx
reckon: 4747 trackpoints  gps 16148.9 m  target 15229.1 m (from file)  result 15229.1 m  factor 0.943045
```

Upload `fixed.tcx` to Strava by hand and the distance will match your watch.

> **Disable the native Fitbit→Strava connection first**, or the uncorrected
> version syncs itself and you get a duplicate.

## How it works

Fitbit and Strava disagree because they compute distance differently, and one of
them is summing noise.

**Strava sums the distance stream in the file, unchanged.** Verified across
eleven exports, and then tested directly: a rescaled file uploaded by hand came
back reporting the rescaled total, 21.4 km, where the original stream said
24.06 km and a raw haversine sum of the same coordinates said 24.08 km. Strava
takes the stream at face value and does not recompute from position.

It is not that Strava never post-processes. On the same upload it reported 179 m
of climb from an altitude stream whose raw deltas sum to 2279 m — it smooths
elevation by a factor of thirteen. It simply does not do that to distance, which
is what makes this correction possible.

**Fitbit's own total is lower, and it is not a raw GPS sum.** Fitbit writes it to
`Lap/DistanceMeters`, it matches what Google Health displays to within 0.06%, and
across the corpus it runs 0.6–12% below the stream.

How large the gap gets depends on what happened during the activity rather than
on how far you went. In testing, two minutes spent standing still talking to
someone added **119 m** of distance to a track that had not moved — about 78 m
per minute of pure noise. That single stop produced 22% of the whole run's
over-measurement while occupying 2% of its time.

The gap looks like high-frequency GPS noise. Sample a track at full resolution
and again at one fix per five seconds: real movement is smooth at that scale, so
the two lengths should agree, and the excess is jitter. That excess tracks the
Fitbit/Strava gap closely — a bike ride showed 2.5% excess against 2.1%
disagreement; the cleanest run 3.5% against 2.8%.

So Reckon takes **the total from Fitbit and the geometry from GPS**: compute
`factor = target / gps_total`, multiply every distance and distance-derived speed
value by it, and copy coordinates, altitudes and timestamps through unchanged.

> **On the name.** Reckon was named for dead reckoning, on the assumption that
> Fitbit's advantage came from fusing GPS with stride cadence. A bicycle ride
> disproved that — there are no strides on a bike, yet Fitbit's total still came
> in below the GPS stream, on both rides tested. Whatever Fitbit does, it is not
> primarily stride-based. The correction works regardless; it never depended on knowing why
> Fitbit's number is better.

## Honest limits

- **It corrects a systematic bias; it does not recover ground truth.** The
  output is as good as the device's own total and no better. If that total is
  wrong, Reckon faithfully reproduces it. Two watches walked the same route side
  by side in testing and reported 1249 m and 974 m — 28% apart, with nothing to
  say which was right. Treat the result as much better than raw GPS, not as
  correct.
- **Splits all shift proportionally.** Every kilometre gets the same factor, so
  the *shape* of your pace curve is preserved exactly, but Reckon cannot tell
  which specific kilometre carried the error.
- **The first few seconds are missing from the file, and nothing can recover
  them.** A watch takes 15–80 seconds to get a fix, and if you set off
  immediately, that ground is never recorded. Across testing this cost 34–152 m,
  or 0.3–6.1% of the activity. The corrected *total* is still right, because the
  device's own figure counts those metres — but they get spread across the part
  of the route that was recorded, so splits stretch very slightly. This is
  already true of the raw file; Reckon neither causes it nor repairs it.
- **Route, timestamps and dates are unchanged.** Strava's *elapsed* time is
  unaffected. Its *moving* time shifts by a few seconds — 24 s on a real upload
  where the distance changed by 10.8% — because Strava derives it from speed, and
  speed is distance over time.
- **Elevation is not corrected.** See below; this is deliberate.
- **The factor is not a constant.** Across fourteen activities it ranged 0.72–0.99
  and tracked neither distance, duration nor pace. It depends on how noisy that
  particular track was. Reckon computes it per file and refuses to guess.
- **A partial GPS track cannot be corrected, and Reckon detects that and
  declines.** If the watch lost its lock for part of the activity, the stream
  covers less ground than you actually travelled, and scaling it up would
  attribute the missing distance to the part of the route that *was* recorded.
  Reckon checks two things: how much of the elapsed time carried a fix, and
  whether the activity's own total exceeds what GPS measured — which cannot
  happen from noise alone, since jitter only ever adds length. Either one refuses
  the correction. The file is still written out, unchanged.
- **Indoor activities are passed through**, not corrected and not dropped. With
  no GPS there is no inflation to remove, so the file is written out unchanged.
- **Reckon does not touch activity type.** It edits distances and speeds only;
  whatever decides whether Strava calls something a run, a ride or yoga is
  outside this tool and is left alone.

## Elevation is left alone, on purpose

The altitude stream in a Fitbit export is at least as noisy as the distance
stream. On one 21 km run its raw deltas sum to **2279 m** of climb, which is not
a plausible number for the route.

Reckon does not correct it, and that is a deliberate scope decision rather than
an omission:

- **There is no target to correct it against.** The distance fix works because
  the file already carries a trustworthy total in `Lap/DistanceMeters`. Nothing
  equivalent exists for elevation — Google Health does not report it at all — so
  there is no reference figure to rescale to. Correcting a number with nothing to
  check it against would be inventing one.
- **Strava already handles it, and handles it well.** On that same 21 km run
  Strava reported **179 m**, which is reasonable for the route. It smooths the
  altitude stream by roughly a factor of thirteen. That is a job it already does
  properly, with better data than this tool has.

So Reckon copies every `AltitudeMeters` value through byte-identically and leaves
the interpretation to Strava. The one visible consequence is that Strava's
elevation figure moves very slightly after a correction — 179 m to 177 m on that
upload. Reckon did not change the altitudes; Strava recomputed. Its smoothing
appears to operate over distance-based windows, so a shorter distance stream
nudges the result. The effect is about 1%, in a figure that was always an
estimate.

**This asymmetry is the whole opportunity.** Strava is perfectly willing to
post-process a stream it judges noisy — it does exactly that to elevation. It
simply does not do it to distance. That gap is where this tool lives.

## Usage

```
reckon rescale INPUT [--distance DIST] [-o OUTPUT]
               [--tolerance FLOAT] [--on-tolerance {abort,clamp,proceed}]
```

| Argument | Meaning |
|----------|---------|
| `INPUT` | TCX file to read. |
| `--distance DIST` | Override the target. `15.23km`, `9.46mi`, or a bare number meaning metres. Defaults to the file's own `Lap/DistanceMeters`. |
| `-o`, `--output` | Write here instead of stdout. |
| `--tolerance` | How far *below* 1 the factor may fall before the guard fires. Default `0.4`. The bound is asymmetric — see below. |
| `--on-tolerance` | `abort` (default), `clamp` to the tolerance bound, or `proceed` anyway. |

The report line goes to stderr and the file to stdout, so
`reckon rescale in.tcx > out.tcx` works and stays readable.

**Every activity comes out the other side.** Anything Reckon cannot correct is
written through byte-identically rather than dropped, so an indoor session still
reaches Strava — just with the numbers it came with. Running Reckon twice is a
no-op: the second pass computes a factor of exactly 1.

**Guards.** Reckon leaves an activity alone, with a warning naming the reason,
rather than fabricating data:

| Situation | What happens |
|-----------|--------------|
| No `Position` elements — an indoor activity | Passed through unchanged. There is no GPS inflation to remove. |
| No `DistanceMeters` in the stream, or a zero total | Passed through unchanged. Reckon will not invent a stream. |
| Partial GPS — the watch lost its lock for part of the activity | Passed through unchanged. Scaling would attribute the missing distance to the part of the route that *was* recorded. |
| A non-monotonic stream | Warns and proceeds; multiplication preserves ordering. |
| A factor further below 1 than `--tolerance` | Aborts by default. The stream over-measured by more than jitter can explain, so the target is probably wrong. |
| A factor above 1 | The stream measured *short*, which jitter cannot cause. Treated as partial GPS when the target came from the file, or as a bad `--distance` when you supplied one. |

The bound is deliberately **asymmetric**. GPS noise only ever adds length, so a
factor below 1 is the normal case and can legitimately be large — one real walk
in testing measured 0.723, a 38% over-read. A factor above 1 means something
quite different and gets handled separately.

## Status

Alpha, and honest about it: the offline `rescale` command works and is
validated against real exports. Automatic fetching from Fitbit, uploading to
Strava, and the AWS deployment are planned but not built. See `PLAN.md`.

## Alternatives

If you want a click-and-forget bridge rather than a correction tool: FitToStrava,
SyncMyTracks, Health Sync, RunGap. Note that tapiriik has no Fitbit connector.
None of these correct the inflation — that is the whole reason this exists.

## Contributing

`make check` runs what CI runs: `ruff` plus the suite at a 100% line-and-branch
coverage gate. The gate is not negotiable and was set on the first commit rather
than retrofitted.

## Licence

MIT. See [LICENSE](LICENSE).
