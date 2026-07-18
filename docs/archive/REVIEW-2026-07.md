# Полное ревью tg-claude-orchestrator

> ⚠️ Исторический документ. После декомпозиции 2026-07-18 (пакет
> `orchestrator/`, выделены turn.py/toolline.py/cbdata.py/runner.py/
> transcript.py/proctree.py/hookscript.py) упомянутые здесь `file:line`
> устарели; пункты D1/D4/D5/D6/D7 закрыты этим рефакторингом.

Объём: ~5k строк Python, 9 модулей + 6 тестов. Архитектура здравая, много
внимания уделено робастности (resume/clear fallback, дедуп релея, паритет
текстов). Но есть реальные проблемы по безопасности и несколько утечек/расов.
Ниже — по убыванию важности, с `file:line` и конкретными фиксам.

Уровни: 🔴 чинить, 🟠 стоит, 🟡 чистота/запахи.

---

## 🔴 Безопасность

### S1. Внутренний HTTP-API оркестратора без авторизации
`reply_server.py:73` поднимает `:18080` на `127.0.0.1`, эндпоинты `/reply`,
`/event/{name}`, `/permission/{name}` не проверяют никакого токена.
`handle_reply` (`bot.py:1309`) принимает **контролируемый отправителем** JSON:
- `file_path` → `_send_file` (`bot.py:1351`) отправляет **любой читаемый
  локальный файл** (до 50 МБ) в чат. Любой локальный процесс может выгрузить
  `~/.ssh/id_rsa`, `.env`, `~/.bash_history` в Telegram.
- `text` + `context_id` → вставляет произвольное сообщение от имени бота в
  любой топик (`_parse_context` ещё и падает в основной чат при кривом id).
- `/permission/{name}` → рисует фейковый permission-запрос.

Угроза не только «локальный злоумышленник»: DNS-rebinding + вкладка браузера
может POST'нуть на `127.0.0.1:18080/reply` (aiohttp `request.json()` не
проверяет Content-Type).

**Фикс:** общий секретный токен в `.env` (`ORCH_TOKEN`), проверять заголовок
`Authorization: Bearer …` на каждом эндпоинте; либо unix-сокет с правами 0600.
Минимум — отдельный токен именно на `/reply`+`file_path`.

### S2. `send_file_to_telegram` без jail по пути
`bot.py:1351` (`_send_file`) принимает любой абсолютный путь. Claude
полу-доверенный, но промпт-инъекция из прочитанного файла или чужого
`CLAUDE.md` (а сессия с `linked_path` грузит проектный `CLAUDE.md`!) может
заставить модель вызвать тул с `~/.ssh/id_rsa`. Цель — владелец, но если чат
расшарен/читается — это утечка.

**Фикс:** разрешать только пути внутри `effective_cwd(session)` (resolve +
`is_relative_to`), остальное — отказ с причиной.

### S3. `enableAllProjectMcpServers: True` всегда — RCE открытием чужого проекта
`sessions.py:454` безусловно пишет `enableAllProjectMcpServers: True` в
`settings.local.json`. При `/new имя /чужой/проект` claude запускается с
`cwd=проект` и **авто-одобряет** проектный `.mcp.json` — его `command` стартует
на поднятии сессии без всякого consent. В любом режиме разрешений (не только
`bypass`) — серверный процесс запущен, а это уже RCE.

**Фикс:** ставить `enableAllProjectMcpServers=True` только при
`session.linked_path is None`. Канал-сервер это не ломает — он идёт через
`--mcp-config`/`--dangerously-load-development-channels`, отдельный путь.

### S4. `--dangerously-skip-permissions` + `bypass` + чужой cwd
Документировано, но стоит отдельной строкой: `PERMISSION_MODE=bypass` + linked
проект = Claude делает в нём что угодно без вопросов. Это воля пользователя,
но в связке с S3 (авто-апрув MCP) — комбо. Минимум — предупреждать в `/new`
при линке пути в bypass-режиме.

---

## 🔴 Баги корректности / утечки

### B1. Утечка bash-процессов: `BashShellManager.close` нигде не вызывается
`bashshell.py:109` определяет `close(thread_id)`, но он **не вызывается ни
разуу** во всём проекте (ни в `bot.py`, ни в `sessions.py`, ни в `launcher.py`).
Каждый `/bash` в топике создаёт вечный `bash -i` под PTY (`BashSession`), который
живёт до смерти процесса бота. Переживает `/close_session`, `/delete_session`,
удаление топика. На долгоживущем боте — рост числа процессов и файловых
дескрипторов master-PTY.

**Фикс:** вызывать `self.bash.close(thread_id)` в `cmd_close`, `on_delete_button`,
`notify_session_dead`, `notify_idle_closed`; плюс periodic-ребилдер в
`BashShellManager` (закрывать не-`running` сессии). И закрывать все при
`shutdown`.

