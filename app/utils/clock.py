"""The database's clock -- the one that stamps every ``server_default=now()``.

Comparing "now" against stored timestamps must use the same clock that wrote
them (UTC on SQLite, the server's zone on MySQL), or "today" drifts by the
zone offset. This is not a second timezone system: it defers to whatever the
database is already configured to use.
"""

from __future__ import annotations

from datetime import date, datetime

import sqlalchemy as sa

from app.extensions import db


def db_now() -> datetime:
    now = db.session.scalar(sa.select(sa.func.now()))
    if isinstance(now, str):  # SQLite hands CURRENT_TIMESTAMP back as text
        now = datetime.fromisoformat(now)
    return now.replace(tzinfo=None)


def db_today() -> date:
    return db_now().date()
