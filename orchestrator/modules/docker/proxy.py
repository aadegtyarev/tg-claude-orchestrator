"""Тонкий прокси над docker.sock. Стоит ТОЛЬКО в песочнице модели: внутрь
биндится ЭТОТ сокет на /run/docker.sock, настоящий скрыт под --tmpfs /run. Личный
докер оператора идёт напрямую, мимо прокси — ему тут ничего не запрещено.

Почему тонкий и не зависает: единственная сложность прошлой версии была в живых
потоках (run -it, logs -f) и долгих keep-alive соединениях (compose шлёт несколько
create по одному). Убираем это трюком — дописываем `Connection: close`, чтобы
КАЖДАЯ команда шла своим соединением. Тогда на соединении всегда ровно один запрос:
проверяем его (если это create — по телу через decision) и дальше просто
перекачиваем байты насквозь до закрытия. Ответы/стримы не разбираем вообще —
нечему зависать. Hijack (Upgrade) не трогаем: он и так одноразовый.

Модель угроз — в decision.py (соломка от случайностей, не от побега).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from .decision import Policy, endpoint, evaluate

logger = logging.getLogger(__name__)

_REAL_SOCK = "/run/docker.sock"
_CHUNK = 65536


class DockerProxy:
    """Один экземпляр на оркестратор (докер глобальный, не per-session). Политика
    берётся из $HOME оператора."""

    def __init__(self, sock_path: Path, *, policy: Policy, real_sock: str = _REAL_SOCK) -> None:
        self.sock_path = Path(sock_path)
        self.policy = policy
        self.real_sock = real_sock
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        if self.sock_path.exists():
            self.sock_path.unlink()
        self.sock_path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.sock_path))
        self.sock_path.chmod(0o660)
        logger.info("docker-proxy слушает %s → %s", self.sock_path, self.real_sock)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None
        if self.sock_path.exists():
            try:
                self.sock_path.unlink()
            except OSError:
                pass

    # ── одно клиентское соединение = один запрос (Connection: close) ─────
    async def _handle(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        up_r = up_w = None
        try:
            up_r, up_w = await asyncio.open_unix_connection(self.real_sock)
        except OSError as e:
            await self._error(cw, 502, f"docker недоступен: {e}")
            await self._close(cw)
            return
        try:
            await self._serve(cr, cw, up_r, up_w)
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception:  # noqa: BLE001 — сбой одного соединения не роняет прокси
            logger.exception("docker-proxy: ошибка соединения")
        finally:
            await self._close(cw)
            if up_w is not None:
                await self._close(up_w)

    async def _serve(self, cr, cw, up_r, up_w) -> None:
        head = await self._read_head(cr)
        if head is None:
            return
        first, hdrs, low = head
        try:
            method, path, _ = first.split(" ", 2)
        except ValueError:
            await self._error(cw, 400, "битый запрос")
            return

        kind = endpoint(method, path)
        if kind in ("create", "volume_create"):
            body = await self._read_body(cr, low)
            try:
                obj = json.loads(body) if body else None
            except ValueError:
                obj = None
            verdict = evaluate(method, path, obj, policy=self.policy)
            if not verdict.allow:
                logger.info("DENY %s %s — %s", method, path, verdict.reason)
                await self._error(cw, 403, verdict.reason)
                return
            # Тело не меняем (decision только решает) — фиксируем Content-Length и
            # Connection: close, шлём голову и тело.
            out = self._rebuild(hdrs, content_length=len(body))
            await self._send_head(up_w, f"{method} {path} HTTP/1.1", out)
            up_w.write(body)
            await up_w.drain()
        else:
            # Не create: hijack (Upgrade) не трогаем — он одноразовый; иначе шлём
            # Connection: close. Тело запроса (build-tar, stdin) уедет сплайсом.
            keep = "upgrade" in low.get("connection", "").lower() or "upgrade" in low
            out = hdrs if keep else self._rebuild(hdrs, content_length=None)
            await self._send_head(up_w, f"{method} {path} HTTP/1.1", out)

        await self._splice(cr, cw, up_r, up_w)

    # ── разбор/перекачка ────────────────────────────────────────────────
    async def _read_head(self, r):
        try:
            raw = await r.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            return None
        text = raw[:-4].decode("latin-1")
        lines = text.split("\r\n")
        hdrs, low = [], {}
        for ln in lines[1:]:
            if not ln:
                continue
            name, _, value = ln.partition(":")
            hdrs.append((name, value.strip()))
            low[name.strip().lower()] = value.strip()
        return lines[0], hdrs, low

    async def _read_body(self, r, low) -> bytes:
        if "content-length" in low:
            n = int(low["content-length"])
            return await r.readexactly(n) if n > 0 else b""
        if low.get("transfer-encoding", "").lower() == "chunked":
            out = bytearray()
            while True:
                size_line = await r.readuntil(b"\r\n")
                size = int(size_line.strip().split(b";")[0], 16)
                if size == 0:
                    await r.readuntil(b"\r\n")
                    break
                out += await r.readexactly(size)
                await r.readexactly(2)
            return bytes(out)
        return b""

    @staticmethod
    def _rebuild(hdrs, *, content_length: int | None):
        """Голова с Connection: close. content_length не None → выставить его и
        убрать transfer-encoding (тело раздетое)."""
        out = []
        for k, v in hdrs:
            kl = k.lower()
            if kl == "connection":
                continue
            if content_length is not None and kl in ("content-length", "transfer-encoding"):
                continue
            out.append((k, v))
        if content_length is not None:
            out.append(("Content-Length", str(content_length)))
        out.append(("Connection", "close"))
        return out

    async def _send_head(self, w, first: str, hdrs) -> None:
        buf = first + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdrs) + "\r\n"
        w.write(buf.encode("latin-1"))
        await w.drain()

    async def _splice(self, cr, cw, up_r, up_w) -> None:
        async def pump(src, dst):
            try:
                while True:
                    chunk = await src.read(_CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
        await asyncio.gather(pump(cr, up_w), pump(up_r, cw))

    async def _error(self, w, status: int, message: str) -> None:
        reasons = {400: "Bad Request", 403: "Forbidden", 502: "Bad Gateway"}
        body = json.dumps({"message": f"[docker-proxy] {message}"}).encode()
        head = (
            f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            w.write(head + body)
            await w.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    @staticmethod
    async def _close(w) -> None:
        try:
            w.close()
            await w.wait_closed()
        except (OSError, asyncio.CancelledError):
            pass


__all__ = ["DockerProxy"]
