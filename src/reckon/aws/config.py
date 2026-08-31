"""Assembling the pipeline inside Lambda, from configuration.

The Lambda equivalent of `cli._build_pipeline`: the same clients and the same
pipeline, differing only in where the store lives and where secrets come from.
Kept out of the handlers so those stay thin enough to test with a dictionary
(`PLAN.md` §7).

`secret` is a callable rather than a direct `os.environ` read so phase 7 can
decide between environment variables and an SSM lookup without touching a
handler. §9 specifies SSM SecureString; whether Terraform resolves those at apply
time into environment variables or the function reads them at runtime is a
deployment question, and this is the seam that keeps it one.
"""

import os
import time
from collections.abc import Callable

from reckon.aws.secrets import Secrets
from reckon.clients import health as health_api
from reckon.clients import strava as strava_api
from reckon.clients.http import Transport, retrying, send
from reckon.pipeline import Pipeline, token_holder
from reckon.stores.base import TokenStore
from reckon.stores.dynamo import DynamoStore


def from_environment(name: str) -> str:
    """An environment variable that must be present.

    Used for values that are not secret — the table name, the queue URL — and as
    the local-development fallback. Secrets come from `aws.secrets.Secrets`,
    which checks the environment first and then SSM.
    """
    try:
        return os.environ[name]
    except KeyError:
        raise KeyError(f"{name} is not set; the function cannot start without it") from None


def build_pipeline(
    *,
    store: TokenStore | None = None,
    transport: Transport | None = None,
    secret: Callable[[str], str] | None = None,
    now: Callable[[], float] = time.time,
    dry_run: bool = False,
) -> Pipeline:
    """The same pipeline the CLI builds, pointed at DynamoDB."""
    secret = Secrets() if secret is None else secret
    transport = retrying(send) if transport is None else transport
    store = DynamoStore(secret("RECKON_TABLE"), now=now) if store is None else store

    google = token_holder(
        store,
        "google",
        transport=transport,
        token_url=health_api.TOKEN_URL,
        client_id=secret("RECKON_GOOGLE_CLIENT_ID"),
        client_secret=secret("RECKON_GOOGLE_CLIENT_SECRET"),
        now=now,
    )
    strava = token_holder(
        store,
        "strava",
        transport=transport,
        token_url=strava_api.TOKEN_URL,
        client_id=secret("RECKON_STRAVA_CLIENT_ID"),
        client_secret=secret("RECKON_STRAVA_CLIENT_SECRET"),
        now=now,
    )
    return Pipeline(
        health=health_api.GoogleHealth(transport, google),
        strava=strava_api.Strava(transport, strava),
        logs=store,  # type: ignore[arg-type]
        now=now,
        dry_run=dry_run,
    )
