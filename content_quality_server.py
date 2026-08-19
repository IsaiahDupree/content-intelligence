from __future__ import annotations

import argparse

from services.content_quality import create_content_quality_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the loopback-only Content Quality API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=6010, type=int)
    args = parser.parse_args()
    create_content_quality_app().run(host=args.host, port=args.port, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
