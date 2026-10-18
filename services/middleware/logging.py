"""Request logging middleware - logs all API requests and responses."""
from flask import Flask, request, g
from datetime import datetime, timezone
import logging
import json
from time import time

logger = logging.getLogger("content-intelligence.requests")


def setup_request_logging(app: Flask):
    """Setup request and response logging for Flask app."""

    @app.before_request
    def log_request_start():
        """Log the start of request processing."""
        g.start_time = time()

        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": request.method,
            "path": request.path,
            "remote_addr": request.remote_addr,
            "user_agent": request.user_agent.string,
            "content_type": request.content_type
        }

        logger.info(f"→ Incoming {request.method} {request.path}", extra=log_data)

    @app.after_request
    def log_request_end(response):
        """Log the end of request processing."""
        if hasattr(g, 'start_time'):
            elapsed = time() - g.start_time
        else:
            elapsed = 0

        log_data = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": request.method,
            "path": request.path,
            "status_code": response.status_code,
            "duration_ms": round(elapsed * 1000, 2),
            "content_type": response.content_type,
            "response_size": len(response.get_data())
        }

        logger.info(
            f"← Response {response.status_code} {request.method} {request.path} ({elapsed:.3f}s)",
            extra=log_data
        )

        return response

    return app


def setup_structured_logging():
    """Setup structured JSON logging."""
    # Configure loguru for structured JSON output
    try:
        from loguru import logger as loguru_logger

        # Remove default handler
        loguru_logger.remove()

        # Add JSON structured logging
        loguru_logger.add(
            "logs/api.log",
            format="{message}",
            serialize=True,
            level="INFO"
        )

        # Also add console output for development
        loguru_logger.add(
            lambda msg: print(msg, end=""),
            format="<level>{level: <8}</level> | {time:YYYY-MM-DD HH:mm:ss} | {message}",
            level="DEBUG"
        )

    except ImportError:
        logger.warning("loguru not available, using standard logging")
