"""Юнит-контракт core/hookscript.render — рендер хук-диспетчера Claude Code.

Важно для agent-vm: адрес оркестратора в хук-скрипте должен быть параметризован
(host-gateway гостя), а не хардкод 127.0.0.1 — иначе PreToolUse/Stop-хуки из
гостя VM не достучатся до хоста. Под bwrap/off host=127.0.0.1 → как раньше.

Запуск: .venv/bin/python tests/hookscript_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core import hookscript  # noqa: E402


def test_local_host_substitution():
    """host=127.0.0.1 (bwrap/off) → URL как раньше, все плейсхолдеры подставлены."""
    out = hookscript.render("127.0.0.1", 18080, "noos", "tok-abc")
    assert '_ORCH = "http://127.0.0.1:18080"' in out
    assert '_NAME = "noos"' in out
    assert '_TOKEN = "tok-abc"' in out
    assert "__HOST__" not in out and "__PORT__" not in out
    assert "__NAME__" not in out and "__TOKEN__" not in out
    print("OK render: host=127.0.0.1 — прежний URL, плейсхолдеры подставлены")


def test_gateway_host_for_agentvm():
    """host=host-gateway IP (agent-vm) → адрес оркестратора указывает на хост."""
    out = hookscript.render("10.0.2.2", 18080, "noos", "t")
    assert '_ORCH = "http://10.0.2.2:18080"' in out
    assert "127.0.0.1" not in out  # хардкода больше нет
    for ph in ("__HOST__", "__PORT__", "__NAME__", "__TOKEN__"):
        assert ph not in out, ph  # ни одного неподставленного плейсхолдера
    print("OK render: host-gateway IP — хуки бьют на хост, не на loopback гостя")


def test_special_chars_literal_replace():
    """Токен/имя подставляются literal-replace (без .format) — безопасно для
    любых символов (напр. '{' в токене не сломает рендер)."""
    out = hookscript.render("127.0.0.1", 1, "s", "a{b}c-$%")
    assert '_TOKEN = "a{b}c-$%"' in out
    print("OK render: literal-replace терпит спецсимволы в токене")


def main():
    test_local_host_substitution()
    test_gateway_host_for_agentvm()
    test_special_chars_literal_replace()
    print("ALL HOOKSCRIPT OK")


if __name__ == "__main__":
    main()
