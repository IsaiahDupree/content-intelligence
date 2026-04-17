"""Security middleware - CORS, CSP, and security headers."""
from flask import Flask
from functools import wraps


def configure_security_headers(app: Flask):
    """Configure CORS and security headers for Flask app."""

    @app.after_request
    def set_security_headers(response):
        """Add security headers to all responses."""

        # CORS headers
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"

        # Security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"

        # Content Security Policy
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "font-src 'self'; "
            "connect-src 'self' https://ivhfuhxorppptyuofbgq.supabase.co"
        )

        # Referrer Policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Feature Policy / Permissions Policy
        response.headers["Permissions-Policy"] = "accelerometer=(), camera=(), microphone=(), payment=()"

        return response

    @app.before_request
    def handle_preflight():
        """Handle CORS preflight requests."""
        from flask import request
        if request.method == "OPTIONS":
            response = app.make_default_options_response()
            response.headers.add("Access-Control-Allow-Origin", "*")
            response.headers.add("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            response.headers.add("Access-Control-Allow-Headers", "Content-Type, Authorization")
            return response, 200

    return app
