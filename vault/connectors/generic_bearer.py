"""generic-bearer — базовый коннектор-фолбэк (§4.5): подставляет секрет
`Authorization: Bearer <value>` и держит скоуп по URL-префиксам. Покрывает
сегодняшний «curl со скоупом» без знания конкретного сервиса.

Без OAuth/минта/резолва — сервис неизвестен, префиксы литеральные. Без живых
вызовов: in_scope смотрит только сам URL и список префиксов из scope.
"""

from __future__ import annotations

import logging
import posixpath
from urllib.parse import unquote, urlsplit

from ..secret import Secret
from .contract import Connector, HttpReq, ScopeVerdict, with_header

logger = logging.getLogger("vault.connectors")

# Потолок итераций percent-decode: защищает от бесконечного/аномального
# многослойного кодирования. Реальные URL декодируются за 1 проход; двойное/
# тройное — за 2–3. Не сошлось за потолок → URL подозрительный, скоуп его не
# принимает (см. _canonical → None).
_MAX_DECODE_PASSES = 5

# Схема → её дефолтный порт: явный `:443`/`:80` в netloc эквивалентен его
# отсутствию, реальные клиенты часто ставят порт явно. Нормализуем, чтобы
# `https://api.svc/` совпадал с `https://api.svc:443/…`.
_DEFAULT_PORTS = {"https": 443, "http": 80}


def _fully_unquoted(s: str) -> str | None:
    """percent-decode В ЦИКЛЕ до неподвижной точки (потолок _MAX_DECODE_PASSES).

    Один `unquote` НЕ ловит двойное кодирование: `%252e` → `%2e` (пройдёт мимо
    проверки `..`), нужен второй проход → `.`. Декодируем, пока строка меняется;
    не сошлось за потолок → None (подозрительный URL, скоуп отклонит)."""
    prev = s
    for _ in range(_MAX_DECODE_PASSES):
        cur = unquote(prev)
        if cur == prev:
            return cur
        prev = cur
    return None  # за потолок не сошлось — не доверяем


def _canonical(url: str) -> tuple[str, str, str] | None:
    """URL → (scheme, netloc, path) в канонической форме для СРАВНЕНИЯ префиксов,
    либо None если URL подозрительный (не декодируется за потолок проходов).

    Схема/хост — в нижний регистр; дефолтный порт (:443 https / :80 http) из
    netloc выкидываем. Путь: percent-decode до неподвижной точки (чтобы
    `%2e%2e`/`%2f` И их многослойные варианты `%252e…` не проскочили мимо
    проверки), затем resolve dot-segments (`posixpath.normpath` схлопывает
    `..`/`.`) — иначе `…/v1/../admin` строково-начинался бы с разрешённого
    `…/v1/`, но резолвился в `/admin` (урок docker/decision про пути). normpath
    снимает хвостовой слэш: `/v1/`→`/v1`.
    """
    p = urlsplit(url)
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower()
    port = p.port
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    netloc = host if port is None else f"{host}:{port}"
    path = _fully_unquoted(p.path or "/")
    if path is None:
        return None
    if not path.startswith("/"):
        path = "/" + path
    return scheme, netloc, posixpath.normpath(path)


def _under_prefix(
    req_c: tuple[str, str, str], pref_c: tuple[str, str, str] | None
) -> bool:
    """req под префиксом: та же схема+netloc И путь равен префиксу либо лежит под
    ним НА ГРАНИЦЕ СЕГМЕНТА (`/v1` покрывает `/v1/x`, но НЕ `/v1abc`). Битый
    (нераскодируемый) префикс → False."""
    if pref_c is None:
        return False
    rs, rn, rp = req_c
    ps, pn, pp = pref_c
    if rs != ps or rn != pn:
        return False
    return rp == pp or rp.startswith(pp.rstrip("/") + "/")


def _canonical_prefix(pref: str) -> tuple[str, str, str] | None:
    """Канонизировать префикс из policy; None если он непригоден. Префикс без
    схемы (`api.svc/v1/` вместо `https://…`) канонизировался бы в пустую схему и
    молча «съедал» бы всё/ничего — логируем громко (прозрачность оператору, как
    реестр логирует неизвестный коннектор) и отбрасываем."""
    canon = _canonical(pref)
    if canon is None:
        logger.warning(
            "generic-bearer: префикс скоупа %r не декодируется — игнорирую", pref
        )
        return None
    scheme, netloc, _path = canon
    if not scheme or not netloc:
        logger.warning(
            "generic-bearer: префикс скоупа %r без схемы/хоста "
            "(нужен полный URL вида https://api.svc/v1/) — игнорирую",
            pref,
        )
        return None
    return canon


class GenericBearerConnector:
    """Фолбэк-коннектор: Bearer-подстановка + скоуп по URL-префиксам."""

    name = "generic-bearer"

    def authorize(self, req: HttpReq, secret: Secret) -> HttpReq:
        """`Authorization: Bearer <secret.value>`. Возвращает копию запроса."""
        return with_header(req, "Authorization", f"Bearer {secret.value}")

    def in_scope(self, req: HttpReq, scope: dict) -> ScopeVerdict:
        prefixes = list(scope.get("url_prefixes") or [])
        if not prefixes:
            return ScopeVerdict.deny(
                reason="у секрета не задан scope.url_prefixes — в скоуп не входит ничего",
                remedy=(
                    "Этому секрету не выдан ни один URL-префикс, поэтому любой запрос "
                    "вне скоупа. Попроси оператора добавить нужные префиксы в "
                    "scope.url_prefixes этого секрета."
                ),
            )
        req_c = _canonical(req.url)
        if req_c is None:
            return ScopeVerdict.deny(
                reason=f"URL «{req.url}» подозрителен (многослойное кодирование)",
                remedy=(
                    "URL не декодируется однозначно (многослойный percent-encoding) — "
                    "кошелёк такой не пропускает. Обратись к сервису обычным URL без "
                    "лишнего кодирования, под разрешёнными префиксами: "
                    + "; ".join(prefixes) + "."
                ),
            )
        if any(_under_prefix(req_c, _canonical_prefix(pref)) for pref in prefixes):
            return ScopeVerdict.allow()
        listed = "; ".join(prefixes)
        return ScopeVerdict.deny(
            reason=f"URL «{req.url}» вне выданного скоупа секрета",
            remedy=(
                f"Этот URL не под разрешёнными префиксами. В скоупе (URL-префиксы): "
                f"{listed}. Зови только URL под этими префиксами (обычным запросом — "
                f"кошелёк подставит кред сам); нужен доступ шире — попроси оператора "
                f"расширить scope.url_prefixes этого секрета."
            ),
        )

    # --- опциональные умения контракта: generic-bearer их не поддерживает ---
    def oauth_flow(self) -> object | None:
        return None  # сервис неизвестен — интерактивного OAuth нет

    def resolve_scope(self, human: dict) -> dict:
        return human  # префиксы литеральные, резолвить нечего

    def mint(self, scope: dict) -> Secret | None:
        return None  # суб-токены выпускать не умеет

    def refresh(self, secret: Secret) -> Secret | None:
        return None  # своего сервисного OAuth нет


# Проверяем соответствие контракту в момент импорта: @runtime_checkable даёт
# РЕАЛЬНый isinstance (наличие методов), в отличие от голой аннотации, которую
# CPython не проверяет. Забыл метод — упадём при импорте, а не в рантайме демона.
assert isinstance(GenericBearerConnector(), Connector), (
    "GenericBearerConnector не соответствует контракту Connector"
)
