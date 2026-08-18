---
name: work-metrics
description: "Собирает и согласует журналы активности из разных сессий, харнесов, моделей и инструментов, восстанавливает активные интервалы, паузы, проектные рабочие окна и отложенное состояние, оценивает полноту покрытия и считает расширяемые метрики работы. Используй для active/business/calendar таймеров, постфактум-восстановления активности, сверки checkpoint-ов, проектной калибровки времени, стоимости и finding-yield ролевых проходов; Vigers и Delivery Engineering подключаются как адаптеры, но модуль от них не зависит."
---

# Work Metrics

Считай метрики по наблюдаемым событиям, а не по открытому секундомеру. Храни
универсальное ядро отдельно от адаптеров конкретных процессов.

## Порядок работы

1. Определи один `work_item.id` и устойчивый `project_key`.
2. Нормализуй источники в bundle по `references/event-contract.md`.
3. Пометь полноту каждого журнала и общую `coverage_declaration`. Не называй
   неполную выборку полной ради пригодности к обучению.
4. Выполни:

   ```text
   python3 {baseDir}/scripts/work_metrics.py reconcile \
     --bundle <activity-bundle.json> --write <activity-reconciliation.json>
   ```

5. Используй `active_seconds` как восстановленную чистую работу,
   `business_elapsed_seconds` как срок в рабочих окнах проекта без явного
   `deferred`, `calendar_elapsed_seconds` как полный календарный интервал, а
   `active_observed_seconds` — как доказанную нижнюю границу. Business-метрики
   доступны только при переданном project calendar. Наблюдаемая работа вне
   рабочих окон добавляется в active и business; фон между событиями — нет.
6. Допускай результат к проектной калибровке только при
   `training_eligible: true`. Partial recovery годится для ретроспективы, но не
   для автоматического обучения.

## Производственный календарь

Календарь принадлежит проекту, но не привязан к конкретному процессу. Для
поддерживаемой страны материализуй нужные годы после настройки недельных окон:

```text
python3 {baseDir}/scripts/production_calendar.py materialize \
  --calendar <project-root>/.vigers/timing-calendar.json \
  --country ru --year 2026 \
  --write <project-root>/.vigers/timing-calendar.json
```

Основной источник — `isdayoff.ru`, независимая сверка — `xmlcalendar.ru`;
расхождение закрывает обновление без записи. Общие overrides покрывают праздники,
рабочие переносы и сокращённые дни. Верхнеуровневые project `holidays` и
`day_overrides` имеют приоритет. Персональные отпуска не вноси: не начатая задача
не запускает часы, переданная задача меняет lifecycle/owner, а ручная пауза нужна
только для исключения, когда активная задача осталась на прежнем владельце.

## Источники и приоритет

- Явная пауза или остановка сильнее вычисленного состояния.
- `limit_exhausted` открывает паузу; новое наблюдаемое действие может закрыть её
  с предупреждением `implicit_resume_from_activity`.
- Интервалы модели и инструментов являются наблюдаемой активностью.
- Пульсы событий объединяются в рабочую сессию, если разрыв не превышает
  `idle_threshold_seconds`; больший разрыв становится вычисленным простоем.
- Вне project working windows не требуй ручных pause-маркеров: при отсутствии
  наблюдаемой активности там и так не возникает business-time. Явная пауза
  нужна только когда должна разорвать иначе непрерывный наблюдаемый интервал.
- Несколько сессий и харнесов объединяются по времени без двойного счёта.
- Отсутствие события в partial-журнале не доказывает паузу.
- `deferred` исключает интервал из business elapsed, но не стирает реально
  наблюдаемую работу внутри него; сырой calendar elapsed всегда сохраняется.

## Vigers

Для Vigers прочитай `references/vigers-adapter.md` и используй:

```text
python3 {baseDir}/scripts/vigers_adapter.py reconcile \
  --case-root <case-root> --forecast <timing-forecast.json> \
  --business-calendar <project-root>/.vigers/timing-calendar.json \
  --harness-log <session.jsonl> --logs-complete \
  --write <case-root>/activity-reconciliation.json
```

Адаптер читает Vigers ledgers, но не меняет их. Vigers валидирует fingerprint,
case/project identity, coverage и eligibility до использования результата.
Первичный цикл заканчивается первым `development_handoff`. Доанализ по данным
разработки после передачи измеряй новым `post-handoff-followup` work item с
собственным окном; межцикловое ожидание в elapsed не входит.

## Расширение метрик

Ядро возвращает массив `metric_results`. `activity-time` вычисляет время;
`observed-counters` суммирует нормализованные observations, например tokens,
retries или findings. Новую метрику добавляй отдельным provider над тем же
bundle; не меняй контракт событий ради конкретного процесса.

Для Vigers и Delivery Engineering агрегируй уже записанные model ledgers без
новых вызовов:

```text
python3 {baseDir}/scripts/review_yield.py \
  --agent-ledger <case-a>/agent-ledger.json \
  --agent-ledger <case-b>/agent-ledger.json \
  --write <review-yield.json>
```

Сравнивай роли, modes, models и versioned lenses по duration/tokens/retries,
reported/accepted/rejected/duplicate/verified. Run без final receipt остаётся
`unclassified`; не выдавай его за нулевую полезность. Lens-строки не складывай
между собой, если один run имел несколько lenses.

## Проверка

```text
python3 {baseDir}/scripts/work_metrics.py validate --bundle <bundle.json>
python3 -m unittest discover -s {baseDir}/scripts -p 'test_*.py'
```

Для обнаружения одним установленным skill root в Codex и Claude выполни
`python3 {baseDir}/scripts/install.py`; installer сначала проверяет оба target и
при любом конфликте ничего не меняет.
