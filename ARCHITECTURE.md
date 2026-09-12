# Architecture

The README answers "should I use this, and how". This answers "how do I change
it without breaking something quiet".

> **A note on `PLAN.md`.** Comments throughout the source cite it by section —
> `PLAN.md` §5, §8 and so on. It is the implementation plan and calibration
> record, and it is deliberately not published: its calibration sections describe
> specific outings in enough detail to say more about a person's movements than a
> specification needs to. Everything those citations carry that a contributor
> needs is restated here. Treat them as provenance, not as a broken link.

## Source map

One line each, saying what the module owns.

| Module | Owns |
|---|---|
| `core/tcx.py` | Parsing and serialising TCX. Namespaces, element lookup, GPS coverage, recording gaps, the device name and the start timestamp. Knows nothing about rescaling. |
| `core/rescale.py` | The transform. Pure: bytes and a target in, bytes and numbers out. |
| `core/analyse.py` | Corpus measurement. Pure. Feeds `reckon analyse`. |
| `core/heartrate.py` | Putting heart rate back into a TCX: a fetched series, or the summary's average onto the lap. |
| `core/errors.py` | The exception hierarchy, and the `Transient` marker. |
| `clients/http.py` | **The only module that touches the network.** `send` performs one request; `retrying` decides what is worth repeating. |
| `clients/oauth.py` | OAuth 2.0: authorisation URLs, code exchange, refresh, and `TokenHolder`. |
| `clients/health.py` | Google Health: listing exercises, downloading TCX. |
| `clients/strava.py` | Strava: multipart upload, polling, duplicate detection. |
| `stores/base.py` | The three persistence ports, as protocols, plus the vocabulary they are defined in — `Status`, `LogEntry`, `MatchKind`, `InventoryEntry` and the chronological key both adapters order on. |
| `stores/file.py` | Local adapter. All three ports in one 0600 JSON file, under `flock`. |
| `pipeline.py` | Activity in → rescale → upload → record. Shared by `sync`, `local` and the Lambda worker; `sync` fetches the bytes, `local` reads them from a file. |
| `stores/dynamo.py` | AWS adapter. Same three ports over one DynamoDB table, plus the index that makes a windowed inventory listing a query rather than a scan; the only place besides `aws/` that may import boto3. |
| `aws/receiver.py` | Webhook endpoint. Authenticates, enqueues, acknowledges. Nothing else. |
| `aws/worker.py` | SQS handler. Routes the two message shapes; re-enqueues delayed rather than sleeping. |
| `aws/queue.py` | The SQS seam, as `http.py` is the network seam. |
| `aws/config.py` | Assembles the pipeline inside Lambda, as `cli.py` does locally. |
| `aws/secrets.py` | Configuration resolution: environment first, then SSM SecureString at run time. |
| `stores/transfer.py` | Copying one store's contents into another. Direction-agnostic, because both satisfy the same ports. |
| `deploy/terraform/` | The deployment. Depends on `src/`, never the reverse. |
| `cli.py` | Argument parsing and configuration for the local CLI, and the only lazy import in the codebase — `--table` pulls in `stores/dynamo.py`, and with it boto3, inside the function rather than at module scope. |

## The invariants

Rules a change must not break, in rough order of how expensively they fail.

**`core/` imports the standard library and `reckon.core`, nothing else.** It is
the part that must keep working in thirty years. *Enforced by
`tests/test_layering.py`.*

**All network access goes through `clients/http.py`.** `urllib.request`,
`http.client`, `socket` and `ssl` may appear there and nowhere else. Everything
above that seam takes a `Transport` and is tested by passing it a list of canned
responses — no mock library, no patching. *Enforced.*

**boto3 appears only in `stores/dynamo.py` and `aws/`.** *Enforced.*

**`pipeline.py` imports the store *protocols*, never an implementation.** This is
what lets one pipeline serve both the CLI and Lambda. It fails invisibly:
importing `stores/file.py` works perfectly right up to the Lambda with no
writable home directory. *Enforced.*

**Timestamps are never modified.** `rescale` scales by tag name across the
activity subtree, so it cannot touch a time-bearing element by construction.
*Convention, plus a test that compares every timestamp before and after.*

**`Lap/DistanceMeters` is never scaled.** It is the *target*, not part of the
stream being corrected. Scaling it destroys the ground truth and makes the
transform non-idempotent — a second pass would shrink the file again. This is why
`DistanceMeters` is absent from `_SCALED_SPEED_TAGS` and handled separately.
*Convention. It has been got wrong once.*

