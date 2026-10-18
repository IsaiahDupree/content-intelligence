#!/usr/bin/env python3
"""Run database migrations in production environment."""

import os
import sys
import argparse
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.supabase_client import get_supabase_client


def read_migration_file(filepath: str) -> str:
    """Read SQL migration file."""
    try:
        with open(filepath, "r") as f:
            return f.read()
    except FileNotFoundError:
        print(f"❌ Migration file not found: {filepath}")
        sys.exit(1)


def run_migration(name: str, sql_file: str) -> bool:
    """Run a single migration.

    Args:
        name: Migration name
        sql_file: Path to SQL file

    Returns:
        True if successful
    """
    print(f"\n📋 Running migration: {name}")
    print(f"   File: {sql_file}")

    try:
        sql = read_migration_file(sql_file)

        # Get Supabase client
        supabase = get_supabase_client()

        if not supabase.enabled:
            print("❌ Supabase client not initialized")
            return False

        # Execute migration
        result = supabase.client.postgrest.execute(sql)
        print(f"✅ Migration {name} completed successfully")
        return True

    except Exception as e:
        print(f"❌ Migration {name} failed: {e}")
        return False


def main():
    """Run all database migrations."""
    parser = argparse.ArgumentParser(description="Run database migrations")
    parser.add_argument("--dry-run", action="store_true", help="Show migrations without running")
    parser.add_argument("--migration", type=str, help="Run specific migration by name")
    args = parser.parse_args()

    migrations_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "migrations"
    )

    # Define migrations in order
    migrations = [
        ("add_ai_ugc_format", os.path.join(migrations_dir, "add_ai_ugc_format.sql")),
        ("add_ai_repurpose_formats", os.path.join(migrations_dir, "add_ai_repurpose_formats.sql")),
        ("add_ai_performance_score", os.path.join(migrations_dir, "add_ai_performance_score.sql")),
        ("create_format_history", os.path.join(migrations_dir, "create_format_history.sql")),
        ("rls_format_data", os.path.join(migrations_dir, "rls_format_data.sql")),
        ("add_indexes", os.path.join(migrations_dir, "add_indexes.sql")),
        ("add_validation", os.path.join(migrations_dir, "add_validation.sql")),
    ]

    print("=" * 60)
    print("Content Intelligence — Database Migrations")
    print(f"Timestamp: {datetime.now().isoformat()}")
    print("=" * 60)

    if args.dry_run:
        print("\n🔍 DRY RUN MODE - No changes will be made\n")

    # Filter to specific migration if requested
    if args.migration:
        migrations = [(n, p) for n, p in migrations if n == args.migration]
        if not migrations:
            print(f"❌ Migration '{args.migration}' not found")
            sys.exit(1)

    successful = 0
    failed = 0

    for name, filepath in migrations:
        if not os.path.exists(filepath):
            print(f"⏭️  Skipping {name} - file not found")
            continue

        if args.dry_run:
            sql = read_migration_file(filepath)
            print(f"\n📋 Migration: {name}")
            print("SQL Preview (first 500 chars):")
            print(sql[:500] + ("..." if len(sql) > 500 else ""))
        else:
            if run_migration(name, filepath):
                successful += 1
            else:
                failed += 1

    # Summary
    print("\n" + "=" * 60)
    if args.dry_run:
        print("✅ DRY RUN COMPLETE - All migrations reviewed")
    else:
        print("📊 Migration Summary")
        print(f"   ✅ Successful: {successful}")
        print(f"   ❌ Failed: {failed}")
        print(f"   Total: {successful + failed}")

        if failed > 0:
            print("\n⚠️  Some migrations failed. Please review and rerun.")
            sys.exit(1)
        else:
            print("\n✅ All migrations completed successfully!")

    print("=" * 60)


if __name__ == "__main__":
    main()
