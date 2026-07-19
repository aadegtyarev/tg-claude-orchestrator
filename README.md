# claude-orchestrator

Оркестратор N параллельных сессий [Claude Code](https://code.claude.com/docs)
через [channels](https://code.claude.com/docs/en/channels) (research preview):
ядро + подключаемые интерфейсы (Telegram-бот, локальный веб) + песочницы
исполнения + модули (кошелёк секретов).

Каждая сессия — отдельный экземпляр Claude со своим контекстом и рабочей
директорией проекта; наблюдаемость (статус-бабл, события инструментов,
permission-кнопки) работает в каждом интерфейсе.

> Ранее проект назывался **tg-claude-orchestrator**. Официальный
> Telegram-плагин (`/plugin install telegram@claude-plugins-official`) — мост
> «один чат ↔ одна сессия»; этот проект решает другую задачу: много именованных
> сессий, привязка к папкам проектов, статус-баблы, resume, песочница,
> несколько интерфейсов параллельно.

## Архитектура

```
                    ┌───────────── адаптеры (ADAPTERS) ─────────────┐
 Telegram (топики)◄─┤ adapters/telegram │ adapters/web ├─► Браузер (SPA+WS)
                    └───────▲───────────┴──────▲───────┘
                            │   Transport-протокол (core/transport.py)
                    ┌───────┴──────────────────┴───────┐
                    │ core/app.py  OrchestratorCore    │  команды, роутинг,
                    │ core/bubble  core/turn  core/... │  бабл, сторожа, jail
                    └───────▲──────────────────▲───────┘
                            │                  │ HTTP :18080 (/reply /event
                    ┌───────┴────────┐         │  /permission /stop, токен)
                    │ core/sessions  │         │
                    │ SessionManager │  ┌──────┴────────────┐
                    └───┬────────────┘  │ channel_server.py │◄─ спавнит сам
                        │ PTY + раннер  │ (в песочке, stdio)│   Claude Code
                ┌───────┴───────┐       └───────────────────┘
                │ runners/      │  bwrap | agent-vm | off
                └───────────────┘
 modules/wallet ── демон секретов на хосте + CLI `wallet` в песочнице
```

**Кто кого запускает:** оркестратор запускает только процессы `claude`
(под PTY, через раннер-изоляцию). `channel_server.py` запускает **сам Claude
Code** — по `.mcp.json`, переданному флагом `--mcp-config`. Ядро общается с
channel-сервером только по HTTP; все внутренние эндпоинты — под bearer-токеном
(`ORCH_TOKEN`).

**Одна сессия — все интерфейсы.** Сессия принадлежит ядру; каждый адаптер
хранит свой адрес (binding): у Telegram — форум-топик, у веба адрес не нужен.
Ответы, статус-бабл и permission-запросы доставляются во все активные
адаптеры; применяется первый ответ на permission (как с параллельным
TUI-диалогом).

**Почему PTY:** headless-запуск не работает — без TTY claude уходит в
`--print`, а в stream-json режиме channel-события не будят ход (проверено).
Интерактивная сессия под PTY — документированный сценарий «persistent
terminal»; стартовые диалоги отвечает автоматика. Бонус PTY: кнопка «⛔
Прервать» — настоящий Esc в терминал (жёсткое прерывание хода, которого нет
в channels-протоколе).

## Структура пакета

```
orchestrator/
  __main__.py        — сборка: config → runner.preflight → core → адаптеры/модули
  config.py          — .env → неизменяемый Config (ADAPTERS, MODULES, SANDBOX…)
  channel_server.py  — MCP-канал (raw JSON-RPC + HTTP; проектных импортов нет)
  core/              — ядро (транспорт-независимое):
    app.py           — OrchestratorCore: команды, роутинг ответов, jail, bash,
                       журнал событий, permission relay, подтверждения модулей
    transport.py     — протокол Transport + Origin/PermissionRequest
    sessions.py      — SessionManager: PTY-процессы, resume/clear/model,
                       персистентный дом сессии, состояние на диске
    turn.py          — TurnSupervisor: typing, вотчдог зависаний, релей ошибок
    bubble.py        — статус-бабл: строки, схлопывание, заморозка (мульти-адаптер)
    bashshell.py     — постоянные bash-терминалы (мимо Claude)
    reply_server.py  — HTTP-приёмник от channel-серверов и хуков
    toolline/texts/transcript/mdrender/logsignals/ansi/slug/proctree/hookscript
  adapters/
    telegram/        — aiogram: топики, кнопки, реакции, файлы
    web/             — aiohttp: SPA (vanilla JS) + WebSocket, REST API
  runners/           — изоляция: direct | bwrap (+sandbox.py) | agentvm
  modules/
    wallet/          — кошелёк секретов (демон + policy); CLI — bin/wallet
tests/               — офлайн-тесты (run_all.sh гоняет все)
docs/                — дизайн-доки (agent-vm, secrets-wallet, коннекторы)
install.sh           — venv + systemd user-unit (+ --uninstall, миграция имени)
```

## Требования

- Python ≥ 3.10
- Claude Code ≥ 2.1 в PATH, залогинен (channels требуют claude.ai / Console)
- Для Telegram-адаптера: бот от [@BotFather](https://t.me/BotFather), группа
  с Topics, бот — админ с правом «Manage Topics»
- `bubblewrap` (`apt install bubblewrap`) для песочницы по умолчанию
  (`SANDBOX=off` — отключить; `SANDBOX=agent-vm` — microVM, нужен KVM)

⚠️ Channels — research preview: синтаксис флагов и протокол могут меняться.

## Установка и запуск

```bash
./install.sh      # venv + зависимости + systemd user-unit + linger
nano .env         # TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS; ADAPTERS=telegram,web
systemctl --user enable --now claude-orchestrator
journalctl --user -u claude-orchestrator -f
```

Вручную: `.venv/bin/python -m orchestrator`. Старый юнит
`tg-claude-orchestrator` install.sh снимает автоматически.

Тесты офлайновые (без Telegram и Claude): `.venv/bin/python -m pytest`
(или `tests/run_all.sh` — те же файлы как отдельные скрипты; каждый тест
запускается обоими способами). Линт: `.venv/bin/ruff check .` CI (GitHub
Actions) гоняет pytest + ruff на 3.10/3.12.

## Конфигурация (.env)

| Параметр | По умолчанию | Описание |
|----------|-------------|----------|
| `ADAPTERS` | `telegram` | Интерфейсы через запятую: `telegram`, `web` (можно оба) |
| `MODULES` | — | Модули: `wallet` |
| `TELEGRAM_BOT_TOKEN` | — | Токен бота (обязателен при telegram в ADAPTERS) |
| `TELEGRAM_CHAT_ID` | автопривязка | ID группы; узнать — `/chat_id` |
| `ALLOWED_USER_IDS` | пусто (никто) | **Обязательно.** Белый список, через запятую |
| `WEB_HOST` / `WEB_PORT` | `127.0.0.1:8180` | Адрес веб-интерфейса |
| `WEB_TOKEN` | автогенерация | Токен доступа к вебу; пуст — печатается URL в лог |
| `WALLET_SECRETS_FILE` | `~/.config/claude-orchestrator/secrets.toml` | Секреты кошелька (0600) |
| `CHANNEL_PORT_START`/`_END` | авто | Пул портов channel-серверов |
| `SESSIONS_DIR` | `~/tg-claude-sessions` | Директория сессий (дефолт прежний — совместимость) |
| `MAX_INSTANCES` | 5 | Лимит одновременных сессий |
| `CLAUDE_BIN` | `claude` | Путь к бинарнику Claude Code |
| `DEFAULT_MODEL` / `DEFAULT_EFFORT` | — | Модель/effort новых сессий; `/model` перекрывает |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Свой профиль Claude Code |
| `CLAUDE_ENV_<ИМЯ>` | — | Проброс env в процесс claude |
| `ORCH_PORT` / `ORCH_HOST` | `18080` / `127.0.0.1` | Внутренний HTTP ядра |
| `ORCH_TOKEN` | автогенерация | Секрет внутреннего API (зафиксируй для стабильности) |
| `SHOW_TOOL_CALLS` | `true` | Вызовы инструментов в статус-бабле |
| `DELETE_BUBBLE` | `true` | Удалять бабл после ответа (`false` — журнал) |
| `SHOW_COMMAND_MENU` | `true` | Меню «/» в Telegram |
| `INCOMING_DIR` | `incoming` | Куда класть присланные файлы |
| `PERMISSION_MODE` | `auto` | `auto`/`bypass`/`acceptEdits`/`manual`/`dontAsk`/`plan` |
| `SANDBOX` | `bwrap` | `bwrap` / `agent-vm` (эксперимент) / `off` |
| `SANDBOX_EXTRA_RW` | — | Доп. RW-пути из песочницы (через `:`) |
| `SANDBOX_DBUS` | `true` | Проброс **всего** system D-Bus в песочницу (для mDNS/`.local`/avahi-browse); `off` — запретить |
| `AGENT_VM_MEMORY_GIB`/`_CPUS`/`_IMAGE` | — | Ресурсы/пин образа microVM |
| `BOT_LANG` | `ru` | Язык сообщений: `ru` / `en` |
| `IDLE_TIMEOUT_H` | 6 | Авто-останов после N ч простоя (0 — выкл.) |
| `LOG_MAX_MB` | 10 | Ротация `claude.log` (0 — выкл.) |

⚠️ **Безопасность.** Доступ строго по белому списку: пустой
`ALLOWED_USER_IDS` = все сообщения игнорируются. Кто в списке — тот и
одобряет permission-запросы. Веб-интерфейс защищён токеном, но наружу
(не localhost) выставляй только за reverse-proxy с TLS.

## Веб-интерфейс (`ADAPTERS=…,web`)

Локальная SPA (vanilla JS, без CDN) на `http://127.0.0.1:8180/?token=…`
(URL с токеном печатается в лог при старте, как у Jupyter):

- список сессий со статусами, создание (имя + путь проекта), остановка/удаление;
- чат сессии: история (журнал ядра) + живые события по WebSocket;
- статус-бабл в реальном времени с кнопками «⏹ Стоп» и «⛔ Прервать»;
- permission-запросы карточками ✅/❌ (первый ответ побеждает — хоть из веба,
  хоть из Telegram);
- файлы в обе стороны (drag&drop / скрепка; от Claude — ссылки на скачивание,
  строго внутри workspace сессии);
- `/stats`, `/usage`, смена модели, `/compact`, `/clear`, bash-панель.

Работает отдельно (`ADAPTERS=web` — без Telegram вообще) или параллельно.

## Файловая песочница (`SANDBOX`)

По умолчанию (`SANDBOX=bwrap`) процесс `claude` и `/bash`-терминал каждой
сессии заперты в mount-namespace ([bubblewrap](https://github.com/containers/bubblewrap),
без root). Наружу видны **только**:

| Что | Доступ | Зачем |
|-----|--------|-------|
| Папка сессии + папка проекта | чтение/запись | собственно работа |
| Приватный дом сессии (`SESSIONS_DIR/.homes/<имя>`) | как `$HOME` | venv/кэши агента, **переживают рестарт** |
| `CLAUDE_CONFIG_DIR` + `~/.claude.json` | чтение/запись | токены, скиллы, транскрипты |
| Бинарь claude, репозиторий оркестратора | чтение | запуск, MCP-канал, `wallet` CLI |
| Системный рантайм (`/usr`, `/etc`, `/run/systemd/resolve`) | чтение | библиотеки, TLS, **DNS при systemd-resolved** |
| `SANDBOX_EXTRA_RW` | чтение/запись | доп. каталоги |

Всё остальное — другие проекты, `~/.ssh`, `~/.aws`, реальный `$HOME` — не
видно ни на чтение, ни на запись. В отличие от нативного `/sandbox` Claude
Code (только Bash-тул), обёртка накрывает все инструменты разом.

⚠️ **`$HOME` изолирован.** Раз реальный дом скрыт, глобальный `~/.venv` и
инструменты из твоего дома агенту не видны. Держи окружение **в проекте**
(`python -m venv .venv` в папке проекта — она RW и переживает рестарты); venv
в `~` сессии тоже переживёт рестарты (дом персистентный), но проект чище.

**Сеть** общая с хостом (нужна для API и localhost-ядра). DNS работает и под
песочницей (цель симлинка `resolv.conf` проброшена). mDNS/локальная сеть
(`.local`-хосты, `avahi-browse`, DNS-SD) доступны при `SANDBOX_DBUS=true`
(по умолчанию) — для этого в песочницу проброшен **весь** system D-Bus (не
только Avahi: systemd/logind/NetworkManager тоже, read-методы работают,
мутации под polkit). `SANDBOX_DBUS=off` запрещает его (базовый `.local`-резолв
хоста остаётся — он идёт multicast'ом без D-Bus).

`SANDBOX=agent-vm` — сессии в microVM через
[wirenboard/agent-vm](https://github.com/wirenboard/agent-vm): жёсткая
изоляция ядра ОС, креды через host-side прокси. Каркас готов
(`runners/agentvm.py`), живой прогон — см. `docs/agent-vm-integration.md`;
одна сессия на каталог (гвард в ядре). `SANDBOX=off` — без изоляции.

## Кошелёк секретов (`MODULES=wallet`)

Токены/пароли для CLI-тулз (gh, git push, kubectl…) **без доступа модели к
значениям** — секрет не существует в адресном пространстве песочницы:

- секреты и policy — `WALLET_SECRETS_FILE` (TOML, 0600, вне allowlist
  песочницы): каким сессиям доступен, шаблоны разрешённых команд, нужен ли
  confirm;
- в сессии доступен CLI: `wallet ls`, `wallet run <имя> -- gh pr list` —
  демон ядра исполняет команду **на хосте** с секретом в env ребёнка;
- известные значения секретов вымарываются из stdout/stderr (`•••`);
- каждый запуск виден: строка `🔐 wallet: …` в бабле, при `confirm=true` —
  кнопки подтверждения во всех адаптерах до исполнения.

Формат и модель угроз: `docs/secrets-wallet.md`. С `SANDBOX=off` кошелёк
бессмыслен (модель прочитает файл секретов напрямую) — модуль предупредит.

## Режимы разрешений и permission relay

`PERMISSION_MODE` — как в прежних версиях (`auto` по умолчанию; `bypass` =
`--dangerously-skip-permissions`). Во всех режимах, кроме `bypass`, запросы
разрешений прилетают кнопками ✅/❌ во все интерфейсы; применяется первый
ответ (параллельно остаётся и локальный TUI-диалог). Канальные тулы
(`reply_to_user`, `send_file_to_user`) предразрешены. `AskUserQuestion`
запрещён (виснет без TUI) — Claude спрашивает через канал нумерованными
вариантами.

## Команды (Telegram)

Меню команд регистрируется автоматически. В основном чате: `/new <имя>`,
`/new [имя] /путь`, `/list`, `/ls [путь]`, `/skills`, `/chat_id`, `/help`.

В топике сессии: текст/фото/файл — Claude (остановленная сессия возобновится
сама); `/stats`, `/usage`, `/model [имя]`, `/compact`, `/clear`,
`/close_session`, `/delete_session`, `/bash <cmd>` и `/bashin <ввод>`
(постоянный терминал мимо Claude, в той же песочнице и с тем же домом
сессии); прочие `/команды` печатаются в терминал Claude Code.

## Жизненный цикл сессии

```
/new ──> работает ──/close_session──> остановлена ──сообщение──> resume
                    (или падение,     (топик/запись живы)  │
                     или рестарт                           ├─ claude --resume — контекст продолжен
                     оркестратора)                         └─ не вышло → чистый старт (честно сообщается)
```

- Готовность = channel-сервер отвечает на `/ping` (до 60 с).
- Смерть Claude ловит watcher → уведомление с хвостом `claude.log` во все
  интерфейсы; простой > `IDLE_TIMEOUT_H` — авто-останов с сохранением записи.
- Состояние в `SESSIONS_DIR/.sessions.json` (атомарно); старый формат
  (thread_id) мигрирует в bindings автоматически.

## Статус-бабл

Пока Claude работает, в каждом интерфейсе живёт обновляемый статус:

```
⏳ Работаю…
📨 почини тесты
⚡ Bash: pytest -x
  ↳ 5× 📖 Read: conftest.py     ← тулы сабагента, схлопнуто
💬 Нашёл причину, чиню conftest.py
[⏹ Стоп-отчёт] [⛔ Прервать]
```

- События — из PreToolUse-хука; серии одинаковых вызовов схлопываются
  (`N×`), сабагенты — с отступом. `SHOW_TOOL_CALLS=false` отключает.
- **⏹ Стоп-отчёт** — мягкая остановка (push «сверни работу и отчитайся»).
- **⛔ Прервать** — настоящий Esc в PTY: ход обрывается немедленно, контекст
  сессии сохраняется (эквивалент Esc в TUI; в самом channels-протоколе
  прерывания нет).
- Финал (`complete=true`) приходит обычным сообщением, бабл удаляется
  (`DELETE_BUBBLE=false` — остаётся журналом).
- Stop-хук страхует «потерянный финал»: если ход кончился голым текстом без
  reply-тула, текст доотправляется.

## Протокол (по [channels-reference](https://code.claude.com/docs/en/channels-reference))

- Capability `claude/channel` + `claude/channel/permission`; push
  `notifications/claude/channel` c `{content, meta:{context_id}}`.
- `context_id = <адаптер>:<имя-сессии>:<токен-адаптера>` — по нему ядро
  находит сессию и отдаёт адаптеру-источнику reply-цитату.
- Ответ: тул `reply_to_user(context_id, text, complete)`; файлы —
  `send_file_to_user` (только по явной просьбе пользователя; jail по
  workspace). MCP-сервер сессии называется `channel-<имя>`.
- Запуск: `claude --session-id=<uuid> [--model …] [--effort …]
  --mcp-config <сессия>/.mcp.json --settings <сессия>/.claude/settings.local.json
  --dangerously-load-development-channels server:channel-<имя>`, cwd = папка
  проекта; изоляция — через раннер.
- Хуки (PreToolUse + Stop) — один скрипт-диспетчер с токеном в 0600-файле.

## Как расширять

Принцип проекта — **наблюдаемость**: всё, что модель делает в фоне, должно
быть видно пользователю в каждом интерфейсе.

- **Новый интерфейс (Matrix, …)** — подпакет в `adapters/` с реализацией
  `Transport` + ветка в `adapters.make_adapters` + имя в
  `config._parse_adapters`. Ядро трогать не нужно
  (см. `docs/messaging-connector.md`).
- **Новый способ изоляции** — модуль в `runners/` (протокол `Runner`:
  `wrap`, `preflight`, `unique_cwd`) + ветка в `make_runner` + значение
  `SANDBOX`.
- **Новый модуль** — подпакет в `modules/` (объект с `name`,
  `start(core)`, `stop()`) + ветка в `modules.make_modules` + имя в
  `config._parse_modules`. В ядре для модулей есть `core.session_hooks`
  (обвязка новых сессий) и `core.request_confirmation()` (кнопки ✅/❌ во
  всех интерфейсах).
- **Новая команда** — логика в `core/app.py`, тонкие обработчики в
  адаптерах; тексты — парой ru/en в `core/texts.py` (паритет проверяет
  smoke-тест).

После правок: `.venv/bin/python -m pytest` и `.venv/bin/ruff check .`.

## Дизайн-документы (`docs/`)

| Документ | Что описывает |
|----------|----------------|
| [`docs/agent-vm-integration.md`](docs/agent-vm-integration.md) | Сессии в microVM (wirenboard/agent-vm): каркас готов, план живого прогона |
| [`docs/secrets-wallet.md`](docs/secrets-wallet.md) | Кошелёк секретов: модель угроз, policy, этапы (этап 1 реализован) |
| [`docs/messaging-connector.md`](docs/messaging-connector.md) | Транспорт-коннектор: реализован; памятка для новых адаптеров |
| [`docs/archive/`](docs/archive/) | Исторические ревью |

## Лицензия

[MIT](LICENSE) © Alexandr Degtyarev
