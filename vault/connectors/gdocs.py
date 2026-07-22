"""gdocs — коннектор Google Docs/Drive/Sheets/Slides (§4.5, фаза 2 редизайна
claude-box). Скоуп «только эти документы/папки»: понимает URL Google API, где в
запросе лежит fileId, и рубит операции вне «читать/править содержимое» (шаринг/
экспорт/копирование/листинг) даже для документа В скоупе.

Достоверность: URL-паттерны Google закодированы ИЗ ЗНАНИЙ (живого сервиса на
проверку нет), поэтому политика КОНСЕРВАТИВНА и fail-closed: неизвестный/
неоднозначный URL под Google-хостом → DENY, а не ALLOW. Разбор id идёт по
сегментам пути (не подстрокой) поверх канонизации из generic_bearer (percent-
decode до неподвижной точки + resolve `..`), поэтому обход кодированием/traversal/
поддомен-обманом (`docs.google.com.evil`) не проходит.

Хранение/рефреш OAuth-токена — НЕ в этом срезе: authorize берёт готовый
access-token из secret.value (как generic-bearer), а oauth_flow/refresh/mint/
resolve_scope объявлены (контракт), но пока заглушки — «live-OAuth, следующий
срез».
"""

from __future__ import annotations

import logging
from urllib.parse import parse_qsl, urlsplit

from ..secret import Secret
from .contract import Connector, HttpReq, ScopeVerdict, with_header
from .generic_bearer import _canonical, _fully_unquoted

logger = logging.getLogger("vault.connectors")

# Официальные Google-хосты (UI + REST API). Хост сверяется СТРОГИМ равенством
# (после lowercase) — это рубит поддомен-обман `docs.google.com.evil`, userinfo-
# трюк `docs.google.com@evil` (urlsplit.hostname отдаёт evil), путь-обман
# `evil/docs.google.com`. Незнакомый/чужой хост → DENY.
_UI_HOSTS = frozenset(
    {
        "docs.google.com",   # /document|spreadsheets|presentation/d/<id>/…
        "drive.google.com",  # /file/d/<id>/…, /drive/folders/<id>, open?id=, uc?id=
        "sheets.google.com",  # UI-вариант таблиц
    }
)
_API_HOSTS = frozenset(
    {
        "www.googleapis.com",     # Drive API v2/v3: (upload/)drive/v3/files/<id>
        "docs.googleapis.com",    # Docs API v1: documents/<id>[:batchUpdate]
        "sheets.googleapis.com",  # Sheets API v4: spreadsheets/<id>[/values|:batchUpdate]
        "slides.googleapis.com",  # Slides API v1: presentations/<id>[:batchUpdate]
    }
)
_HOSTS = _UI_HOSTS | _API_HOSTS

# Сегмент-«маркер коллекции»: id ресурса идёт СЛЕДУЮЩИМ сегментом пути. `d` —
# форма UI (`/document/d/<id>`), остальные — REST-коллекции. `folders` помечает
# id папки. Маркер без следующего сегмента = обращение к коллекции целиком
# (листинг/создание) → в скоуп «конкретные документы» не входит.
_ID_MARKERS = frozenset({"d", "files", "documents", "spreadsheets", "presentations", "folders"})

# Маркеры REST-коллекций (для внятного reason «листинг/создание», отдельно от
# «неизвестный endpoint»).
_COLLECTION_MARKERS = frozenset({"files", "documents", "spreadsheets", "presentations"})

# Ключи query, где может лежать id ресурса, когда его нет в пути: drive open?id=,
# uc?id=, а также устаревшие ?fileId=/?spreadsheetId=/… (проверяются в lowercase).
_ID_QUERY_KEYS = ("fileid", "spreadsheetid", "presentationid", "documentid", "id")

# Операции ВНЕ «читать/править содержимое» — DENY даже для in-scope id. Это
# сегмент-действие сразу ПОСЛЕ id (`files/<id>/export`, `.../d/<id>/copy`) либо
# кастом-verb на самом id-сегменте (`<id>:watch`). Шаринг/экспорт/копия/подписка/
# ревизии. `export` дополнительно ловится как ключ query (`uc?export=download`).
_DANGEROUS_OPS = frozenset({"export", "permissions", "copy", "watch", "revisions"})


