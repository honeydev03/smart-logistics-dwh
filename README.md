# Smart Logistics Data & ML Platform — прототип (ETL → DWH → ML)

Сквозной конвейер по тестовому заданию Middle Data Engineer («Умная Логистика»):
PostgreSQL 17 (слои `raw → core → mart`), ETL на Python + SQL (ELT), витрина коридора ставок и
ML-прогноз тарифа (CatBoost) с batch-скорингом «Факт vs Прогноз».

> **Статус проверки.** Python-код компилируется, юнит-тесты `tests/test_features.py` (split, циклические
> признаки, защита от утечек) пройдены. SQL и интеграционные тесты `tests/test_pipeline_db.py` **не запускались
> против живого PostgreSQL/Docker** (в среде разработки не было ни того, ни другого) — первый
> `docker compose up` и `pytest` на вашей машине и есть проверка. Метрики ML в этом README не вписаны
> намеренно: они считаются при запуске (см. «Метрики»).

## Запуск

```bash
cp .env.example .env          # задайте POSTGRES_PASSWORD
docker compose up --build     # postgres:17 (init: schema.sql + synthetic.sql) → pipeline (ETL → витрины → ML)
```

`pipeline` отрабатывает один раз и завершается. Первая инициализация источника (300 000 сделок) занимает
несколько минут. Данные хранятся в томе `pgdata`; чтобы начать с нуля — `docker compose down -v`.
Размер выборки: сейчас дефолт генератора (300 000); для другого значения измените
`-v n_prices=...` в `synthetic.sql` через собственный init-скрипт.

Повтор и backfill (идемпотентно):

```bash
docker compose run --rm pipeline python -m etl.run_all                                   # полное окно из .env
docker compose run --rm pipeline python -m etl.run_all --start 2026-09-01 --end 2026-09-30   # backfill окна
docker compose run --rm pipeline python -m etl.run_all --skip-ml
docker compose run --rm pipeline pytest -q tests                                          # тесты (БД-тесты — на живой БД)
```

## Архитектура

```
public.* (raw: schema.sql + synthetic.sql)
   │  sql/etl/10_load_core.sql  — DQ, курсы, категории; DELETE окна + INSERT в одной транзакции
   ▼
core.prices_clean · core.price_body_groups · core.dq_quarantine_prices · core.dim_city (view) · core.etl_run_log
   │  sql/dwh/30_mart_route_card.sql — атомарный пересчёт
   ├──▶ mart.mart_route_card  (+ view mart.mart_market_corridor)      ← BI, Вопрос 2
   └──▶ ml/ (features → time split → CatBoost) ─▶ mart.ml_price_predictions, mart.ml_model_metrics   ← Вопрос 3
```

Структура: `etl/` (Python-оркестрация), `sql/etl|dwh/` (ELT-трансформации и DDL), `ml/`, `tests/`,
`docs/` (исходные требования). Подключения — только через переменные окружения (`.env.example`).

**Почему такой стек.** PostgreSQL 17 покрывает и хранилище, и перцентили (`percentile_cont`) в SQL; тяжёлая
работа (DQ, join, агрегаты) выполняется в БД набором SQL (ELT), Python только оркеструет — без построчных
INSERT. CatBoost — нативные категории и устойчивость на табличных данных (по R&D-отчёту). Airflow, Superset и
ClickHouse (задачи со звёздочкой) **не реализованы**.

## Вопрос 1. ETL и Data Quality

Окно `[start, end]` — по `prices.loading_date`. Правила отсева (приоритет сверху вниз, одна первичная причина):

| Код причины | Условие |
|---|---|
| `PRICE_NON_POSITIVE` | `price <= 0` |
| `RUB_PRICE_LE_1000` | `currency_code = '643'` и `price <= 1000` |
| `DISTANCE_NON_POSITIVE` | `distance <= 0` |
| `NEGATIVE_WEIGHT` / `NEGATIVE_VOLUME` | `total_weight < 0` / `total_volume < 0` |
| `CITY_NOT_FOUND` | gc_id отправления или назначения отсутствует в `cities` |
| `UNSUPPORTED_CURRENCY` | валюта вне 643/840/978 |
| `NO_FX_RATE` | USD/EUR без точного курса на `nearest_rate_date` (в т.ч. при `NULL`) |

- Курс: `exchange_rates.setting_date = nearest_rate_date` (точное совпадение), `price_rub = price × value`;
  fallback по `loading_date` не используется. Для RUB курс 1.0.
- Совпадение `loading_city_gcid = unloading_city_gcid` **не** отбраковывается (как в production-job).
- Пропуски `weight_category`/`volume_category` заполняются по правилам задания (вес округляется до 0.1 т).
- `outlier_mark` берётся из источника и не пересчитывается; такие строки хранятся в core с флагом и исключаются
  в витрине и при обучении.
- Баланс строк проверяется в каждом запуске: `прочитано = загружено + в карантине`, иначе транзакция откатывается.
  Статистика отсева логируется и пишется в `core.etl_run_log`.
- **Ограничение источника:** сделки последнего дня снимка (2026-10-26) ещё не прошли ночную разметку аномалий и
  имеют `outlier_mark = false`; выбросы среди них остаются в выборке. Поэтому в ML-метриках есть дополнительный
  срез `test_excl_last_day`. Собственный пересчёт выбросов (аналог `MarkOutlierPrices`) не делался.
- Идемпотентность: `DELETE` окна + `INSERT` в одной транзакции; повторный запуск даёт тот же результат (проверяется тестом).

## Вопрос 2. Витрина `mart.mart_route_card`

Зерно: **месяц (`price_dates.price_date`) × город отправления × город назначения × группа кузова × весовая категория.**

