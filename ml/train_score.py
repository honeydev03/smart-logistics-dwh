"""Этап 3: обучение (time split), метрики и batch-скоринг с записью в mart.ml_price_predictions."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import sqlalchemy as sa
from sklearn.compose import ColumnTransformer
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error, mean_squared_error, r2_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from etl.config import Settings
from etl.db import connect
from ml.features import (CAT_FEATURES, FEATURES, NUM_FEATURES, TARGET, assert_no_leakage, prepare,
                         split_bounds, split_masks)

log = logging.getLogger(__name__)

LOAD_SQL = """
SELECT c.price_id, c.loading_date, c.loading_city_gcid, c.unloading_city_gcid,
       c.distance, c.total_weight, c.total_volume, c.weight_category, c.volume_category,
       c.body_group_primary AS body_group, c.price_rub, c.outlier_mark,
       lc.name AS origin_city, lc.region AS origin_region, lc.latitude AS origin_lat, lc.longitude AS origin_lon,
       uc.name AS destination_city, uc.region AS destination_region, uc.latitude AS dest_lat, uc.longitude AS dest_lon
FROM core.prices_clean c
JOIN core.dim_city lc ON lc.gc_id = c.loading_city_gcid
JOIN core.dim_city uc ON uc.gc_id = c.unloading_city_gcid
WHERE c.is_fact AND NOT c.complex AND c.price_rub > 0 AND c.distance > 0
  AND c.loading_date >= :from_date
