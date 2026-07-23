"""CLI `claude-box`: запустить команду в песочнице через box.launch + Engine.

Первый срез Launcher (Слой 2, docs/ARCHITECTURE-claude-box.md §5.1). Что делает:

  1. Сборка argv: минимальный Engine-конфиг (только то, что читают make_runner
     и Runner.wrap) → раннер bwrap|off → wrap([claude, …], chdir=cwd,
     extra_rw=[cwd]). БЕЗ session/channel/hooks/wallet — их докидывает Слой 3.
  2. Запуск: box.launch(argv, …) поднимает процесс под PTY и драйвером вывода.
  3. PTY-relay: stdin терминала → master-fd процесса, вывод (on_output) → stdout;
     терминал в raw при tty, восстановление на выходе; дождаться процесса,
     вернуть его код. Ctrl-C уходит в процесс (raw), fd не текут (драйвер владеет
     и закрывает master; мы джойним его поток).

НЕ в этом срезе (честные заглушки, а не тихий no-op — правило прозрачности):
`--vm`, `--profile`, `--wallet`, `-p`, подкоманды `init`/`profile`/`connect`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import termios
import tty
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

from box.launch import launch
from box.pty import TERM_COLS, TERM_ROWS
from orchestrator.runners import Runner, make_runner


# ── Минимальный Engine-конфиг ────────────────────────────────────────────────
# make_runner читает только .sandbox; BwrapRunner.wrap — .claude_config_dir,
# .sandbox_extra_rw, .sandbox_dbus; DirectRunner (off) — ничего. Полный
# orchestrator.config.Config для этого не нужен (и требовал бы TELEGRAM_BOT_TOKEN
# и десятки полей UX-оркестратора — это Слой 3). Здесь ровно нужный минимум.
@dataclass(frozen=True)
class EngineConfig:
    sandbox: str  # "bwrap" | "off"
    claude_config_dir: Path | None = None
    sandbox_extra_rw: tuple[Path, ...] = ()
    sandbox_dbus: bool = True


ENGINES = ("bwrap", "off")
_STUB_SUBCOMMANDS = ("init", "profile", "connect")
# Флаги следующих срезов: распознаём, чтобы дать честный отказ, а не «unknown».
_STUB_FLAGS = ("--vm", "--profile", "--wallet", "-p")

_USAGE = (
    "claude-box [--engine bwrap|off] [-- <аргументы claude>]\n"
    "  Запустить claude (или CLAUDE_BIN) в песочнице и отдать терминал.\n"
    "  --engine bwrap  файловая песочница bubblewrap (по умолчанию)\n"
    "  --engine off    без изоляции\n"
    "  --              всё, что после, пробрасывается в claude\n"
    "  -h, --help      эта справка\n"
    "\nНе реализовано в этом срезе (следующие срезы): "
    "--vm, --profile, --wallet, -p, init/profile/connect."
)


class CliError(SystemExit):
    """Ошибка разбора аргументов: печатает в stderr и выходит с кодом 2."""

    def __init__(self, message: str):
        sys.stderr.write("claude-box: " + message + "\n")
        super().__init__(2)


def repo_root() -> Path:
    """Корень репозитория (RO-виден в bwrap как код/венв)."""
    return Path(__file__).resolve().parent.parent


def _config_dir_from_env() -> Path | None:
    raw = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


# ── Разбор аргументов ────────────────────────────────────────────────────────
def parse_args(argv: Sequence[str]) -> tuple[str, list[str]]:
    """Разобрать argv → (engine, passthrough). Заглушки/ошибки → CliError.

    Всё после первого `--` — сквозные аргументы claude (не парсятся здесь).
    """
    argv = list(argv)
    if "--" in argv:
        cut = argv.index("--")
        opts, passthrough = argv[:cut], argv[cut + 1:]
    else:
        opts, passthrough = argv, []

    if opts and opts[0] in _STUB_SUBCOMMANDS:
        raise CliError(
            f"подкоманда «{opts[0]}» ещё не реализована (следующие срезы). "
            "Пока доступен только запуск: claude-box [--engine bwrap|off] [-- …]."
        )

    engine = "bwrap"
    i = 0
    while i < len(opts):
        a = opts[i]
        if a in ("-h", "--help"):
            sys.stdout.write(_USAGE + "\n")
            raise SystemExit(0)
        if a == "--engine":
            if i + 1 >= len(opts):
                raise CliError("--engine требует значение (bwrap|off)")
            engine = opts[i + 1]
            i += 2
            continue
        if a.startswith("--engine="):
            engine = a.split("=", 1)[1]
            i += 1
            continue
        if a in _STUB_FLAGS:
            raise CliError(
                f"флаг «{a}» ещё не реализован (следующие срезы: "
                "профили/кошелёк/VM/unattended). Сейчас — только запуск в "
                "песочнице: claude-box [--engine bwrap|off] [-- …]."
            )
        raise CliError(f"неизвестный аргумент «{a}». См. claude-box --help.")

    if engine not in ENGINES:
        raise CliError(f"--engine={engine!r} — допустимо: {' | '.join(ENGINES)}")
    return engine, passthrough


# ── Сборка запуска ───────────────────────────────────────────────────────────
def make_engine_runner(engine: str, root: Path) -> Runner:
    """Раннер Engine (Слой 0) по минимальному конфигу."""
    config = EngineConfig(sandbox=engine, claude_config_dir=_config_dir_from_env())
    return make_runner(config, root)


def build_argv(runner: Runner, command: Sequence[str], cwd: Path) -> list[str]:
    """Завернуть команду раннером: cwd — рабочий каталог и единственный RW-путь.

    Без session/channel/hooks: только «запусти это в песочнице в этом каталоге».
    """
    return runner.wrap(list(command), chdir=cwd, extra_rw=[cwd])


def build_env(engine: str) -> dict[str, str]:
    """Окружение процесса: копия текущего + TERM; под bwrap вырезаем X/Wayland.

    Зеркалит минимум из sessions._start_claude: без $DISPLAY процесс в песочнице
    не дёрнет хостовый GUI (сеть у bwrap общая с хостом, X-сокет достижим).
    """
    env = os.environ.copy()
    env.setdefault("TERM", "xterm-256color")
    if engine == "bwrap":
        for var in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY"):
            env.pop(var, None)
    return env


# ── PTY-relay и очистка терминала ────────────────────────────────────────────
def copy_ready(src_fd: int, dst_fd: int) -> bool:
    """Скопировать доступную порцию src_fd → dst_fd. False = EOF/ошибка (стоп).

    Логика relay в изоляции — тестируется на pipe/pty-паре без интерактива.
    """
    try:
        data = os.read(src_fd, 65536)
    except OSError:
        return False
    if not data:  # EOF
        return False
    try:
        os.write(dst_fd, data)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def raw_terminal(fd: int):
    """Перевести tty в raw на время блока, гарантированно вернуть настройки.

    Не-tty (пайп/файл) — no-op. Восстановление в finally: даже при Ctrl-C или
    исключении терминал не остаётся сломанным.
    """
    if not os.isatty(fd):
        yield
        return
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


async def run(
    argv: Sequence[str],
    *,
    cwd: str,
    env: dict[str, str],
    on_output: Callable[[bytes], None],
    interactive: bool,
    stdin_fd: int = 0,
    rows: int = TERM_ROWS,
    cols: int = TERM_COLS,
) -> int:
    """Поднять argv под PTY, (при interactive) релеить stdin, дождаться кода.

    Драйвер box.launch дренирует вывод процесса в on_output и владеет master-fd;
    мы джойним его поток после смерти процесса — так весь вывод дослан, а master
    закрыт (fd не течёт). Ввод: add_reader на stdin_fd копирует в master
    процесса; на EOF stdin — снимаем reader, процесс живёт дальше.
    """
    handle = await launch(
        argv, cwd=cwd, env=env, on_output=on_output, name="claude-box",
        rows=rows, cols=cols,
    )
    loop = asyncio.get_running_loop()
    reader_added = False

    if interactive:
        def _on_stdin() -> None:
            if not copy_ready(stdin_fd, handle.pty_master):
                with contextlib.suppress(Exception):
                    loop.remove_reader(stdin_fd)
        try:
            loop.add_reader(stdin_fd, _on_stdin)
            reader_added = True
        except (OSError, ValueError):
            reader_added = False  # stdin не селектится — просто без relay ввода

    try:
        await handle.process.wait()
    finally:
        if reader_added:
            with contextlib.suppress(Exception):
                loop.remove_reader(stdin_fd)
        # Дать драйверу дочитать буфер PTY и закрыть master (иначе fd утечёт).
        handle.driver_thread.join(timeout=5)

    return handle.process.returncode or 0


async def main_async(argv: Sequence[str]) -> int:
    engine, passthrough = parse_args(argv)
    root = repo_root()
    runner = make_engine_runner(engine, root)

    ok, why = runner.preflight()
    if not ok:
        sys.stderr.write(
            f"claude-box: движок «{engine}» не готов: {why}\n"
            "Попробуй --engine off (без изоляции).\n"
        )
        return 1

    cwd = os.getcwd()
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    command = [claude_bin, *passthrough]
    full_argv = build_argv(runner, command, Path(cwd))
    env = build_env(engine)

    stdin_fd, stdout_fd = 0, 1
    interactive = os.isatty(stdin_fd)
    rows, cols = TERM_ROWS, TERM_COLS
    if interactive:
        try:
            size = os.get_terminal_size(stdout_fd)
            rows, cols = size.lines, size.columns
        except OSError:
            pass

    def on_output(chunk: bytes) -> None:
        with contextlib.suppress(OSError):
            os.write(stdout_fd, chunk)

    with raw_terminal(stdin_fd):
        return await run(
            full_argv, cwd=cwd, env=env, on_output=on_output,
            interactive=interactive, stdin_fd=stdin_fd, rows=rows, cols=cols,
        )


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130
