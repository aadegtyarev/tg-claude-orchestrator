"""generic-bearer — базовый коннектор-фолбэк (§4.5): подставляет секрет
`Authorization: Bearer <value>` и держит скоуп по URL-префиксам. Покрывает
сегодняшний «curl со скоупом» без знания конкретного сервиса.

Без OAuth/минта/резолва — сервис неизвестен, префиксы литеральные. Без живых
вызовов: in_scope смотрит только сам URL и список префиксов из scope.
"""

from __future__ import annotations

import posixpath
from urllib.parse import unquote, urlsplit

from ..secret import Secret
from .contract import Connector, HttpReq, ScopeVerdict, with_header


def _canonical(url: str) -> tuple[str, str, str]:
    """URL → (scheme, netloc, path) в канонической форме для СРАВНЕНИЯ префиксов.

    Схема/хост — в нижний регистр; порт — в netloc. Путь: сначала percent-decode
    (чтобы `%2e%2e`/`%2f` не проскочили мимо проверки), затем resolve dot-segments
    (`posixpath.normpath` схлопывает `..`/`.`) — иначе `…/v1/../admin`
    строково-начинался бы с разрешённого `…/v1/`, но резолвился в `/admin`
    (урок docker/decision про пути). normpath снимает хвостовой слэш: `/v1/`→`/v1`.
    """
    p = urlsplit(url)
    scheme = (p.scheme or "").lower()
    host = (p.hostname or "").lower()
    netloc = host if p.port is None else f"{host}:{p.port}"
    path = unquote(p.path or "/")
    if not path.startswith("/"):
        path = "/" + path
    return scheme, netloc, posixpath.normpath(path)


def _under_prefix(req_c: tuple[str, str, str], pref_c: tuple[str, str, str]) -> bool:
    """req под префиксом: та же схема+netloc И путь равен префиксу либо лежит под
    ним НА ГРАНИЦЕ СЕГМЕНТА (`/v1` покрывает `/v1/x`, но НЕ `/v1abc`)."""
    rs, rn, rp = req_c
    ps, pn, pp = pref_c
    if rs != ps or rn != pn:
        return False
    return rp == pp or rp.startswith(pp.rstrip("/") + "/")


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
        if any(_under_prefix(req_c, _canonical(pref)) for pref in prefixes):
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


# Подтверждаем структурное соответствие контракту в момент импорта модуля
# (ошибись в сигнатуре — увидим сразу, а не в рантайме демона).
_: Connector = GenericBearerConnector()
