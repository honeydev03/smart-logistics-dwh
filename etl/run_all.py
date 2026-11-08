"""Точка входа: DDL -> ETL core -> витрины -> ML. Любой шаг безопасно перезапускается.

Примеры:
  python -m etl.run_all                                  # полное окно из .env
  python -m etl.run_all --start 2026-09-01 --end 2026-09-30   # backfill окна (витрины и ML пересчитываются)
  python -m etl.run_all --skip-ml
"""
from __future__ import annotations

import argparse
import logging
from datetime import date

from etl import build_marts, load_core, migrate
from etl.config import load_settings
from etl.logging_setup import setup_logging

log = logging.getLogger("run_all")


def main() -> None:
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--skip-ml", action="store_true")
    ap.add_argument("--skip-marts", action="store_true")
    args = ap.parse_args()

    s = load_settings()
    start, end = args.start or s.etl_start, args.end or s.etl_end
    log.info("Старт: окно ETL %s..%s, as_of=%s", start, end, s.as_of)

    migrate.run(s)
    load_core.run(s, start, end)
    if not args.skip_marts:
        build_marts.run(s)
    if not args.skip_ml:
        from ml import train_score  # ленивый импорт: CatBoost нужен только для ML
        train_score.run(s)
    log.info("Готово")


if __name__ == "__main__":
    main()
