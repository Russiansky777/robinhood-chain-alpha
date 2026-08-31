# Результаты — Sprint 1 kill-тест

Сгенерировано автоматически `analysis/run_pipeline.py` в {{GENERATED_AT}}.
Период: июль 2026 (train) → август 2026 (test). Параметры гейтов:
MIN_TRADES={{MIN_TRADES}}, MIN_UNIQUE_TOKENS={{MIN_UNIQUE_TOKENS}},
SNIPER_BLOCK_WINDOW={{SNIPER_BLOCK_WINDOW}}, COHORT_SIZE={{COHORT_SIZE}}.

## 1. Сводная статистика по когортам

| Метрика | Когорта А (топ July PnL) | Когорта Б (контроль) |
|---|---|---|
| N кошельков | {{N_A}} | {{N_B}} |
| Медиана July PnL | {{JULY_MEDIAN_A}} | {{JULY_MEDIAN_B}} |
| Медиана August PnL | {{AUG_MEDIAN_A}} | {{AUG_MEDIAN_B}} |
| Среднее August PnL | {{AUG_MEAN_A}} | {{AUG_MEAN_B}} |
| Доля профитных в августе (PnL>0) | {{PCT_PROFITABLE_A}} | {{PCT_PROFITABLE_B}} |
| Медиана числа сделок (июль) | {{JULY_TRADES_MEDIAN_A}} | {{JULY_TRADES_MEDIAN_B}} |
| Медиана уник. токенов (июль) | {{JULY_TOKENS_MEDIAN_A}} | {{JULY_TOKENS_MEDIAN_B}} |

Всего кошельков прошло гейты 1-2 (снайперы/инсайдеры исключены,
≥{{MIN_TRADES}} сделок, ≥{{MIN_UNIQUE_TOKENS}} токенов): {{TOTAL_GATED}}.
Исключено как снайперы/инсайдеры: {{TOTAL_EXCLUDED}}.

## 2. Статистический тест (главный вывод спринта)

- Mann-Whitney U (one-sided, A > B): U = {{MWU_U}}, p = {{MWU_P}}
- Rank-biserial effect size: {{EFFECT_SIZE}}
- Bootstrap 95% CI разницы медиан (A − B), August PnL: [{{BOOT_CI_LOW}}, {{BOOT_CI_HIGH}}]
- Доля профитных: A = {{PCT_PROFITABLE_A}}, Б = {{PCT_PROFITABLE_B}} (Fisher exact p = {{FISHER_P}})
- Spearman ρ(July rank, August rank), все кошельки после гейтов 1-2: ρ = {{SPEARMAN_RHO}}, p = {{SPEARMAN_P}}

## 3. Топ-20 персистентных кошельков

Пересечение "топ по July PnL" (когорта А) и "топ по August PnL" внутри
неё же, отсортировано по августовскому PnL.

{{TOP20_TABLE}}

## 4. Вердикт

**{{VERDICT}}**

{{VERDICT_REASONING}}

## 5. Sensitivity-анализ гейта шума (MIN_TRADES = 10 vs 15)

| MIN_TRADES | Размер пула после гейтов 1-2 | p-value Mann-Whitney |
|---|---|---|
| 10 | {{SENS_10_N}} | {{SENS_10_P}} |
| 15 | {{SENS_15_N}} | {{SENS_15_P}} |
