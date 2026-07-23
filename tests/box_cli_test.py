"""Тесты CLI `claude-box` (box_cli): сборка argv, relay-логика, e2e-запуск.

Запуск: .venv/bin/python tests/box_cli_test.py (как весь tests/, без pytest).

Что покрыто:
  • parse_args — engine, passthrough, заглушки (--vm/init/…) → честный отказ;
  • build_argv — bwrap-обёртка на месте, cwd RW; off — команда как есть;
  • copy_ready — relay-логика на pipe-паре (данные + EOF), без интерактива;
  • run() e2e — команда в песочнице/direct: вывод «BOXOK» доходит через
    on_output, код 0, fd не текут (мягкий скип bwrap при отсутствии).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import pty
import sys
import termios
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from box_cli import cli


def _fds() -> set[int]:
    """Открытые fd процесса — для проверки, что relay/драйвер их не течёт."""
    try:
        return {int(x) for x in os.listdir("/proc/self/fd")}
    except OSError:
        return set()


# ── parse_args ───────────────────────────────────────────────────────────────
def test_parse_default_engine_bwrap():
    opts = cli.parse_args([])
    assert opts.engine == "bwrap"
    assert opts.passthrough == []
    assert opts.wallet is None and opts.secrets is None


def test_parse_engine_and_passthrough():
    opts = cli.parse_args(["--engine", "off", "--", "--model", "opus"])
    assert opts.engine == "off"
    assert opts.passthrough == ["--model", "opus"]
    # форма --engine=off
    assert cli.parse_args(["--engine=off"]).engine == "off"


def test_parse_wallet_and_secrets():
    """--wallet <секрет> и --secrets <файл>: обе формы (пробел и =)."""
    opts = cli.parse_args(["--wallet", "svc", "--", "-p", "hi"])
    assert opts.wallet == "svc"
    assert opts.passthrough == ["-p", "hi"], "аргументы после -- не трогаем"
    opts2 = cli.parse_args(["--wallet=svc", "--secrets=/tmp/s.toml"])
    assert opts2.wallet == "svc"
    assert opts2.secrets == Path("/tmp/s.toml")
    # --secrets без --wallet — бессмысленно → отказ код 2.
    try:
        cli.parse_args(["--secrets", "/tmp/s.toml"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("--secrets без --wallet должен упасть")
    # --wallet без значения → отказ код 2.
    try:
        cli.parse_args(["--wallet"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("--wallet без значения должен упасть")


def test_parse_bad_engine_rejected():
    try:
        cli.parse_args(["--engine", "docker"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("плохой --engine должен упасть CliError")


def test_parse_stub_flags_and_subcommands_refused():
    # Заглушки: честный отказ (код 2), не тихий no-op и не «unknown».
    # (--wallet больше НЕ заглушка — реализован; см. test_parse_wallet_and_secrets.)
    for args in (["--vm"], ["--profile", "work"], ["-p", "task"]):
        try:
            cli.parse_args(args)
        except SystemExit as e:
            assert e.code == 2, args
        else:
            raise AssertionError(f"{args} должно быть заглушено отказом")
    for sub in ("init", "profile", "connect"):
        try:
            cli.parse_args([sub])
        except SystemExit as e:
            assert e.code == 2, sub
        else:
            raise AssertionError(f"подкоманда {sub} должна быть заглушена")


def test_parse_help_exits_zero():
    try:
        cli.parse_args(["--help"])
    except SystemExit as e:
        assert e.code == 0
    else:
        raise AssertionError("--help должен выйти с кодом 0")


def test_parse_unknown_arg_refused():
    try:
        cli.parse_args(["--nope"])
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("неизвестный аргумент должен упасть")


# ── build_argv ───────────────────────────────────────────────────────────────
def test_build_argv_off_is_command_asis():
    runner = cli.make_engine_runner("off", cli.repo_root())
    argv = cli.build_argv(runner, ["sh", "-c", "true"], Path("/tmp"))
    assert argv == ["sh", "-c", "true"]


def test_build_argv_bwrap_wraps_and_cwd_rw():
    runner = cli.make_engine_runner("bwrap", cli.repo_root())
    cwd = Path("/tmp")
    argv = cli.build_argv(runner, ["claude", "--model", "opus"], cwd)
    assert argv[0] == "bwrap", "должна быть bwrap-обёртка"
    assert "--" in argv, "префикс bwrap завершается '--'"
    # cwd смонтирован RW (--bind-try /tmp /tmp) и стоит как chdir.
    joined = " ".join(argv)
    assert "--bind-try /tmp /tmp" in joined, "cwd должен быть RW-биндом"
    assert "--chdir /tmp" in joined
    # команда — после '--', в исходном порядке.
    tail = argv[argv.index("--") + 1:]
    assert tail == ["claude", "--model", "opus"]


# ── copy_ready (relay-логика) ────────────────────────────────────────────────
def test_copy_ready_moves_bytes_and_detects_eof():
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    try:
        os.write(w_in, b"hello relay")
        assert cli.copy_ready(r_in, w_out) is True
        assert os.read(r_out, 1024) == b"hello relay"
        # Закрытая запись → EOF на чтении → False (стоп relay).
        os.close(w_in)
        assert cli.copy_ready(r_in, w_out) is False
    finally:
        for fd in (r_in, r_out, w_out):
            try:
                os.close(fd)
            except OSError:
                pass


def test_copy_ready_write_error_returns_false():
    r_in, w_in = os.pipe()
    r_out, w_out = os.pipe()
    os.write(w_in, b"x")
    os.close(r_out)  # приёмник закрыт → запись упадёт → False
    try:
        assert cli.copy_ready(r_in, w_out) is False
    finally:
        for fd in (r_in, w_in, w_out):
            try:
                os.close(fd)
            except OSError:
                pass


# ── e2e: run() через Engine ──────────────────────────────────────────────────
async def _run_capture(engine: str) -> tuple[int, bytes]:
    buf = bytearray()
    argv = cli.build_argv(
        cli.make_engine_runner(engine, cli.repo_root()),
        ["sh", "-c", "echo BOXOK"],
        Path(os.getcwd()),
    )
    code = await cli.run(
        argv, cwd=os.getcwd(), env=os.environ.copy(),
        on_output=buf.extend, interactive=False,
    )
    return code, bytes(buf)


async def test_e2e_off_boxok_reaches_output():
    """off (без изоляции) — всегда доступен: вывод доходит, код 0, fd не текут."""
    before = _fds()
    code, out = await _run_capture("off")
    assert code == 0, f"код возврата {code}"
    assert b"BOXOK" in out, f"вывод не дошёл: {out!r}"
    leaked = _fds() - before
    assert not leaked, f"утекли fd: {leaked}"


async def test_e2e_bwrap_boxok_or_skip():
    """bwrap — та же проверка в песочнице; мягкий скип, если bwrap недоступен."""
    runner = cli.make_engine_runner("bwrap", cli.repo_root())
    ok, why = runner.preflight()
    if not ok:
        print(f"SKIP bwrap e2e: {why}")
        return
    before = _fds()
    code, out = await _run_capture("bwrap")
    assert code == 0, f"код возврата {code}"
    assert b"BOXOK" in out, f"вывод не дошёл из песочницы: {out!r}"
    leaked = _fds() - before
    assert not leaked, f"утекли fd: {leaked}"


# ── raw_terminal: не сломать терминал ────────────────────────────────────────
def test_raw_terminal_restores_on_normal_exit():
    """tty-настройки после нормального выхода из with == до входа."""
    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        with cli.raw_terminal(slave):
            pass  # внутри — raw (termios изменён)
        after = termios.tcgetattr(slave)
        assert after == before, "raw_terminal не восстановил tty при нормальном выходе"
    finally:
        os.close(master)
        os.close(slave)


def test_raw_terminal_restores_on_exception():
    """Исключение внутри with НЕ оставляет терминал сломанным (restore в finally)."""
    master, slave = pty.openpty()

    class _Boom(Exception):
        pass

    try:
        before = termios.tcgetattr(slave)
        try:
            with cli.raw_terminal(slave):
                raise _Boom()
        except _Boom:
            pass
        after = termios.tcgetattr(slave)
        assert after == before, "raw_terminal не восстановил tty при исключении"
    finally:
        os.close(master)
        os.close(slave)


def test_raw_terminal_noop_on_non_tty():
    """Не-tty (pipe) — no-op, не падает (isatty False)."""
    r, w = os.pipe()
    try:
        with cli.raw_terminal(r):
            pass
    finally:
        os.close(r)
        os.close(w)


# ── интерактивный relay: stdin доходит до процесса ───────────────────────────
async def test_interactive_relay_stdin_reaches_process():
    """interactive=True: байты из stdin_fd доезжают в pty процесса (add_reader/
    copy_ready), процесс их отражает, fd после чисты."""
    stdin_r, stdin_w = os.pipe()
    before = _fds()
    buf = bytearray()
    argv = cli.build_argv(
        cli.make_engine_runner("off", cli.repo_root()),
        ["sh", "-c", "read x; echo GOT-$x"],
        Path(os.getcwd()),
    )
    # Данные готовы ДО запуска — add_reader сработает сразу, как поднимется цикл.
    os.write(stdin_w, b"HELLO\n")
    try:
        code = await cli.run(
            argv, cwd=os.getcwd(), env=os.environ.copy(),
            on_output=buf.extend, interactive=True, stdin_fd=stdin_r,
        )
    finally:
        os.close(stdin_w)
        os.close(stdin_r)
    assert code == 0, f"код возврата {code}"
    assert b"GOT-HELLO" in bytes(buf), f"stdin не дошёл до процесса: {bytes(buf)!r}"
    # stdin_r/stdin_w уже были в before (созданы раньше) → в leaked не попадут;
    # проверяем, что pty_master драйвер закрыл (не течёт).
    leaked = _fds() - before
    assert not leaked, f"утекли fd: {leaked}"


# ── честный отказ вместо трейсбека при сбое запуска ──────────────────────────
def test_nonexistent_claude_bin_honest_error():
    """CLAUDE_BIN на несуществующий бинарь → внятная ошибка + код 1, не трейсбек."""
    old = os.environ.get("CLAUDE_BIN")
    os.environ["CLAUDE_BIN"] = "/nonexistent/claude-xyz-42"
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            code = cli.main(["--engine", "off"])
    finally:
        if old is None:
            os.environ.pop("CLAUDE_BIN", None)
        else:
            os.environ["CLAUDE_BIN"] = old
    assert code == 1, f"ожидался ненулевой код, получен {code}"
    assert "не удалось запустить" in err.getvalue(), (
        f"нет честного сообщения об ошибке: {err.getvalue()!r}"
    )


def main() -> None:
    test_parse_default_engine_bwrap()
    test_parse_engine_and_passthrough()
    test_parse_wallet_and_secrets()
    test_parse_bad_engine_rejected()
    test_parse_stub_flags_and_subcommands_refused()
    test_parse_help_exits_zero()
    test_parse_unknown_arg_refused()
    test_build_argv_off_is_command_asis()
    test_build_argv_bwrap_wraps_and_cwd_rw()
    test_copy_ready_moves_bytes_and_detects_eof()
    test_copy_ready_write_error_returns_false()
    test_raw_terminal_restores_on_normal_exit()
    test_raw_terminal_restores_on_exception()
    test_raw_terminal_noop_on_non_tty()
    test_nonexistent_claude_bin_honest_error()
    asyncio.run(test_interactive_relay_stdin_reaches_process())
    asyncio.run(test_e2e_off_boxok_reaches_output())
    asyncio.run(test_e2e_bwrap_boxok_or_skip())
    print("ALL BOX-CLI OK")


if __name__ == "__main__":
    main()
