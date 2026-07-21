"""Правка policy кошелька из бота: просмотр + surgical-edit secrets.toml.

Границы безопасности (важно):
  * значения токенов (value/env для inject) НЕ показываются и НЕ вводятся через
    бот — только host-файлом. Через бот правится ЛИШЬ policy: sessions,
    commands, deny, confirm, а также создание/удаление host-passthrough секрета;
  * `allow_unsafe` (отключение guard'а) через бот не трогаем — это security-
    downgrade, только host-файлом;
  * команда `/wallet` — пользовательская (ALLOWED_USER_IDS), модель её вызвать
    не может (приходит от юзера, не из Claude).

tomlkit сохраняет комментарии/форматирование при правке; запись атомарна
(tmp+os.replace) с валидацией (результат перечитываем tomllib перед заменой)
и правами 0600.

Без зависимостей оркестратора: только stdlib + tomlkit/tomllib.
"""
from __future__ import annotations

import html
import os
from pathlib import Path

try:
    import tomllib  # stdlib с 3.11
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import tomlkit

# под-команда правки списка → ключ в TOML
_LIST_FIELDS = {"session": "sessions", "cmd": "commands", "deny": "deny"}

# Список под-команд правки — моноширинным блоком (ровно, без «плавающих»
# переносов построчных <code>). Внутри <pre> экранируем только &<> (quote=False).
_EDIT_CMDS = (
    "/wallet confirm <имя> on|off\n"
    "/wallet session <имя> +<шаблон> | -<шаблон>\n"
    "/wallet cmd     <имя> +<шаблон> | -<шаблон>\n"
    "/wallet deny    <имя> +<шаблон> | -<шаблон>\n"
    "/wallet new <имя>\n"
    "/wallet rm  <имя>\n"
    "/wallet help"
)

USAGE = (
    "🔐 <b>/wallet</b> — policy кошелька (значения токенов не видны и не вводятся):\n"
    "<pre>" + html.escape(_EDIT_CMDS, quote=False) + "</pre>\n"
    "session/cmd/deny — fnmatch-шаблоны (напр. <code>*</code>, <code>dev-*</code>, "
    "<code>gh api *</code>). Значения (inject value/env) и allow_unsafe — только host-файлом."
)


class PolicyError(Exception):
    """Ошибка правки policy — текст показывается пользователю."""


def _toml_list(seq) -> str:
    """Список → TOML-массив-строка ["a", "b"] (для вывода «как из файла»)."""
    return "[" + ", ".join(f'"{x}"' for x in seq) + "]"


def _edit_footer(allow_edit: bool) -> str:
    """Подсказка по правке прямо в ответе /wallet (или заметка, что выключено)."""
    if not allow_edit:
        return (
            "✏️ <i>Правка из чата выключена (WALLET_POLICY_EDIT=0) — "
            "меняй host-файл secrets.toml.</i>"
        )
    return (
        "✏️ <i>Правка (значения токенов — только host-файлом):</i>\n"
        "<pre>" + html.escape(_EDIT_CMDS, quote=False) + "</pre>"
    )


