"""Модель угроз docker-прокси в чистом виде: (метод, URI, тело) → allow/deny.
Ни сети, ни сокета — только решение. Транспорт (тонкий прокси над сокетом) в
proxy.py; прокси стоит ТОЛЬКО в песочнице модели — личный докер оператора мимо
него, поэтому оператору тут ничего не запрещено.

Две цели (оператор задал явно): не дать модели (1) снести систему и (2) добраться
до секретов по стандартным Linux-путям. Не от намеренного побега — та граница у
agent-vm; это соломка от СЛУЧАЙНОСТЕЙ.

Запрещаем (у модели):
  * снести систему: bind системных путей (/, /etc, /usr, /bin, /lib, /boot,
    /sys, /proc, /root); --privileged; Pid/Ipc/UTS/Userns=host
  * секреты по стандартным путям: bind ~/.ssh, ~/.aws, ~/.gnupg, ~/.kube,
    ~/.docker, каталога secrets.toml, /run+/var/run (там docker.sock)
  * опасные --cap-add (SYS_ADMIN и пр. — тоже путь снести систему)
  * volume create с device+o=bind на запрещённый путь (обходной bind)

Сознательно РАЗРЕШАЕМ:
  * --device (USB/TTY — оператор просил пускать)
  * bind любых прочих путей, включая соседние проекты
  * публикацию портов и network=host — песочница и так на сети хоста

Денайлист (запрет перечисленного, остальное пускаем) — коарзно, но соразмерно
«соломке от случайностей». Нераспарсили create → deny (безопасная сторона).

См. [[docker-in-sandbox]].
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

# Опасные capabilities: фактический выход на хост. Узкий список — не весь набор.
_DANGEROUS_CAPS = {"SYS_ADMIN", "SYS_PTRACE", "SYS_MODULE", "SYS_BOOT",
                   "SYS_RAWIO", "DAC_READ_SEARCH", "ALL"}

# host-namespace режимы в HostConfig.<ключ>.
_HOST_NS = ("PidMode", "IpcMode", "UTSMode", "UsernsMode")

# Путь версионируется: /v1.43/... — терпим к префиксу.
_VERSION_RE = re.compile(r"^/v[0-9]+\.[0-9]+")


def default_forbidden(home: str) -> tuple[str, ...]:
    """Корни хоста, bind которых запрещаем. home — $HOME оператора (креды под ним).
    Оператор может расширить список в конфиге. Заметь: /dev тут НЕТ — устройства
    пускаем через --device (не через bind /dev)."""
    h = home.rstrip("/")
    return (
        # снести систему: RW-bind этих путей может испортить хост
        "/", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/lib32",
        "/boot", "/sys", "/proc", "/root",
        "/var/run", "/run",                     # сокеты, вкл. docker.sock
        # секреты по стандартным Linux-путям
        f"{h}/.ssh", f"{h}/.aws", f"{h}/.gnupg", f"{h}/.kube",
        f"{h}/.docker", f"{h}/.config/claude-orchestrator",  # secrets.toml
    )


@dataclass(frozen=True)
class Verdict:
    allow: bool
    reason: str = ""


@dataclass(frozen=True)
class Policy:
    """Настройка решения. forbidden — префиксы путей, bind которых запрещён."""

    forbidden: tuple[str, ...]
    dangerous_caps: frozenset[str] = field(default_factory=lambda: frozenset(_DANGEROUS_CAPS))

    @classmethod
    def for_home(cls, home: str, extra_forbidden: tuple[str, ...] = ()) -> "Policy":
        return cls(forbidden=tuple(default_forbidden(home)) + tuple(extra_forbidden))


def _norm(path: str) -> str:
    """Нормализовать абсолютный путь хоста для сравнения по префиксу.

    Демон видит уже абсолютные пути (docker CLI резолвит `-v ./x` в абсолют до
    отправки). Схлопываем `..`/симлинк-неустойчивость на уровне строки —
    normpath убирает `a/../b`; ведущий `~` не трогаем (демон его не пришлёт)."""
    return posixpath.normpath(path)


def _under(path: str, root: str) -> bool:
    p = PurePosixPath(_norm(path))
    r = PurePosixPath(_norm(root))
    if r == PurePosixPath("/"):
        # Корень как префикс поймал бы ВСЁ. Запрещаем только bind самого «/».
        return p == r
    return p == r or r in p.parents


def _is_host_path(src: str) -> bool:
    """bind хоста (абсолютный путь) против именованного тома (`myvol`)."""
    return src.startswith("/")


def _forbidden_bind(src: str, forbidden: tuple[str, ...]) -> str | None:
    if not _is_host_path(src):
        return None  # именованный/анонимный том — не путь хоста
    norm = _norm(src)
    for root in forbidden:
        if _under(norm, root):
            return (
                f"монтирование `{src}` запрещено: путь `{root}` содержит "
                f"системные файлы/креды хоста. Смонтируй рабочий каталог проекта, "
                f"а не системный путь."
            )
    return None


def _check_binds(binds, forbidden) -> str | None:
    for entry in binds or []:
        parts = str(entry).split(":")
        if len(parts) < 2:
            continue  # анонимный том без src
        if reason := _forbidden_bind(parts[0], forbidden):
            return reason
    return None


def _check_mounts(mounts, forbidden) -> str | None:
    for mnt in mounts or []:
        if (mnt.get("Type") or "volume") != "bind":
            continue
        src = mnt.get("Source") or mnt.get("source") or ""
        if src and (reason := _forbidden_bind(src, forbidden)):
            return reason
    return None


def _strip_version(path: str) -> str:
    return _VERSION_RE.sub("", path, count=1)


def endpoint(method: str, uri: str) -> str:
    """Ключевой эндпоинт: 'create'|'volume_create'|'other'. Прокси по нему решает,
    надо ли читать и проверять тело запроса (только у create/volume_create)."""
    p = _strip_version(uri).split("?", 1)[0].rstrip("/")
    if method.upper() == "POST" and p == "/containers/create":
        return "create"
    if method.upper() == "POST" and p == "/volumes/create":
        return "volume_create"
    return "other"


def evaluate(method: str, uri: str, body: dict | None, *, policy: Policy) -> Verdict:
    """Решение authz по одному запросу демона.

    body — распарсенное JSON-тело запроса (None, если тела нет/не JSON). Всё, что
    не create-контейнера и не create-тома, разрешаем: host-ФС оно не монтирует.
    """
    kind = endpoint(method, uri)
    if kind == "other":
        return Verdict(True)

    if body is None:
        # create без разбираемого тела — не рискуем (deny — безопасная сторона).
        return Verdict(False, "тело create не разобрано — отказано на всякий случай")

    if kind == "volume_create":
        # Обходной bind: driver=local + o=bind + device=/host/path.
        opts = (body.get("DriverOpts") or {})
        dev = opts.get("device") or opts.get("Device") or ""
        o = (opts.get("o") or opts.get("O") or "")
        if dev and "bind" in str(o) and (reason := _forbidden_bind(dev, policy.forbidden)):
            return Verdict(False, reason)
        return Verdict(True)

    # kind == 'create'
    hc = body.get("HostConfig") or {}

    if hc.get("Privileged"):
        return Verdict(False, (
            "--privileged даёт доступ к устройствам и ядру хоста — это выход из "
            "песочницы. Для сервисов/тестов он не нужен."
        ))

    for key in _HOST_NS:
        if (hc.get(key) or "") == "host":
            return Verdict(False, (
                f"{key}=host разделяет namespace с хостом (его процессы/IPC) — "
                f"пролом песочницы. Убери host-режим."
            ))

    # --device (Devices) СОЗНАТЕЛЬНО пускаем: USB/TTY по делу (оператор просил).

    caps = {str(c).upper() for c in (hc.get("CapAdd") or [])}
    if bad := (caps & policy.dangerous_caps):
        return Verdict(False, (
            f"capabilities {', '.join(sorted(bad))} дают выход на хост. Убери --cap-add."
        ))

    if reason := _check_binds(hc.get("Binds"), policy.forbidden):
        return Verdict(False, reason)
    if reason := _check_mounts(hc.get("Mounts"), policy.forbidden):
        return Verdict(False, reason)

    return Verdict(True)


__all__ = ["Verdict", "Policy", "evaluate", "endpoint", "default_forbidden"]
