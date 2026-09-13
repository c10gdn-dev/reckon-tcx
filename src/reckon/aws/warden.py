"""Watch the age of the grant, and say something before it expires.

The `testing` profile runs against an unpublished OAuth client, whose refresh
token Google expires after **seven days**. Nothing reports a refresh token's
expiry — not the token endpoint, not the console — so the only way to know the
clock is running out is to remember when a human last authorised, which is what
`Tokens.authorised_at` is for.

**This is a convenience, not a safety net, and the distinction matters.** A dead
token costs latency and not data: Google retries a failed webhook delivery with
backoff for up to seven days (`PLAN.md` §8), the worker raises rather than
recording anything, and SQS redelivers. Everything that arrived while the grant
was dead is delivered once it is renewed. What this buys is being told on a
Sunday evening rather than discovering it on Wednesday.

Scheduled daily rather than run per-activity: the answer changes once a day, and
a check that runs on every notification is a check that fails on every
notification when something is wrong with it.
"""

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from reckon.aws.secrets import Secrets
from reckon.clients.health import Profile
from reckon.clients.oauth import Tokens
from reckon.stores.base import TokenStore
from reckon.stores.dynamo import DynamoStore

# Google's documented life for an unpublished client's refresh token.
GRANT_DAYS = 7.0

# How long before that to start saying so. One day is enough to act on and short
# enough that the warning is not routine background noise — a warning that is
# always on is one nobody reads.
WARN_AFTER_DAYS = 6.0

# Every service whose grant is reported. Which of them is *watched* depends on
# the profile: Strava's grant never expires, and Google's expires weekly only on
# an unpublished client. A published client's lasts about six months.
SERVICES = ("google", "strava")

# Google's documented life for a published client's refresh token: revoked, or
# six months unused. A deployment refreshing on every notification never
# approaches it, so `published` is reported and never warned about — warning at
# six days there would email a false alarm within the week and teach the reader
# to ignore the one alarm that matters.
PUBLISHED_IS_WATCHED = False


def handler(event: Mapping[str, Any], context: Any = None) -> dict[str, Any]:
    """EventBridge entry point. Prints the report, and never raises for age.

    An expiring grant is news, not a fault: raising would put this on the
    dead-letter queue alongside activities that genuinely failed to reach Strava,
    which is the one signal that should mean "something is missing".

    **Printed, not merely returned.** A scheduled invocation's return value goes
    nowhere — EventBridge discards it — so the alarm is driven by a metric filter
    over this line in the log. `{"warn": true}` is what it matches, which is why
    the key is a bare boolean at the top level rather than something nested and
    more descriptive.
    """
    report = check()
    print(json.dumps(report))
    return report


def check(
    *,
    store: TokenStore | None = None,
    secret: Callable[[str], str] | None = None,
    now: Callable[[], float] = time.time,
    warn_after_days: float = WARN_AFTER_DAYS,
    profile: Profile | None = None,
) -> dict[str, Any]:
    """How old each stored grant is, and whether any of them wants attention."""
    secret = Secrets() if secret is None else secret
    store = DynamoStore(secret("RECKON_TABLE"), now=now) if store is None else store
    profile = _profile(secret) if profile is None else profile

    moment = now()
    report: dict[str, Any] = {"services": {}, "warn": False, "profile": str(profile)}
    for service in SERVICES:
        stored = store.load(service)
        report["services"][service] = _age(
            service, stored.tokens if stored else None, moment, profile
        )

    report["warn"] = any(
        entry["days"] is not None and entry["days"] >= warn_after_days
        for entry in report["services"].values()
        if entry["watched"]
    )
    return report


def _profile(secret: Callable[[str], str]) -> Profile:
    """Which client this deployment authenticates against, or the safe default.

    Unreadable or unset means `published`, which warns about nothing — the
    failure that matters here is a false alarm, not a missed one. A missed
    warning costs a manual re-authorisation the operator would have to do
    anyway; a false one costs the credibility of the alarm channel.
    """
    try:
        return Profile(secret("RECKON_GOOGLE_PROFILE"))
    except (KeyError, ValueError):
        return Profile.PUBLISHED


def _age(service: str, tokens: Tokens | None, moment: float, profile: Profile) -> dict[str, Any]:
    """One service's grant age, and whether its expiry is worth watching.

    `days` is None in two different situations that must not be confused: no
    tokens are stored at all, and tokens stored before `authorised_at` existed.
    The first means nobody has authorised; the second means somebody did and we
    cannot say when. Neither is grounds for a warning — a warning that fires on
    "unknown" fires forever — and both are reported plainly instead.
    """
    if tokens is None:
        return {"days": None, "state": "not authorised", "watched": False}
    days = tokens.granted_days_ago(moment)
    if days is None:
        return {"days": None, "state": "authorised before this was recorded", "watched": False}
    # Only Google's grant runs on a seven-day clock, and only on an unpublished
    # client. Strava's lasts until revoked; a published Google client's lasts
    # months. Both are reported and neither is warned about.
    watched = service == "google" and profile is Profile.TESTING
    return {"days": round(days, 2), "state": "ok", "watched": watched}
