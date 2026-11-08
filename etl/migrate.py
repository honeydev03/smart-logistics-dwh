"""Применение DDL слоёв core/mart (идемпотентно)."""
from __future__ import annotations

import logging

from etl.config import SQL_DIR, Settings
from etl.db import connect, run_sql_file

log = logging.getLogger(__name__)


def run(settings: Settings) -> None:
    with connect(settings) as conn:
        with conn.transaction():
            run_sql_file(conn, SQL_DIR / "dwh" / "10_ddl.sql")
    log.info("DDL core/mart применён")
