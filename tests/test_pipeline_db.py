"""Интеграционные проверки на реальной БД (пропускаются, если Postgres недоступен).
Запуск после `docker compose up`:  docker compose run --rm pipeline pytest -q tests
"""
from datetime import date

import psycopg
import pytest

from etl import build_marts, load_core, migrate
from etl.config import load_settings

S = load_settings()
WINDOW = (date(2026, 9, 1), date(2026, 9, 30))


def _conn():
    return psycopg.connect(S.conninfo)


try:
    with _conn() as _c, _c.cursor() as _cur:
        _cur.execute("SELECT to_regclass('public.prices')")
        _HAS_DB = _cur.fetchone()[0] is not None
except Exception:  # noqa: BLE001
    _HAS_DB = False

pytestmark = pytest.mark.skipif(not _HAS_DB, reason="PostgreSQL с источником недоступен")


def q(sql, params=None):
    with _conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


@pytest.fixture(scope="module", autouse=True)
def prepared():
    migrate.run(S)
    load_core.run(S, *WINDOW)


def test_row_balance_read_equals_loaded_plus_quarantined():
    read = q("SELECT count(*) FROM public.prices WHERE loading_date BETWEEN %s AND %s", WINDOW)[0][0]
    loaded = q("SELECT count(*) FROM core.prices_clean WHERE loading_date BETWEEN %s AND %s", WINDOW)[0][0]
    quar = q("SELECT count(*) FROM core.dq_quarantine_prices WHERE loading_date BETWEEN %s AND %s", WINDOW)[0][0]
    assert read == loaded + quar and read > 0


def test_core_has_only_valid_rows():
    bad = q("""SELECT count(*) FROM core.prices_clean
               WHERE price_src <= 0 OR price_rub <= 0 OR distance <= 0 OR total_weight < 0 OR total_volume < 0
                  OR (currency_code = '643' AND price_src <= 1000)""")[0][0]
    assert bad == 0


def test_foreign_currency_without_exact_rate_is_quarantined():
    leaked = q("""SELECT count(*) FROM core.prices_clean c JOIN public.prices p ON p.id = c.price_id
                  WHERE p.currency_code <> '643' AND p.nearest_rate_date IS NULL""")[0][0]
    assert leaked == 0


def test_idempotent_reload_same_window():
    def snapshot():
        return q("""SELECT count(*), round(sum(price_rub)::numeric, 2),
                           (SELECT count(*) FROM core.price_body_groups g JOIN core.prices_clean c USING (price_id)
                            WHERE c.loading_date BETWEEN %s AND %s),
                           (SELECT count(*) FROM core.dq_quarantine_prices WHERE loading_date BETWEEN %s AND %s)
                    FROM core.prices_clean WHERE loading_date BETWEEN %s AND %s""", WINDOW * 3)
    before = snapshot()
    load_core.run(S, *WINDOW)
    assert snapshot() == before


def test_one_row_per_price_and_body_group():
    dup = q("SELECT count(*) FROM (SELECT price_id, body_group FROM core.price_body_groups "
            "GROUP BY 1, 2 HAVING count(*) > 1) t")[0][0]
    assert dup == 0
    no_group = q("SELECT count(*) FROM core.prices_clean c WHERE NOT EXISTS "
                 "(SELECT 1 FROM core.price_body_groups g WHERE g.price_id = c.price_id)")[0][0]
    assert no_group == 0


def test_dim_city_unique_gc_id():
    assert q("SELECT count(*) - count(DISTINCT gc_id) FROM core.dim_city")[0][0] == 0


def test_weight_and_volume_category_boundaries():
    rows = q("SELECT core.weight_category(10.0), core.weight_category(30.0), core.weight_category(29.96), "
             "core.weight_category(2.5), core.weight_category(0.44), core.volume_category(0), "
             "core.volume_category(120), core.volume_category(120.5)")[0]
    assert rows == ("FTL", "G20T", "G20T", "5T", "OTH", "OTH", "FTL", "OTH")


def test_mart_grain_unique_no_future_and_null_rules():
    build_marts.run(S)
    build_marts.run(S)  # повторный пересчёт не задваивает
    assert q("SELECT count(*) - count(DISTINCT (month, loading_city_gcid, unloading_city_gcid, body_group, "
             "weight_category)) FROM mart.mart_route_card")[0][0] == 0
    assert q("SELECT count(*) FROM mart.mart_route_card WHERE last_price_date > %s", (S.as_of,))[0][0] == 0
    assert q("SELECT count(*) FROM mart.mart_route_card WHERE trips_count < 2 AND cv_rate IS NOT NULL")[0][0] == 0
    # MoM = NULL, если нет календарно предыдущего месяца в той же ячейке
    assert q("""SELECT count(*) FROM mart.mart_route_card a WHERE a.mom_pct IS NOT NULL AND NOT EXISTS (
                  SELECT 1 FROM mart.mart_route_card p WHERE p.loading_city_gcid = a.loading_city_gcid
                  AND p.unloading_city_gcid = a.unloading_city_gcid AND p.body_group = a.body_group
                  AND p.weight_category = a.weight_category AND p.month = (a.month - INTERVAL '1 month')::date)""")[0][0] == 0
    assert q("SELECT count(*) FROM mart.mart_route_card WHERE p25_trip > median_trip OR median_trip > p75_trip")[0][0] == 0


def test_future_predictions_have_null_actuals():
    rows = q("SELECT count(*) FROM mart.ml_price_predictions WHERE scored_period = 'future' "
             "AND (actual_price_rub IS NOT NULL OR abs_error_rub IS NOT NULL OR ape_pct IS NOT NULL)")
    assert rows[0][0] == 0
