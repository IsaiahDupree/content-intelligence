"""Request validation middleware - validates incoming API requests."""
from functools import wraps
from flask import request, jsonify
from typing import Dict, Any, Optional, Callable


class RequestValidator:
    """Validates API requests against schemas."""

    # Define expected request schemas
    SCHEMAS = {
        "/api/classify": {
            "POST": ["content"]
        },
        "/api/score": {
            "POST": ["metrics"]
        },
        "/api/repurpose/*": {
            "GET": []
        },
        "/api/analyze": {
            "POST": ["content"]
        }
    }

    @staticmethod
    def validate_json(f: Callable) -> Callable:
        """Decorator to validate JSON content-type."""
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.content_type and "application/json" not in request.content_type:
                return jsonify({"error": "Content-Type must be application/json"}), 400

            try:
                request.get_json()
            except Exception as e:
                return jsonify({"error": "Invalid JSON"}), 400

            return f(*args, **kwargs)
        return decorated_function

    @staticmethod
    def validate_fields(required_fields: list) -> Callable:
        """Decorator to validate required fields in JSON payload."""
        def decorator(f: Callable) -> Callable:
            @wraps(f)
            def decorated_function(*args, **kwargs):
                data = request.get_json()

                if not data:
                    return jsonify({"error": "No JSON data provided"}), 400

                missing_fields = [field for field in required_fields if field not in data]

                if missing_fields:
                    return jsonify({
                        "error": f"Missing required fields: {', '.join(missing_fields)}"
                    }), 400

                return f(*args, **kwargs)
            return decorated_function
        return decorator


def validate_api_request(required_fields: Optional[list] = None) -> Callable:
    """Combined validation decorator."""
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # Check content-type
            if request.method == "POST" and request.content_type:
                if "application/json" not in request.content_type:
                    return jsonify({"error": "Content-Type must be application/json"}), 400

            # Validate JSON
            try:
                data = request.get_json()
            except Exception as e:
                return jsonify({"error": "Invalid JSON"}), 400

            # Check required fields
            if required_fields and data:
                missing = [f for f in required_fields if f not in data]
                if missing:
                    return jsonify({
                        "error": f"Missing required fields: {', '.join(missing)}"
                    }), 400

            return f(*args, **kwargs)
        return decorated_function
    return decorator
