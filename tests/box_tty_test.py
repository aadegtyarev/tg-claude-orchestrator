"""Арбитр терминала claude-box (box_cli/tty.py): stdin у relay и кошелька ОДИН.

Регрессия, ради которой всё написано: раньше PTY-relay вешал свой add_reader на
stdin, а TtyVaultHost на первом же confirm вешал СВОЙ на тот же fd и в finally
делал remove_reader. В asyncio второй add_reader затирает колбэк, а remove_reader
снимает читателя целиком — после первого confirm ввод в сессию умирал НАВСЕГДА, а
ответ печатался вслепую (raw = нет эха) и воровал байты у claude.

Что проверяем (на реальной pty-паре, без интерактивного терминала):
  * байты доходят до PTY-приёмника ДО вопроса;
  * на время вопроса терминал возвращается в нормальный режим (ЭХО включено),
    ответ собирается строкой;
  * ПОСЛЕ вопроса raw восстановлен и байты СНОВА доходят до приёмника (ключевой
    тест — именно это ломалось);
  * таймаут: молчание оператора → «» → DENY (не зависание);
  * отмена/исключение посреди вопроса не оставляют терминал в сломанном режиме;
  * хвост строки после ответа не теряется, а уходит в сессию;
  * relay честно гаснет на EOF stdin и на мёртвом приёмнике;
  * BoxVaultHost: y/да → True, n/пусто/нет tty → False.

Запуск: .venv/bin/python tests/box_tty_test.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import termios
import tty as ttymod
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from box_cli.tty import BoxVaultHost, StdinArbiter  # noqa: E402

SESSION = "claude-box"


class _Pty:
    """pty-пара: master — «клавиатура оператора», slave — stdin арбитра."""

    def __init__(self) -> None:
        self.master, self.slave = os.openpty()
        self.sink: list[bytes] = []

    def sink_write(self, data: bytes) -> bool:
        self.sink.append(data)
        return True

    def typed(self, data: bytes) -> None:
        os.write(self.master, data)

    def relayed(self) -> bytes:
        return b"".join(self.sink)

    def echo_on(self) -> bool:
        return bool(termios.tcgetattr(self.slave)[3] & termios.ECHO)

    def close(self) -> None:
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


async def _until(cond, timeout: float = 2.0) -> bool:
    """Подождать условия, крутя event loop (relay асинхронный)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if cond():
            return True
        await asyncio.sleep(0.01)
    return cond()


async def test_relay_survives_prompt():
    """ГЛАВНОЕ: после отработавшего вопроса ввод снова доходит до PTY."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        raw_attrs_before = termios.tcgetattr(p.slave)
        ttymod.setraw(p.slave)  # как делает лончер на настоящем терминале
        assert arb.start(), "читатель stdin не повесился"
        assert not p.echo_on(), "raw: эха быть не должно"

        p.typed(b"before")
        assert await _until(lambda: p.relayed() == b"before"), p.relayed()

        task = asyncio.create_task(arb.prompt("вопрос?", "gh auth token"))
        assert await _until(p.echo_on), "на время вопроса эхо обязано включиться"
        p.typed(b"y\n")
        assert (await task).strip() == "y"
        assert not p.echo_on(), "после ответа raw должен вернуться"

        p.typed(b"after")
        assert await _until(lambda: p.relayed().endswith(b"after")), p.relayed()
        assert p.relayed().startswith(b"before"), p.relayed()

        arb.stop()
        termios.tcsetattr(p.slave, termios.TCSANOW, raw_attrs_before)
        print("OK арбитр: relay → вопрос (с эхом) → relay снова живой")
    finally:
        p.close()


async def test_prompt_timeout_denies():
    """Молчание оператора: таймаут → «» (= DENY у хоста), а не зависание."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=0.15)
        ttymod.setraw(p.slave)
        arb.start()
        assert await arb.prompt("вопрос?", "preview") == ""
        assert not p.echo_on(), "терминал вернулся в raw и после таймаута"
        # После таймаута арбитр снова релеит.
        p.typed(b"z")
        assert await _until(lambda: p.relayed() == b"z"), p.relayed()
        arb.stop()
        print("OK арбитр: таймаут вопроса → пустой ответ (DENY), relay жив")
    finally:
        p.close()


