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
`--vm`, `-p`, подкоманда `connect` (agent-vm/unattended — отдельные треки).

`--profile <name>` — изолированная идентичность claude (свой CLAUDE_CONFIG_DIR и,
под bwrap, свой $HOME): модель не видит реальные ~/.claude / ~/.ssh оператора,
профили не пересекаются. Подкоманды `init`/`profile` управляют каталогами
профилей. Реализация — box_cli/profiles.py.

`--wallet <secret>` — vault-перехват TLS (Launcher §5.2): под капотом поднимается
standalone MITM-прокси для прокси-секрета и в песочницу докидывается HTTPS_PROXY +
объединённый CA-bundle, чтобы трафик к сервису под секретом шёл через кошелёк
(значение секрета в песочницу не попадает). Реализация — box_cli/wallet.py.
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
# init/profile реализованы (диспетчеризуются в subcommand_result до parse_args);
# connect — заглушка (agent-vm-трек заблокирован).
_STUB_SUBCOMMANDS = ("connect",)
# Флаги следующих срезов: распознаём, чтобы дать честный отказ, а не «unknown».
_STUB_FLAGS = ("--vm", "-p")

DEFAULT_SECRETS = "~/.config/claude-orchestrator/secrets.toml"

_USAGE = (
    "claude-box [--engine bwrap|off] [--profile <имя>] "
    "[--wallet <секрет> [--secrets <файл>]] [-- <аргументы claude>]\n"
    "  Запустить claude (или CLAUDE_BIN) в песочнице и отдать терминал.\n"
    "  --engine bwrap   файловая песочница bubblewrap (по умолчанию)\n"
    "  --engine off     без изоляции\n"
    "  --profile <имя>  изолированная идентичность claude: свой CLAUDE_CONFIG_DIR\n"
    "                   и (под bwrap) свой $HOME; реальные ~/.claude/~/.ssh скрыты\n"
    "  --wallet <секрет> vault-перехват TLS для прокси-секрета: трафик к сервису\n"
    "                   идёт через кошелёк, значение секрета в песочницу не входит\n"
    f"  --secrets <файл> путь к secrets.toml (по умолчанию {DEFAULT_SECRETS})\n"
    "  --               всё, что после, пробрасывается в claude\n"
    "  -h, --help       эта справка\n"
    "\nПодкоманды:\n"
    "  init <имя>       создать профиль (идемпотентно) и напечатать путь\n"
    "  profile          список профилей;  profile rm <имя>  удалить профиль\n"
    "\nНе реализовано (следующие треки): --vm, -p, connect."
)


