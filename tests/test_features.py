"""Юнит-тесты feature pipeline (БД не нужна)."""
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ml.features import (FEATURES, FORBIDDEN_FEATURES, add_time_features, assert_no_leakage, cyclical,
                         split_bounds, split_masks)


def test_split_bounds_fixed_as_of():
    start, end_train, as_of = split_bounds(date(2026, 10, 26))
    assert start == date(2026, 7, 26)
    assert end_train == date(2026, 10, 5)
    assert as_of == date(2026, 10, 26)


def test_split_boundaries_are_exclusive_for_train_and_inclusive_for_test():
    dates = [date(2026, 7, 25), date(2026, 7, 26), date(2026, 10, 4), date(2026, 10, 5),
             date(2026, 10, 26), date(2026, 10, 27)]
    df = pd.DataFrame({"loading_date": dates})
    train, test = split_masks(df, date(2026, 10, 26))
    assert train.tolist() == [False, True, True, False, False, False]
    assert test.tolist() == [False, False, False, True, True, False]  # 10-05 только в test, будущее вне обоих


def test_no_overlap_and_chronological():
    df = pd.DataFrame({"loading_date": pd.date_range("2026-06-01", "2026-11-30")})
    train, test = split_masks(df, date(2026, 10, 26))
    assert not (train & test).any()
    assert df[train]["loading_date"].max() < df[test]["loading_date"].min()


def test_cyclical_neighbors_close_on_year_boundary():
    s, c = cyclical(np.array([365, 1]), 365.25)
    dist = np.hypot(s[0] - s[1], c[0] - c[1])
    far_s, far_c = cyclical(np.array([1, 183]), 365.25)
    assert dist < 0.05 < np.hypot(far_s[0] - far_s[1], far_c[0] - far_c[1])


def test_time_features_columns_and_unit_circle():
    df = add_time_features(pd.DataFrame({"loading_date": ["2026-12-31", "2027-01-01"]}))
    for a, b in (("month_sin", "month_cos"), ("dow_sin", "dow_cos"), ("doy_sin", "doy_cos")):
        np.testing.assert_allclose(df[a] ** 2 + df[b] ** 2, 1.0)
    assert df["year"].tolist() == [2026, 2027]


def test_features_have_no_leakage():
    assert_no_leakage(FEATURES)
    assert not set(FEATURES) & FORBIDDEN_FEATURES


@pytest.mark.parametrize("bad", ["price_with_vat", "avg_price", "price_group_id", "min_km_price",
                                 "max_km_price", "price_rub", "outlier_mark"])
def test_leakage_guard_rejects_forbidden(bad):
    with pytest.raises(ValueError):
        assert_no_leakage(FEATURES + [bad])
