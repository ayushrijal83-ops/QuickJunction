"""Development entry point.

    python run.py

Production is served by a WSGI server, never by this file:

    gunicorn "app:create_app('production')"

Debug is taken from the resolved configuration, so it can only ever be on in
the development environment.
"""

from __future__ import annotations

import os

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLASK_RUN_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLASK_RUN_PORT", "5000")),
        debug=app.config["DEBUG"],
    )
