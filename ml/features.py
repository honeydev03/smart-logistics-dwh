"""Feature pipeline: признаки без утечек, циклическое кодирование дат, хронологический split."""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

TARGET = "price_rub"

CAT_FEATURES = [
    "origin_city", "destination_city", "origin_region", "destination_region",
    "body_group", "weight_category", "volume_category",
]
NUM_FEATURES = [
    "distance", "total_weight", "total_volume",
    "origin_lat", "origin_lon", "dest_lat", "dest_lon",
    "year", "month_sin", "month_cos", "dow_sin", "dow_cos", "doy_sin", "doy_cos",
]
FEATURES = CAT_FEATURES + NUM_FEATURES

# Всё, что прямо или косвенно производно от целевого тарифа текущей сделки.
FORBIDDEN_FEATURES = {
    TARGET, "price", "price_src", "price_with_vat", "price_km", "price_per_km", "price_per_ton",
    "carrier_cost", "customer_cost", "transportation_cost", "profit",
    "price_group_id", "avg_price", "min_km_price", "max_km_price",
    "outlier_mark", "fx_rate",
}


def assert_no_leakage(features: list[str]) -> None:
    bad = sorted(set(features) & FORBIDDEN_FEATURES)
    if bad:
        raise ValueError(f"Утечка таргета: запрещённые признаки {bad}")


def cyclical(t: np.ndarray | pd.Series, period: float) -> tuple[np.ndarray, np.ndarray]:
    angle = 2.0 * np.pi * np.asarray(t, dtype=float) / period
    return np.sin(angle), np.cos(angle)


def add_time_features(df: pd.DataFrame, col: str = "loading_date") -> pd.DataFrame:
    d = pd.to_datetime(df[col])
    out = df.copy()
    out["year"] = d.dt.year
    out["month_sin"], out["month_cos"] = cyclical(d.dt.month, 12)
    out["dow_sin"], out["dow_cos"] = cyclical(d.dt.dayofweek, 7)
    out["doy_sin"], out["doy_cos"] = cyclical(d.dt.dayofyear, 365.25)
    return out


def split_bounds(as_of: date) -> tuple[date, date, date]:
    """train = [as_of-3 мес, as_of-3 нед), test = [as_of-3 нед, as_of]; as_of фиксирован, не now()."""
    train_start = (pd.Timestamp(as_of) - pd.DateOffset(months=3)).date()
    train_end = as_of - timedelta(weeks=3)  # входит только в test
    return train_start, train_end, as_of


def split_masks(df: pd.DataFrame, as_of: date) -> tuple[pd.Series, pd.Series]:
    train_start, train_end, test_end = split_bounds(as_of)
    d = pd.to_datetime(df["loading_date"]).dt.date
    train = (d >= train_start) & (d < train_end)
    test = (d >= train_end) & (d <= test_end)
    return train, test


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Добавляет время-признаки и приводит категории к строкам без NaN."""
    out = add_time_features(df)
    for c in CAT_FEATURES:
        out[c] = out[c].fillna("Не указан").astype(str)
    return out