@dataclass(frozen=True)
class Options:
    """Разобранные аргументы CLI. wallet=None — перехват не запрошен."""

    engine: str
    passthrough: list[str]
    wallet: str | None = None
    secrets: Path | None = None
    profile: str | None = None  # имя профиля (CLAUDE_CONFIG_DIR/HOME-редирект)


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
def parse_args(argv: Sequence[str]) -> Options:
    """Разобрать argv → Options. Заглушки/ошибки → CliError.

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
            f"подкоманда «{opts[0]}» ещё не реализована (agent-vm-трек заблокирован). "
            "Доступно: запуск claude-box [--engine bwrap|off] [--profile <имя>] "
            "[-- …], init <имя>, profile."
        )

    engine = "bwrap"
    wallet: str | None = None
    secrets: Path | None = None
    profile: str | None = None

    def _value(flag: str, idx: int, inline: str | None) -> tuple[str, int]:
        """Значение флага: из `--flag=val` (inline) либо из следующего аргумента."""
        if inline is not None:
            return inline, idx + 1
        if idx + 1 >= len(opts):
            raise CliError(f"{flag} требует значение")
        return opts[idx + 1], idx + 2

    i = 0
    while i < len(opts):
        a = opts[i]
        key, eq, inline_val = a.partition("=")
        inline = inline_val if eq else None
        if a in ("-h", "--help"):
            sys.stdout.write(_USAGE + "\n")
            raise SystemExit(0)
        if key == "--engine":
            engine, i = _value("--engine", i, inline)
            continue
        if key == "--wallet":
            wallet, i = _value("--wallet", i, inline)
            continue
        if key == "--secrets":
            raw, i = _value("--secrets", i, inline)
            secrets = Path(raw).expanduser()
            continue
        if key == "--profile":
            profile, i = _value("--profile", i, inline)
            continue
        if a in _STUB_FLAGS:
            raise CliError(
                f"флаг «{a}» ещё не реализован (следующие срезы: "
                "профили/VM/unattended). Сейчас — запуск в песочнице с "
                "опциональным --wallet: claude-box [--engine bwrap|off] "
                "[--wallet <секрет>] [-- …]."
            )
        raise CliError(f"неизвестный аргумент «{a}». См. claude-box --help.")

    if engine not in ENGINES:
        raise CliError(f"--engine={engine!r} — допустимо: {' | '.join(ENGINES)}")
    if secrets is not None and wallet is None:
        raise CliError("--secrets имеет смысл только с --wallet <секрет>.")
    return Options(
        engine=engine, passthrough=passthrough, wallet=wallet,
        secrets=secrets, profile=profile,
    )


# ── Сборка запуска ───────────────────────────────────────────────────────────
def make_engine_runner(
    engine: str, root: Path, *, claude_config_dir: Path | None = None,
) -> Runner:
    """Раннер Engine (Слой 0) по минимальному конфигу.

    claude_config_dir — куда указывает CLAUDE_CONFIG_DIR (профиль передаёт свой
    <profile>/.claude). Без него — из окружения. Важно для bwrap: раннер биндит
    ИМЕННО этот каталог RW; если оставить None у профиля, раннер прибиндил бы
    реальный ~/.claude оператора (утечка) — поэтому профиль всегда его задаёт.
    """
    config = EngineConfig(
        sandbox=engine,
        claude_config_dir=claude_config_dir or _config_dir_from_env(),
    )
    return make_runner(config, root)


def build_argv(
    runner: Runner, command: Sequence[str], cwd: Path,
    extra_rw: Sequence[Path] = (),
) -> list[str]:
    """Завернуть команду раннером: cwd — рабочий каталог и RW-путь; extra_rw —
    дополнительные RW-бинды (напр. временный каталог CA-bundle для --wallet, чтобы
    он был виден в песочнице тем же путём).

    Без session/channel/hooks: только «запусти это в песочнице в этом каталоге».
    """
    return runner.wrap(list(command), chdir=cwd, extra_rw=[cwd, *extra_rw])


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
    opts = parse_args(argv)
    engine = opts.engine
    root = repo_root()

    # Профиль (--profile): изолированная идентичность claude. Резолвим ДО раннера,
    # чтобы передать ему <profile>/.claude как CLAUDE_CONFIG_DIR (иначе раннер
    # прибиндил бы реальный ~/.claude). env-довесок (CLAUDE_CONFIG_DIR + HOME под
    # bwrap) и RW-бинд каталога профиля добавляются ниже.
    profile_env_extra: dict[str, str] = {}
    profile_rw: list[Path] = []
    profile_config_dir: Path | None = None
    if opts.profile is not None:
        from .profiles import ProfileError, profile_env
        if engine != "bwrap":
            sys.stderr.write(
                "claude-box: --profile без bwrap НЕ изолирует $HOME — модель видит "
                "реальную домашку оператора; редирект CLAUDE_CONFIG_DIR работает, но "
                "полную изоляцию профиля даёт только --engine bwrap.\n")
        try:
            profile_env_extra, pdir = profile_env(opts.profile, engine=engine)
        except ProfileError as e:
            sys.stderr.write(f"claude-box: --profile: {e}\n")
            return e.code
        profile_config_dir = pdir / ".claude"
        profile_rw = [pdir]

    runner = make_engine_runner(engine, root, claude_config_dir=profile_config_dir)

    ok, why = runner.preflight()
    if not ok:
        sys.stderr.write(
            f"claude-box: движок «{engine}» не готов: {why}\n"
            "Попробуй --engine off (без изоляции).\n"
        )
        return 1

    cwd = os.getcwd()
    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    command = [claude_bin, *opts.passthrough]
    env = build_env(engine)
    # Профиль-редирект поверх окружения (CLAUDE_CONFIG_DIR + HOME под bwrap);
    # wallet ниже добавляет свой HTTPS_PROXY/CA — не пересекается с этими ключами.
    env.update(profile_env_extra)

    # Vault-перехват (--wallet): поднять standalone-прокси ДО песочницы и получить
    # env-довесок (HTTPS_PROXY + CA-bundle) + доп. RW-бинд каталога bundle. Отказ
    # (нет секрета/не прокси/сбой окружения) — честное сообщение + код из WalletError,
    # без трейсбека. Teardown — в finally ниже (снять прокси, снести временный каталог).
    intercept = None
    # extra_rw начинается с каталога профиля (RW-бинд src==dst → HOME/CONFIG_DIR
    # валидны изнутри песочницы); wallet при наличии докидывает свой bundle-каталог.
    extra_rw: list[Path] = list(profile_rw)
    if opts.wallet is not None:
        from .wallet import WalletError, setup_wallet_intercept
        if engine != "bwrap":
            sys.stderr.write(
                "claude-box: --wallet без bwrap НЕ изолирует $HOME — модель читает "
                "secrets.toml/окружение напрямую; перехват включён, но используй "
                "его только с --engine bwrap.\n")
        secrets_path = opts.secrets or Path(DEFAULT_SECRETS).expanduser()
        try:
            intercept = await setup_wallet_intercept(opts.wallet, secrets_path=secrets_path)
        except WalletError as e:
            sys.stderr.write(f"claude-box: --wallet: {e}\n")
            return e.code
        env.update(intercept.env)
        extra_rw.extend(intercept.extra_rw)

    # try/finally оборачивает ВСЁ после подъёма перехвата (build_argv, замер
    # терминала, запуск) — иначе исключение в этом узком окне утекло бы прокси-порт
    # и временный каталог bundle до входа в защищённый блок (нашло ревью, LOW-4).
    try:
        full_argv = build_argv(runner, command, Path(cwd), extra_rw)

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

        # raw_terminal возвращает терминал в finally — обёртка исключений НЕ должна
        # его перекрыть, поэтому try внутри with (терминал восстановлен раньше, чем
        # печатаем ошибку). Сбой спавна/запуска (напр. CLAUDE_BIN на несуществующий
        # бинарь) → честный отказ в stderr + код 1, а не сырой трейсбек: остальной
        # CLI держит эту планку (CliError), держим и здесь.
        with raw_terminal(stdin_fd):
            try:
                return await run(
                    full_argv, cwd=cwd, env=env, on_output=on_output,
                    interactive=interactive, stdin_fd=stdin_fd, rows=rows, cols=cols,
                )
            except OSError as e:
                sys.stderr.write(
                    f"claude-box: не удалось запустить «{claude_bin}»: {e}\n"
                    "Проверь CLAUDE_BIN и что бинарь есть в PATH.\n"
                )
                return 1
            except Exception as e:  # noqa: BLE001 — любой сбой запуска = честный отказ
                sys.stderr.write(f"claude-box: сбой запуска: {e}\n")
                return 1
    finally:
        # Снять прокси (порт освобождается) и снести временный каталог bundle —
        # даже при исключении/Ctrl-C. Не течём.
        if intercept is not None:
            await intercept.close()


# ── Подкоманды управления профилями ──────────────────────────────────────────
# init/profile НЕ запускают claude (нет PTY/asyncio) — диспетчеризуются в main до
# запуска рантайма. Логика профилей — в box_cli.profiles (stdlib, без оркестратора).
def cmd_init(args: Sequence[str]) -> int:
    """`init <имя>`: идемпотентно создать профиль и напечатать его путь (код 0)."""
    from .profiles import ProfileError, ensure_profile
    if len(args) != 1:
        raise CliError("init требует ровно один аргумент: init <имя>.")
    try:
        path = ensure_profile(args[0])
    except ProfileError as e:
        raise CliError(str(e)) from e
    sys.stdout.write(f"{path}\n")
    return 0


def cmd_profile(args: Sequence[str]) -> int:
    """`profile` — список профилей; `profile rm <имя>` — удалить каталог профиля."""
    from .profiles import ProfileError, list_profiles, remove_profile
    if not args:
        names = list_profiles()
        if not names:
            sys.stdout.write("нет профилей (создай: claude-box init <имя>)\n")
        else:
            sys.stdout.write("\n".join(names) + "\n")
        return 0
    if args[0] == "rm":
        if len(args) != 2:
            raise CliError("profile rm требует имя: profile rm <имя>.")
        try:
            path = remove_profile(args[1])
        except ProfileError as e:
            raise CliError(str(e)) from e
        sys.stdout.write(f"удалён профиль: {path}\n")
        return 0
    raise CliError(
        f"неизвестный подарг profile «{args[0]}». Доступно: profile, profile rm <имя>.")


def subcommand_result(argv: Sequence[str]) -> int | None:
    """Если argv — подкоманда, выполнить её и вернуть код; иначе None (запуск claude).

    Подкомандой считаем только первый токен-НЕ-флаг из известного набора, чтобы
    `--engine`/`--profile …` уходили в обычный разбор parse_args.
    """
    if not argv:
        return None
    cmd = argv[0]
    if cmd == "init":
        return cmd_init(argv[1:])
    if cmd == "profile":
        return cmd_profile(argv[1:])
    if cmd == "connect":
        # Заглушка: agent-vm-трек заблокирован. Честный отказ (код 2), не no-op.
        raise CliError(
            "подкоманда «connect» ещё не реализована (agent-vm-трек заблокирован).")
    return None


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        sub = subcommand_result(args)
    except SystemExit as e:  # CliError (код 2) — уже напечатал в stderr
        return e.code if isinstance(e.code, int) else 2
    if sub is not None:
        return sub
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130
