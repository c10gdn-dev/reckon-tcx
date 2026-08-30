#!/usr/bin/env python3
"""One-time OAuth authorisation, for both services.

Argparse plumbing only, deliberately: every decision this makes — building the
URL, checking `state`, exchanging the code, reading the expiry — lives in
`reckon.clients.oauth`, where it is covered (`PLAN.md` §7). A script past about
thirty lines of logic means something is in the wrong place.

    python scripts/authorize.py google --credentials client_secret_....json
    python scripts/authorize.py strava --client-id ... --client-secret ...

Prefer `--credentials` with the JSON downloaded from the Google Cloud console:
a secret passed as a flag lands in shell history and in `ps` output for every
other user on the machine.

Copy the printed URL into a browser, approve, then paste the address bar back.
The browser will show a connection error at the redirect — that is expected and
harmless; the code is in the URL, not the page.
"""

import argparse
import sys
import time
from pathlib import Path

from reckon.clients import health, strava
from reckon.clients.http import retrying, send
from reckon.clients.oauth import (
    ClientCredentials,
    authorization_url,
    code_from_redirect,
    exchange_code,
    new_state,
    read_client_credentials,
)
from reckon.stores.file import DEFAULT_PATH, FileStore

REDIRECT_URI = "http://localhost:8721/callback"

SERVICES = {
    "google": (health.AUTHORIZE_URL, health.TOKEN_URL, health.SCOPES, health.AUTHORIZE_EXTRA, " "),
    "strava": (
        strava.AUTHORIZE_URL,
        strava.TOKEN_URL,
        strava.SCOPES,
        strava.AUTHORIZE_EXTRA,
        strava.SCOPE_SEPARATOR,
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("service", choices=sorted(SERVICES))
    parser.add_argument(
        "--credentials",
        type=Path,
        help="the credentials JSON downloaded from the Google Cloud console",
    )
    parser.add_argument("--client-id")
    parser.add_argument("--client-secret")
    parser.add_argument("--redirect-uri", default=REDIRECT_URI)
    parser.add_argument(
        "--store",
        type=Path,
        default=DEFAULT_PATH,
        help=f"token and dedupe store to write into (default {DEFAULT_PATH})",
    )
    args = parser.parse_args(argv)

    if args.credentials is not None:
        client = read_client_credentials(args.credentials.read_text(encoding="utf-8"))
    elif args.client_id and args.client_secret:
        client = ClientCredentials(args.client_id, args.client_secret)
    else:
        parser.error("pass --credentials FILE, or both --client-id and --client-secret")

    authorize_url, token_url, scopes, extra, separator = SERVICES[args.service]
    state = new_state()
    print(
        authorization_url(
            authorize_url,
            client_id=client.client_id,
            redirect_uri=args.redirect_uri,
            scopes=scopes,
            state=state,
            extra=extra,
            scope_separator=separator,
        )
    )
    redirect = input("\nPaste the full redirect URL: ").strip()

    tokens = exchange_code(
        retrying(send),
        token_url,
        client_id=client.client_id,
        client_secret=client.client_secret,
        code=code_from_redirect(redirect, expected_state=state),
        redirect_uri=args.redirect_uri,
    )

    # Written through the store rather than as a bare JSON dict, so that
    # `reckon sync` can read it: FileStore expects a schema-versioned document
    # and refuses anything else, which is exactly the sort of seam that only
    # fails once real credentials are in hand.
    store = FileStore(args.store)
    current = store.load(args.service)
    store.save(args.service, tokens, expected_version=0 if current is None else current.version)

    remaining = tokens.expires_at - time.time()
    print(f"stored {args.service} tokens in {args.store}", file=sys.stderr)
    print(f"access token expires in {remaining / 60:.0f} min", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
