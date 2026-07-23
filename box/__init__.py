"""Box — автономный launcher сессий Claude Code (Слой 2 редизайна).

Пакет БЕЗ зависимостей оркестратора (нет aiogram/Telegram, нет
orchestrator.*): PTY-запуск, авто-ответы на стартовые диалоги и ожидание
готовности «по тишине» — самодостаточная механика поднятия одной сессии
claude. Оркестратор — надстройка над этим пакетом: он клиент launcher'а через
тонкий адаптер, ровно как для кошелька (пакет `vault/`).

Фаза 3 редизайна (docs/ARCHITECTURE-claude-box.md §5, §11 «Launcher-extract:
пакет box/») выносит сюда launch-механику из orchestrator/core/sessions.py.
Самодостаточные хелперы без зависимостей от SessionManager/оркестратора:
  • ansi.py   — снятие ANSI-последовательностей (strip_ansi), чтобы пакет был
                автономен (не тянул orchestrator.core.ansi);
  • dialog.py — матчер стартовых диалогов экран→клавиши (_DialogAnswerer,
                _DIALOGS);
  • ready.py  — дедлайн готовности «по тишине» (_ReadyDeadline) + окна времени
                (READY_SILENCE_SEC, READY_TIMEOUT_MAX);
  • pty.py    — ядро PTY-запуска: open_pty (openpty + размер терминала) и
                драйвер (дренаж вывода + авто-ответы на стартовые диалоги),
                отдающий вывод наружу через колбэк on_output;
  • transcript_path.py — «конфиг клиента»: КУДА claude пишет транскрипт
                (профиль CLAUDE_CONFIG_DIR/$HOME + кодирование cwd), §5.2.
                Чистые функции resolve_config_dir/transcript_path.

`launch(task)->Handle` (единый фасад запуска: сборка argv/env, ожидание
готовности, resume/clear) и CLI `claude-box` переезжают следующими срезами;
оркестратор пока сам собирает argv/env, спавнит процесс на slave-fd от open_pty
и пишет вывод драйвера в claude.log, а _DialogAnswerer/_ReadyDeadline/READY_*
реэкспортит из sessions.py для обратной совместимости кода/тестов.
"""