ORDER BY c.loading_date, c.price_id
"""


def metrics(y: np.ndarray, p: np.ndarray) -> dict:
    return {
        "rows": int(len(y)),
        "r2": float(r2_score(y, p)),
        "mape_pct": float(mean_absolute_percentage_error(y, p) * 100),
        "rmse_rub": float(np.sqrt(mean_squared_error(y, p))),
        "mae_rub": float(mean_absolute_error(y, p)),
    }


def _ridge(seed: int) -> Pipeline:
    pre = ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore"), CAT_FEATURES),
        ("num", Pipeline([("imp", SimpleImputer(strategy="median")), ("sc", StandardScaler())]), NUM_FEATURES),
    ])
    return Pipeline([("pre", pre), ("model", Ridge(alpha=1.0, random_state=seed))])  # fit только на train


def _py(v):
    return None if v is None or (isinstance(v, float) and v != v) else v


def _write_predictions(settings: Settings, df: pd.DataFrame) -> None:
    cols = ["price_id", "model_name", "loading_date", "loading_city_gcid", "unloading_city_gcid", "body_group",
            "actual_price_rub", "predicted_price_rub", "abs_error_rub", "ape_pct", "scored_period",
            "outlier_mark", "scored_at"]
    data = [df[c].tolist() for c in cols]
    with connect(settings) as conn:
        with conn.transaction():  # атомарная перезапись прогнозов модели
            with conn.cursor() as cur:
                cur.execute("DELETE FROM mart.ml_price_predictions WHERE model_name = %s", (settings.model_name,))
                with cur.copy(f"COPY mart.ml_price_predictions ({', '.join(cols)}) FROM STDIN") as cp:
                    for row in zip(*data):
                        cp.write_row([_py(v) for v in row])


def _write_metrics(settings: Settings, all_metrics: dict[str, dict]) -> None:
    with connect(settings) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                for key, m in all_metrics.items():
                    model, split = key.split("/", 1)
                    cur.execute(
                        "INSERT INTO mart.ml_model_metrics (model_name, split_name, rows_count, r2, mape_pct, rmse_rub, "
                        "mae_rub, as_of, calculated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now()) "
                        "ON CONFLICT (model_name, split_name) DO UPDATE SET rows_count=EXCLUDED.rows_count, "
                        "r2=EXCLUDED.r2, mape_pct=EXCLUDED.mape_pct, rmse_rub=EXCLUDED.rmse_rub, "
                        "mae_rub=EXCLUDED.mae_rub, as_of=EXCLUDED.as_of, calculated_at=now()",
                        (model, split, m["rows"], m["r2"], m["mape_pct"], m["rmse_rub"], m["mae_rub"], settings.as_of))


def run(settings: Settings) -> dict:
    from catboost import CatBoostRegressor

    assert_no_leakage(FEATURES)
    as_of = settings.as_of
    train_start, train_end, _ = split_bounds(as_of)
    log.info("Split: train=[%s, %s), test=[%s, %s]", train_start, train_end, train_end, as_of)

    engine = sa.create_engine(settings.sqlalchemy_url)
    with engine.connect() as c:
        raw = pd.read_sql(sa.text(LOAD_SQL), c, params={"from_date": train_start})
    engine.dispose()
    df = prepare(raw)
    log.info("Загружено строк (факты, прямые, валидные): %s", len(df))

    is_train, is_test = split_masks(df, as_of)
    # Таргет чистим по входному outlier_mark; сам флаг в признаки не попадает.
    train = df[is_train & ~df["outlier_mark"]]
    test_all = df[is_test]
    test = test_all[~test_all["outlier_mark"]]
    if train.empty or test.empty:
        raise RuntimeError(f"Пустая выборка: train={len(train)}, test={len(test)}")
    log.info("train=%s (с учётом outlier_mark), test=%s (без помеченных выбросов)", len(train), len(test))

    # Ridge: бейзлайн (импутация/масштаб/OHE обучаются только на train внутри Pipeline).
    ridge = _ridge(settings.seed).fit(train[FEATURES], train[TARGET])
    cb = CatBoostRegressor(iterations=800, depth=8, learning_rate=0.05, loss_function="RMSE",
                           random_seed=settings.seed, verbose=0, allow_writing_files=False)
    cb.fit(train[FEATURES], train[TARGET], cat_features=CAT_FEATURES)

    last_day = test[pd.to_datetime(test["loading_date"]).dt.date < as_of]
    res = {
        f"ridge/test": metrics(test[TARGET].values, ridge.predict(test[FEATURES])),
        f"{settings.model_name}/test": metrics(test[TARGET].values, cb.predict(test[FEATURES])),
        # доп. срез: последний день снимка ещё не размечен ночным джобом выбросов (ограничение источника)
        f"{settings.model_name}/test_excl_last_day": metrics(last_day[TARGET].values, cb.predict(last_day[FEATURES])),
    }
    for k, m in res.items():
        log.info("метрики %-32s n=%s R2=%.4f MAPE=%.2f%% RMSE=%.0f MAE=%.0f",
                 k, m["rows"], m["r2"], m["mape_pct"], m["rmse_rub"], m["mae_rub"])
    imp = sorted(zip(FEATURES, cb.get_feature_importance()), key=lambda x: -x[1])[:8]
    log.info("топ-признаки CatBoost: %s", [(n, round(float(v), 1)) for n, v in imp])

    # Batch scoring: тест + плановые заявки (loading_date > as_of).
    future = df[pd.to_datetime(df["loading_date"]).dt.date > as_of]
    to_score = pd.concat([test_all.assign(scored_period="test"), future.assign(scored_period="future")])
    pred = cb.predict(to_score[FEATURES])
    out = pd.DataFrame({
        "price_id": to_score["price_id"].astype("int64").values,
        "model_name": settings.model_name,
        "loading_date": pd.to_datetime(to_score["loading_date"]).dt.date.values,
        "loading_city_gcid": to_score["loading_city_gcid"].astype(int).values,
        "unloading_city_gcid": to_score["unloading_city_gcid"].astype(int).values,
        "body_group": to_score["body_group"].values,
        "predicted_price_rub": np.round(pred, 2),
        "scored_period": to_score["scored_period"].values,
        "outlier_mark": to_score["outlier_mark"].astype(bool).values,
        "scored_at": datetime.now(timezone.utc),
    })
    actual = to_score[TARGET].values.astype(float)
    is_test_row = out["scored_period"].eq("test").values
    out["actual_price_rub"] = np.where(is_test_row, actual, np.nan)  # для future строго NULL
    out["abs_error_rub"] = np.where(is_test_row, np.abs(out["predicted_price_rub"].values - actual), np.nan)
    out["ape_pct"] = np.where(is_test_row, out["abs_error_rub"].values / actual * 100, np.nan)

    _write_predictions(settings, out)
    _write_metrics(settings, res)
    log.info("Записано прогнозов: test=%s, future=%s", int(is_test_row.sum()), int((~is_test_row).sum()))

    settings.artifacts_dir.mkdir(parents=True, exist_ok=True)
    cb.save_model(str(settings.artifacts_dir / f"{settings.model_name}.cbm"))
    (settings.artifacts_dir / "metrics.json").write_text(
        json.dumps({"as_of": str(as_of), "metrics": res}, ensure_ascii=False, indent=2), encoding="utf-8")
    return res
