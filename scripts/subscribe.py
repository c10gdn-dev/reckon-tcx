#!/usr/bin/env python3
"""Register the deployed webhook with Google Health, or list what is registered.

Argparse plumbing only, as `PLAN.md` §7 requires of everything in `scripts/`.

Run it after `terraform apply`, with the `webhook_url` output:

    URL=$(terraform -chdir=deploy/terraform output -raw webhook_url)
    python scripts/subscribe.py create --url "$URL" --secret "$WEBHOOK_SECRET" \
        --credentials ~/Downloads/client_secret_*.json
    python scripts/subscribe.py list --credentials ~/Downloads/client_secret_*.json

Google verifies the endpoint as it registers it, with two probes: one carrying
the Authorization header, which must answer 200 or 201, and one without, which
must answer 401 or 403. `--secret` must therefore match the `webhook_secret` in
SSM exactly, or registration fails with a verification error rather than a
useful message.
"""

import argparse
import json
import sys
from pathlib import Path

from reckon.clients import health
from reckon.clients.http import Request, retrying, send
from reckon.clients.oauth import read_client_credentials
from reckon.pipeline import token_holder
from reckon.stores.file import DEFAULT_PATH, FileStore

SUBSCRIBERS = "https://health.googleapis.com/v4/subscribers"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("action", choices=("create", "list", "delete"))
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--store", type=Path, default=DEFAULT_PATH)
    parser.add_argument("--url", help="the Function URL, for `create`")
    parser.add_argument("--secret", help="the Authorization header value Google should send")
    parser.add_argument("--name", default="reckon", help="subscriber name")
    args = parser.parse_args(argv)

    if args.action == "create" and not (args.url and args.secret):
        parser.error("create needs --url and --secret")

    client = read_client_credentials(args.credentials.read_text(encoding="utf-8"))
    transport = retrying(send)
    token = token_holder(
        FileStore(args.store),
        "google",
        transport=transport,
        token_url=health.TOKEN_URL,
        client_id=client.client_id,
        client_secret=client.client_secret,
    ).access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    if args.action == "list":
        response = transport(Request("GET", SUBSCRIBERS, headers=headers))
    elif args.action == "delete":
        response = transport(Request("DELETE", f"{SUBSCRIBERS}/{args.name}", headers=headers))
    else:
        response = transport(
            Request(
                "POST",
                f"{SUBSCRIBERS}?subscriberId={args.name}",
                headers=headers,
                body=json.dumps(
                    {
                        "endpointUri": args.url,
                        "authorizationHeader": args.secret,
                        "dataTypes": ["exercise"],
                    }
                ).encode(),
            )
        )
    print(response.body.decode("utf-8", errors="replace"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
