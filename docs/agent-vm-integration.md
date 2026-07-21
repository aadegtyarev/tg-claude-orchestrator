# Интеграция с agent-vm: сессии в microVM вместо bwrap

Статус 2026-07: **каркас реализован** — `orchestrator/runners/agentvm.py`
(`SANDBOX=agent-vm`): сборка argv (`--allow-host`, `--publish` порта канала,
`--mount` путей сессии/репозитория, ресурсы/образ из `AGENT_VM_*`),
`preflight()` (наличие agent-vm и /dev/kvm), гвард «одна сессия на cwd»
(`unique_cwd`) в SessionManager; тесты `tests/runner_agentvm_test.py`. Guest-facing
адресация «сессия → оркестратор» выводится `config.guest_orch_host` (bind
reply-сервера при этом на host-side `orch_host`; последний хардкод `127.0.0.1` в
хук-диспетчере исправлен 2026-07-21). Живой
эксперимент (Шаг 1) ПРОВЕДЁН 2026-07-21 (`@wirenboard/agent-vm` 0.1.25 из npm) —
см. «Результаты живого эксперимента»: адресация guest→host и mount подтверждены,
остался один блокер (нет `aiohttp` в госте для `channel_server`). До его решения
`SANDBOX=agent-vm` считать экспериментальным. Цель: поднимать сессии
Claude Code в изолированных виртуальных машинах через
[wirenboard/agent-vm](https://github.com/wirenboard/agent-vm) и управлять ими
так же, как обычными сессиями — топик на сессию, статус-бабл, /stats, /model.

## Статус guest-facing адресации (аудит 2026-07-21)

Под agent-vm гость (сеть `public_only`) не видит хостовый loopback, поэтому ВСЕ
адреса «сессия → оркестратор» должны указывать на host-gateway (+ `--allow-host`),
а «оркестратор → канал» — на порт, проброшенный `--publish`. Аудит guest-facing
путей:

| Путь | Место | Статус |
|------|-------|--------|
| `.mcp.json` env `ORCH_HOST` (guest-facing) | `sessions.py` `_write_mcp_json` | ✅ `config.guest_orch_host` (agent-vm → host-gateway имя) |
| channel_server → оркестратор (`/reply`, `/permission`) | `channel_server.py` | ✅ читает `ORCH_HOST` из env |
| reply-сервер **bind** (host-side) | `reply_server.py` | ✅ `config.orch_host` (127.0.0.1) — host-resolvable, НЕ guest-facing имя |
| **хук-диспетчер → оркестратор** | `hookscript.py` | ✅ `render(config.guest_orch_host, …)` (был хардкод 127.0.0.1) |
| канал bind внутри гостя | `channel_server.py` (`TCPSite host=127.0.0.1`) | ✅ ПОДТВЕРЖДЕНО: `--publish HOST:GUEST` форвардит на loopback гостя (0.0.0.0 не нужен) |
| оркестратор → канал (`/ping /notify /permission`) | `sessions.py` (127.0.0.1:port) | ⚠️ `--publish` host→guest вживую ещё не прогонял (см. «Ещё не верифицировано») |

Вывод: bind (host-side) и guest-facing адрес РАЗВЕДЕНЫ — `Config.guest_orch_host`
выводит guest-facing из `sandbox`, reply-сервер биндится на `orch_host`
(127.0.0.1). Переключение движка = одно действие (`SANDBOX=agent-vm`); `ORCH_HOST`
менять НЕ нужно. Осталось прогнать `--publish` host→guest живьём.

## Что такое agent-vm (факты, важные для нас)

- **Stateless CLI-лаунчер** на Rust: `agent-vm claude [...args]` сам поднимает
  microVM (libkrun/microsandbox, старт ~2–3 с) и пробрасывает stdin/stdout.
  Демона/API нет; каждый вызов независим.
- **Гость** — Debian-образ (`ghcr.io/wirenboard/agent-vm-template`) с
  предустановленными claude/codex/opencode; образ пересобирается ежечасно.
- **Одна VM на директорию**: имя сандбокса — SHA256 от cwd; повторный запуск
  в том же cwd убивает старую VM (`.replace()`, 10 с grace).
- **I/O**: при TTY — `attach()` (интерактивный PTY), без TTY — `exec`
  с пробросом exit code. Наш запуск под PTY совместим.
- **Креды не попадают в гостя**: реальные токены остаются на хосте,
  в VM — плейсхолдеры; TLS-перехватывающий прокси подменяет их на выходе.
  GitHub-доступ ограничен allow-list'ом репозиториев.
- **Сеть гостя по умолчанию `public_only`**: loopback, RFC1918, host —
  запрещены. Есть флаги `--publish` (порт наружу), `--allow-host` (доступ
  к хосту), `--mount HOST[:GUEST]` (доп. бинды).
- Требования: Linux + KVM (`/dev/kvm`, группа `kvm`), Docker (registry для
  образа), Node 18+.

## Как это ложится на оркестратор

Точка входа готова — **раннер-шов** (пакет `orchestrator/runners/`): и `claude`,
и `/bash` запускаются через `Runner.wrap(argv, *, chdir, extra_rw, home_dir,
publish_ports)`. Раннер объявляет `supports_prefix` (можно ли изолировать
отдельный `/bash`); у agent-vm он `False` — `/bash` в VM отдельно от claude не
поднять (одна VM на cwd), ядро отдаёт понятный отказ. Реализация —
`runners/agentvm.py`; ниже упрощённый эскиз (актуальную сборку argv см. в коде):

```python
class AgentVmRunner:
    name = "agent-vm"
    unique_cwd = True          # имя VM = hash(cwd) → вторая сессия убьёт первую
    supports_prefix = False    # отдельный /bash в VM не изолировать
    def wrap(self, argv, *, chdir, extra_rw, home_dir=None, publish_ports=()):
        if not argv:           # префикс-режим (/bash) — в VM не заворачиваем
            return []
        cmd, *rest = argv      # agent-vm сам знает "claude"
        out = ["agent-vm", Path(cmd).name, "--allow-host"]
        for port in publish_ports:
            out += ["--publish", f"{port}:{port}"]   # /notify /ping канала
        for m in {chdir, *extra_rw}:
            out += ["--mount", f"{m}:{m}"]           # channel_server.py + пути сессии
        return out + ["--", *rest]
```

Выбор — тем же конфигом: `SANDBOX=agent-vm` (валидация в
`config._parse_sandbox`, ветка в `runner.make_runner`). Плюс новые ключи
`.env`: `AGENT_VM_MEMORY_GIB`, `AGENT_VM_CPUS`, `AGENT_VM_IMAGE` (пин образа —
ежечасные пересборки могут уехать от channels-флагов research preview).

«Сессия с несколькими VM» = то, что уже есть: топик ↔ сессия ↔ своя папка ↔
своя VM. `/list` показывает работающие VM, `/close_session` гасит процесс
`agent-vm` (VM умирает вместе с ним), resume поднимает новую VM в той же папке.

## Проблемы, которые надо решить (по убыванию тяжести)

1. **Канал и хуки через границу VM.** ✅ РЕШЕНО. guest-facing адрес выводится
   `Config.guest_orch_host` из `sandbox` (agent-vm → `host.microsandbox.internal`,
   раннер даёт `--allow-host`), reply-сервер биндится на `orch_host` (127.0.0.1,
   host-side). Переключение = одно действие `SANDBOX=agent-vm`. `--publish` порта
   канала (host→guest) собирает `AgentVmRunner.wrap`; bind канала на `127.0.0.1`
   в госте совместим (`--publish` форвардит на loopback гостя — подтверждено).
   Остаётся прогнать `--publish` host→guest вживую. Раньше при неверной адресации
   первый эксперимент (шаг 1, runbook ниже).
2. **channel_server.py внутри гостя.** Его спавнит сам Claude по `.mcp.json`.
   Python3 в госте есть (3.13), сам файл — монтируем репозиторий; `.mcp.json`
   указывает на путь внутри гостя и системный python. **ПОДТВЕРЖДЕНО (Шаг 1):
   `aiohttp` в госте НЕТ** → это главный блокер. Варианты обхода (A/B/C/D) — в
   «Результаты живого эксперимента». Рекомендация — stdlib-канал (D), но это
   решение оператора (prod-критичный компонент).
3. **Транскрипты и /stats.** `transcript.py` читает
   `CLAUDE_CONFIG_DIR/projects/...` — внутри гостя это отдельный дом.
   State agent-vm живёт на хосте (`${XDG_STATE_HOME}/agent-vm/<hash>/`) —
   выяснить, попадает ли туда `~/.claude/projects` гостя; если нет —
   монтировать свой каталог под транскрипты или читать их через
   `agent-vm shell`-вызов.
4. **Одна VM на cwd.** Две сессии на одну и ту же `linked_path` невозможны
   (вторая убьёт VM первой). Гвард уже есть — `_guard_unique_cwd` (вызывается
   в `create`/resume/clear, отказ с понятным текстом); при bwrap проблемы нет.
5. **Login/креды.** agent-vm сам берёт `~/.claude/.credentials.json` хоста и
   работает через свой прокси — `CLAUDE_CONFIG_DIR` из нашего `.env` теряет
   смысл внутри VM; `CLAUDE_ENV_*` надо пробрасывать через env гостя
   (у agent-vm есть `-e`? проверить; если нет — через
   `.agent-vm.runtime.sh`-хук в папке проекта).

## Результаты живого эксперимента (Шаг 1 — 2026-07-21)

Проведён на этой машине после установки **`@wirenboard/agent-vm` 0.1.25** из npm
(«built on microsandbox», НЕ Lima; ставится в `~/.npm-global`, node≥18, без sudo).
`agent-vm setup` скачал+проверил образ `ghcr.io/wirenboard/agent-vm-template:latest`
(microVM грузится ~3с warm; внутри Claude Code 2.1.216, codex 0.144.6).

**Подтверждено живьём (smoke-тесты через `agent-vm shell --allow-host`):**
- ✅ **microVM грузится**, `/workspace` (проброс cwd) **RW** — гость пишет файл,
  хост читает. Наши `--mount session_dir/repo` сработают. Гость: Debian, root,
  python **3.13.5**, есть node/npm/git/gh/claude/codex.
- ✅ **guest→host loopback НАПРЯМУЮ** через `host.microsandbox.internal` +
  `--allow-host`: и `curl`, и `python-urllib` достучались до хостового сервиса,
  **который слушал на 127.0.0.1** (важно: microsandbox мапит это имя на хостовый
  loopback, а не на отдельный gateway-IP). Это ровно механизм «channel_server/
  хуки → оркестратор». Реализовано как **производный `Config.guest_orch_host`**:
  под `SANDBOX=agent-vm` → `host.microsandbox.internal` (в `.mcp.json`/хук-скрипт),
  а reply-сервер продолжает биндиться на `orch_host`=127.0.0.1 (host-side; само
  имя `host.microsandbox.internal` на ХОСТЕ не резолвится — bind им упал бы
  gaierror). **Переключение движка = ОДНО действие: `SANDBOX=agent-vm`**, адреса
  выводятся сами, `ORCH_HOST` менять НЕ нужно.
- ✅ **Прокси-нюанс СНЯТ**: в госте `HTTP_PROXY`/`NO_PROXY` пусты (egress agent-vm
  рулит на сетевом уровне, не через env) — POST'ы канала/хуков идут прямо, без
  обхода прокси. Хук-скрипт на stdlib `urllib` — работает в госте как есть.
- ✅ Адресация не зависит от env-инheritance: `.mcp.json` (env-блок, монтируется)
  кормит channel_server; hook_dispatch.py (host вшит на рендере, монтируется) —
  хуки. Оба независимы от пустого env гостя.
- ✅ Флаги CLI совпадают с `AgentVmRunner.wrap`: `--mount HOST[:GUEST]`,
  `-p/--publish [BIND:]HOST_PORT:GUEST_PORT` (docker-style, HOST_BIND по умолч.
  127.0.0.1 → канал в госте достижим оркестратором на `127.0.0.1:port`; bind
  канала на 0.0.0.0 НЕ нужен), `--allow-host`. Есть и `--auto-publish` (зеркалит
  ВСЕ гостевые listeners на хост loopback — альтернатива явному `--publish`).

**❌ Блокер полной интеграции: `aiohttp` в госте НЕТ.** `channel_server.py`
(564 стр.) держит на aiohttp и исходящие POST (легко на stdlib `urllib`), и
ВХОДЯЩИЙ сервер `/ping /notify /permission` (интегрирован с asyncio-циклом
MCP-stdio — сложнее: нужен `http.server` в потоке + мост в event loop). Варианты:

| Вариант | Суть | Плюсы | Минусы |
|---------|------|-------|--------|
| **A. Кастомный образ** | `--image`/`AGENT_VM_IMAGE_TAG` с предустановленным aiohttp | канал не трогаем | нужен свой OCI-образ (Docker/registry), уедет от ежечасных пересборок upstream |
| **B. pip в госте per-session** | обёртка `pip install aiohttp && claude …` | без образа | у нового agent-vm НЕТ runtime-хука; VM эфемерны → ставить каждый старт (+латентность, сеть) |
| **C. Vendored aiohttp через --mount** | — | — | НЕ годится: C-расширения aiohttp несовместимы (host py3.12 vs guest py3.13) |
| **D. channel на stdlib** (реком.) | переписать channel_server без aiohttp (http.server) | ноль внешних зависимостей, работает и в bwrap, и в VM; наш код | **prod-критично**: канал на пути КАЖДОЙ сессии (вкл. bwrap) — тонкий async/threading-баг сломает всё; нужен careful review + прогон, не overnight-автомерж |

**Рекомендация:** D (stdlib-канал) — но это осознанное решение оператора: либо
цельный переписанный канал (риск для prod-пути, тщательное ревью + деплой под
присмотром), либо агент-vm-ONLY stdlib-вариант рядом с aiohttp-версией (дублирование,
но нулевой риск для bwrap). До этого решения `SANDBOX=agent-vm` доходит до старта
VM, но канал в госте не поднимется (нет aiohttp) → сессия не заработает.

**Ещё не верифицировано:** `--publish` host→guest вживую (оркестратор → канал в
госте); транскрипты/stats внутри VM (проблема #3); конфликт `CLAUDE_CONFIG_DIR`/
креды (проблема #5 — agent-vm держит Claude-auth сам через свой прокси).

## Поэтапный план

1. **Ручной эксперимент** (без кода): `agent-vm claude` в папке сессии с
   `--publish`/`--allow-host`/`--mount`, проверить: старт под PTY, спавн
   channel_server, handshake, push→ход→ответ, PreToolUse-хук доносится.
   Результат зафиксировать здесь.
2. ✅ **AgentVmRunner** + `SANDBOX=agent-vm` + гвард уникальности cwd +
   конфиг-ключи. Тесты: сборка argv (`tests/runner_agentvm_test.py`), без VM.
3. **Наблюдаемость и статистика**: транскрипты/stats/usage внутри VM.
4. **Кошелёк секретов** ([secrets-wallet.md](secrets-wallet.md)): прокси
   agent-vm уже решает подмену HTTP-токенов — объединить дизайны (детали ниже).
5. **Upstream-хотелки** в wirenboard/agent-vm: несколько VM на один cwd
   (`--name`), RO-монтирование (`--mount :ro`), документированный способ
   guest→host для локального оркестратора, python-зависимости канала в образе.

## Кошелёк секретов под agent-vm — дизайн (Этап 4)

Под bwrap кошелёк работает так: демон на `127.0.0.1:<порт>` (host); в приватный
`$HOME` сессии (`SESSIONS_DIR/.homes/<имя>`, смонтирован в песочницу как `$HOME`)
кладётся `~/.wallet.json` (URL демона + токен) и каталог обёрток `.wallet-bin`
(ставится первым в PATH); `session_env` кладёт маркеры/значения секретов в env
процесса. Под agent-vm КАЖДЫЙ из четырёх механизмов ломается (аудит 2026-07-21):

| Механизм | Под bwrap | Под agent-vm (разрыв) |
|----------|-----------|------------------------|
| Демон слушает | `127.0.0.1` (`module.py`) | гость `public_only` не видит loopback хоста |
| `~/.wallet.json` URL | `http://127.0.0.1:<порт>` | тот же loopback-разрыв |
| `$HOME` сессии (где лежат `.wallet.json`/`.wallet-bin`) | смонтирован в песочницу | `AgentVmRunner.wrap` НЕ монтирует `home_dir` — у гостя свой $HOME |
| `session_env`/PATH | env процесса под bwrap | применяются к env ХОСТОВОГО `agent-vm`-лаунчера, не к процессу в госте |

**Два пути интеграции (выбрать после живого эксперимента):**

- **A. Опереться на встроенный прокси agent-vm.** agent-vm сам держит креды на
  хосте и подменяет HTTP-токены в госте плейсхолдерами (TLS-MITM), GitHub — по
  allow-list репозиториев. Это ПОКРЫВАЕТ основной юзкейс кошелька (gh/git/network
  без выдачи токена модели) БЕЗ нашего демона. Тогда под agent-vm кошелёк-модуль
  не подключаем (уже так: `_default_modules` не включает wallet вне bwrap), а
  host-passthrough отдаём прокси agent-vm. Плюс: ноль новой сетевой поверхности.
  Минус: наши inject/shared-секреты (произвольные env, не только git/gh) прокси
  не покрывает — только то, что ходит по HTTP через его MITM.

- **B. Пробросить наш демон в гостя** (для inject/shared-секретов и не-HTTP
  инструментов). Хорошая новость по эксперименту: демон может **остаться на
  127.0.0.1** — гость достучится до него через `host.microsandbox.internal` +
  `--allow-host` (как reply-сервер), БЕЗ расширения bind (security-риск снят).
  Нужно: (1) `~/.wallet.json` URL = `host.microsandbox.internal:<port>` под
  agent-vm (по аналогии с `guest_orch_host`); (2) смонтировать `session_home`
  (или точечно `.wallet.json`+`.wallet-bin`) в гостя — `AgentVmRunner.wrap`
  сейчас `home_dir` игнорирует, надо пробросить через `--mount`; (3) прокинуть
  `session_env`-маркеры и PATH в env ГОСТЯ — но env гостя ПУСТ (эксперимент: ORCH_*
  тоже не наследуются), у нового agent-vm НЕТ runtime-хука → механизм неясен
  (кандидаты: свой wrapper-скрипт как agent-cmd, кастомный образ). Открытый вопрос.

**Рекомендация:** сначала A (прокси agent-vm) как дефолт — он уже закрывает
git/gh/network. B добавлять, только если реально нужны inject/shared-секреты в
VM-режиме (осн. блокер B — проброс env-маркеров в гостя, не сетевой bind).

## Runbook живого эксперимента (Шаг 1) — ВЫПОЛНЕН 2026-07-21

Результаты — в разделе «Результаты живого эксперимента» выше. Ниже — исходный
чек-лист (что проверяли), оставлен для воспроизводимости на другой машине:

1. Установить `@wirenboard/agent-vm` из npm (`npm i -g @wirenboard/agent-vm`,
   Node18+/KVM), `agent-vm --version`, `agent-vm setup` (тянет base-образ).
2. guest→host: гость видит хост как `host.microsandbox.internal` (микросэндбокс
   мапит на хостовый 127.0.0.1 при `--allow-host`). В коде это выводит
   `Config.guest_orch_host` — вручную ничего вписывать НЕ нужно.
3. Ручной старт в папке сессии:
   `agent-vm claude --allow-host --publish <port>:<port> --mount <repo>:<repo> --mount <session_dir>:<session_dir> -- --mcp-config … --settings …`
   Проверить: старт под PTY; Claude спавнит `channel_server.py` в госте (есть ли
   python3 + aiohttp в образе? если нет — vendored-канал на stdlib или доустановка
   в образ); handshake `/ping`; `push → ход → ответ` доходит; PreToolUse-хук
   доносится на хост (теперь адрес хука параметризован — проверить, что бьёт на
   host-gateway).
4. Выяснить bind канала: работает ли `--publish` c `TCPSite host=127.0.0.1` в
   госте, или канал надо бинтить на `0.0.0.0` (тогда — новый флаг/ветка).
5. Выяснить env-passthrough agent-vm (для `CLAUDE_ENV_*` и wallet-маркеров):
   есть ли `-e KEY=VAL`? Проброс через runtime-хук?
6. Транскрипты/stats: где живёт `~/.claude/projects` гостя, попадает ли в
   state-каталог agent-vm на хосте, читаем ли мы его для `/stats`.

Результаты записать сюда — они разблокируют шаги 3–5 и выбор A/B для кошелька.

## Почему не заменяем bwrap сразу

bwrap остаётся дефолтом: без KVM/Docker, мгновенный старт, ноль новых
зависимостей. agent-vm — опция для машин, где нужна жёсткая изоляция
(чужой код, длинные автономные сессии) и есть KVM. Раннер-шов позволяет держать
оба и выбирать per-инсталляция.
