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
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

import tomlkit

# под-команда правки списка → ключ в TOML
_LIST_FIELDS = {"session": "sessions", "cmd": "commands", "deny": "deny"}

USAGE = (
    "🔐 <b>/wallet</b> — policy кошелька (значения токенов не видны и не вводятся):\n"
    "• <code>/wallet</code> — показать секреты и policy\n"
    "• <code>/wallet confirm &lt;имя&gt; on|off</code> — спрашивать кнопкой\n"
    "• <code>/wallet session &lt;имя&gt; +&lt;шаблон&gt;|-&lt;шаблон&gt;</code>"
    " — кому доступен (fnmatch: * или dev-*)\n"
    "• <code>/wallet cmd &lt;имя&gt; +&lt;шаблон&gt;|-&lt;шаблон&gt;</code>"
    " — команды (gh, git, «gh api *»)\n"
    "• <code>/wallet deny &lt;имя&gt; +&lt;шаблон&gt;|-&lt;шаблон&gt;</code>"
    " — запрет поверх commands\n"
    "• <code>/wallet new &lt;имя&gt;</code> — создать host-passthrough (deny-by-default)\n"
    "• <code>/wallet rm &lt;имя&gt;</code> — удалить секрет\n"
    "Значения (inject value/env) и allow_unsafe — только host-файлом."
)


class PolicyError(Exception):
    """Ошибка правки policy — текст показывается пользователю."""


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

    def render(self) -> str:
        """Список секретов и policy — БЕЗ значений токенов."""
        secrets = self._secrets(self._load_doc())
        if not secrets:
            return "🔐 Кошелёк: секретов нет. Создать: <code>/wallet new &lt;имя&gt;</code>"
        out = ["🔐 <b>Секреты кошелька</b> (значения скрыты):"]
        for name, s in secrets.items():
            inject = bool(s.get("value")) and bool(s.get("env"))
            mode = "inject" if inject else "host-passthrough"
            sess = ", ".join(s.get("sessions", ())) or "— (никому)"
            cmds = ", ".join(s.get("commands", ())) or "— (дефолт host: gh/git/ssh/scp)"
            conf = "on" if s.get("confirm", True) else "off"
            block = (
                f"\n<b>{name}</b> [{mode}] · confirm={conf}"
                f"\n  sessions: {sess}"
                f"\n  commands: {cmds}"
            )
            deny = s.get("deny", ())
            if deny:
                block += f"\n  deny: {', '.join(deny)}"
            if inject:
                block += f"\n  env: ${s.get('env')} (значение — в host-файле)"
            desc = s.get("description")
            if desc:
                block += f"\n  — {desc}"
            out.append(block)
        return "\n".join(out)

    # ── правка ──────────────────────────────────────────────────

    def apply(self, args: list[str]) -> str:
        """Диспетчер под-команд. args — токены после «/wallet»."""
        if args and args[0] in ("help", "?"):
            return USAGE
        if not args or args[0] in ("list", "ls"):
            return self.render()
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
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)  # атомарно; демон перечитает по mtime
