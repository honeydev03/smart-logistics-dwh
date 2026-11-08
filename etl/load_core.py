"""Этап 1: валидация, очистка, конвертация валют и идемпотентная загрузка окна в core."""
from __future__ import annotations

import json
import logging
from datetime import date

from etl.config import SQL_DIR, Settings
from etl.db import connect, run_sql_file

log = logging.getLogger(__name__)


def run(settings: Settings, start: date, end: date) -> dict:
    if start > end:
        raise ValueError(f"start {start} > end {end}")
    params = {"start": start, "end": end}
    with connect(settings) as conn:
        with conn.transaction():  # окно перезаписывается атомарно
            run_sql_file(conn, SQL_DIR / "etl" / "10_load_core.sql", params)
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.prices WHERE loading_date BETWEEN %(start)s AND %(end)s", params)
                rows_read = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM core.prices_clean WHERE loading_date BETWEEN %(start)s AND %(end)s", params)
                rows_loaded = cur.fetchone()[0]
                cur.execute(
                    "SELECT dq_reason, count(*) FROM core.dq_quarantine_prices "
                    "WHERE loading_date BETWEEN %(start)s AND %(end)s GROUP BY 1 ORDER BY 2 DESC", params)
                by_reason = {r: int(n) for r, n in cur.fetchall()}
                quarantined = sum(by_reason.values())
                if rows_read != rows_loaded + quarantined:
                    raise RuntimeError(
                        f"Баланс строк нарушен: read={rows_read}, loaded={rows_loaded}, quarantined={quarantined}")
                cur.execute(
                    "INSERT INTO core.etl_run_log (step, window_start, window_end, rows_read, rows_loaded, "
                    "rows_quarantined, details) VALUES ('load_core', %s, %s, %s, %s, %s, %s)",
                    (start, end, rows_read, rows_loaded, quarantined, json.dumps(by_reason)))
    log.info("load_core [%s .. %s]: прочитано=%s, загружено=%s, в карантине=%s %s",
             start, end, rows_read, rows_loaded, quarantined, by_reason)
    return {"read": rows_read, "loaded": rows_loaded, "quarantined": quarantined, "by_reason": by_reason}