def _segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def _split_verb(seg: str) -> tuple[str, str | None]:
    """`<id>:batchUpdate` → (id, 'batchupdate'); `<id>` → (id, None).

    Кастом-verb REST-методов Google цепляется к последнему сегменту через `:`.
    Диапазоны Sheets (`Sheet1!A1:B2`) приходят ОТДЕЛЬНЫМ сегментом после
    `values`, поэтому split id-сегмента по `:` их не задевает."""
    if ":" in seg:
        base, verb = seg.split(":", 1)
        return base, verb.lower()
    return seg, None


def _extract(path: str, query_pairs: list[tuple[str, str]]) -> dict:
    """Разобрать канонический путь+query → {marker, id, verb, op}.

      * id  — fileId/документа (по маркеру коллекции в пути или по query-ключу);
      * verb — кастом-verb на id-сегменте (`<id>:batchUpdate`);
      * op  — сегмент-действие СРАЗУ после id (`export`/`permissions`/`values`…);
      * marker — что сматчило (маркер коллекции / 'query' / None).

    id из пути имеет приоритет над query. Маркер коллекции без следующего
    сегмента → id=None (обращение к коллекции: листинг/создание)."""
    segs = _segments(path)
    for i, seg in enumerate(segs):
        if seg in _ID_MARKERS:
            if i + 1 >= len(segs):
                return {"marker": seg, "id": None, "verb": None, "op": None}
            # UI-форма `/spreadsheets/d/<id>`: `spreadsheets` — тоже маркер, но
            # реальный id идёт за вложенным `d`. Если следующий сегмент сам маркер
            # — этот пропускаем, id возьмёт внутренний маркер.
            if segs[i + 1] in _ID_MARKERS:
                continue
            file_id, verb = _split_verb(segs[i + 1])
            op = _split_verb(segs[i + 2])[0].lower() if i + 2 < len(segs) else None
            return {"marker": seg, "id": file_id or None, "verb": verb, "op": op}
    qd = {k.lower(): v for k, v in query_pairs}
    for key in _ID_QUERY_KEYS:
        if qd.get(key):
            return {"marker": "query", "id": qd[key], "verb": None, "op": None}
    return {"marker": None, "id": None, "verb": None, "op": None}


