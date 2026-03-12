#!/usr/bin/env python3
"""Compatibility shim for the legacy dashboard module name."""

from dashboard_backend import *  # noqa: F401,F403


if __name__ == "__main__":
    import os

    from dashboard_v2.app import create_app

    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    debug = os.environ.get("DASHBOARD_DEBUG", "").lower() in {"1", "true", "yes"}
    app = create_app()
    print("dashboard.py is now a compatibility shim; starting Dashboard V2...")
    app.run(host=host, port=port, debug=debug)
