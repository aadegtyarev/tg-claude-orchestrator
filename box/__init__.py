"""Box — автономный launcher сессий Claude Code (Слой 2 редизайна).

Пакет БЕЗ зависимостей оркестратора (нет aiogram/Telegram, нет
orchestrator.*): PTY-запуск, авто-ответы на стартовые диалоги и ожидание
готовности «по тишине» — самодостаточная механика поднятия одной сессии
claude. Оркестратор — надстройка над этим пакетом: он клиент launcher'а через
тонкий адаптер, ровно как для кошелька (пакет `vault/`).

Фаза 3 редизайна (docs/ARCHITECTURE-claude-box.md §5, §11 «Launcher-extract:
пакет box/») выносит сюда launch-механику из orchestrator/core/sessions.py.
Этот первый срез — фундамент: самодостаточные хелперы без зависимостей от
SessionManager/оркестратора:
  • ansi.py   — снятие ANSI-последовательностей (strip_ansi), чтобы пакет был
                автономен (не тянул orchestrator.core.ansi);
  • dialog.py — матчер стартовых диалогов экран→клавиши (_DialogAnswerer,
                _DIALOGS);
  • ready.py  — дедлайн готовности «по тишине» (_ReadyDeadline) + окна времени
                (READY_SILENCE_SEC, READY_TIMEOUT_MAX).

PTY-запуск, `launch(task)->Handle` и CLI переезжают следующими срезами;
оркестратор пока реэкспортит перенесённое из sessions.py для обратной
совместимости кода/тестов.
"""
