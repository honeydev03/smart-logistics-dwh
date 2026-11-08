-- Витрина «Карточка маршрута и коридор рыночных ставок».
-- Зерно: месяц (price_dates.price_date) x город отправления x город назначения x группа кузова x весовая категория.
-- volume_category в зерно не входит (не обязательна для BI-карточки и дробила бы выборки без выгоды).
-- Пересчитывается целиком атомарно (DELETE + INSERT в одной транзакции) -> повторный запуск не задваивает строки.
-- Параметр: %(as_of)s.

DELETE FROM mart.mart_route_card;

INSERT INTO mart.mart_route_card
    (month, loading_city_gcid, unloading_city_gcid, body_group, weight_category, loading_city, unloading_city,
     p25_trip, median_trip, p75_trip, avg_trip, p25_km, median_km, p75_km,
     trips_count, carriers_count, cv_rate, last_price_date, freshness_days,
     mom_pct, yoy_pct, reliability_level, as_of)
WITH base AS (
    -- одна строка на (цена, месяц, группа кузова): цена не дублируется внутри группы
    SELECT DISTINCT ON (c.price_id, date_trunc('month', pd.price_date), g.body_group)
           date_trunc('month', pd.price_date)::date AS month,
           c.price_id, c.loading_city_gcid, c.unloading_city_gcid, g.body_group, c.weight_category,
           c.organization_id, c.price_type, c.price_rub,
           c.price_rub / NULLIF(c.distance, 0) AS price_km,
           pd.price_date
    FROM core.prices_clean c
    JOIN public.price_dates pd       ON pd.price_id = c.price_id
    JOIN core.price_body_groups g    ON g.price_id  = c.price_id
    WHERE c.is_fact
      AND c.price_type IN (1, 2, 3, 4, 5, 9)
      AND NOT c.complex
      AND NOT c.outlier_mark
      AND c.price_rub > 0
      AND c.distance > 0
      AND pd.price_date <= %(as_of)s::date          -- плановые месяцы из будущего не входят в историю рынка
    ORDER BY c.price_id, date_trunc('month', pd.price_date), g.body_group, pd.price_date DESC
),
agg AS (
    SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY price_rub) AS p25_trip,
           percentile_cont(0.50) WITHIN GROUP (ORDER BY price_rub) AS median_trip,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY price_rub) AS p75_trip,
           avg(price_rub)                                           AS avg_trip,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY price_km)   AS p25_km,
           percentile_cont(0.50) WITHIN GROUP (ORDER BY price_km)   AS median_km,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY price_km)   AS p75_km,
           count(DISTINCT price_id)::int                            AS trips_count,
           count(DISTINCT organization_id) FILTER (WHERE price_type IN (3, 9))::int AS carriers_count,
           CASE WHEN count(*) < 2 OR avg(price_rub) = 0 THEN NULL
                ELSE stddev_samp(price_rub) / avg(price_rub) END    AS cv_rate,
           max(price_date)                                          AS last_price_date
    FROM base
    GROUP BY month, loading_city_gcid, unloading_city_gcid, body_group, weight_category
)
SELECT a.month, a.loading_city_gcid, a.unloading_city_gcid, a.body_group, a.weight_category,
       lc.name, uc.name,
       a.p25_trip, a.median_trip, a.p75_trip, a.avg_trip, a.p25_km, a.median_km, a.p75_km,
       a.trips_count, a.carriers_count, a.cv_rate, a.last_price_date,
       (%(as_of)s::date - a.last_price_date)::int AS freshness_days,
       -- MoM/YoY только по календарно соседней ячейке; нет ячейки или знаменатель 0 -> NULL
       CASE WHEN pm.median_trip IS NULL OR pm.median_trip = 0 THEN NULL
            ELSE round(((a.median_trip / pm.median_trip - 1) * 100)::numeric, 2) END AS mom_pct,
       CASE WHEN py.median_trip IS NULL OR py.median_trip = 0 THEN NULL
            ELSE round(((a.median_trip / py.median_trip - 1) * 100)::numeric, 2) END AS yoy_pct,
       CASE WHEN a.trips_count >= 30 AND a.carriers_count >= 5
                 AND (%(as_of)s::date - a.last_price_date) <= 30 THEN 'HIGH'
            WHEN a.trips_count >= 10 THEN 'MEDIUM'
            ELSE 'LOW_DATA' END AS reliability_level,
       %(as_of)s::date
FROM agg a
LEFT JOIN agg pm ON pm.loading_city_gcid = a.loading_city_gcid AND pm.unloading_city_gcid = a.unloading_city_gcid
                AND pm.body_group = a.body_group AND pm.weight_category = a.weight_category
                AND pm.month = (a.month - INTERVAL '1 month')::date
LEFT JOIN agg py ON py.loading_city_gcid = a.loading_city_gcid AND py.unloading_city_gcid = a.unloading_city_gcid
                AND py.body_group = a.body_group AND py.weight_category = a.weight_category
                AND py.month = (a.month - INTERVAL '1 year')::date
LEFT JOIN core.dim_city lc ON lc.gc_id = a.loading_city_gcid
LEFT JOIN core.dim_city uc ON uc.gc_id = a.unloading_city_gcid;
