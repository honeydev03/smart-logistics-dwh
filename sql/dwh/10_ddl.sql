-- DDL слоёв core / mart. Полностью идемпотентен (IF NOT EXISTS / OR REPLACE).
-- Слои: raw = схема public (источник) -> core (очищенные факты) -> mart (витрины BI и ML).
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;

-- Категории: правила из задания (вес округляется до 0.1 т).
CREATE OR REPLACE FUNCTION core.weight_category(w double precision) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE
        WHEN w IS NULL THEN NULL
        WHEN round(w::numeric, 1) >= 30  THEN 'G20T'
        WHEN round(w::numeric, 1) >= 10  THEN 'FTL'
        WHEN round(w::numeric, 1) >= 5   THEN '10T'
        WHEN round(w::numeric, 1) >= 2.5 THEN '5T'
        WHEN round(w::numeric, 1) >= 0.5 THEN 'L5T'
        ELSE 'OTH' END
$$;

CREATE OR REPLACE FUNCTION core.volume_category(v double precision) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN v IS NULL THEN NULL WHEN v > 0 AND v <= 120 THEN 'FTL' ELSE 'OTH' END
$$;

-- Стандартная группа кузова по коду 1С. std_id из body_types приоритетнее,
-- при его отсутствии используется явная карта id -> std_id из требований.
CREATE OR REPLACE FUNCTION core.body_group_of(body_id integer, std integer) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE COALESCE(std, CASE
                WHEN body_id IN (56, 57, 58, 59, 60, 61) THEN 56
                WHEN body_id IN (4, 46, 47, 48)          THEN 4
                WHEN body_id = 2                         THEN 2
                WHEN body_id IN (1, 15)                  THEN 15 END)
        WHEN 56 THEN 'Тент'
        WHEN 4  THEN 'Рефрижератор'
        WHEN 2  THEN 'Изотерм'
        WHEN 15 THEN 'Борт'
        ELSE 'Прочие' END
$$;

-- Дедуплицированный справочник городов: один gc_id -> запись с минимальным cities.id.
-- Регион = часть full_name_local после первой запятой ('Казань, Республика Татарстан').
CREATE OR REPLACE VIEW core.dim_city AS
SELECT DISTINCT ON (c.gc_id)
       c.gc_id,
       c.id AS city_id,
       c.name,
       c.full_name_local,
       CASE WHEN strpos(c.full_name_local, ',') > 0
            THEN NULLIF(btrim(substr(c.full_name_local, strpos(c.full_name_local, ',') + 1)), '')
       END AS region,
       c.latitude,
       c.longitude
FROM public.cities c
ORDER BY c.gc_id, c.id;

