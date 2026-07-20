"""Регресс: гейт доступа Telegram-адаптера (_accept / _user_allowed).

Единственный барьер бота: доступ строго по ALLOWED_USER_IDS + привязка к одной
группе. Пустой список = игнорировать всех. Проверяем deny-all при пустом списке
(дефолт), отказ приватному чату, отказ чужому чату после привязки, allow для
разрешённого юзера в привязанной группе. Без сети и Telegram.

Запуск: .venv/bin/python tests/access_gate_test.py
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")

from orchestrator.adapters.telegram.adapter import TelegramAdapter  # noqa: E402


def _adapter(allowed, chat_id=None):
    a = TelegramAdapter.__new__(TelegramAdapter)
    a.config = SimpleNamespace(allowed_user_ids=set(allowed))
    a.chat_id = chat_id
    return a


def _msg(uid, chat_id=-100, chat_type="supergroup"):
    from_user = None if uid is None else SimpleNamespace(id=uid)
    return SimpleNamespace(
        from_user=from_user,
        chat=SimpleNamespace(id=chat_id, type=chat_type),
    )


def test_user_allowed():
    a = _adapter({1, 2})
    assert a._user_allowed(SimpleNamespace(id=1)) is True
    assert a._user_allowed(SimpleNamespace(id=999)) is False
    assert a._user_allowed(None) is False
    print("OK _user_allowed: свой да, чужой/None нет")


def test_empty_allowlist_denies_all():
    # Дефолт при не заданном ALLOWED_USER_IDS — пустой список = deny-all.
    a = _adapter(set())
    assert a._user_allowed(SimpleNamespace(id=1)) is False
    assert a._accept(_msg(1)) is False
    print("OK пустой ALLOWED_USER_IDS → отклоняем всех")


def test_private_chat_denied():
    a = _adapter({1}, chat_id=None)
    assert a._accept(_msg(1, chat_type="private")) is False
    # приватный чат не должен привязаться
    assert a.chat_id is None
    print("OK приватный чат → отказ, привязки нет")


def test_binds_first_group_then_rejects_other():
    a = _adapter({1}, chat_id=None)
    # Первое сообщение из группы разрешённого юзера — привязывает chat_id.
    assert a._accept(_msg(1, chat_id=-100)) is True
    assert a.chat_id == -100
    # Второе из ДРУГОГО чата (тот же юзер) — отказ, привязка не меняется.
    assert a._accept(_msg(1, chat_id=-200)) is False
    assert a.chat_id == -100
    print("OK привязка к первой группе, чужой чат после — отказ")


def test_stranger_in_bound_group_denied():
    a = _adapter({1}, chat_id=-100)
    assert a._accept(_msg(999, chat_id=-100)) is False
    print("OK чужой юзер в привязанной группе → отказ")


def main():
    test_user_allowed()
    test_empty_allowlist_denies_all()
    test_private_chat_denied()
    test_binds_first_group_then_rejects_other()
    test_stranger_in_bound_group_denied()
    print("ALL ACCESS-GATE OK")


if __name__ == "__main__":
    main()
