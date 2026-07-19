# Интеграция с agent-vm: сессии в microVM вместо bwrap

Статус 2026-07: **каркас реализован** — `orchestrator/runners/agentvm.py`
(`SANDBOX=agent-vm`): сборка argv (`--allow-host`, `--publish` порта канала,
`--mount` путей сессии/репозитория, ресурсы/образ из `AGENT_VM_*`),
`preflight()` (наличие agent-vm и /dev/kvm), гвард «одна сессия на cwd»
(`unique_cwd`) в SessionManager; тесты `tests/runner_agentvm_test.py`.
НЕ сделано: живой сквозной прогон (шаг 1 ниже) — до него режим считать
экспериментальным. Цель: поднимать сессии
Claude Code в изолированных виртуальных машинах через
[wirenboard/agent-vm](https://github.com/wirenboard/agent-vm) и управлять ими
так же, как обычными сессиями — топик на сессию, статус-бабл, /stats, /model.

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

Точка входа готова — **раннер-шов** (`orchestrator/runner.py`): и `claude`,
и `/bash` запускаются через `Runner.wrap(argv, chdir, extra_rw)`. Третий
раннер встаёт рядом с bwrap:

```python
class AgentVmRunner:
    name = "agent-vm"
    def wrap(self, argv, *, chdir, extra_rw):
        # argv = ["claude", "--session-id=…", …] → agent-vm сам знает "claude"
        return [
            "agent-vm", argv[0],
            "--allow-host",                      # хуки/канал → оркестратор на хосте
            "--publish", f"{port}:{port}",       # /notify /ping канала — с хоста в гостя
            "--mount", f"{repo}:{repo}",         # channel_server.py + .venv (RO нет — просить upstream)
            *(f"--mount={p}" for p in extra_rw), # папка сессии (cwd монтируется сам)
            "--", *argv[1:],
        ]
```

Выбор — тем же конфигом: `SANDBOX=agent-vm` (валидация в
`config._parse_sandbox`, ветка в `runner.make_runner`). Плюс новые ключи
`.env`: `AGENT_VM_MEMORY_GIB`, `AGENT_VM_CPUS`, `AGENT_VM_IMAGE` (пин образа —
ежечасные пересборки могут уехать от channels-флагов research preview).

«Сессия с несколькими VM» = то, что уже есть: топик ↔ сессия ↔ своя папка ↔
своя VM. `/list` показывает работающие VM, `/close_session` гасит процесс
`agent-vm` (VM умирает вместе с ним), resume поднимает новую VM в той же папке.

## Проблемы, которые надо решить (по убыванию тяжести)

1. **Канал и хуки через границу VM.** Сейчас всё на localhost:
   - оркестратор → channel_server: `POST 127.0.0.1:<port>/notify` — канал
     будет слушать внутри гостя → нужен `--publish <port>`;
   - channel_server/хуки → оркестратор: `POST 127.0.0.1:ORCH_PORT` — изнутри
     гостя это не хост → нужен `--allow-host`, а `ORCH_HOST`/адрес в
     `.mcp.json` и hook_dispatch.py должен стать IP host-gateway гостя.
   Без этого бот слепнет (ни бабла, ни ответов) — противоречит базовому
   принципу наблюдаемости. Это первый эксперимент, который надо провести
   руками до всякого кода.
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
   (вторая убьёт VM первой). Нужен гвард в `SessionManager.create`
   (отказ с понятным текстом) — при bwrap такой проблемы нет.
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
   agent-vm уже решает подмену HTTP-токенов — объединить дизайны.
5. **Upstream-хотелки** в wirenboard/agent-vm: несколько VM на один cwd
   (`--name`), RO-монтирование (`--mount :ro`), документированный способ
   guest→host для локального оркестратора, python-зависимости канала в образе.

## Почему не заменяем bwrap сразу

bwrap остаётся дефолтом: без KVM/Docker, мгновенный старт, ноль новых
зависимостей. agent-vm — опция для машин, где нужна жёсткая изоляция
(чужой код, длинные автономные сессии) и есть KVM. Раннер-шов позволяет держать
оба и выбирать per-инсталляция.
