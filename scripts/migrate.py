#!/usr/bin/env python3
"""Copy a local store into DynamoDB, or back again.

Argparse plumbing only; the copying itself is `reckon.stores.transfer`, where it
is tested (`PLAN.md` §7).

Run this once after `terraform apply`, before registering the webhook — the table
starts empty, so a notification arriving first would reach a worker with no
credentials.

    python scripts/migrate.py --table reckon
    python scripts/migrate.py --table reckon --down     # DynamoDB -> local file

Both directions exist because both adapters satisfy the same ports, so copying
the deployed state back to a file is a free way to inspect it.
"""

import argparse
import sys
from pathlib import Path

from reckon.stores.dynamo import DynamoStore
from reckon.stores.file import DEFAULT_PATH, FileStore
from reckon.stores.transfer import copy_logs, copy_tokens


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--table", required=True, help="the DynamoDB table name")
    parser.add_argument("--store", type=Path, default=DEFAULT_PATH, help="the local store")
    parser.add_argument("--region", help="override the AWS region")
    parser.add_argument(
        "--down", action="store_true", help="copy DynamoDB into the local file instead"
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="replace tokens the destination already holds"
    )
    parser.add_argument(
        "--tokens-only", action="store_true", help="skip the processed-activity log"
    )
    args = parser.parse_args(argv)

    client = None
    if args.region:
        import boto3

        client = boto3.client("dynamodb", region_name=args.region)

    local = FileStore(args.store)
    remote = DynamoStore(args.table, client=client)
    source, destination = (remote, local) if args.down else (local, remote)

    result = copy_tokens(source, destination, overwrite=args.overwrite)
    for warning in result.warnings:
        print(f"migrate: warning: {warning}", file=sys.stderr)
    for service in result.copied:
        print(f"copied {service} tokens")

    # Only the file store can enumerate its log; `ProcessedLogStore` has no such
    # method, deliberately, because the pipeline only ever asks about one
    # activity. Copying upwards is the direction that matters.
    if not args.tokens_only and not args.down:
        count = copy_logs(local.entries(), destination)
        print(f"copied {count} processed-activity records")
    elif args.down and not args.tokens_only:
        print("migrate: note: the log is not copied downwards", file=sys.stderr)

    if result.skipped:
        print(f"migrate: {len(result.skipped)} service(s) left alone", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