- Фильтр: `is_fact`, `price_type IN (1,2,3,4,5,9)`, `NOT complex`, `NOT outlier_mark`, `price_rub > 0`, `distance > 0`,
  `price_date <= as_of` (плановые будущие месяцы не попадают в историю рынка).
- Города: `core.dim_city` — одна запись на `gc_id` (минимальный `cities.id`); регион — часть `full_name_local` после первой запятой.
- Кузов: `core.price_body_groups` — одна строка на (цена, группа). Тент 56–61, Рефрижератор 4/46/47/48, Изотерм 2, Борт 1/15,
  остальное — «Прочие»; нет строк в `price_body_types` → fallback на `prices.body_type` → «Не указан».
  Сделка с кузовами из разных групп (например, тент + борт) учитывается в **каждой** из групп — это следствие зерна «× группа кузова».
- Метрики: `p25/median/p75/avg` за рейс, `p25/median/p75` за км, `trips_count`, `carriers_count` (`price_type IN (3,9)`),
  `cv_rate` (NULL при <2 наблюдений или нулевом среднем), `mom_pct`/`yoy_pct` по P50 рейса (NULL, если календарно соседней ячейки нет
  или знаменатель 0), `reliability_level`.
- Допущение по «свежести»: `freshness_days = as_of − MAX(price_date)` внутри ячейки; для `HIGH` нужно ≤ 30 дней, поэтому
  ячейки старых месяцев не бывают `HIGH` (интерпретация формулировки «свежесть последнего месяца» неоднозначна — зафиксирована здесь).
- `volume_category` в зерно не включена (не требуется карточке, дробила бы выборки).
- Пересчёт — полный и атомарный (`DELETE` + `INSERT`), поэтому окно ETL на витрину не влияет.

Проверочные запросы:

```sql
-- коридор по маршруту за последние месяцы
SELECT month, body_group, weight_category, p25_trip, median_trip, p75_trip, median_km,
       trips_count, carriers_count, cv_rate, mom_pct, yoy_pct, reliability_level
FROM mart.mart_route_card
WHERE loading_city = 'Москва' AND unloading_city = 'Казань' AND body_group = 'Тент'
ORDER BY month DESC, weight_category LIMIT 24;

-- доля надёжности
SELECT reliability_level, count(*) FROM mart.mart_route_card GROUP BY 1;

-- карантин по причинам
SELECT dq_reason, count(*) FROM core.dq_quarantine_prices GROUP BY 1 ORDER BY 2 DESC;

-- уникальность зерна (ожидается 0)
SELECT count(*) - count(DISTINCT (month, loading_city_gcid, unloading_city_gcid, body_group, weight_category))
FROM mart.mart_route_card;
```

## Вопрос 3. ML

- Таргет: `price_rub`. Выборка: `is_fact`, `NOT complex`, `price_rub > 0`, `distance > 0`; для обучения и оценки исключаются `outlier_mark = true`.
- Split по `loading_date` от фиксированной `as_of = 2026-10-26`: **train** `[2026-07-26, 2026-10-05)`, **test** `[2026-10-05, 2026-10-26]`
  (граница 5 октября — только в test); `shuffle` нигде не используется; тесты проверяют границы.
- Признаки: `distance`, `total_weight`, `total_volume`; город и регион отправления/назначения раздельно, координаты;
  `body_group`, весовая/объёмная категории; `year` и sin/cos месяца (T=12), дня недели (T=7), дня года (T=365.25).
  Замечание: на train-окне из трёх месяцев `year` константен — признак оставлен по заданию, пользы не несёт.
- Утечки: `ml/features.py::assert_no_leakage` блокирует `price_with_vat`, удельные ставки, маржу/прибыль, `price_group_stats`
  (`avg_price`, `min/max_km_price`, `price_group_id`), `outlier_mark` как признак. Импутер/скейлер/OHE бейзлайна обучаются только на train (внутри `Pipeline`).
- Модели: `CatBoostRegressor` (основная, `random_seed` из `RANDOM_SEED`) и `Ridge` как бейзлайн.
- Скоринг: тестовый период и плановые заявки (`loading_date > as_of`) → `mart.ml_price_predictions`
  (для `future` поля `actual_price_rub`, `abs_error_rub`, `ape_pct` = NULL); перезапись прогнозов модели атомарна (`DELETE` + `COPY`).
  Сверх требуемых колонок добавлены `scored_period` и `outlier_mark` (чтобы отличать тест от плана и подсвечивать помеченные рейсы).

### Метрики

Считаются при запуске и пишутся в лог, `artifacts/metrics.json` и `mart.ml_model_metrics`:

```sql
SELECT model_name, split_name, rows_count, r2, mape_pct, rmse_rub, mae_rub FROM mart.ml_model_metrics;
```

Ориентиры из R&D-отчёта: CatBoost R² ≈ 0.93, MAPE ≈ 13 %; точные значения на синтетике зависят от окна и признаков.

```sql
-- Факт vs Прогноз: рейсы, закрытые заметно выше прогноза
SELECT price_id, loading_date, body_group, actual_price_rub, predicted_price_rub, ape_pct
FROM mart.ml_price_predictions
WHERE scored_period = 'test' AND actual_price_rub > predicted_price_rub * 1.3
ORDER BY ape_pct DESC LIMIT 20;
```

## Известные ограничения

- Не реализованы Airflow, Superset, ClickHouse (бонус).
- Интеграционные тесты требуют запущенной БД; без неё пропускаются.
- Загрузка в core идёт окнами по `loading_date`, а витрина строится по `price_dates` — поэтому она пересчитывается целиком.