async def test_prompt_cancel_restores_terminal():
    """Отмена посреди вопроса (Ctrl-C/shutdown) не оставляет терминал сломанным."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=5.0)
        ttymod.setraw(p.slave)
        raw = termios.tcgetattr(p.slave)
        arb.start()
        task = asyncio.create_task(arb.prompt("вопрос?", "preview"))
        assert await _until(p.echo_on)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert termios.tcgetattr(p.slave) == raw, "raw-настройки не восстановлены"
        p.typed(b"q")
        assert await _until(lambda: p.relayed() == b"q"), p.relayed()
        arb.stop()
        print("OK арбитр: отмена вопроса возвращает raw, relay продолжает работать")
    finally:
        p.close()


async def test_tail_after_answer_goes_to_session():
    """Байты, напечатанные ПОСЛЕ ответа в той же порции, не теряются."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=2.0)
        ttymod.setraw(p.slave)
        arb.start()
        task = asyncio.create_task(arb.prompt("вопрос?", "preview"))
        assert await _until(p.echo_on)
        p.typed(b"y\nhello")  # канонический режим отдаёт строку, хвост — следом
        assert (await task).strip() == "y"
        assert await _until(lambda: p.relayed().endswith(b"hello")), p.relayed()
        arb.stop()
        print("OK арбитр: хвост после ответа уходит в сессию, а не в никуда")
    finally:
        p.close()


async def test_relay_stops_on_eof_and_dead_sink():
    """Relay честно останавливается: EOF stdin и мёртвый приёмник (процесс закрыл
    PTY) не дают ни исключений, ни бесконечного цикла колбэков."""
    # EOF stdin: пайп, запись закрыли.
    r, w = os.pipe()
    arb = StdinArbiter(r, write_bytes=lambda d: True, timeout=0.1)
    assert arb.start()
    os.write(w, b"tail")
    os.close(w)
    assert await _until(lambda: not arb._reader_on, 1.0), "читатель не снят на EOF"
    os.close(r)

    # Приёмник умер (write вернул False) — relay выключается, арбитр жив.
    p = _Pty()
    try:
        dead = StdinArbiter(p.slave, write_bytes=lambda d: False, timeout=0.1)
        ttymod.setraw(p.slave)
        dead.start()
        p.typed(b"x")
        assert await _until(lambda: not dead._relay_on, 1.0), "relay не выключился"
        assert await dead.prompt("вопрос?", "preview") == ""  # вопросы ещё работают
        dead.stop()
    finally:
        p.close()
    print("OK relay: EOF снимает читателя, мёртвый приёмник гасит relay без падений")


async def test_box_vault_host_verdicts():
    """BoxVaultHost: y/да → True, n и пустой ответ → False, нет tty → False."""
    p = _Pty()
    try:
        arb = StdinArbiter(p.slave, write_bytes=p.sink_write, timeout=1.0)
        ttymod.setraw(p.slave)
        arb.start()
        host = BoxVaultHost(arb)

        for typed, expect in ((b"y\n", True), ("да\n".encode(), True), (b"n\n", False),
                              (b"\n", False)):
            task = asyncio.create_task(host.confirm(SESSION, "git push", "origin main"))
            assert await _until(p.echo_on)
            p.typed(typed)
            got = await task
            assert got is expect, f"{typed!r}: {got} != {expect}"
        # ask — тот же путь (и тоже с таймаутом: молчание = отказ).
        assert await host.ask(SESSION, "GET api", "https://api/x") is False
        arb.stop()
        print("OK BoxVaultHost: y/да → разрешить, n/пусто/таймаут → отказ")
    finally:
        p.close()

    # Нет tty (пайп вместо терминала) — спрашивать некому, отказ без вопроса.
    r, w = os.pipe()
    try:
        host = BoxVaultHost(StdinArbiter(r, timeout=0.1))
        assert await host.confirm(SESSION, "git push", "x") is False
        assert await host.ask(SESSION, "GET", "x") is False
        print("OK BoxVaultHost: без tty confirm/ask = отказ (не вопрос в никуда)")
    finally:
        os.close(r)
        os.close(w)


async def test_assume_yes_does_not_ask():
    """assume_yes — подтверждаем без вопроса (неинтерактивный сценарий)."""
    r, w = os.pipe()
    try:
        host = BoxVaultHost(StdinArbiter(r, timeout=0.1), assume_yes=True)
        assert await host.confirm(SESSION, "x", "y") is True
        assert await host.ask(SESSION, "x", "y") is True
        print("OK BoxVaultHost: assume_yes подтверждает без вопроса")
    finally:
        os.close(r)
        os.close(w)


def main() -> None:
    asyncio.run(test_relay_survives_prompt())
    asyncio.run(test_prompt_timeout_denies())
    asyncio.run(test_prompt_cancel_restores_terminal())
    asyncio.run(test_tail_after_answer_goes_to_session())
    asyncio.run(test_relay_stops_on_eof_and_dead_sink())
    asyncio.run(test_box_vault_host_verdicts())
    asyncio.run(test_assume_yes_does_not_ask())
    print("ALL BOX-TTY OK")


if __name__ == "__main__":
    main()
