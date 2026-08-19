#!/usr/bin/env python3
"""Deploy and verify the Market Tape schema without exposing credentials."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services.market_tape.migration import (  # noqa: E402
    MIGRATION_PATH,
    SupabaseMigrationManager,
    migration_sql,
    validate_migration,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Market Tape Supabase schema management")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="Validate the checked-in idempotent SQL contract")
    subparsers.add_parser("status", help="Probe all required tables on the configured project")
    subparsers.add_parser("sql", help="Print the canonical SQL for dashboard execution")
    verify_parser = subparsers.add_parser(
        "verify",
        help="Verify RLS and append-only invariants through the Management API",
    )
    verify_parser.add_argument(
        "--project-ref",
        required=True,
        help="Exact target project ref; must match SUPABASE_URL",
    )
    counts_parser = subparsers.add_parser(
        "counts",
        help="Count remote rows in every Market Tape table through the Management API",
    )
    counts_parser.add_argument(
        "--project-ref",
        required=True,
        help="Exact target project ref; must match SUPABASE_URL",
    )
    apply_parser = subparsers.add_parser("apply", help="Apply through the Supabase Management API")
    apply_parser.add_argument(
        "--project-ref",
        required=True,
        help="Exact target project ref; must match SUPABASE_URL",
    )
    args = parser.parse_args()

    if args.command == "validate":
        return _emit(validate_migration())
    if args.command == "sql":
        print(f"-- source: {MIGRATION_PATH}")
        print(migration_sql(), end="")
        return 0

    manager = SupabaseMigrationManager()
    try:
        if args.command == "status":
            result = manager.inspect()
        elif args.command == "verify":
            result = manager.verify_database(args.project_ref)
        elif args.command == "counts":
            result = manager.remote_counts(args.project_ref)
        else:
            result = manager.apply(args.project_ref)
    finally:
        manager.close()
    return _emit(result)


def _emit(result: dict) -> int:
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("state") in {"ready", "applied"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
