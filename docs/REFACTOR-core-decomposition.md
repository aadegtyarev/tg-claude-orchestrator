# Рефакторинг: декомпозиция ядра перед развитием — план

Цель: `OrchestratorCore` (`core/app.py`, ~1516 строк) стал god-object'ом — в него
вросло несколько самодостаточных подсистем со своим per-session состоянием.
`_teardown_runtime` вручную помнит про `pop` каждого словаря (комментарий автора
там фиксирует, что последовательность «уже разъезжалась»), а логика одной фичи
размазана по `app.py` + `turn.py` + `sessions.py`.

Приводим в порядок ПЕРЕД развитием: выносим когезивные подсистемы в отдельные
классы-коллабораторы, каждый владеет своим состоянием и даёт `forget(name)`.
`OrchestratorCore` остаётся тонким координатором и СОХРАНЯЕТ публичный API
(адаптеры не трогаем). Поведение идентично, всё под тестами.

## Принципы

- **Инкрементально:** каждый этап — отдельный PR, `ruff` чистый, оба раннера
  зелёные. Деплой на прод — ТОЛЬКО по явной команде оператора (не автоматом).
- **Фасад стабилен:** публичные методы `OrchestratorCore`, что зовут адаптеры/
  reply_server, остаются на месте; меняются только внутренности (делегация).
- **Без смены поведения:** чистый рефактор, не чиним и не добавляем фич по пути.
- **Тесты на шов:** существующие покрытия (core_fixes, turn_supervisor, callbacks,
  bubble…) остаются зелёными; где шов тонкий — добавляем точечный тест на новый класс.

## Этапы (по убыванию ценность/риск)

### Этап 1 — `HookTracker` (активность тула + именование сабагентов) ⬅ начать здесь
Наибольшая ценность: снимает дрейф-риск, который недавний фикс кнопки ⏭ расширил
(3 словаря × 3 места очистки).
- Владеет: `_last_tool`, `_inflight_tools`, `_inflight_cleared_at`, `_agent_types`,
  `_agent_spawns`.
- Поглощает: `handle_tool_event`, `_handle_post_tool`, `_handle_subagent_stop`,
  `_pop_agent_type`, `_read_subagent_model`, `unblock_action`,
  `_foreground_tool_active`, `_unblock_available`, hook-часть `handle_stop_event`.
- Даёт: `on_pre/on_post/on_subagent_stop`, `unblock_action(name)`,
  `tool_inflight(name)`, `forget(name)`.
- Итог: `_teardown_runtime`/границы хода зовут один `hooks.forget(name)` вместо
  5 `pop`; логика ⏭/вотчдога — в одном файле. `turn.py` получает `tool_inflight`
  из трекера (как сейчас — колбэком). ~250 строк уходят из `app.py`.

### Этап 2 — `PermissionRelay`
- Владеет: `_pending_perms`, `_local_perms`.
- Поглощает: `handle_permission_request`, `request_confirmation`,
  `permission_verdict`, `_broadcast_perm_resolved`, `_drop_pending_perms`.
- Даёт: `request(...)`, `resolve(...)`, `forget(session)`.

### Этап 3 — `HistoryLog` (журнал событий для веб-истории)
- Владеет: `_history`.
- Поглощает: `_record`, `history`, `_load_history`, `save_history`.
- Персист (`.history.json`) инкапсулируется здесь.

### Этап 4 — `core/reports.py` (статы/отчёты — чистые функции)
- Выносим `stats_text`, `usage_text`, `model_display`, `_parse_cost` в модуль
  чистых функций (мало состояния, низкий риск). `OrchestratorCore` тонко зовёт.

### Этап 5 (опционально) — глушь вокруг `/bash`
- `bash_*`-обвязка уже делегирует в `BashShellManager`; орг-glue можно собрать в
  тонкий фасад, если после 1–4 это ещё будет мозолить.

## Не входит (осознанно)
- Смена слоистости adapters→Transport→core→sessions→runners (она чистая — храним).
- Вариант C из ревью (единый `SessionRuntime` на ВСЁ) — размывает разные времена
  жизни (permission-futures ≠ Session-поля); не берём.
- Функциональные правки, новые фичи.

## Статус
- ✅ Этап 0 — характеризационная сетка teardown-forget (PR #28)
- ✅ Этап 1 — по SRP разбит на два ЧИСТЫХ объекта состояния (вместо монолитного
  HookTracker): `ToolActivity` (PR #29) + `SubagentNaming` (PR #30). Event-хендлеры
  остались тонкой оркестрацией в app.py — они законно нуждаются в bubbles/turns.
- ✅ Этап 2 — `UserError`→`core/errors.py` (PR #31) + `PermissionRelay` (этот PR).
- ⬜ Этап 3 — HistoryLog
- ⬜ Этап 4 — reports.py
- ⬜ Этап 5 — bash-фасад (опц.)
