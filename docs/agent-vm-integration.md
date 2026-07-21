# Интеграция с agent-vm: сессии в microVM вместо bwrap

Статус 2026-07: **каркас реализован** — `orchestrator/runners/agentvm.py`
(`SANDBOX=agent-vm`): сборка argv (`--allow-host`, `--publish` порта канала,
`--mount` путей сессии/репозитория, ресурсы/образ из `AGENT_VM_*`),
`preflight()` (наличие agent-vm и /dev/kvm), гвард «одна сессия на cwd»
(`unique_cwd`) в SessionManager; тесты `tests/runner_agentvm_test.py`. Guest-facing
адресация «сессия → оркестратор» параметризована через `config.orch_host`
(последний хардкод `127.0.0.1` в хук-диспетчере исправлен 2026-07-21 — см. статус
ниже). НЕ сделано: живой сквозной прогон (шаг 1 ниже; agent-vm требует машину с
KVM) — до него режим считать экспериментальным. Цель: поднимать сессии
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
| `.mcp.json` env `ORCH_HOST` | `sessions.py` `_write_mcp_json` | ✅ уже `config.orch_host` |
| channel_server → оркестратор (`/reply`, `/permission`) | `channel_server.py` | ✅ читает `ORCH_HOST` из env |
| reply-сервер bind | `reply_server.py` | ✅ уже слушает на `config.orch_host` |
| **хук-диспетчер → оркестратор** | `hookscript.py` | ✅ **ИСПРАВЛЕНО** (был хардкод `127.0.0.1`; теперь `render(host, …)` из `config.orch_host`) |
| канал bind внутри гостя | `channel_server.py` (`TCPSite host=127.0.0.1`) | ⚠️ открыто: работает ли `agent-vm --publish` как NAT на loopback гостя, или нужен bind `0.0.0.0` — **выяснить живым экспериментом (шаг 1)** |
| оркестратор → канал (`/ping /notify /permission`) | `sessions.py` (127.0.0.1:port) | ⚠️ зависит от семантики `--publish` (тот же эксперимент) |

Вывод: при верном `ORCH_HOST=<host-gateway>` в `.env` вся адресация «сессия →
хост» готова. Осталось подтвердить семантику `--publish` живьём (bind канала).

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

1. **Канал и хуки через границу VM.** Адресация «сессия → оркестратор» уже
   параметризована через `config.orch_host` (см. таблицу статуса выше:
   `.mcp.json`, channel_server, reply-сервер, а теперь и hook_dispatch.py —
   последний хардкод исправлен). Остаётся:
   - `ORCH_HOST` в `.env` выставить в IP host-gateway гостя + `--allow-host`
     (guest → host); конкретный IP — из живого эксперимента;
   - оркестратор → channel_server (`/notify`, `/ping`): канал слушает внутри
     гостя → `--publish <port>`; открытый вопрос — держит ли `--publish`
     совместимость с bind канала на `127.0.0.1` (иначе бинтить `0.0.0.0`).
   Без верной адресации бот слепнет (ни бабла, ни ответов). Именно это —
   первый эксперимент (шаг 1, runbook ниже).
2. **channel_server.py внутри гостя.** Его спавнит сам Claude по `.mcp.json`.
   В госте должен быть python3 (в Debian-образе есть) и сам файл — монтируем
   репозиторий оркестратора. `.mcp.json` должен указывать на путь внутри
   гостя и системный python (не `.venv` хоста — бинарная несовместимость
   не грозит, но зависимостей aiohttp в госте нет → либо ставить в образ,
   либо vendored-вариант канала без aiohttp на stdlib).
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
  инструментов). Нужно: (1) демон слушает на host-gateway-достижимом адресе
  (новый `WALLET_HOST`/переиспользовать `ORCH_HOST`) + `--allow-host` — ⚠️
  security: демон секретов расширяет сетевую поверхность, обязательно оставить
  bind на loopback под bwrap и открывать шире ТОЛЬКО под agent-vm, токен-гейт
  (`_authed`) обязателен; (2) `~/.wallet.json` URL = тот же host-gateway;
  (3) смонтировать `session_home` (или точечно `.wallet.json`+`.wallet-bin`) в
  гостя — `AgentVmRunner.wrap` должен пробрасывать `home_dir` через `--mount`;
  (4) прокинуть `session_env`-маркеры и PATH в env ГОСТЯ — механизм agent-vm для
  env выяснить (флаг `-e`? `.agent-vm.runtime.sh`-хук? — открытый вопрос).

**Рекомендация:** сначала A (прокси agent-vm) как дефолт — он уже закрывает
git/gh/network. B добавлять, только если реально нужны inject/shared-секреты в
VM-режиме, и только с явным security-ревью сетевой экспозиции демона.

## Runbook живого эксперимента (Шаг 1 — на машине с KVM+agent-vm)

Здесь agent-vm НЕ установлен (только `/dev/kvm`+docker), поэтому эксперимент
проводится на подготовленной машине. Что проверить и зафиксировать сюда:

1. Установить agent-vm (Node18+/Docker/KVM, `github.com/wirenboard/agent-vm`),
   `agent-vm --version`, `ls -l /dev/kvm`, членство в группе `kvm`.
2. Узнать host-gateway IP гостя (адрес, по которому гость видит хост при
   `--allow-host`) — вписать в `ORCH_HOST` `.env`.
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
