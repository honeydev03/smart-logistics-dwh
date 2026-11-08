"""Этап 2: атомарный пересчёт витрины карточки маршрута."""
from __future__ import annotations

import logging

from etl.config import SQL_DIR, Settings
from etl.db import connect, run_sql_file

log = logging.getLogger(__name__)


def run(settings: Settings) -> int:
    with connect(settings) as conn:
        with conn.transaction():
            run_sql_file(conn, SQL_DIR / "dwh" / "30_mart_route_card.sql", {"as_of": settings.as_of})
            with conn.cursor() as cur:
                cur.execute("SELECT count(*), count(*) FILTER (WHERE reliability_level = 'HIGH') FROM mart.mart_route_card")
                total, high = cur.fetchone()
                cur.execute(
                    "INSERT INTO core.etl_run_log (step, rows_loaded, details) "
                    "VALUES ('build_marts', %s, %s)", (total, f'{{"high": {high}}}'))
    log.info("mart_route_card: строк=%s (HIGH=%s), as_of=%s", total, high, settings.as_of)
    return total