**Clock, sleep and randomness are injected, never read globally.** Otherwise the
retry, backoff and expiry branches are untestable or slow. *Convention.*

**No import-time side effects.** `boto3.client(...)` at module scope needs
credentials and a region, fails in CI, and distorts coverage. Both AWS clients
are therefore built on first use. *Convention, with a test on each.*

**The two stores are interchangeable.** `stores/file.py` and `stores/dynamo.py`
must be indistinguishable to `pipeline.py` — that is what the whole local/AWS
split rests on. *Enforced by `tests/test_store_contract.py`, which runs one set
of behaviours against both.* It is also why `chronological` lives in
`stores/base.py`: both adapters must order a window the same way, and two copies
of that normalisation would drift until they disagreed on a boundary.

**Inventory records never expire; log records do.** The table has TTL enabled on
a `ttl` attribute and DynamoDB never expires an item that lacks it, so the
omission *is* the mechanism. A forgotten inventory record means Reckon no longer
knows an activity exists and uploads it again. *Convention, with a test on the
adapter.*

## Why local and AWS share a pipeline

`PLAN.md` §2 in full. In short: only **storage** and **trigger** differ.

| | Local | AWS |
|---|---|---|
| Trigger | `reckon sync`, `reckon local` | webhook → SQS |
| Stores | one JSON file | DynamoDB |
| Retry | none, fail loudly | SQS + DLQ |

Everything from "here is an activity id" to "Strava has it" is `pipeline.py`,
unchanged. The port/adapter boundary is not decoration: it is the reason the
whole transform could be built and validated before any cloud account existed.

## Distinctions that must not be collapsed

This codebase has repeatedly been bitten by one mechanism standing in for two
meanings. It has happened six times now, and every one of these looks redundant
until you try to merge it back.

The tell is always the same: two things that answer the same question *most* of
the time, and disagree exactly when it matters. Splitting them has never once
been the wrong call here, so when a new one appears, split it.

**`Transient` is a mixin marker, not a `retryable` flag.** "What went wrong" and
"would retrying help" are independent questions. A 429 and a dropped connection
are nothing alike as faults but identical as decisions; a 429 and a 404 are the
reverse. Catch `Transient` to decide about retrying, the concrete class to decide
what to tell the user.

**`gps_coverage`, `recording_gaps` and `unrecorded_time` are three measures, not
one.** They sound like the same question and are not:

| Measure | Asks | What is lost |
|---|---|---|
| `gps_coverage` | did the trackpoints carry a position? | distance — the stream stops advancing |
| `recording_gaps` | were there trackpoints between the first and the last? | shape only — the stream chords across |
| `unrecorded_time` | does the track span the activity at all? | distance — the stream never ran |

Each has a blind spot only the others cover. A real file reported 100% coverage
while 47% of its elapsed time fell between trackpoints. Another reported 100%
coverage, no gap over five seconds, and was still missing its first kilometre —
the track began 365 s after the activity did, and both of the first two measures
are bounded by the track itself, so neither could see it.

Folding any pair together breaks something. Gaps into coverage would have turned
a warning into a refusal. Late starts into gaps would have conflated a loss of
shape with a loss of distance. `_fully_recorded` in `rescale.py` requires all
three, and the refusal message names which one fired, because they mean different
things to whoever reads it.

**`Status.on_strava` is separate from whether a correction happened.** An
activity that reached Strava uncorrected is a *success*: yoga, an indoor walk, a
track whose GPS dropped out. Collapsing "cannot correct" into "do not upload"
would silently drop them, which is the one thing the design forbids. Hence four
outcomes — `uploaded`, `passed_through`, `withheld`, `failed` — plus a raised
exception for transient faults, which is never recorded so that a redelivery is
never mistaken for a decision.

**"Cannot correct" is separate from "cannot identify."** This one decides what
`reckon local` does with a file. Cannot
correct means upload it anyway — that is the rule above. Cannot identify means
*withhold*, and for a reason that has nothing to do with correction: the activity
id is what Strava deduplicates on, so an unidentified file uploaded twice becomes
two activities that nothing can ever reconcile. The file stays on disk and the
next run tries again.

