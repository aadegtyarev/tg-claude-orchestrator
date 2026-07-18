# tg-claude-orchestrator

Telegram-обёртка для Claude Code через [channels](https://code.claude.com/docs/en/channels)
(research preview).

N параллельных сессий Claude Code в форум-топиках Telegram. Каждый топик —
отдельный экземпляр Claude со своим контекстом и рабочей директорией.

> Есть и официальный Telegram-плагин (`/plugin install telegram@claude-plugins-official`),
> но он — мост «один чат ↔ одна сессия». Этот проект решает другую задачу:
> много именованных сессий в топиках, привязка к папкам проектов, статус-баблы,
> resume, статистика.

## Архитектура

```
Telegram Group (Topics)
  ├── 🧵 data-analyst → claude #1 (PTY, cwd=проект) ──(--mcp-config)──> channel_server :18761
  ├── 🧵 devops       → claude #2 (PTY, cwd=проект) ──(--mcp-config)──> channel_server :18762
  └── 🧵 coder        → claude #3 (PTY, cwd=проект) ──(--mcp-config)──> channel_server :18763

launcher (один процесс)
  ├── бот aiogram: команды, файлы, статус-баблы
  ├── HTTP :18080 /reply        <── ответы Claude (тул reply_to_telegram)
  ├── HTTP :18080 /event/<имя>  <── вызовы инструментов (PreToolUse-хук)
  └── HTTP POST :1876x /notify  ──> push сообщений в Claude
```

**Кто кого запускает:** launcher запускает только процессы `claude` (под PTY).
`channel_server.py` запускает **сам Claude Code** — по `.mcp.json`, который
передан флагом `--mcp-config` (лежит в папке сессии). Оркестратор общается с
channel-сервером только по HTTP.

**cwd = папка проекта:** если сессия создана с путём (`/new имя /path`), claude
запускается прямо в проекте — грузит его `CLAUDE.md`, `.mcp.json`, `.claude/`
(натуральное поведение «cd в проект и claude»). Канал-сервер и настройки бота
подсасываются флагами (`--mcp-config`, `--settings`) и consent не просят;
профиль (`CLAUDE_CONFIG_DIR`) и проект остаются нетронутыми.

**Почему PTY:** headless-запуск не работает — без TTY claude уходит в
`--print` и завершается, а в `-p`/stream-json режиме channel-события не будят
ход (проверено). Интерактивная сессия под PTY — документированный сценарий
«persistent terminal»: пуш сам запускает ход. Стартовые диалоги (trust folder,
bypass permissions, dev channels) отвечает автоматика (`_pty_driver`).

## Модули

| Файл | Ответственность |
|------|-----------------|
| `launcher.py` | Точка входа: сборка, graceful shutdown |
| `config.py` | Чтение `.env` в неизменяемый `Config` |
| `bot.py` | Бот: команды, файлы, permission-кнопки, отправка, ретранслятор ошибок API, вотчдог зависаний |
| `bubble.py` | Статус-бабл: буфер строк, троттлинг правок, закрытие |
| `sessions.py` | `SessionManager`: PTY-процессы Claude, жизненный цикл, resume, авто-close, ротация логов, статистика |
| `sandbox.py` | Файловая песочница (bubblewrap): сборка argv-обёртки, проверка доступности |
| `reply_server.py` | aiohttp: `POST /reply`, `/event/{имя}`, `/permission/{имя}` |
| `channel_server.py` | MCP-канал: raw JSON-RPC на stdio + HTTP `/notify`, `/ping`, `/permission` |
| `texts.py` | Все сообщения бота на ru/en (`BOT_LANG`) |

## Требования

- Python ≥ 3.10
- [Claude Code](https://code.claude.com/docs) ≥ 2.1 в PATH, залогинен
  (channels требуют claude.ai / Console-аутентификацию)
- Telegram-бот от [@BotFather](https://t.me/BotFather)
- Группа с включёнными Topics, бот — админ с правом «Manage Topics»
  (создаёт и удаляет темы сам)
- `bubblewrap` (`apt install bubblewrap`) для файловой песочницы —
  включена по умолчанию; можно отключить `SANDBOX=off` (см. ниже)

⚠️ Channels — research preview: синтаксис флагов и протокол могут меняться.
Кастомные каналы загружаются через `--dangerously-load-development-channels`.

## Установка и запуск

```bash
./install.sh      # venv + зависимости + systemd user-unit + linger
nano .env         # TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS
systemctl --user enable --now tg-claude-orchestrator
journalctl --user -u tg-claude-orchestrator -f
```

Вручную: `source .venv/bin/activate && python launcher.py`.

Тесты без Telegram и Claude (контракт channel-сервера, логика кнопок,
гейт бабла, конфиг, паритет текстов ru/en):

```bash
.venv/bin/python tests/smoke_test.py       # протокол канала, конфиг, тексты
.venv/bin/python tests/callbacks_test.py   # inline-кнопки: парсинг, гварды
.venv/bin/python tests/bubble_test.py      # статус-бабл: гейт от сирот
.venv/bin/python tests/watchdog_test.py    # вотчдог: живая работа vs зависание
.venv/bin/python tests/tool_line_test.py   # компактные строки тулов, reply-цитаты
.venv/bin/python tests/error_relay_test.py # ретранслятор ошибок API: строгий баннер, без ложных
```

Сервис — user-unit (`Restart=on-failure`), `install.sh` включает
`loginctl enable-linger`: работает в фоне и переживает разлогин.
При остановке launcher'а записи сессий сохраняются в
`SESSIONS_DIR/.sessions.json` — после перезапуска сессии возобновляются
по первому сообщению в топике.

## Конфигурация (.env)

| Параметр | По умолчанию | Описание |
|----------|-------------|----------|
| `TELEGRAM_BOT_TOKEN` | — | **Обязательно.** Токен бота |
| `TELEGRAM_CHAT_ID` | автопривязка | ID группы; узнать — команда `/chat_id`. Лучше задать явно |
| `ALLOWED_USER_IDS` | пусто (никто) | **Обязательно.** Белый список, через запятую |
| `CHANNEL_PORT_START` / `_END` | авто | Пул портов channel-серверов. Не задано — ОС выдаёт свободный localhost-порт на сессию; задать диапазон только для предсказуемых портов |
| `SESSIONS_DIR` | `~/tg-claude-sessions` | Директория сессий |
| `MAX_INSTANCES` | 5 | Лимит одновременных сессий |
| `CLAUDE_BIN` | `claude` | Путь к бинарнику Claude Code |
| `DEFAULT_MODEL` | — | Модель по умолчанию для новых сессий (псевдоним `opus`/`sonnet`/… или точное имя). Не задано — дефолт Claude/профиля/проекта. `/model` перекрывает |
| `DEFAULT_EFFORT` | — | Effort по умолчанию: `low`/`medium`/`high`/`xhigh`/`max`. Не задано — решает Claude/профиль/проект |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Свой профиль Claude Code |
| `CLAUDE_ENV_<ИМЯ>` | — | Проброс env в процесс claude: `CLAUDE_ENV_ANTHROPIC_BASE_URL=…` → claude получит `ANTHROPIC_BASE_URL=…` |
| `ORCH_PORT` | 18080 | HTTP-порт оркестратора |
| `ORCH_HOST` | 127.0.0.1 | Хост оркестратора для channel-серверов |
| `SHOW_TOOL_CALLS` | `true` | Вызовы инструментов в статус-бабле |
| `DELETE_BUBBLE` | `true` | Удалять бабл после ответа (`false` — оставить как журнал) |
| `INCOMING_DIR` | `incoming` | Куда класть присланные файлы (отн. путь — в папке сессии) |
| `PERMISSION_MODE` | `auto` | Режим разрешений: `auto`/`bypass`/`acceptEdits`/`manual`/`dontAsk`/`plan` |
| `SANDBOX` | `bwrap` | Файловая песочница: `bwrap` (изоляция ФС через bubblewrap) / `off` (без изоляции) |
| `SANDBOX_EXTRA_RW` | — | Доп. пути, открытые из песочницы на запись (через `:`) |
| `BOT_LANG` | `ru` | Язык сообщений бота: `ru` / `en` |
| `IDLE_TIMEOUT_H` | 6 | Авто-останов сессии после N ч простоя (0 — выкл.) |
| `LOG_MAX_MB` | 10 | Ротация `claude.log` при превышении размера (0 — выкл.) |

⚠️ **Безопасность.** Доступ строго по белому списку: пустой
`ALLOWED_USER_IDS` = бот игнорирует всех. Кто в списке — тот и одобряет
запросы разрешений (permission relay), доверяй его только себе.

## Файловая песочница (`SANDBOX`)

По умолчанию (`SANDBOX=bwrap`) процесс `claude` и `/bash`-терминал каждой
сессии запускаются в изолированном mount-namespace через
[bubblewrap](https://github.com/containers/bubblewrap) — без root, на
unprivileged user namespaces. Внутри `$HOME` подменён пустым tmpfs, и наружу
видны **только** явно разрешённые пути:

| Что | Доступ | Зачем |
|-----|--------|-------|
| Папка сессии + папка проекта (`linked_path`) | чтение/запись | собственно работа |
| `CLAUDE_CONFIG_DIR` (`~/.claude`) + `~/.claude.json` | чтение/запись | токены, скиллы, plugins, транскрипты |
| Бинарь claude (`~/.local/share/claude`, `~/.local/bin`) | чтение | запуск |
| Репозиторий оркестратора (`channel_server.py` + `.venv`) | чтение | MCP-канал и хуки |
| Системный рантайм (`/usr`, `/etc`, …) | чтение | библиотеки, TLS-сертификаты, DNS |
| `SANDBOX_EXTRA_RW` | чтение/запись | доп. рабочие каталоги |

Всё остальное — другие проекты, `~/.ssh`, `~/.aws`, история shell, системные
каталоги — **не видно** ни на чтение, ни на запись. Запись в подменённый
`$HOME` уходит в эфемерный tmpfs и на диск не попадает.

В отличие от нативного `/sandbox` Claude Code (он ограничивает только
Bash-тул, а `Read`/`Write`/`Edit`, MCP и хуки оставляет на хосте), обёртка
вокруг всего процесса накрывает **все** инструменты сразу. Это дополняет
app-level jail для `send_file_to_telegram` (проверка путей от промпт-инъекций)
ОС-уровневой изоляцией.

Сеть — общая с хостом (нужна для API Anthropic и localhost-оркестратора);
фильтрации по доменам bubblewrap не делает. Требуется пакет `bubblewrap` и
разрешённые unprivileged user namespaces (Ubuntu 24.04+ — см.
`kernel.apparmor_restrict_unprivileged_userns`). Если песочница включена, но
недоступна, бот **не стартует** с понятной ошибкой — молча без изоляции не
запускается. `SANDBOX=off` отключает изоляцию (менее безопасно; для машин без
bwrap).

## Режимы разрешений и permission relay

`PERMISSION_MODE` управляет тем, что Claude может делать без вопросов:

| Режим | Поведение |
|-------|-----------|
| `auto` (по умолчанию) | Классификатор Claude Code решает по каждому вызову; сомнительное — спрашивает |
| `bypass` | Без ограничений (`--dangerously-skip-permissions`) — ничего не спрашивает |
| `acceptEdits` | Правки файлов без вопросов, остальное спрашивает |
| `manual` | Спрашивает всё, что не разрешено правилами |
| `dontAsk` | Запрещает всё неразрешённое, не спрашивая |
| `plan` | Только чтение; правки и команды спрашивает |

Во всех режимах, кроме `bypass`, запросы разрешений прилетают **в топик
кнопками ✅/❌** (официальный permission relay из контракта каналов):

```
🔐 Запрос разрешения
Bash: Установить зависимости проекта
npm install
[✅ Разрешить] [❌ Отклонить]
```

Параллельно остаётся открытым и локальный TUI-диалог — применяется тот
ответ, который пришёл первым. Тулы самого канала (`reply_to_telegram`,
`send_file_to_telegram`) предразрешены в настройках сессии — на каждое
сообщение кнопка не выскакивает.

**Интерактивные вопросы Claude.** Plan mode и инструмент AskUserQuestion
всплывали бы в терминале сессии, невидимо для Telegram. Инструкция канала
явно запрещает их: когда Claude нужно решение, он задаёт вопрос через
`reply_to_telegram` нумерованными вариантами и ждёт следующего сообщения
(проверено вживую). Так «слепых» зависаний в терминале не возникает.

## Команды

Меню команд регистрируется в Telegram автоматически (кнопка «/»).

**В основном чате:**

| Команда | Описание |
|---------|----------|
| `/new <имя>` | Новая сессия. Имя может быть с пробелами/эмодзи (`/new Мой проект` или `/new "Data Analyst"`) — это название топика; для папки/портов берётся безопасный slug |
| `/new /путь` | Сессия с привязкой папки (создастся, если нет); имя = basename |
| `/new имя /путь` | То же, со своим именем (путь — токен, начинающийся с `/`) |
| `/list` | Сессии со статусами (🔄 работает / 🟢 ожидает / ⏸ остановлена) и кнопками |
| `/ls [путь]` | Файлы (по умолчанию `SESSIONS_DIR`; `~` разворачивается) |
| `/skills` | Список скиллов профиля (работает и в топике) |
| `/chat_id` | Показать ID чата и привязать бота (работает в любой группе) |
| `/help` | Справка |

**В топике сессии:**

| Команда | Описание |
|---------|----------|
| Текст | Отправляется в Claude; остановленная сессия возобновится сама |
| Фото/файл | Скачивается в `INCOMING_DIR`, путь передаётся Claude |
| `/stats` | Модель, контекст (токены), сгенерировано, ходы, аптайм (из транскрипта) |
| `/usage` | Расходы и лимиты плана: стоимость сессии, % лимита 5ч и недели, сброс (парсит `/cost` Claude Code) |
| `/model [имя]` | Модель: кнопки-синонимы fable/opus/sonnet/haiku или точное имя. Перезапуск с resume |
| `/compact` | Сжать контекст (печатается в терминал сессии) |
| `/clear` | Чистый контекст: перезапуск с новым UUID, топик остаётся |
| `/close_session` | Остановить процесс; топик и запись остаются |
| `/delete_session` | Удалить сессию вместе с топиком |
| `/что-угодно` | Неизвестные команды печатаются в терминал Claude — работают команды Claude Code (`/context`, `/mcp`, `/usage`…). Их вывод остаётся в `claude.log` |

**Файлы в обе стороны:** присланное в топик фото/документ сохраняется в
`INCOMING_DIR` и путь передаётся Claude (картинки он читает инструментом
Read); обратно Claude присылает файлы сам — тулом
`send_file_to_telegram(context_id, file_path, caption)` (до 50 МБ).

## Жизненный цикл сессии

```
/new ──> работает ──/close_session──> остановлена ──сообщение──> resume
                    (или падение,     (топик жив)      │
                     или рестарт                       ├─ claude --resume <uuid> — контекст продолжен
                     launcher'а)                       └─ не вышло → чистый старт (честно сообщается)
```

- Готовность = channel-сервер Claude отвечает на `/ping` (до 60 с).
- За процессом следит watcher; при смерти Claude сессия помечается
  остановленной, в топик приходит уведомление с хвостом `claude.log`.
- Простаивающие сессии (> `IDLE_TIMEOUT_H`) авто-останавливаются, освобождая
  память; топик и контекст сохраняются, resume — по сообщению.
- При старте бот постит в чат «онлайн, восстановлено N сессий».
- При остановке убивается вся группа процессов (иначе channel_server
  осиротеет и продолжит держать порт).
- `stdout/stderr` (вывод PTY) — в `SESSIONS_DIR/<имя>/claude.log`.

## Статус-бабл

Пока Claude работает, в топике живёт одно редактируемое сообщение:

```
⏳ Работаю…
⚡ Bash: pytest -x
📖 Read: test_api.py
💬 Нашёл причину, чиню conftest.py
🤖 Сабагент reviewer: проверяю дифф
[⏹ Стоп]
```

- Вызовы инструментов (PreToolUse-хук → `POST /event/<имя>`): иконка под
  инструмент (⚡ Bash, 📖 Read, ✏️ Edit, 🔍 Grep, 🌐 WebFetch…), имя жирным,
  деталь моноширинно (для файловых — только имя файла). Спавн сабагента —
  строкой `🤖 Сабагент <тип>`. Отключается `SHOW_TOOL_CALLS=false`.
- 💬 — промежуточные ответы (`reply_to_telegram` с `complete=false`), курсивом.
- Редактирование не чаще раза в ~1.5 с, без уведомлений; при переполнении
  старые строки вытесняются. Событие после завершения хода игнорируется
  (не создаёт бабл-сироту).
- **⏹ Стоп** — мягкая остановка (push «прекрати и отчитайся»); это не Esc:
  жёсткого прерывания текущего хода в channels нет. Зависшую bash-команду
  надёжно обрывает только `/close_session` (kill группы процессов + resume).
- Финальный ответ (`complete=true`) — обычным сообщением, бабл удаляется.
- Токенового стриминга нет: ответ приходит целиком через вызов тула.

## Протокол (по [channels-reference](https://code.claude.com/docs/en/channels-reference))

- Capability: `capabilities.experimental["claude/channel"] = {}` + `tools: {}`;
  `instructions` в initialize объясняет Claude формат событий и reply-тул.
- Push: `notifications/claude/channel` c `params = {content, meta: {context_id}}`;
  Claude видит `<channel source="tg-channel-<имя>" context_id="tg:...">текст</channel>`.
- Ответ: тул `reply_to_telegram(context_id, text, complete)` →
  `POST /reply` оркестратора → сообщение в нужный топик.
- Слэш-команды (`/compact`, `/context`…) печатаются прямо в PTY сессии,
  не через канал.
- Запуск: `claude --session-id=<uuid> [--model <имя>] [--effort <уровень>]
  --mcp-config <сессия>/.mcp.json --settings <сессия>/.claude/settings.local.json
  --dangerously-load-development-channels server:tg-channel-<имя>
  (--permission-mode <mode> | --dangerously-skip-permissions при bypass)`,
  `cwd` = папка проекта. Dev-флаг ссылается на сервер из `--mcp-config`;
  `--session-id` требует UUID. `--model`/`--effort` — только если заданы
  `DEFAULT_MODEL`/`DEFAULT_EFFORT` (или `/model` на сессию).
- Согласие на MCP-сервер предодобрено `enableAllProjectMcpServers` в
  `.claude/settings.local.json` сессии (подаётся через `--settings`, мержится
  с профилем и проектом). `AskUserQuestion` запрещён в `permissions.deny`
  (интерактивное меню виснет без TTY); в bypass-режиме страж — системный промпт.

- Permission relay: capability `claude/channel/permission`; запрос
  (`notifications/claude/channel/permission_request`) пересылается на
  `POST /permission/<имя>` оркестратора, вердикт возвращается через
  `POST /permission` channel-сервера уведомлением
  `notifications/claude/channel/permission {request_id, behavior}`.

Проверено вживую (claude 2.1.205): спавн channel-сервера, handshake,
push→ход→ответ, хуки инструментов, close/resume с fallback,
`send_file_to_telegram`, смена модели (`--model sonnet`), проброс
слэш-команд в PTY (`/context`), permission relay в режиме `default`
(запрос → allow → выполнение → ответ).

## Лицензия

[MIT](LICENSE) © Alexandr Degtyarev
