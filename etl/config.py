"""Конфигурация только из переменных окружения (см. .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"


def _date(name: str, default: str) -> date:
    return date.fromisoformat(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    as_of: date
    etl_start: date
    etl_end: date
    seed: int
    model_name: str
    artifacts_dir: Path

    @property
    def conninfo(self) -> str:
        return (f"host={self.host} port={self.port} dbname={self.dbname} "
                f"user={self.user} password={self.password}")

    @property
    def sqlalchemy_url(self) -> str:
        return (f"postgresql+psycopg://{self.user}:{self.password}"
                f"@{self.host}:{self.port}/{self.dbname}")


def load_settings() -> Settings:
    return Settings(
        host=os.getenv("POSTGRES_HOST", "localhost"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        dbname=os.getenv("POSTGRES_DB", "deals_bigdata"),
        user=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        as_of=_date("AS_OF_DATE", "2026-10-26"),
        etl_start=_date("ETL_START_DATE", "2023-01-01"),
        etl_end=_date("ETL_END_DATE", "2027-12-31"),
        seed=int(os.getenv("RANDOM_SEED", "42")),
        model_name=os.getenv("MODEL_NAME", "catboost_v1"),
        artifacts_dir=Path(os.getenv("ARTIFACTS_DIR", str(ROOT / "artifacts"))),
    )
