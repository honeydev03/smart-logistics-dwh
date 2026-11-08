"""Подключение к PostgreSQL и выполнение SQL-файлов."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Mapping

import psycopg

from etl.config import Settings

log = logging.getLogger(__name__)


def connect(settings: Settings, retries: int = 30, delay: float = 2.0) -> psycopg.Connection:
    """ClientCursor: клиентская подстановка параметров, поэтому один SQL-файл
    может содержать несколько операторов и параметры (%(name)s)."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return psycopg.connect(settings.conninfo, cursor_factory=psycopg.ClientCursor)
        except psycopg.OperationalError as exc:  # БД ещё стартует
            last = exc
            log.info("БД недоступна (попытка %s/%s), жду %.0f c", attempt, retries, delay)
            time.sleep(delay)
    raise RuntimeError(f"Не удалось подключиться к БД: {last}")


def run_sql_file(conn: psycopg.Connection, path: Path, params: Mapping[str, Any] | None = None) -> None:
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql, params)  # без commit: управляет вызывающий (transaction())
