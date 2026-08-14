# Адаптер Vigers

`scripts/vigers_adapter.py` — тонкая граница. Он не импортирует Vigers и не
изменяет case package.

## Входы

- `automation-timing.json`: окно, explicit pauses, publication и handoff;
- `agent-ledger.json`: точные интервалы модельных проходов и счётчики;
- один или несколько timestamped JSONL журналов Codex/Claude/другого харнеса;
- `timing-forecast.json`: `project_key` и связь с исходным прогнозом.
- опциональный `.vigers/timing-calendar.json`: рабочие и handoff-окна проекта;
  без него сохраняются legacy active/elapsed, но business elapsed не вычисляется.

Timestamped JSONL должен иметь `timestamp` или `at` на верхнем уровне. Записи
`session_meta` и `turn_context` считаются метаданными; остальные записи дают
activity pulse. Точные agent intervals имеют приоритет над пульсами только по
детализации; объединение времени не считает пересечение дважды.

## Полнота

Без `--logs-complete` результат всегда partial и не допускается к обучению.
Флаг означает, что переданы все известные журналы всех сессий/харнесов work
item. Если это неизвестно, флаг запрещён.

## Порядок

```text
python3 scripts/vigers_adapter.py reconcile \
  --case-root <case-root> \
  --forecast <case-root>/timing-forecast.json \
  --business-calendar <project-root>/.vigers/timing-calendar.json \
  --harness-log <codex-or-claude-session.jsonl> \
  --harness-log <another-session.jsonl> \
  --logs-complete \
  --write <case-root>/activity-reconciliation.json

python3 <vigers>/scripts/timing_model.py update \
  --profile-id <profile> --project-root <root> \
  --mode-decision <case-root>/mode-decision.json \
  --plan <planning-root>/plan.json \
  --ledger <case-root>/automation-timing.json \
  --forecast <case-root>/timing-forecast.json \
  --activity-reconciliation <case-root>/activity-reconciliation.json
```

Основной вызов имеет `--cycle-kind initial-specification` по умолчанию и
обрывает окно ровно на первом `development_handoff`; записи журналов после этой
точки игнорируются. Если после передачи разработчики принесли новые факты,
создай отдельный follow-up case в момент фактического возобновления анализа и
измерь его отдельно:

```text
python3 scripts/vigers_adapter.py reconcile \
  --case-root <followup-case-root> \
  --project-key <same-project-key> \
  --cycle-kind post-handoff-followup \
  --parent-case-id <original-case-id> \
  --harness-log <followup-session.jsonl> --logs-complete \
  --write <followup-case-root>/activity-reconciliation.json
```

Такой результат остаётся доступен для общей аналитики Work Metrics, но Vigers
не добавляет его в модель времени первоначальной постановки.

При partial recovery Vigers продолжает использовать собственный explicit
dual-timer факт. Он не подменяет измерение неполной реконструкцией.

Последний `ready_for_handoff` отделяет завершение аналитической работы от
организационного ожидания передачи. `deferred` остаётся в raw calendar elapsed,
но исключается из business elapsed. Поэтому пятница-вечер → понедельник не
становится дополнительной трудоёмкостью, а длительная задача в backlog не
обучает модель месяцам фиктивной работы.