**An inventory record is separate from a log entry** — planned for deployed mode,
`PLAN.md` §13.2. "What exists in Google Health, and is it on Strava" is a fact
about the world; "what Reckon decided about it" is a decision Reckon made. The
codebase has already conflated them once: `mark_done` writes a `LogEntry` with
status `uploaded` and the reason "already on Strava before Reckon", which records
a fact in the shape of a decision. It is expedient and it is wrong, and it is why
a third activity state — *known, never processed* — cannot currently be
expressed. Two record types, not a fifth `Status`.

**`Outcome.archived` is separate from `Outcome.status`.** "Did it reach Strava"
and "is the file still in the way" are different questions. A move that fails
leaves a true status and a false flag, rather than casting doubt on an upload
that already happened.

## Why local mode exists

Google's API export and the Google Health app's export are not the same file. The
API's `:exportExerciseTcx` omits heart rate; the app's has it. That was first
diagnosed as the export lagging the activity and it is not — a 21-day-old
activity re-fetched through the API still came back without it. It is provenance.

Strava computes Relative Effort, and therefore Fitness, from heart rate. The
per-second series is available through the API, behind a Restricted scope that
requires Google's verification and an annual paid security assessment. That was
declined, so the only route to a heart-rate trace is a file exported by hand — and
`reckon local` is the mode that takes one.

**Confirmed on the first real run, 2026-09-05.** Five activities uploaded this
way showed Relative Effort on all five, where the previous day's `sync` upload
carrying only a lap average showed none. Two of the five — a yoga session and a
weights session — have no GPS and were passed through with no correction at all,
and still gained it. Reckon is not only a distance tool.

It is not a second pipeline. `local` supplies bytes where `sync` fetches them,
and the two converge on the same line of `_decide`. The one thing it still needs
the API for is the sport, because `Sport="Other"` in a real export covers a 5 km
walk and a stationary yoga session alike.

## Building a heart-rate trace is not fabricating one

Two operations look alike and one of them is forbidden, so the line is drawn here
rather than left to judgement.

**Forbidden:** synthesising a per-trackpoint trace from an activity's *average*
heart rate. A flat line across a run is not a measurement of anything, and it
would look exactly like data to whoever read it.

**Allowed, and planned for deployed mode** (`PLAN.md` §13.4): emitting one
trackpoint per sample from the `heart-rate` data type, for an activity whose TCX
carries no GPS. Every value is a real per-second reading; nothing is interpolated
and nothing is invented. The output is the shape the phone app writes for the
same activity — the corpus's yoga file is 1867 trackpoints of `Time` and
`HeartRateBpm` with no position and no distance, and Strava computed Relative
Effort from it.

The difference is whether a number in the output was measured. Assembling
measurements into a format is not the same act as deriving many values from one.

## Design decisions, and when to revisit them

**Zero runtime dependencies.** `urllib`, `xml.etree`, `hmac`, `json`. boto3 is in
the Lambda runtime and dev-only locally. This is what lets Terraform's
`archive_file` zip `src/` with no build step — no layers, no container, no
registry. It also rules out `requests`, and it is why the histogram is
hand-emitted XML rather than matplotlib.

*Revisit if* the webhook receiver ever needs to verify the real ECDSA signature
rather than a shared secret. That needs a crypto library, and the trade would
have to be argued explicitly.

**Rescaling, not smoothing or map matching.** Reckon multiplies the distance
stream by one factor. It does not Kalman-filter the track, snap it to roads, or
touch a single coordinate. The reason is epistemic rather than technical: the
device's own total is a *measurement*, and rescaling to it makes one honest
adjustment with a known basis. Smoothing would invent a route that was never
recorded and give no way to check the result.

*Revisit if* per-split accuracy ever matters more than the total. Rescaling
spreads the correction proportionally, so it cannot recover which kilometre
carried the error.

**Coverage is gated at 100% from the first commit.** Not retrofitted, and never
lowered. A phase that cannot reach it is information about the design, not a
reason to relax the threshold. Every `# pragma: no cover` carries a justification;
"hard to reach" is a design smell, not a pragma.

Line coverage cannot tell you a multiplication is correct, which is precisely
what this project is — hence `mutmut` over `core/` as a separate, non-blocking
job.

**Guard cases come from `tests/builders.py`, not hand-written XML.** The branch
coverage of §5's guards needs inputs that mostly do not occur in real data.
`tests/fixtures/*.tcx` do a different job: they are anonymised real exports,
proving the parser copes with what the device actually emits.

## The deployment

`archive_file` zips `src/` directly with `source_dir = "../../src"`, so the
archive root is `reckon/` and handler strings resolve as
`reckon.aws.receiver.handler`. No layer, no container, no registry, no build
step. This works **only** because the runtime dependencies are zero and boto3
ships in the Lambda runtime; adding one third-party package replaces this with a
build pipeline.