### B2. `cmd_bash`: race по `busy` + таймаут оставляет процесс
`bot.py:608` ставит `shell.busy = True` без лок — два быстрых `/bash` (aiogram
не сериализует апдейты per-chat) могут оба пройти проверку и испортить маркеры.
При `BASH_TIMEOUT` (`bot.py:662`) процесс остаётся работать в фоне (текст
это признаёт), `busy` сбрасывается, следующий `/bash` стартует в тот же PTY —
вывод старой команды мешается с новой.

**Фикс:** asyncio.Lock на shell (или per-thread), на таймауте слать `\x03`
(Ctrl-C) / убивать и пересоздавать сессию.

### B3. `_watch` не защищает `on_dead` — watcher-таск молча дохнет
`sessions.py:739` — `await self.on_dead(session, code)` без try/except. Любое
исключение в `notify_session_dead` (например TG-ошибка при отправке) убьёт
задачу watcher'а с «Task exception was never retrieved». Сессия уже помечена
остановленной, но логирование/воспринимаемость страдает.

**Фикс:** обернуть колбэк в try/except с `logger.exception`.

### B4. `TELEGRAM_CHAT_ID` не-число → необработанный `ValueError`
`config.py:52` — `int(chat_id_raw)` без отлова. Опечатка в `.env` даёт трейс
вместо дружелюбного сообщения (как у `TELEGRAM_BOT_TOKEN` / `PERMISSION_MODE`).

**Фикс:** try/except → `SystemExit` с подсказкой.

### B5. `_error_relay_loop` читает ВЕСЬ лог каждые 6 c
`bot.py:1241` — `data = log.read_bytes()` затем `data[offset:]`. Под ограничением
`LOG_MAX_MB` (10 МБ по умолчанию) это терпимо, но это полные чтения файла в
память ради среза хвоста.

**Фикс:** `data.seek(offset); chunk = data.read()` через file handle, или
хранить хендл и читать приращение.

### B6. `_parse_context` падает в основной чат при кривом `context_id`
`bot.py:1544` — на мусорном `context_id` возвращает `(self.chat_id, None, None)`,
т.е. ответ летит в основной чат. В связке с S1 — удобный вектор. Даже без него:
баг Клода/канала может вбросить ответ не туда.

**Фикс:** на синтаксически неверном `context_id` логировать и игнорировать, а не
дефолтить в main chat.

### B7. `_with_quote` пишет INFO на каждое сообщение
`bot.py:1032` — `logger.info(...)` на каждый текст. Шум в journald. Опустить на
DEBUG (это диагностика цитат в форумах, сейчас уже понятна).

### B8. `save_state` — синхронное файловое I/O на event-loop
`sessions.py:297` (`write_text` + `os.replace`) вызывается из обработчиков и
watcher'а в основном потоке цикла. JSON маленький, но на медленном диске/NFS
блокирует loop. Обернуть в `asyncio.to_thread` (вызовы и так async-контекст).

---

## 🟠 Робастность / обработка ошибок

- **E1. aiohttp.ClientSession на каждый запрос** (`sessions.py:794,878`,
  `channel_server.py:283,301`). Создание сессии = новый connection pool, без
  переиспользования keep-alive. Завести по одной сессии на процесс (or per
  manager), закрывать в `close()`/`shutdown()`.
- **E2. `_send` (`bot.py:1585`)** — агрессивная деградация: любой сбой `plain`
  отправки после двух ретраев теряет сообщение. Транзиентные сетевые ошибки
  съедают финальные ответы. Хотя бы 1–2 ретрая с backoff до потери.
- **E3. Широкие `except Exception: pass`** в косметике (`_strip_markup`,
  `_edit_or_pass`, flush бабла, edit `/bash`) — на уровне `debug`. Когда
  «бабл не обновился», причины не найти. Поднять повторяющиеся на `warning`
  с throttle, либо хотя бы раз в N.
- **E4. `read_stats` / `tail_log`** — блокирующее I/O; в основном вызываются
  через `to_thread`, но `_model_display` (`bot.py:828`) синхронно читает
  транскрипт, и вызывается из `_switch_model` через `to_thread` — ок; из
  `on_model_button` — нет, там `_switch_model` сам по себе. Проверить, что ни
  один путь не дёргает чтение файла прямо из loop'а.
- **E5. `_idle_sweeper`** (`launcher.py:71`) — хороший try/except, но
  `manager.close_idle` идёт последовательно (`for ... await close`), одна
  тяжёлая сессия тормозит остальные; можно параллелить через gather.

---

## 🟡 Декомпозиция / запахи

### D1. Год-модули
- `bot.py` **1633 строки**: хендлеры команд + рендер markdown + парсинг
  лог-сигналов + рендер bash + парсинг `/cost` + форматирование бабла + три
  фоновых лупы + permission relay + работа с файлами.
- `sessions.py` **994 строки**: жизненный цикл + PTY-драйвер + `/proc`-сигналы
  + чтение транскрипта (stats/pollution) + slugify/транслит.

