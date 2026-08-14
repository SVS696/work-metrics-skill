# Контракт событий Work Metrics

## Bundle schema 1

```json
{
  "schema": 1,
  "work_item": {
    "id": "CASE-123",
    "project_key": "stable-project-fingerprint",
    "kind": "specification",
    "cycle_kind": "initial-specification",
    "parent_id": null
  },
  "window": {
    "started_at": "2026-08-13T09:00:00+03:00",
    "ended_at": "2026-08-13T12:00:00+03:00",
    "terminal": true
  },
  "policy": {
    "idle_threshold_seconds": 300,
    "pulse_grace_seconds": 30
  },
  "business_calendar": {
    "schema": 1,
    "calendar_id": "project-calendar",
    "timezone": "Europe/Moscow",
    "working_windows": [
      {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
    ],
    "handoff_windows": [
      {"weekdays": [1, 2, 3, 4, 5], "start": "09:00", "end": "18:00"}
    ],
    "holidays": [],
    "production_calendar": {
      "schema": 1,
      "provider": "isdayoff.ru",
      "country": "ru",
      "years": [2026],
      "day_overrides": []
    },
    "day_overrides": []
  },
  "coverage_declaration": "complete",
  "sources": []
}
```

`production_calendar.day_overrides` — материализованный общий календарь страны:
праздники, перенесённые рабочие дни и сокращённые окна. Его строит
`scripts/production_calendar.py`; по умолчанию `isdayoff.ru` сверяется с
`xmlcalendar.ru`, а расхождение блокирует запись. `holidays` и верхнеуровневые
`day_overrides` принадлежат проекту и имеют приоритет над provider data.
Если измеряемое окно выходит за перечисленные `years`, reconciliation завершается
ошибкой до добавления следующего года, а не подменяет календарь обычной неделей.
Персональные отпуска сюда не входят: не начатый work item не запускает таймер,
передача другому владельцу закрывает текущий lifecycle, а ручная пауза остаётся
явным исключением для уже активной, но сознательно отложенной работы.

`cycle_kind` разделяет жизненные циклы одного результата. Для Vigers основной
прогноз использует `initial-specification`: от начала approved execution после
preliminary analysis до первой фактической передачи в разработку. Обратная
связь после этой передачи — новый `post-handoff-followup` с новым `work_item.id`
и `parent_id` исходного case. Поэтому ожидание разработчика между циклами не
становится ни active, ни elapsed временем анализа.

`coverage_declaration=complete` означает, что адаптер перечислил все известные
сессии/харнесы данного work item. Это утверждение проверяется источниками, а не
выводится из наличия хотя бы одного файла.

## Source

```json
{
  "id": "codex-session-01",
  "kind": "harness",
  "required_for_coverage": true,
  "coverage": {
    "status": "complete",
    "started_at": "2026-08-13T09:00:00+03:00",
    "ended_at": "2026-08-13T10:00:00+03:00",
    "reason": null
  },
  "events": []
}
```

Вспомогательные источники (`agent-ledger`, explicit checkpoints) могут иметь
`required_for_coverage=false`. Partial-вспомогательный источник не понижает
полноту, если полный harness-журнал уже доказывает окно.

## Event types

### Наблюдаемый интервал

```json
{
  "id": "run-1",
  "type": "activity_interval",
  "started_at": "2026-08-13T09:05:00+03:00",
  "finished_at": "2026-08-13T09:07:00+03:00",
  "category": "model",
  "attributes": {}
}
```

### Пульс активности

```json
{
  "id": "tool-output-1",
  "type": "activity_pulse",
  "at": "2026-08-13T09:08:00+03:00",
  "category": "tool",
  "attributes": {}
}
```

### Явная пауза

```json
{
  "id": "pause-1",
  "type": "pause_interval",
  "started_at": "2026-08-13T09:30:00+03:00",
  "finished_at": "2026-08-13T09:45:00+03:00",
  "reason": "user_pause"
}
```

### State marker

```json
{
  "id": "limit-1",
  "type": "state_marker",
  "at": "2026-08-13T10:00:00+03:00",
  "state": "limit_exhausted"
}
```

Допустимые состояния: `work_started`, `pause_started`, `limit_exhausted`,
`deferred`, `resume`, `work_finished`, `ready_for_handoff`, `handoff`.

### Наблюдение метрики

```json
{
  "id": "tokens-1",
  "type": "metric_observation",
  "at": "2026-08-13T09:07:00+03:00",
  "metric": "input_tokens",
  "value": 1500,
  "unit": "count",
  "dimensions": {"model": "example"}
}
```

## Результат reconciliation

`metric_results` содержит независимые providers. Обязательный provider
`activity-time` возвращает:

- `active_observed_seconds` — объединённые реальные интервалы/пульсы;
- `active_seconds` — рабочие сессии с короткими межсобытийными разрывами;
- `elapsed_seconds` — полное окно;
- `calendar_elapsed_seconds` — явный alias полного календарного окна;
- `business_elapsed_seconds` — объединение project working windows и реально
  наблюдаемой работы за вычетом отложенного WIP; вне working windows считается
  только фактическая активность, поэтому ночные паузы отдельно отмечать не нужно;
- `scheduled_nonworking_seconds`, `off_schedule_active_seconds` и
  `deferred_seconds` — разложение календарного срока;
- `ready_for_handoff_at`, `handoff_wait_seconds` и
  `handoff_wait_business_seconds` — ожидание между готовностью и передачей;
  доступное рабочее ожидание вычитается из обучаемого
  `business_elapsed_seconds`, а фактическая активность после ready сохраняется и
  даёт reconciliation warning;
- `explicit_pause_seconds`;
- `inferred_idle_seconds`;
- `training_eligible`.

Fingerprint покрывает весь результат кроме самого поля `fingerprint`.