Two deployment decisions are worth knowing before changing them.

**Secrets are read from SSM at run time, not passed as environment variables.**
Resolving a SecureString in Terraform would write the plaintext into Terraform
state *and* into the function's configuration, where `lambda:GetFunction` reads
it back. Non-secret values — the table name, the queue URL — are environment
variables, and `aws/secrets.py` checks the environment before SSM so a laptop and
a Lambda are configured identically.

**Concurrency is bounded on the event source mapping, not the function.**
`scaling_config { maximum_concurrency = 2 }`, never
`reserved_concurrent_executions = 1`. Reserved concurrency of 1 has a known
failure mode with SQS: the poller scales independently of the throttle, throttled
deliveries expire their visibility timeout, receive counts climb, and healthy
messages poison into the DLQ.

## Diagrams

`docs/diagrams/*.puml` are the source; the committed `.svg` files are generated.
`make diagrams` renders them and `make check-diagrams` fails if a source is
invalid or a committed SVG is out of date.

**The comparison is on rendered text, not bytes**, because plantuml derives box
geometry from font metrics — a CI runner without this machine's fonts produces a
different-but-correct SVG for an unchanged diagram. That was measured rather than
assumed: rendering one diagram under two fonts gives two different files. Editing
a diagram means editing its labels, so comparing them catches the failure that
actually happens.

`make diagrams` also strips two things from plantuml's output. Its version stamp,
so an upgrade is not mistaken for a change. And every `textLength` /
`lengthAdjust="spacing"` pair: plantuml pins each string to a width computed from
Java's font metrics, and a browser resolving `sans-serif` differently — Safari
does — stretches the letter spacing to hit a width for a font it is not using,
which makes the labels unreadable on GitHub.

**Be clear about what this catches.** A `.puml` that no longer renders, and an
SVG that no longer matches its source. It does **not** catch a diagram that
renders perfectly and describes something the code no longer does — that has
happened here once, when `upload-lifecycle.puml` still showed a filtered API call
the live API rejects. Only reading the diagram against the code finds that.

## Where the bodies are buried

Things that will look wrong until you know why.

- **The tolerance guard is asymmetric.** `DEFAULT_TOLERANCE` bounds the *low*
  side only; the high side is `MAX_CREDIBLE_FACTOR`. GPS jitter only ever adds
  length, so a factor below 1 is ordinary and can be large, while a factor above
  1 means the track measured *short*, which jitter cannot cause. A symmetric band
  falsely refused a real walk at 0.723.
- **GPS coverage is measured in seconds, not trackpoints.** The watch samples
  roughly half as often without a fix, so counting trackpoints understates a
  dropout badly enough to hide one.
- **The recording-gap threshold is relative, not absolute.** A gap is an interval
  longer than `max(3 s, 3 x the file's own median)`. An absolute threshold was
  written first and was wrong: real files sample at 1 s and 2 s and the synthetic
  builders at 10 s, so a fixed 3 s called ordinary sampling a gap on every
  generated fixture. The multiple is the measured one — seventeen of twenty real
  files top out at exactly 3 s against a 1 s median.
- **A recording gap warns and never refuses, but a late start does refuse.** The
  two look alike and are opposites. A gap between two trackpoints is chorded
  across, so no distance is lost, only the shape of that stretch. Time before the
  first trackpoint is not in the stream at all, so scaling up to the device's
  total spreads a missing stretch of route over the part that was recorded —
  exactly the failure the partial-GPS guard exists to prevent. Hence
  `unrecorded_time` alongside `recording_gaps`, and `MAX_UNRECORDED_FRACTION`
  alongside `MAX_GAP_FRACTION` despite the equal value: the numbers are free to
  diverge because the failures already have.
- **`unrecorded_time` reports zero when the lap time is shorter than the track.**
  Some producers write moving time rather than elapsed. This measure can refuse a
  correction, so it must never invent a reason to.
- **The exercise listing is filtered client-side.** The API's documented `filter`
  parameter is rejected for that data type in every spelling. The listing is
  ordered newest-first, so paging stops once it passes the window — but the
  ordering decides only when to *stop*, never what to yield.
- **`ax`, not `ns2`, for the ActivityExtension namespace.** ElementTree reserves
  the `ns<digits>` prefix form and raises on registering one. Invisible on real
  data, which carries no `Extensions` at all.
