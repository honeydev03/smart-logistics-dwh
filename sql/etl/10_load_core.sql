-- Идемпотентная загрузка окна [start, end] по prices.loading_date: DELETE окна + INSERT в одной транзакции.
-- Параметры: %(start)s, %(end)s. Курс валюты берётся по ТОЧНОМУ совпадению с nearest_rate_date
-- (fallback по loading_date не используется). Литерала процента в файле нет (client-side binding).

CREATE TEMP TABLE stg_prices ON COMMIT DROP AS
SELECT p.id AS price_id, p.loading_date, p.end_loading_date, p.created_at,
       p.loading_city_gcid, p.unloading_city_gcid, p.distance, p.total_weight, p.total_volume,
       p.body_type, p.organization_id, p.price_type, p.is_fact, p.complex, p.outlier_mark,
       p.currency_code, p.price, p.nearest_rate_date,
       p.weight_category, p.volume_category,
       CASE WHEN p.currency_code = '643' THEN 1.0 ELSE fx.value END AS fx_rate,
       -- первичная причина отсева: порядок правил = приоритет
       CASE
           WHEN p.price <= 0                                   THEN 'PRICE_NON_POSITIVE'
           WHEN p.currency_code = '643' AND p.price <= 1000    THEN 'RUB_PRICE_LE_1000'
           WHEN p.distance <= 0                                THEN 'DISTANCE_NON_POSITIVE'
           WHEN p.total_weight < 0                             THEN 'NEGATIVE_WEIGHT'
           WHEN p.total_volume < 0                             THEN 'NEGATIVE_VOLUME'
           WHEN lc.gc_id IS NULL OR uc.gc_id IS NULL           THEN 'CITY_NOT_FOUND'
           WHEN p.currency_code NOT IN ('643', '840', '978')   THEN 'UNSUPPORTED_CURRENCY'
           WHEN p.currency_code <> '643' AND fx.value IS NULL  THEN 'NO_FX_RATE'
       END AS dq_reason
FROM public.prices p
LEFT JOIN core.dim_city lc ON lc.gc_id = p.loading_city_gcid
LEFT JOIN core.dim_city uc ON uc.gc_id = p.unloading_city_gcid
LEFT JOIN LATERAL (
    SELECT er.value
    FROM public.exchange_rates er
    WHERE er.num_code = p.currency_code
      AND er.setting_date = p.nearest_rate_date
    ORDER BY er.id DESC
    LIMIT 1
) fx ON p.currency_code <> '643'
WHERE p.loading_date BETWEEN %(start)s AND %(end)s;

-- Перезапись окна (каскад чистит core.price_body_groups).
DELETE FROM core.dq_quarantine_prices WHERE loading_date BETWEEN %(start)s AND %(end)s;
DELETE FROM core.prices_clean         WHERE loading_date BETWEEN %(start)s AND %(end)s;

INSERT INTO core.dq_quarantine_prices
    (price_id, loading_date, dq_reason, currency_code, price, distance, total_weight, total_volume,
     loading_city_gcid, unloading_city_gcid, nearest_rate_date)
SELECT price_id, loading_date, dq_reason, currency_code, price, distance, total_weight, total_volume,
       loading_city_gcid, unloading_city_gcid, nearest_rate_date
FROM stg_prices
WHERE dq_reason IS NOT NULL;

INSERT INTO core.prices_clean
    (price_id, loading_date, end_loading_date, created_at, loading_city_gcid, unloading_city_gcid,
     distance, total_weight, total_volume, body_type, body_group_primary, organization_id, price_type,
     is_fact, complex, outlier_mark, currency_code, price_src, fx_rate, price_rub,
     weight_category, volume_category)
SELECT s.price_id, s.loading_date, s.end_loading_date, s.created_at, s.loading_city_gcid, s.unloading_city_gcid,
       s.distance, s.total_weight, s.total_volume, s.body_type,
       CASE WHEN s.body_type IS NULL THEN 'Не указан' ELSE core.body_group_of(s.body_type, bt.std_id) END,
       s.organization_id, s.price_type, s.is_fact, s.complex, s.outlier_mark,
       s.currency_code, s.price, s.fx_rate,
       round((s.price * s.fx_rate)::numeric, 2)::double precision,
       COALESCE(s.weight_category, core.weight_category(s.total_weight)),
       COALESCE(s.volume_category, core.volume_category(s.total_volume))
FROM stg_prices s
LEFT JOIN public.body_types bt ON bt.id = s.body_type
WHERE s.dq_reason IS NULL;

-- Группы кузова: DISTINCT по (цена, группа) убирает дубли (напр. тент 56 + тент бок 57).
-- Нет строк в price_body_types -> fallback на prices.body_type, затем 'Не указан'.
INSERT INTO core.price_body_groups (price_id, body_group)
SELECT DISTINCT c.price_id, core.body_group_of(pbt.body_type_id, bt.std_id)
FROM core.prices_clean c
JOIN public.price_body_types pbt ON pbt.price_id = c.price_id
LEFT JOIN public.body_types bt ON bt.id = pbt.body_type_id
WHERE c.loading_date BETWEEN %(start)s AND %(end)s
UNION
SELECT c.price_id, c.body_group_primary
FROM core.prices_clean c
WHERE c.loading_date BETWEEN %(start)s AND %(end)s
  AND NOT EXISTS (SELECT 1 FROM public.price_body_types x WHERE x.price_id = c.price_id);