class GDocsConnector:
    """Коннектор Google Docs/Drive/Sheets/Slides: OAuth-Bearer + скоуп по
    docs/folders с deny на share/export/copy/листинг (fail-closed)."""

    name = "gdocs"

    def authorize(self, req: HttpReq, secret: Secret) -> HttpReq:
        """OAuth access-token (уже готовый в secret.value; хранение/рефреш —
        следующий срез) как `Authorization: Bearer <value>`. Значение НЕ
        логируем. Возвращает копию запроса (исходный не мутируем)."""
        return with_header(req, "Authorization", f"Bearer {secret.value}")

    def in_scope(self, req: HttpReq, scope: dict) -> ScopeVerdict:
        docs = tuple(scope.get("docs") or [])
        folders = tuple(scope.get("folders") or [])
        allowed = set(docs) | set(folders)

        if not docs and not folders:
            return ScopeVerdict.deny(
                reason="секрету gdocs не выдан ни один документ/папка — в скоуп не входит ничего",
                remedy=(
                    "Этому секрету gdocs не выдан ни один документ или папка, поэтому любой "
                    "запрос вне скоупа. Попроси оператора добавить нужные id в scope.docs / "
                    "scope.folders этого секрета."
                ),
            )

        canon = _canonical(req.url)
        if canon is None:
            return ScopeVerdict.deny(
                reason=f"URL «{req.url}» подозрителен (многослойное кодирование)",
                remedy=self._remedy(
                    "URL не декодируется однозначно (многослойный percent-encoding) — "
                    "кошелёк такой не пропускает. Обратись обычным URL без лишнего кодирования.",
                    docs, folders,
                ),
            )
        scheme, _netloc, path = canon
        sp = urlsplit(req.url)
        host = (sp.hostname or "").lower()
        port = sp.port
        if scheme != "https" or host not in _HOSTS or port not in (None, 443):
            return ScopeVerdict.deny(
                reason=f"хост/схема «{scheme}://{host}» — не официальный Google-хост",
                remedy=self._remedy(
                    f"Запрос идёт на «{host or '?'}» (или не по https/на нестандартный порт) — "
                    "это не официальный Google-хост. Поддомен-обман (docs.google.com.evil) и "
                    "чужие хосты кошелёк не пропускает. Официальные: "
                    + ", ".join(sorted(_HOSTS)) + ".",
                    docs, folders,
                ),
            )

        # query с декодом до неподвижной точки (тот же класс защиты, что путь).
        pairs: list[tuple[str, str]] = []
        for k, v in parse_qsl(sp.query, keep_blank_values=True):
            dk, dv = _fully_unquoted(k), _fully_unquoted(v)
            if dk is None or dv is None:
                return ScopeVerdict.deny(
                    reason=f"query URL «{req.url}» подозрителен (многослойное кодирование)",
                    remedy=self._remedy(
                        "Параметры URL не декодируются однозначно — кошелёк такой не пропускает.",
                        docs, folders,
                    ),
                )
            pairs.append((dk, dv))

        info = _extract(path, pairs)

        # Опасная операция вне «читать/править содержимое» → DENY ДАЖЕ для in-scope id.
        danger = None
        if req.method.upper() == "DELETE":
            danger = "удаление ресурса (DELETE)"
        elif info["verb"] in _DANGEROUS_OPS:
            danger = info["verb"]
        elif info["op"] in _DANGEROUS_OPS:
            danger = info["op"]
        elif any(k.lower() == "export" for k, _ in pairs):
            danger = "export"
        if danger is not None:
            return ScopeVerdict.deny(
                reason=(
                    f"операция «{danger}» вне «читать/править содержимое» — "
                    "запрещена даже для документа в скоупе"
                ),
                remedy=self._remedy(
                    f"Операция «{danger}» (шаринг/экспорт/копирование/удаление/подписка) "
                    "коннектором gdocs запрещена — даже для документа из твоего скоупа.",
                    docs, folders,
                ),
            )

        file_id = info["id"]
        if file_id is None:
            if info["marker"] in _COLLECTION_MARKERS:
                reason = (
                    "запрос к коллекции целиком (листинг/создание, без конкретного id) — "
                    "вне скоупа «конкретные документы»"
                )
                lead = (
                    "Это листинг/создание в коллекции (без конкретного документа) — "
                    "коннектор gdocs такое не пропускает."
                )
            else:
                reason = f"в URL «{req.url}» не выделяется конкретный документ из скоупа"
                lead = (
                    "В этом URL не удаётся однозначно выделить конкретный документ — "
                    "коннектор gdocs fail-closed отклоняет неоднозначное."
                )
            return ScopeVerdict.deny(reason=reason, remedy=self._remedy(lead, docs, folders))

        if file_id in allowed:
            return ScopeVerdict.allow()

        return ScopeVerdict.deny(
            reason=f"документ «{file_id}» не входит в выданный скоуп gdocs",
            remedy=self._remedy(f"Документ «{file_id}» не в твоём скоупе.", docs, folders),
        )

    @staticmethod
    def _fmt(ids: tuple[str, ...]) -> str:
        return ", ".join(ids) if ids else "(нет)"

    def _remedy(self, lead: str, docs: tuple[str, ...], folders: tuple[str, ...]) -> str:
        """Предписывающий remedy (Р0): что не так + что доступно (из КЭША скоупа —
        scope-dict, без живого вызова) + что нельзя + что делать."""
        return " ".join(
            (
                lead,
                f"В твоём скоупе gdocs — документы: [{self._fmt(docs)}]; папки: [{self._fmt(folders)}].",
                "Можно: читать и править СОДЕРЖИМОЕ этих документов (docs.google.com/…/d/<id>/…, "
                "Drive/Docs/Sheets/Slides API по этим id).",
                "Нельзя: шаринг (permissions), экспорт (export), копирование (copy), удаление, "
                "листинг всех файлов, любой документ вне списка.",
                "Что делать: работай только с перечисленными id обычным запросом (кошелёк "
                "подставит OAuth-токен сам); нужен другой документ/папка или операция вне "
                "чтения/правки — попроси оператора расширить scope (docs/folders) этого секрета.",
            )
        )

    # --- опциональные умения контракта: live-OAuth — следующий срез ---
    def oauth_flow(self) -> object | None:
        return None  # интерактивный `vault connect gdocs` — live-OAuth, следующий срез

    def resolve_scope(self, human: dict) -> dict:
        # «Team/X» → folder id требует живого вызова Drive — следующий срез; пока as-is.
        return human

    def mint(self, scope: dict) -> Secret | None:
        return None  # суб-токены gdocs не минтит

    def refresh(self, secret: Secret) -> Secret | None:
        return None  # рефреш сервисного OAuth — live, следующий срез


# Соответствие контракту — в момент импорта (@runtime_checkable: реальный
# isinstance по наличию методов). Забыт метод — падение при импорте, не в демоне.
assert isinstance(GDocsConnector(), Connector), "GDocsConnector не соответствует контракту Connector"