Вырисовываются самостоятельные модули (многие уже тестируются изолированно,
значит граница естественная):
- `logsignals.py` — `_detect_log_signals`, `_classify_api_error`, regex'ы
  (`bot.py:210–261`). Чистые функции, вынести целиком.
- `transcript.py` — `read_stats`, `read_pollution_excerpt`, `_scan_pollution`,
  `_block_snippet`, `transcript_path` (`sessions.py:52–100, 915–994`).
- `mdrender.py` — `md_to_html`, `split_text` (`bot.py:183–280`).
- `slug.py` — `slugify`, `_TRANSLIT` (`sessions.py:107–127`).
- `proctree.py` — `_proc_tree_signals` (`sessions.py:149–189`), Linux-специфика.

### D2. Дублированный `_ANSI_RE`
Один и тот же паттерн в `sessions.py:130`, `bashshell.py:25`, `bot.py:217`
(там `_LOG_ANSI_RE`). Вынести в `ansi.py` с одной `strip()`.

### D3. Мёртвый код: `BubbleManager.open`
`bubble.py:59` — метод определён, но нигде не вызывается. `_active`
заполняется только через `fork`. Либо удалить, либо использовать (в `_start_typing`).

### D4. Парсинг `callback_data` разбросан
`split(":")`, `split(":",2)`, `rsplit(":",1)` в `on_model_button`,
`on_session_button`, `on_perm_button`, `on_delete_button`, `on_stop_button`
(`bot.py:732, 959, 1465, 1496…`). Колонки в request_id — известная мина
(есть тест `callbacks_test.py:122`). Вынести в один `parse_callback(data) ->
(prefix, *parts)` + dataclass.

### D5. Триада фоновых луп на ход
`_typing_loop` / `_watchdog_loop` / `_error_relay_loop` (`bot.py:1160–1305`) —
самая хитрая часть бота и **наименее протестированная**: покрыт только чистый
`_detect_log_signals`, но не сами лупы (дедуп, throttle, reset'ы). Естественный
кандидат в класс `TurnSupervisor(thread_id)` со своим старт/стоп и состоянием
(`last_sig`, `last_retry_k`, `restart_count`).

### D6. Состояние «активного хода» неявное
`_active` set в `BubbleManager` + `_typing/_watchdogs/_error_relays` dict'ы в
боте — один и тот же lifecycle («идёт ход Клода») размазан по двум объектам.
`fork()`/`close()` в бабле и `_start/_stop_typing` в боте должны работать в
унисон, но связи нет — рассинхрон приведёт к сиротам или висящему typing.

### D7. Служебные константы вперемешку
`bot.py:43–107` — лимиты ТГ, релейные интервалы, bash-константы, regex'ы,
иконки тулов, алиасы моделей — одним блоком. Сгруппировать по concern'ам.

### D8. Прочее по мелочи
- `_collect_skills` (`bot.py:568`) парсит YAML-фронтmatter построчно — ломается
  на многострочных полях / `name:` в теле. `python-frontmatter` или yaml.
- `_parse_new_args` (`bot.py:466`) — наивная обработка кавычек; вложенные
  кавычки не учитываются.
- Нет конфига линтера/тайп-чекера (ruff/mypy/pytest.ini). Тесты — отдельные
  скрипты с `__main__`; под pytest они заработают, но единого `pytest`-рана нет.
- `requirements.txt` без lock-файла (pip-tools/uv) — воспроизводимость.
- `install.sh` — `Restart=on-failure`, но нет `StartLimitBurst`/`StartLimitInterval`
  при возможном latch-up (бесконечный ребут при падающей конфигурации).

---

## Что хорошо
- Жизненный цикл сессии (`create/close/resume/clear/set_model`) сериализован
  `session.ops`, есть fallback «resume не удался → чистый старт», честное
  сообщение пользователю.
- Релей ошибок API: триггер строго по баннеру «API Error: <код>», классы с
  разными подсказками, дедуп по сигнатуре, защита от ложных срабатываний на
  прозе модели — и это всё покрыто тестом `error_relay_test.py`.
- `save_state` атомарен (`os.replace`), `.sessions.json.tmp` → rename.
- `_proc_tree_signals` — корректный парсинг `/proc/<pid>/stat` через последний
  `)` (comm может содержать скобки).
- Глубокая деградация отправки: нет reply → без топика → plain (финал не
  теряется).
- Паритет ru/en текстов тестирован (`smoke_test.py:29`).
- PTY-драйвер корректно дренирует вывод и отвечает на стартовые диалоги,
  владеет master-fd и сам закрывает (наконец-то комментарий объясняет почему).

---

## Приоритет на починку
1. **S1** (HTTP без auth) — токен на эндпоинты.
2. **S3** (enableAllProjectMcpServers) — условный флаг.
3. **S2** (jail на send_file).
4. **B1** (утечка bash) — вызывать `BashShellManager.close`.
5. **B3** (guard on_dead), **B4** (chat_id int).
6. **D1/D2** — вынести logsignals/transcript/ansi, убрать дубли.
7. **E1** — переиспользование aiohttp-сессий.
