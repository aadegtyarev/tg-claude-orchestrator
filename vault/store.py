"""Чтение secrets.toml с кэшем по (mtime, mode, size) и дефолтный файл «из коробки».
Без зависимостей оркестратора.
"""

from __future__ import annotations

import logging
from pathlib import Path

try:
    import tomllib  # stdlib с 3.11
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .connectors import get_connector
from .secret import Secret

logger = logging.getLogger(__name__)

# Дефолтный secrets.toml — создаётся при первом запуске, если файла ещё нет, чтобы
# кошелёк работал «из коробки»: прокол на хост (host-passthrough) для gh/git/ssh/
# scp на все сессии, обёртки в PATH заворачивают их сами. Права строго 0600.
DEFAULT_SECRETS_TOML = """\
# Кошелёк секретов claude-orchestrator — создан автоматически при первом запуске.
# Формат, режимы и policy: docs/secrets-wallet.md. Права строго 0600 (иначе файл
# НЕ загрузится). Правится из бота командой /wallet или руками здесь.
#
# Дефолт ниже — «прокол на хост» (host-passthrough) для gh/git/ssh/scp: команды
# идут на ХОСТ с его кредами (keyring, gh-auth, ~/.ssh), модель их значений не
# видит. Обёртки в PATH заворачивают эти инструменты сами — модель зовёт gh/git/
# ssh как обычно. Встроенный guard рубит опасное (печать токена, git-RCE) и здесь.

[secrets.host]
description = "хостовые креды gh/git/ssh/scp"
sessions = ["*"]                          # все сессии; сузь при желании: ["dev-*"]
commands = ["gh", "git", "ssh", "scp"]    # эти инструменты завернутся обёртками
confirm = false                           # без кнопок подтверждения; guard — щит
"""


class SecretStore:
    """Ленивое чтение secrets.toml с кэшем по (mtime, mode, size).

    mode входит в ключ кэша не случайно: chmod не меняет mtime, а ослабление
    прав должно немедленно отключать выдачу секретов.
    """

    def __init__(self, path: Path):
        self._path = path
        self._cache_key: tuple | None = None
        self._secrets: dict[str, Secret] = {}

    def load(self) -> dict[str, Secret]:
        try:
            st = self._path.stat()
        except OSError:
            # Файла нет — кошелёк работает, но секретов нет (warning на старте).
            self._cache_key, self._secrets = None, {}
            return {}
        key = (st.st_mtime_ns, st.st_mode, st.st_size)
        if key == self._cache_key:
            return self._secrets
        self._cache_key, self._secrets = key, {}
        # Права шире 0600 — любой локальный пользователь/группа прочитал бы
        # значения; отказываемся грузить целиком, а не «только пошире-часть».
        if st.st_mode & 0o077:
            logger.error(
                "wallet: %s доступен group/other (права %o) — секреты НЕ загружены; "
                "выполни chmod 600", self._path, st.st_mode & 0o777,
            )
            return {}
        try:
            data = tomllib.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
            logger.error("wallet: не удалось прочитать %s: %s", self._path, e)
            return {}
        for name, raw in (data.get("secrets") or {}).items():
            if not isinstance(raw, dict):
                logger.error("wallet: запись %r — не таблица, пропущена", name)
                continue
            has_value, has_env = "value" in raw, "env" in raw
            is_shared = bool(raw.get("shared", False))
            connector = str(raw.get("connector", ""))
            raw_scope = raw.get("scope", {})
            scope = dict(raw_scope) if isinstance(raw_scope, dict) else {}
            if raw.get("scope") is not None and not isinstance(raw_scope, dict):
                logger.error(
                    "wallet: секрет %r — scope должен быть таблицей "
                    "([secrets.%s.scope]), а не %s; пропущен",
                    name, name, type(raw_scope).__name__)
                continue
            # Прокси-секрет (§4.5): connector задан → кред подставляет MITM-прокси,
            # значение в env песочницы НЕ входит. Требует value (сам кред); env не
            # используется. Неизвестный connector → секрет НЕ активен (не грузим),
            # реестр громко логирует («выключено = не существует»).
            if connector:
                if not has_value:
                    logger.error(
                        "wallet: connector-секрет %r без value — пропущен", name)
                    continue
                if get_connector(connector) is None:
                    # get_connector уже залогировал WARNING с известными именами.
                    continue
            # shared-секрет требует value (env опционален — для env-выдачи);
            # inject — ОБА поля; host-passthrough — НИ ОДНОГО. Иначе ошибка.
            elif is_shared:
                if not has_value:
                    logger.error("wallet: shared-секрет %r без value — пропущен", name)
                    continue
            elif has_value != has_env:
                logger.error(
                    "wallet: секрет %r — value и env задаются только вместе "
                    "(inject) либо оба отсутствуют (host-passthrough)", name)
                continue
            self._secrets[name] = Secret(
                name=str(name),
                value=str(raw.get("value", "")),
                env=str(raw.get("env", "")),
                description=str(raw.get("description", "")),
                sessions=tuple(str(p) for p in raw.get("sessions", ())),
                commands=tuple(str(p) for p in raw.get("commands", ())),
                deny=tuple(str(p) for p in raw.get("deny", ())),
                allow_unsafe=bool(raw.get("allow_unsafe", False)),
                confirm=bool(raw.get("confirm", True)),
                shared=is_shared,
                connector=connector,
                scope=scope,
            )
        return self._secrets
