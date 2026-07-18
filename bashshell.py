"""Постоянный bash-терминал на топик — в обход Claude Code, напрямую в систему.

Один PTY-процесс `bash -i` на топик, живёт между вызовами /bash (поэтому `cd`
внутри одной команды сохраняется для следующей). /bash пишет команду + маркер
конца, стримит вывод в статус-сообщение, ждёт маркер. /bashin пишет сырой ввод
в тот же PTY — ответ на «y/n» интерактивной команды, которая всё ещё крутится
в текущем /bash.

Права и cwd — как у процесса бота (никакого --dangerously-skip-permissions):
пользователь явно просил «мимо Claude Code, сразу в систему».
"""

from __future__ import annotations

import logging
import os
import pty
import signal
import subprocess
import threading
from pathlib import Path

from ansi import strip_ansi

logger = logging.getLogger(__name__)

_BUF_CAP = 200_000  # держим только хвост — на случай болтливой команды


def clean(raw: bytes) -> bytes:
    """Снять ANSI-раскраску/управляющие коды и \\r — для показа в <pre>.

    Тонкая обёртка над общим ansi.strip_ansi (раньше свой дубликат regex)."""
    return strip_ansi(raw)


class BashSession:
    """Один PTY с интерактивным bash и потоком, дренирующим его вывод."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.busy = False  # активен ли /bash (для /bashin — не проверяется)
        self._buf = bytearray()
        self._lock = threading.Lock()
        master, slave = pty.openpty()
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        try:
            self.proc = subprocess.Popen(
                ["/bin/bash", "-i"],
                stdin=slave, stdout=slave, stderr=slave,
                cwd=str(cwd), env=env, start_new_session=True,
            )
        finally:
            os.close(slave)
        self.master = master
        threading.Thread(target=self._reader, name="bashshell-reader", daemon=True).start()

    def _reader(self) -> None:
        while True:
            try:
                chunk = os.read(self.master, 65536)
            except OSError:
                return
            if not chunk:
                return
            with self._lock:
                self._buf.extend(chunk)
                if len(self._buf) > _BUF_CAP:
                    del self._buf[:-_BUF_CAP]

    def snapshot(self) -> bytes:
        with self._lock:
            return bytes(self._buf)

    def write(self, text: str) -> None:
        os.write(self.master, text.encode())

    def interrupt(self) -> None:
        """Послать Ctrl-C (SIGINT фоновому пайплайну bash) — прервать убежавшую
        команду, не убивая саму оболочку. Используется при таймауте /bash,
        чтобы процесс не гадил в общий буфер следующему вызову."""
        try:
            os.write(self.master, b"\x03")
        except OSError:
            pass

    @property
    def running(self) -> bool:
        return self.proc.poll() is None

    def close(self) -> None:
        # Интерактивный bash -i игнорирует SIGTERM — terminate() не дорабатывал,
        # и оболочка утекала (REVIEW.md B1). Поэтому SIGKILL по всей группе
        # процессов (bash + её дети: sleep/сборки/...). start_new_session=True
        # делает bash лидером группы, killpg(pid) накрывает её целиком и не
        # задевает процесс бота (другая сессия). Близко к мгновенному, без
        # долгого ожидания — close не stall-ит event loop из хендлеров.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=3)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired, OSError):
            try:
                self.proc.kill()
                self.proc.wait(timeout=1)
            except Exception:
                pass
        try:
            os.close(self.master)
        except OSError:
            pass


class BashShellManager:
    """thread_id -> BashSession. Одна оболочка на топик, создаётся лениво."""

    def __init__(self) -> None:
        self._sessions: dict[int, BashSession] = {}

    def get_or_create(self, thread_id: int, cwd: Path) -> BashSession:
        sess = self._sessions.get(thread_id)
        if sess is None or not sess.running:
            sess = BashSession(cwd)
            self._sessions[thread_id] = sess
            logger.info("bash-терминал открыт (топик %s, cwd %s)", thread_id, cwd)
        return sess

    def get(self, thread_id: int) -> BashSession | None:
        sess = self._sessions.get(thread_id)
        return sess if sess is not None and sess.running else None

    def close(self, thread_id: int) -> None:
        sess = self._sessions.pop(thread_id, None)
        if sess is not None:
            sess.close()
            logger.info("bash-терминал закрыт (топик %s)", thread_id)

    def close_all(self) -> None:
        """Прибрать все bash-оболочки (остановка launcher'а)."""
        for tid in list(self._sessions):
            self.close(tid)