class PolicyEditor:
    """Читает/правит secrets.toml. Один экземпляр на модуль; без своего кэша —
    каждый вызов читает файл заново (источник правды — файл, его же читает
    демон по mtime)."""

    def __init__(self, path: Path):
        self.path = path

    # ── чтение ──────────────────────────────────────────────────

    def _load_doc(self) -> tomlkit.TOMLDocument:
        try:
            return tomlkit.parse(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return tomlkit.document()
        except Exception as e:  # noqa: BLE001 — любой сбой чтения → понятный текст
            raise PolicyError(f"не читается {self.path}: {e}") from e

    @staticmethod
    def _secrets(doc: tomlkit.TOMLDocument):
        sec = doc.get("secrets")
        return sec if sec is not None else {}

    def render(self, allow_edit: bool = True) -> str:
        """Список секретов и policy — БЕЗ значений токенов. Тело — TOML-подобным
        текстом в <pre>-блоке (видно, что это из файла) + подсказка по правке."""
        secrets = self._secrets(self._load_doc())
        if not secrets:
            body = "# секретов нет"
        else:
            blocks = []
            for name, s in secrets.items():
                inject = bool(s.get("value")) and bool(s.get("env"))
                mode = f"inject (env ${s.get('env')})" if inject else "host-passthrough"
                conf = "on" if s.get("confirm", True) else "off"
                lines = [f"[secrets.{name}]   # {mode} · confirm = {conf}"]
                lines.append(f"sessions = {_toml_list(s.get('sessions', ()))}")
                cmds = s.get("commands", ())
                lines.append(
                    f"commands = {_toml_list(cmds)}"
                    + ("" if cmds else "   # пусто = дефолт host: gh/git/ssh/scp")
                )
                if s.get("deny"):
                    lines.append(f"deny = {_toml_list(s.get('deny'))}")
                if s.get("description"):
                    lines.append(f"# {s.get('description')}")
                blocks.append("\n".join(lines))
            body = "\n\n".join(blocks)
        return (
            "🔐 <b>Секреты кошелька</b> (значения токенов скрыты):\n"
            # quote=False: внутри <pre> экранируем только &<> — кавычки Telegram
            # покажет как есть (hex-entity &#x27; он бы не декодировал).
            "<pre>" + html.escape(body, quote=False) + "</pre>\n"
            + _edit_footer(allow_edit)
        )

    # ── правка ──────────────────────────────────────────────────

    def apply(self, args: list[str], allow_edit: bool = True) -> str:
        """Диспетчер под-команд. args — токены после «/wallet». allow_edit=False
        (WALLET_POLICY_EDIT=0): просмотр работает, операции правки отклоняются."""
        if args and args[0] in ("help", "?"):
            return USAGE
        if not args or args[0] in ("list", "ls"):
            return self.render(allow_edit)
        if not allow_edit:
            raise PolicyError(
                "правка policy из чата выключена (WALLET_POLICY_EDIT=0) — "
                "меняй host-файл secrets.toml"
            )
        sub, rest = args[0], args[1:]
        if sub == "confirm":
            return self._set_confirm(rest)
        if sub in _LIST_FIELDS:
            return self._edit_list(sub, rest)
        if sub == "new":
            return self._new(rest)
        if sub == "rm":
            return self._rm(rest)
        raise PolicyError(f"неизвестная под-команда «{sub}». <code>/wallet help</code>")

    def _get_secret(self, doc, name: str):
        sec = doc.get("secrets")
        if sec is None or name not in sec:
            raise PolicyError(f"нет секрета «{name}». <code>/wallet</code> — список")
        return sec[name]

    def _set_confirm(self, rest: list[str]) -> str:
        if len(rest) != 2 or rest[1] not in ("on", "off"):
            raise PolicyError("использование: <code>/wallet confirm &lt;имя&gt; on|off</code>")
        name, val = rest[0], rest[1] == "on"
        doc = self._load_doc()
        self._get_secret(doc, name)["confirm"] = val
        self._write(doc)
        return f"✅ {name}: confirm = {'on' if val else 'off'}"

    def _edit_list(self, sub: str, rest: list[str]) -> str:
        key = _LIST_FIELDS[sub]
        if len(rest) != 2 or rest[1][0] not in "+-":
            raise PolicyError(
                f"использование: <code>/wallet {sub} &lt;имя&gt; +&lt;шаблон&gt; | -&lt;шаблон&gt;</code>"
            )
        name, op, pat = rest[0], rest[1][0], rest[1][1:]
        if not pat:
            raise PolicyError("пустой шаблон")
        doc = self._load_doc()
        secret = self._get_secret(doc, name)
        cur = list(secret.get(key, []))
        if op == "+":
            if pat in cur:
                raise PolicyError(f"«{pat}» уже в {key} секрета {name}")
            cur.append(pat)
            verb = "добавлен"
        else:
            if pat not in cur:
                raise PolicyError(f"«{pat}» нет в {key} секрета {name}")
            cur.remove(pat)
            verb = "убран"
        secret[key] = cur
        self._write(doc)
        shown = ", ".join(cur) or "— (пусто)"
        return f"✅ {name}: {key} {verb} «{pat}» → [{shown}]"

    def _new(self, rest: list[str]) -> str:
        if len(rest) != 1:
            raise PolicyError("использование: <code>/wallet new &lt;имя&gt;</code>")
        name = rest[0]
        if not name.replace("-", "").replace("_", "").isalnum():
            raise PolicyError("имя секрета: буквы/цифры/дефис/подчёркивание")
        doc = self._load_doc()
        if "secrets" not in doc:
            doc["secrets"] = tomlkit.table(is_super_table=True)
        if name in doc["secrets"]:
            raise PolicyError(f"секрет «{name}» уже есть")
        t = tomlkit.table()
        t["description"] = "host-passthrough (создан через /wallet)"
        t["sessions"] = []            # deny-by-default: пока никому
        t["commands"] = []            # пусто = дефолт host-команд (gh/git/ssh/scp)
        t["confirm"] = True           # безопасный дефолт: спрашивать
        doc["secrets"][name] = t
        self._write(doc)
        return (
            f"✅ создан host-passthrough секрет «{name}» (deny-by-default).\n"
            f"Дальше: <code>/wallet session {name} +*</code> и "
            f"<code>/wallet cmd {name} +gh</code>."
        )

    def _rm(self, rest: list[str]) -> str:
        if len(rest) != 1:
            raise PolicyError("использование: <code>/wallet rm &lt;имя&gt;</code>")
        name = rest[0]
        doc = self._load_doc()
        self._get_secret(doc, name)  # проверка существования
        del doc["secrets"][name]
        self._write(doc)
        return f"✅ секрет «{name}» удалён"

    # ── запись ──────────────────────────────────────────────────

    def _write(self, doc: tomlkit.TOMLDocument) -> None:
        text = tomlkit.dumps(doc)
        # Валидация: результат обязан парситься stdlib-парсером (тем же, что у
        # демона) — иначе не заменяем оригинал, чтобы правка не сломала policy.
        try:
            tomllib.loads(text)
        except Exception as e:  # noqa: BLE001
            raise PolicyError(f"внутренняя ошибка: результат не валиден ({e})") from e
        tmp = self.path.with_name(self.path.name + ".tmp")
        # O_CREAT с 0600 (+O_TRUNC на случай остатка от прерванной записи) — файл
        # с реальными значениями секретов ни мгновения не живёт с широкими
        # правами (write_text открыл бы его под umask, обычно 0644). Ср. с
        # module.py:_provision/_write_default_secrets.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, self.path)  # атомарно; демон перечитает по mtime
