"""Именование завершившихся сабагентов: agent_id → тип (dev-planner/…) на сессию.

Зачем: строка «✅ Сабагент завершил» ОБЯЗАНА называть ИМЕННО того, кто закончил.
При последовательных сабагентах (planner → builder → reviewer) безымянное
«завершил» + начало ходов следующего читались как «завершил, но идёт дальше» —
на деле это уже другой агент.

Тип сабагента добывается двумя путями (в порядке надёжности):
  * дочерний тул-вызов ВНУТРИ сабагента несёт `agent_id` + `agent_type` — точный
    источник (`note_child`);
  * спавн (Agent/Task) несёт `subagent_type`, но `agent_id` ещё нет — копим типы
    по порядку запуска как фолбэк (`note_spawn`) и сматчим при завершении по
    очереди (сабагенты завершаются в порядке запуска).

Чистый объект состояния, без внешних зависимостей: оркестратор (app.py) кормит его
из hook-хендлеров и снимает `pop(name, agent_id)` при SubagentStop; `forget(name)`
на границе хода/teardown. Чтение модели сабагента (I/O транскрипта) остаётся в
оркестраторе — здесь только именование.
"""

from __future__ import annotations


class SubagentNaming:
    """Per-session сопоставление agent_id → тип сабагента для строки завершения.

    Владеет двумя словарями (ключ — имя сессии):
      * `_types`  — {agent_id: тип} из дочерних тул-событий (точный матч);
      * `_spawns` — [тип, …] из спавн-строк по порядку (фолбэк без agent_id).

    Инвариант: `pop` СНИМАЕТ тип с учёта (тип расходуется один раз на завершение),
    `forget(name)` снимает всё состояние сессии одним вызовом.
    """

    def __init__(self) -> None:
        self._types: dict[str, dict[str, str]] = {}
        self._spawns: dict[str, list[str]] = {}
        # agent_id завершившихся сабагентов (закрыты SubagentStop). Хуки шлются
        # независимыми async-POST'ами, порядок не гарантирован — запоздалый
        # тул-хук сабагента может обогнать доставку его же SubagentStop и
        # прилететь ПОСЛЕ строки «завершил». Такие события рендерим верхним
        # уровнем (без отступа под уже завершённого агента) — см. is_closed.
        self._closed: dict[str, set[str]] = {}

    # ── запись (из hook-хендлеров) ──────────────────────────────

    def note_child(self, name: str, agent_id: str, agent_type: str) -> None:
        """Точный источник: дочерний тул сабагента (в payload есть agent_id+type)."""
        self._types.setdefault(name, {})[agent_id] = agent_type

    def note_spawn(self, name: str, agent_type: str) -> None:
        """Фолбэк: строка спавна (Agent/Task) — agent_id ещё нет, копим по порядку."""
        self._spawns.setdefault(name, []).append(agent_type)

    # ── чтение (при SubagentStop) ───────────────────────────────

    def pop(self, name: str, agent_id: str) -> str:
        """Тип завершившегося сабагента, СНИМАЯ его с учёта. Точное совпадение по
        `agent_id` → фолбэк на самый ранний неиспользованный спавн (сабагенты
        завершаются в порядке запуска). '' — тип неизвестен (мягкая деградация)."""
        types = self._types.get(name)
        if types and agent_id in types:
            return types.pop(agent_id)
        spawns = self._spawns.get(name)
        if spawns:
            return spawns.pop(0)
        return ""

    def close(self, name: str, agent_id: str) -> None:
        """Отметить сабагента завершённым (из SubagentStop): его запоздалые
        тул-хуки больше НЕ нестить под ним. Пустой agent_id игнорируем."""
        if agent_id:
            self._closed.setdefault(name, set()).add(agent_id)

    def is_closed(self, name: str, agent_id: str) -> bool:
        """Завершился ли сабагент с этим agent_id (SubagentStop уже пришёл)."""
        return agent_id in self._closed.get(name, ())

    # ── жизненный цикл ──────────────────────────────────────────

    def forget(self, name: str) -> None:
        """Забыть всё состояние именования сессии — на границе хода и при teardown."""
        self._types.pop(name, None)
        self._spawns.pop(name, None)
        self._closed.pop(name, None)
