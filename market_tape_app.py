"""Minimal loopback API for the autonomous Market Tape runtime."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from flask import Flask, jsonify

from services.market_tape.api import register_market_tape_routes
from services.market_tape.config import MarketTapeConfig
from services.market_tape.store import SCHEMA_VERSION
from services.middleware.errors import register_error_handlers
from services.middleware.security import configure_security_headers


SERVICE_VERSION = "2.0.0"


def create_market_tape_app(config: MarketTapeConfig | None = None) -> Flask:
    app = Flask("market-tape")
    configure_security_headers(app)
    register_error_handlers(app)
    store = register_market_tape_routes(app, config)

    @app.get("/health")
    def health():
        with store.connect() as connection:
            row = connection.execute(
                "SELECT value FROM mt_meta WHERE key = 'schema_version'"
            ).fetchone()
        database_schema_version = int(row[0]) if row else 0
        schema_parity = database_schema_version == SCHEMA_VERSION
        payload = {
            "status": "healthy" if schema_parity else "degraded",
            "service": "content-intelligence-market-tape",
            "version": SERVICE_VERSION,
            "code_schema_version": SCHEMA_VERSION,
            "database_schema_version": database_schema_version,
            "schema_parity": schema_parity,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return jsonify(payload), 200 if schema_parity else 503

    return app


app = create_market_tape_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "6006"))
    app.run(
        host=os.getenv("HOST", "127.0.0.1"),
        port=port,
        debug=False,
        use_reloader=False,
    )
