"""VaultProxy — HTTP-forward-прокси с MITM-перехватом HTTPS (§4.2 «произвольный
клиент → MITM-прокси: расшифрованный запрос»). Значение секрета в машину НЕ
входит: прокси подставляет кред МЕЖДУ машиной и сервисом (§4.4, тир «дефолт»).

Автономно: только stdlib + vault.* (ни строки orchestrator — держится
vault_domain_test через walk_packages). Транспорт — asyncio (как daemon.py).

Границы ЭТОГО среза (2.3): ОДИН коннектор + ОДИН секрет + ОДИН scope,
standalone-компонент. Маршрутизация per-session (свой listen-порт на сессию,
§4.3) и интеграция в демон/лончер — следующий срез (2.4), здесь НЕ делается.

Поток на соединение:
  1. Клиент шлёт `CONNECT host:port` (обычный HTTPS-через-прокси). Отвечаем
     `200 Connection established` по plaintext, затем ТЕРМИНИРУЕМ TLS на своей
     стороне leaf-сертом `VaultCA.issue(host)` (server-side `start_tls`). Клиент
     доверяет, т.к. корень Vault лежит у него в trust-store (§4.2).
  2. Читаем расшифрованный HTTP-запрос → собираем `HttpReq` (method, реальный
     url=`https://host/path`, headers, body).
  3. Если host — «свой» для секрета (входит в service_hosts): коннектор решает
     скоуп `in_scope(req, scope)`:
       * ALLOW → `authorize(req, secret)` подставляет кред (значение НЕ логируем)
         и РЕОРИГИНИРУЕМ: TLS-коннект к РЕАЛЬНОМУ host (обычный trust, НЕ наш
         CA), шлём авторизованный запрос, стримим ответ обратно клиенту.
       * DENY → отвечаем 403 с `reason`+`remedy` в теле (Р0: предписывающе), к
         сервису НЕ ходим.
       * ASK → в этой сборке хука спроса ещё нет (§4.6, следующий срез), поэтому
         трактуем как DENY с предписывающим remedy — НЕ вешаем соединение в
         ожидании несуществующего диалога (урок «не зависать»). См. _respond_ask.
  4. host НЕ покрыт секретом → обычный форвард БЕЗ инъекции (§4.4 «путь открыт,
     но пуст»): реоригинируем как есть, сервис сам вернёт 401. Egress сверх
     scope-DENY не режем (Р7).

Про «не зависать» (урок docker-прокси: обработчик оставался pending при обрыве):
  * все чтения И записи (`drain`) — под `asyncio.wait_for` с таймаутами (не-
    читающий клиент заполняет write-буфер, и голый `drain()` завис бы бессрочно
    в обход read-таймаута — resource-hang/DoS от полу-доверенного sandbox-пира);
  * реориджин ОДНОНАПРАВЛЕННЫЙ: запрос дослан целиком, затем качаем upstream→
    client до EOF; upstream принуждаем к EOF заголовком `Connection: close`, так
    не нужно парсить фрейминг ответа и нет висящего keep-alive;
  * обе стороны закрываются в finally; обрыв любой стороны роняет качалку и
    закрывает встречную (нет осиротевшей задачи).

Про server-side start_tls: под соединение, созданное `asyncio.start_server`,
`StreamWriter.start_tls` выводит server_side из наличия client_connected_cb у
протокола — т.е. апгрейд идёт как сервер (что нам и нужно) без прямого доступа к
транспорту. Реориджин к сервису — обычный `open_connection(ssl=…)`, TLS сразу на
коннекте, без ручного апгрейда. Выбор asyncio (а не поток-на-соединение)
оправдан: single-loop без блокировок, таймауты на каждом await, апгрейд через
штатный StreamWriter.start_tls стабилен на 3.11+.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from contextlib import suppress
from urllib.parse import urlsplit

from .connectors import HttpReq
from .connectors.contract import Connector, ScopeVerdict
from .secret import Secret
from .tls import VaultCA, VaultCAError

logger = logging.getLogger(__name__)

# Таймауты (сек): держат прокси от зависаний на медленном/оборванном пире.
_CONNECT_TIMEOUT = 30      # приём CONNECT-строки и коннект к upstream
_HANDSHAKE_TIMEOUT = 30    # MITM-хендшейк с клиентом
_READ_TIMEOUT = 60         # чтение одного запроса (строка+заголовки+тело)
_IDLE_TIMEOUT = 300        # пауза в потоке ответа сервиса → рвём
# Потолки: защита от заливки памяти гигабайтами (как STREAM_LIMIT в redact).
_MAX_LINE = 64 * 1024
_MAX_HEADERS = 200
_MAX_BODY = 32 * 1024 * 1024
_CHUNK = 64 * 1024

# Заголовки хоп-бай-хоп/фрейминга: снимаем перед реориджином, выставляем свои
# (Content-Length по факту + Connection: close, чтобы upstream дал EOF).
_HOP_HEADERS = frozenset({
    "connection", "proxy-connection", "keep-alive", "transfer-encoding",
    "content-length", "te", "trailer", "upgrade", "proxy-authenticate",
    "proxy-authorization",
})


class VaultProxyError(RuntimeError):
    """Сбой конфигурации/парсинга на уровне прокси."""


def _hosts_from_scope(scope: dict) -> set[str]:
    """Хосты, которыми «владеет» секрет — из url_prefixes его scope. Запрос к
    такому хосту обязан быть в скоупе (иначе DENY); к прочим — сквозной форвард."""
    hosts: set[str] = set()
    for pref in scope.get("url_prefixes") or []:
        host = urlsplit(pref).hostname
        if host:
            hosts.add(host.lower())
    return hosts


def _split_authority(authority: str) -> tuple[str, int]:
    """`host:port` из CONNECT → (host, port). Поддержка `[::1]:443` (IPv6)."""
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest.lstrip(":") or "443"
    else:
        host, _, port = authority.partition(":")
        port = port or "443"
    try:
        return host, int(port)
    except ValueError as exc:
        raise VaultProxyError(f"нечисловой порт в CONNECT {authority!r}") from exc


class VaultProxy:
    """MITM-forward-прокси под ОДИН секрет/коннектор/scope (срез 2.3).

    Параметры:
      * ca — VaultCA (корень в trust-store клиента; issue(host) даёт leaf);
      * connector — плагин сервиса (authorize/in_scope);
      * secret — секрет, чей кред подставляем на ALLOW;
      * scope — машинный скоуп (для generic-bearer: {"url_prefixes": [...]});
      * service_hosts — хосты, которыми владеет секрет; None → вывести из scope;
      * upstream_ssl — SSLContext для коннекта к РЕАЛЬНОМУ сервису (обычный
        trust); None → системный default. В тесте сюда кладут trust к локальному
        «сервису» (его серт), чтобы реориджин ему доверял;
      * bind_host/bind_port — где слушать (0 = эфемерный).
    """

    def __init__(
        self,
        ca: VaultCA,
        connector: Connector,
        secret: Secret,
        scope: dict,
        *,
        service_hosts: set[str] | None = None,
        upstream_ssl: ssl.SSLContext | None = None,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
        connect_timeout: float = _CONNECT_TIMEOUT,
        handshake_timeout: float = _HANDSHAKE_TIMEOUT,
        read_timeout: float = _READ_TIMEOUT,
        idle_timeout: float = _IDLE_TIMEOUT,
    ) -> None:
        self.ca = ca
        self.connector = connector
        self.secret = secret
        self.scope = scope
        self.service_hosts = (
            {h.lower() for h in service_hosts}
            if service_hosts is not None
            else _hosts_from_scope(scope)
        )
        self.upstream_ssl = (
            upstream_ssl if upstream_ssl is not None else ssl.create_default_context()
        )
        self.bind_host = bind_host
        self.bind_port = bind_port
        # Таймауты — атрибуты (не только модульные константы): тесты подставляют
        # малые значения, чтобы проверить размыкание по таймауту без 5-мин ожиданий.
        self._connect_timeout = connect_timeout
        self._handshake_timeout = handshake_timeout
        self._read_timeout = read_timeout
        self._idle_timeout = idle_timeout
        self.port: int | None = None
        self._server: asyncio.AbstractServer | None = None
        # Кэш server-SSLContext по host (leaf грузим с диска один раз на host).
        self._leaf_ctx: dict[str, ssl.SSLContext] = {}

    # --- жизненный цикл ----------------------------------------------------

    async def start(self) -> int:
        """Поднять listener, вернуть фактический порт."""
        self._server = await asyncio.start_server(
            self._handle_client, self.bind_host, self.bind_port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        logger.info("VaultProxy слушает на %s:%s", self.bind_host, self.port)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with suppress(Exception):
                await self._server.wait_closed()
            self._server = None

    @property
    def proxy_url(self) -> str:
        if self.port is None:
            raise VaultProxyError("прокси не запущен (нет порта)")
        return f"http://{self.bind_host}:{self.port}"

    async def __aenter__(self) -> VaultProxy:
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()

    # --- обработка соединения ---------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await self._serve(reader, writer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            pass  # клиент оборвался / молчит — просто закрываемся
        except Exception:  # noqa: BLE001 — прокси не должен падать на одном коннекте
            logger.exception("VaultProxy: необработанная ошибка на соединении")
        finally:
            with suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _drain(self, writer: asyncio.StreamWriter) -> None:
        """drain ПОД таймаутом: не-читающий пир заполняет write-буфер, и голый
        `drain()` завис бы бессрочно (read-таймаут на это не влияет — блок на
        записи), держа сокеты — resource-hang/DoS от полу-доверенного клиента.
        Таймаут → TimeoutError вверх → соединение рвётся (finally закроет сокеты),
        как любой другой таймаут."""
        await asyncio.wait_for(writer.drain(), timeout=self._idle_timeout)

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        line = await asyncio.wait_for(
            reader.readuntil(b"\r\n"), timeout=self._connect_timeout
        )
        parts = line.rstrip(b"\r\n").decode("latin-1").split()
        if len(parts) < 2:
            await self._send_response(writer, 400, "Bad Request", b"malformed request line\n")
            return
        method, authority = parts[0].upper(), parts[1]
        if method != "CONNECT":
            # Форвард plain-HTTP absolute-form — вне этого среза (сервисы под
            # секретом всегда HTTPS/CONNECT). Явный, не молчаливый отказ.
            await self._send_response(
                writer, 501, "Not Implemented",
                b"VaultProxy build handles only CONNECT (HTTPS) tunnels\n",
            )
            return
        await self._drain_headers(reader)  # дочитать заголовки CONNECT до пустой

        try:
            host, port = _split_authority(authority)
        except VaultProxyError:
            await self._send_response(writer, 400, "Bad Request", b"bad CONNECT target\n")
            return

        # leaf под host: невалидный host → VaultCAError → 502, не падаем.
        try:
            leaf_ctx = self._leaf_context(host)
        except VaultCAError as exc:
            logger.warning("VaultProxy: не выпустить leaf на %r: %s", host, exc)
            await self._send_response(
                writer, 502, "Bad Gateway",
                f"VaultProxy: cannot issue certificate for host: {exc}\n".encode(),
            )
            return

        writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        await self._drain(writer)

        # Апгрейд клиентской стороны в TLS (server-side — см. module-docstring).
        try:
            await asyncio.wait_for(
                writer.start_tls(leaf_ctx), timeout=self._handshake_timeout
            )
        except (ssl.SSLError, asyncio.TimeoutError, ConnectionError) as exc:
            logger.info("VaultProxy: MITM-хендшейк с клиентом не удался (%s): %s", host, exc)
            return

        await self._serve_decrypted(reader, writer, host, port, authority)

    async def _serve_decrypted(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        host: str,
        port: int,
        authority: str,
    ) -> None:
        method, target, headers, body = await self._read_request(reader)
        # Маршрут — ТОЛЬКО по CONNECT-authority. Валидный запрос в туннеле —
        # origin-form (`/path`). Absolute-form (`GET https://evil/…`) или
        # authority-form — попытка увести реориджин/скоуп на чужой ресурс мимо
        # authority: fail-closed 400, к in_scope мусорный url не пускаем (иначе
        # urlsplit(url).port на битом порту бросал бы ValueError → сырой обрыв).
        if not target.startswith("/"):
            await self._send_response(
                writer, 400, "Bad Request",
                b"VaultProxy: only origin-form request targets are accepted; "
                b"the tunnel routes by the CONNECT authority, not by an "
                b"absolute-form URL or Host header\n",
            )
            return
        url = f"https://{authority}{target}"
        req = HttpReq(method=method, url=url, headers=dict(headers), body=body)

        if host.lower() in self.service_hosts:
            verdict = self.connector.in_scope(req, self.scope)
            if verdict.is_deny:
                await self._respond_deny(writer, verdict)
                return
            if verdict.is_ask:
                await self._respond_ask(writer, verdict)
                return
            # ALLOW — подставляем кред (значение секрета в лог НЕ попадает).
            req = self.connector.authorize(req, self.secret)
            logger.info("VaultProxy: ALLOW %s %s (кред подставлен)", method, url)
        else:
            logger.info("VaultProxy: сквозной форвард %s %s (без кред)", method, url)

        await self._reoriginate(writer, host, port, method, target, req.headers, req.body)

    # --- реориджин к реальному сервису ------------------------------------

    async def _reoriginate(
        self,
        client_writer: asyncio.StreamWriter,
        host: str,
        port: int,
        method: str,
        target: str,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port, ssl=self.upstream_ssl, server_hostname=host
                ),
                timeout=self._connect_timeout,
            )
        except (ssl.SSLError, OSError, asyncio.TimeoutError) as exc:
            # Ошибку логируем/отдаём БЕЗ заголовков (там может быть кред).
            logger.warning("VaultProxy: коннект к сервису %s:%s не удался: %s", host, port, exc)
            await self._send_response(
                client_writer, 502, "Bad Gateway",
                f"VaultProxy: upstream connection failed: {exc}\n".encode(),
            )
            return
        try:
            up_writer.write(self._build_request(method, target, headers, body))
            await self._drain(up_writer)
            # Однонаправленная качалка: upstream принуждён к EOF (Connection:close),
            # поэтому дойдём до конца без парсинга фрейминга ответа и не зависнем.
            await self._pump(up_reader, client_writer)
        finally:
            with suppress(Exception):
                up_writer.close()
                await up_writer.wait_closed()

    async def _pump(
        self, src: asyncio.StreamReader, dst: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                chunk = await asyncio.wait_for(src.read(_CHUNK), timeout=self._idle_timeout)
                if not chunk:
                    break
                dst.write(chunk)
                # drain ПОД таймаутом: если КЛИЕНТ перестал читать большой ответ,
                # write-буфер заполнится и голый drain завис бы бессрочно (MEDIUM).
                await self._drain(dst)
        except (asyncio.TimeoutError, ssl.SSLError, OSError):
            pass  # любая сторона отвалилась — прекращаем, finally закроет сокеты

    # --- парсинг запроса ---------------------------------------------------

    async def _read_request(
        self, reader: asyncio.StreamReader
    ) -> tuple[str, str, dict[str, str], bytes]:
        line = await asyncio.wait_for(
            reader.readuntil(b"\r\n"), timeout=self._read_timeout
        )
        fields = line.rstrip(b"\r\n").decode("latin-1").split(" ", 2)
        if len(fields) < 2:
            raise VaultProxyError("битая строка запроса")
        method, target = fields[0], fields[1]
        headers = await self._read_headers(reader)
        body = await self._read_body(reader, headers)
        return method, target, headers, body

    async def _read_headers(self, reader: asyncio.StreamReader) -> dict[str, str]:
        headers: dict[str, str] = {}
        for _ in range(_MAX_HEADERS):
            hl = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=self._read_timeout)
            if hl in (b"\r\n", b"\n"):
                return headers
            name, sep, value = hl.rstrip(b"\r\n").decode("latin-1").partition(":")
            if not sep:
                continue  # строка без ':' — пропускаем (толерантно)
            headers[name.strip()] = value.strip()
        raise VaultProxyError("слишком много заголовков")

    async def _drain_headers(self, reader: asyncio.StreamReader) -> None:
        """Дочитать заголовки CONNECT до пустой строки (их не форвардим)."""
        for _ in range(_MAX_HEADERS):
            hl = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=self._read_timeout)
            if hl in (b"\r\n", b"\n"):
                return
        raise VaultProxyError("слишком много заголовков CONNECT")

    async def _read_body(
        self, reader: asyncio.StreamReader, headers: dict[str, str]
    ) -> bytes:
        te = _get(headers, "transfer-encoding")
        if te and "chunked" in te.lower():
            return await self._read_chunked(reader)
        cl = _get(headers, "content-length")
        if cl is None:
            return b""
        try:
            length = int(cl)
        except ValueError as exc:
            raise VaultProxyError(f"нечисловой Content-Length: {cl!r}") from exc
        if length < 0 or length > _MAX_BODY:
            raise VaultProxyError(f"Content-Length вне допустимого: {length}")
        if length == 0:
            return b""
        return await asyncio.wait_for(reader.readexactly(length), timeout=self._read_timeout)

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        buf = bytearray()
        while True:
            size_line = await asyncio.wait_for(
                reader.readuntil(b"\r\n"), timeout=self._read_timeout
            )
            size_hex = size_line.split(b";", 1)[0].strip()
            try:
                size = int(size_hex, 16)
            except ValueError as exc:
                raise VaultProxyError("битый chunk-size") from exc
            if size == 0:
                # трейлеры до пустой строки
                while True:
                    tl = await asyncio.wait_for(
                        reader.readuntil(b"\r\n"), timeout=self._read_timeout
                    )
                    if tl in (b"\r\n", b"\n"):
                        break
                return bytes(buf)
            if len(buf) + size > _MAX_BODY:
                raise VaultProxyError("тело chunked превысило лимит")
            buf += await asyncio.wait_for(reader.readexactly(size), timeout=self._read_timeout)
            await asyncio.wait_for(reader.readexactly(2), timeout=self._read_timeout)  # CRLF
        # недостижимо

    # --- сборка исходящего запроса / ответов ------------------------------

    def _build_request(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> bytes:
        out: dict[str, str] = {}
        for name, value in headers.items():
            if name.lower() in _HOP_HEADERS:
                continue
            out[name] = value
        out["Content-Length"] = str(len(body or b""))
        out["Connection"] = "close"  # заставляем сервис дать EOF после ответа
        head = f"{method} {target} HTTP/1.1\r\n"
        head += "".join(f"{k}: {v}\r\n" for k, v in out.items())
        head += "\r\n"
        return head.encode("latin-1") + (body or b"")

    async def _respond_deny(
        self, writer: asyncio.StreamWriter, verdict: ScopeVerdict
    ) -> None:
        body = f"{verdict.reason}\n\n{verdict.remedy}\n".encode()
        await self._send_response(writer, 403, "Forbidden", body)

    async def _respond_ask(
        self, writer: asyncio.StreamWriter, verdict: ScopeVerdict
    ) -> None:
        """ASK без хука спроса (§4.6 — след. срез): предписывающий DENY, не висим."""
        body = (
            f"Доступ требует подтверждения оператора: {verdict.descr}.\n\n"
            "В этой сборке кошелька интерактивный спрос ещё не подключён "
            "(появится в следующем срезе), поэтому запрос не пропускается. "
            "Оставайся в пределах уже разрешённого скоупа секрета либо попроси "
            "оператора расширить его заранее.\n"
        ).encode()
        await self._send_response(writer, 403, "Forbidden", body)

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        code: int,
        phrase: str,
        body: bytes,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        head = (
            f"HTTP/1.1 {code} {phrase}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        writer.write(head + body)
        with suppress(Exception):
            await self._drain(writer)  # под таймаутом — не виснем на не-читающем пире

    # --- вспомогательное ---------------------------------------------------

    def _leaf_context(self, host: str) -> ssl.SSLContext:
        cached = self._leaf_ctx.get(host)
        if cached is not None:
            return cached
        leaf = self.ca.issue(host)  # валидирует host, VaultCAError на плохом
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=str(leaf.cert_path), keyfile=str(leaf.key_path))
        self._leaf_ctx[host] = ctx
        return ctx


def _get(headers: dict[str, str], name: str) -> str | None:
    """Регистронезависимый доступ к заголовку."""
    low = name.lower()
    for k, v in headers.items():
        if k.lower() == low:
            return v
    return None