-- ---------- core ----------
CREATE TABLE IF NOT EXISTS core.prices_clean (
    price_id            bigint PRIMARY KEY,
    loading_date        date             NOT NULL,
    end_loading_date    date,
    created_at          timestamp(0),
    loading_city_gcid   integer          NOT NULL,
    unloading_city_gcid integer          NOT NULL,
    distance            double precision NOT NULL,
    total_weight        double precision NOT NULL,
    total_volume        double precision NOT NULL,
    body_type           integer,
    body_group_primary  text             NOT NULL,   -- группа основного prices.body_type (для ML)
    organization_id     integer,
    price_type          smallint         NOT NULL,
    is_fact             boolean          NOT NULL,
    complex             boolean          NOT NULL,
    outlier_mark        boolean          NOT NULL,   -- входной флаг источника, не пересчитывается
    currency_code       varchar(3)       NOT NULL,
    price_src           double precision NOT NULL,   -- исходная ставка в валюте сделки
    fx_rate             double precision,            -- курс к RUB (1.0 для 643)
    price_rub           double precision NOT NULL,   -- ставка за рейс в RUB без НДС
    weight_category     varchar(4)       NOT NULL,
    volume_category     varchar(4)       NOT NULL,
    loaded_at           timestamptz      NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS prices_clean_loading_date_idx ON core.prices_clean (loading_date);
CREATE INDEX IF NOT EXISTS prices_clean_route_idx ON core.prices_clean (loading_city_gcid, unloading_city_gcid);

-- Одна строка на (цена, стандартная группа кузова): дубли внутри группы исключены PK.
CREATE TABLE IF NOT EXISTS core.price_body_groups (
    price_id   bigint NOT NULL REFERENCES core.prices_clean (price_id) ON DELETE CASCADE,
    body_group text   NOT NULL,
    PRIMARY KEY (price_id, body_group)
);

-- Карантин: отбракованные строки с кодом причины (не теряются бесследно).
CREATE TABLE IF NOT EXISTS core.dq_quarantine_prices (
    price_id            bigint PRIMARY KEY,
    loading_date        date        NOT NULL,
    dq_reason           text        NOT NULL,
    currency_code       varchar(3),
    price               double precision,
    distance            double precision,
    total_weight        double precision,
    total_volume        double precision,
    loading_city_gcid   integer,
    unloading_city_gcid integer,
    nearest_rate_date   date,
    quarantined_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS dq_quarantine_reason_idx ON core.dq_quarantine_prices (dq_reason);

-- Журнал запусков (для наблюдаемости; append-only, на данные не влияет).
CREATE TABLE IF NOT EXISTS core.etl_run_log (
    id               bigserial PRIMARY KEY,
    step             text        NOT NULL,
    window_start     date,
    window_end       date,
    rows_read        bigint,
    rows_loaded      bigint,
    rows_quarantined bigint,
    details          jsonb,
    finished_at      timestamptz NOT NULL DEFAULT now()
);

-- ---------- mart ----------
CREATE TABLE IF NOT EXISTS mart.mart_route_card (
    month               date        NOT NULL,
    loading_city_gcid   integer     NOT NULL,
    unloading_city_gcid integer     NOT NULL,
    body_group          text        NOT NULL,
    weight_category     varchar(4)  NOT NULL,
    loading_city        text,
    unloading_city      text,
    p25_trip            double precision,
    median_trip         double precision,
    p75_trip            double precision,
    avg_trip            double precision,
    p25_km              double precision,
    median_km           double precision,
    p75_km              double precision,
    trips_count         integer     NOT NULL,
    carriers_count      integer     NOT NULL,
    cv_rate             double precision,
    last_price_date     date        NOT NULL,
    freshness_days      integer     NOT NULL,
    mom_pct             numeric(10, 2),
    yoy_pct             numeric(10, 2),
    reliability_level   text        NOT NULL CHECK (reliability_level IN ('HIGH', 'MEDIUM', 'LOW_DATA')),
    as_of               date        NOT NULL,
    refreshed_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (month, loading_city_gcid, unloading_city_gcid, body_group, weight_category)
);
CREATE INDEX IF NOT EXISTS mart_route_card_route_idx
    ON mart.mart_route_card (loading_city_gcid, unloading_city_gcid, body_group, month);

-- Компактный срез «рыночный коридор» для BI-таблицы по сети направлений.
CREATE OR REPLACE VIEW mart.mart_market_corridor AS
SELECT month, loading_city, unloading_city, body_group, weight_category,
       p25_trip, median_trip, p75_trip, p25_km, median_km, p75_km,
       mom_pct, yoy_pct, trips_count, carriers_count, cv_rate, reliability_level
FROM mart.mart_route_card;

CREATE TABLE IF NOT EXISTS mart.ml_price_predictions (
    price_id            bigint           NOT NULL,
    model_name          text             NOT NULL,
    loading_date        date             NOT NULL,
    loading_city_gcid   integer          NOT NULL,
    unloading_city_gcid integer          NOT NULL,
    body_group          text             NOT NULL,
    actual_price_rub    double precision,           -- NULL для будущих заявок
    predicted_price_rub double precision NOT NULL,
    abs_error_rub       double precision,           -- NULL для будущих заявок
    ape_pct             double precision,           -- NULL для будущих заявок
    scored_period       text             NOT NULL CHECK (scored_period IN ('test', 'future')),
    outlier_mark        boolean          NOT NULL,
    scored_at           timestamptz      NOT NULL,
    PRIMARY KEY (price_id, model_name)
);
CREATE INDEX IF NOT EXISTS ml_pred_date_idx ON mart.ml_price_predictions (loading_date);

CREATE TABLE IF NOT EXISTS mart.ml_model_metrics (
    model_name  text             NOT NULL,
    split_name  text             NOT NULL,
    rows_count  integer          NOT NULL,
    r2          double precision,
    mape_pct    double precision,
    rmse_rub    double precision,
    mae_rub     double precision,
    as_of       date             NOT NULL,
    calculated_at timestamptz    NOT NULL DEFAULT now(),
    PRIMARY KEY (model_name, split_name)
);
