#!/usr/bin/env python3
"""One-time OAuth authorisation, for both services.

Argparse plumbing only, deliberately: every decision this makes — building the
URL, checking `state`, exchanging the code, reading the expiry — lives in
`reckon.clients.oauth`, where it is covered (`PLAN.md` §7). A script past about
thirty lines of logic means something is in the wrong place.

    python scripts/authorize.py google --client-id ... --client-secret ...
    python scripts/authorize.py strava --client-id ... --client-secret ...

Copy the printed URL into a browser, approve, then paste the address bar back.
The browser will show a connection error at the redirect — that is expected and
harmless; the code is in the URL, not the page.
"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from reckon.clients import health, strava
from reckon.clients.http import retrying, send
from reckon.clients.oauth import (
    authorization_url,
    code_from_redirect,
    exchange_code,
    new_state,
)

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
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--redirect-uri", default=REDIRECT_URI)
    parser.add_argument("-o", "--output", type=Path, help="write the token pair here, mode 0600")
    args = parser.parse_args(argv)

    authorize_url, token_url, scopes, extra, separator = SERVICES[args.service]
    state = new_state()
    print(
        authorization_url(
            authorize_url,
            client_id=args.client_id,
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
        client_id=args.client_id,
        client_secret=args.client_secret,
        code=code_from_redirect(redirect, expected_state=state),
        redirect_uri=args.redirect_uri,
    )

    payload = json.dumps(asdict(tokens), indent=2)
    if args.output is None:
        print(payload)
    else:
        # Written before the mode is tightened, so create it empty first rather
        # than letting a refresh token exist world-readable for an instant.
        args.output.touch(mode=0o600, exist_ok=True)
        args.output.chmod(0o600)
        args.output.write_text(payload + "\n", encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
