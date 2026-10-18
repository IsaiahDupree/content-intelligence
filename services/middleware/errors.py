"""Error handling middleware - centralized error handling for all endpoints."""
from flask import jsonify
from typing import Dict, Any, Tuple
import traceback
import logging

logger = logging.getLogger(__name__)


class APIError(Exception):
    """Base class for API errors."""
    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(self.message)


class ValidationError(APIError):
    """Raised when request validation fails."""
    def __init__(self, message: str):
        super().__init__(message, 400)


class NotFoundError(APIError):
    """Raised when resource not found."""
    def __init__(self, message: str = "Resource not found"):
        super().__init__(message, 404)


class InternalServerError(APIError):
    """Raised for internal server errors."""
    def __init__(self, message: str = "Internal server error"):
        super().__init__(message, 500)


def handle_error(error: Exception) -> Tuple[Dict[str, Any], int]:
    """Convert exception to JSON response."""
    if isinstance(error, APIError):
        return jsonify({
            "status": "error",
            "error": error.message,
            "code": error.status_code
        }), error.status_code

    # Log unexpected errors
    logger.error(f"Unhandled error: {str(error)}\n{traceback.format_exc()}")

    return jsonify({
        "status": "error",
        "error": "Internal server error",
        "code": 500
    }), 500


def register_error_handlers(app):
    """Register error handlers with Flask app."""

    @app.errorhandler(APIError)
    def handle_api_error(error: APIError):
        return jsonify({
            "status": "error",
            "error": error.message,
            "code": error.status_code
        }), error.status_code

    @app.errorhandler(404)
    def handle_not_found(error):
        return jsonify({
            "status": "error",
            "error": "Endpoint not found",
            "code": 404
        }), 404

    @app.errorhandler(500)
    def handle_internal_error(error):
        logger.error(f"Internal server error: {str(error)}")
        return jsonify({
            "status": "error",
            "error": "Internal server error",
            "code": 500
        }), 500
