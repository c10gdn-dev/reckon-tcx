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
| `core/tcx.py` | Parsing and serialising TCX. Namespaces, element lookup, GPS coverage. Knows nothing about rescaling. |
| `core/rescale.py` | The transform. Pure: bytes and a target in, bytes and numbers out. |
| `core/analyse.py` | Corpus measurement. Pure. Feeds `reckon analyse`. |
| `core/errors.py` | The exception hierarchy, and the `Transient` marker. |
| `clients/http.py` | **The only module that touches the network.** `send` performs one request; `retrying` decides what is worth repeating. |
| `clients/oauth.py` | OAuth 2.0: authorisation URLs, code exchange, refresh, and `TokenHolder`. |
| `clients/health.py` | Google Health: listing exercises, downloading TCX. |
| `clients/strava.py` | Strava: multipart upload, polling, duplicate detection. |
| `stores/base.py` | The two persistence ports, as protocols, plus the vocabulary they are defined in. |
| `stores/file.py` | Local adapter. Both ports in one 0600 JSON file, under `flock`. |
| `pipeline.py` | Activity id → fetch → rescale → upload → record. Shared by both run modes. |
| `stores/dynamo.py` | AWS adapter. Same two ports over one DynamoDB table; the only place besides `aws/` that may import boto3. |
| `aws/receiver.py` | Webhook endpoint. Authenticates, enqueues, acknowledges. Nothing else. |
| `aws/worker.py` | SQS handler. Routes the two message shapes; re-enqueues delayed rather than sleeping. |
| `aws/queue.py` | The SQS seam, as `http.py` is the network seam. |
| `aws/config.py` | Assembles the pipeline inside Lambda, as `cli.py` does locally. |
| `aws/secrets.py` | Configuration resolution: environment first, then SSM SecureString at run time. |
| `stores/transfer.py` | Copying one store's contents into another. Direction-agnostic, because both satisfy the same ports. |
| `deploy/terraform/` | The deployment. Depends on `src/`, never the reverse. |
| `cli.py` | Argument parsing and configuration for the local CLI. |

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
of behaviours against both.*

## Why local and AWS share a pipeline

`PLAN.md` §2 in full. In short: only **storage** and **trigger** differ.

| | Local | AWS |
|---|---|---|
| Trigger | `reckon sync` | webhook → SQS |
| Stores | one JSON file | DynamoDB |
| Retry | none, fail loudly | SQS + DLQ |

Everything from "here is an activity id" to "Strava has it" is `pipeline.py`,
unchanged. The port/adapter boundary is not decoration: it is the reason the
whole transform could be built and validated before any cloud account existed.

## Two axes that must not be collapsed

This codebase has repeatedly been bitten by one mechanism standing in for two
meanings. Two separate guards exist because of it, and both look redundant until
you try to merge them.

**`Transient` is a mixin marker, not a `retryable` flag.** "What went wrong" and
"would retrying help" are independent questions. A 429 and a dropped connection
are nothing alike as faults but identical as decisions; a 429 and a 404 are the
reverse. Catch `Transient` to decide about retrying, the concrete class to decide
what to tell the user.

**`gps_coverage` is separate from `recording_gaps`.** One asks whether the
trackpoints carried a position; the other asks whether there were trackpoints at
all. They sound like the same question and are not: a real file reported 100%
coverage while 47% of its elapsed time fell between trackpoints. Folding gaps
into coverage would have made a warning into a refusal, and the two failures
deserve different answers — a missing *fix* loses distance, a missing
*trackpoint* loses only shape, because the distance stream chords straight
across it.

**`Status.on_strava` is separate from whether a correction happened.** An
activity that reached Strava uncorrected is a *success*: yoga, an indoor walk, a
track whose GPS dropped out. Collapsing "cannot correct" into "do not upload"
would silently drop them, which is the one thing the design forbids. Hence four
outcomes — `uploaded`, `passed_through`, `withheld`, `failed` — plus a raised
exception for transient faults, which is never recorded so that a redelivery is
never mistaken for a decision.

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
- **A recording gap warns and never refuses.** The distance stream joins the two
  ends with a straight line, so no distance is lost, only the shape of that
  stretch. Refusing would be the dropping the design forbids; correcting
  silently would say nothing about a file that is half unrecorded.
- **The exercise listing is filtered client-side.** The API's documented `filter`
  parameter is rejected for that data type in every spelling. The listing is
  ordered newest-first, so paging stops once it passes the window — but the
  ordering decides only when to *stop*, never what to yield.
- **`ax`, not `ns2`, for the ActivityExtension namespace.** ElementTree reserves
  the `ns<digits>` prefix form and raises on registering one. Invisible on real
  data, which carries no `Extensions` at all.
