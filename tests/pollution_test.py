"""Регрессия детектора cross-provider мусора в транскрипте.

Реальный Anthropic ВСЕГДА подписывает thinking; z.ai/GLM — нет. Поэтому
неподписанный thinking в истории = она перешла через чужой бэкенд, и апстрим
падает с 400 (…thinking… must be passed back). Та же история — server_tool_use
с id не формата srvtoolu_… и tool_result, приехавший внутри assistant-сообщения.
Эти маркеры _scan_pollution находит в хвосте транскрипта, чтобы релей мог
приложить эксцепт мусора к сообщению о /clear.

Запуск: .venv/bin/python tests/pollution_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sessions import _block_snippet, _scan_pollution  # noqa: E402


def _asst(content: list) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": content}}


def _user(content) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}}


def main():
    # ── thinking без подписи → детектится (главный кейс: z.ai отравил историю) ──
    r = _scan_pollution([_asst([{"type": "thinking", "thinking": "размышляю"}])])
    assert r is not None and "thinking без подписи" in r, r
    print("OK thinking без подписи → детектится")

    # thinking с подписью → чисто (так выглядит настоящий Anthropic)
    assert _scan_pollution(
        [_asst([{"type": "thinking", "thinking": "x", "signature": "sig"}])]
    ) is None
    print("OK thinking с подписью → чисто")

    # ── server_tool_use с чужим id → детектится; srvtoolu_… → чисто ──
    r = _scan_pollution(
        [_asst([{"type": "server_tool_use", "id": "toolu_abc", "name": "web_search",
                 "input": {"q": "x"}}])]
    )
    assert r is not None and "server_tool_use" in r and "web_search" in r, r
    assert _scan_pollution(
        [_asst([{"type": "server_tool_use", "id": "srvtoolu_abc", "name": "web_search",
                 "input": {}}])]
    ) is None
    print("OK server_tool_use: чужой id → детектится, srvtoolu_ → чисто")

    # ── tool_result в assistant → детектится; в user (норма) → чисто ──
    assert _scan_pollution(
        [_asst([{"type": "tool_result", "tool_use_id": "x", "content": "y"}])]
    ) is not None
    assert _scan_pollution(
        [_user([{"type": "tool_result", "tool_use_id": "x", "content": "y"}])]
    ) is None
    print("OK tool_result: в assistant → детектится, в user → чисто")

    # ── newest-first: возвращается САМЫЙ СВЕЖИЙ загрязнённый блок ──
    seq = [
        _asst([{"type": "server_tool_use", "id": "bad_old", "name": "old", "input": {}}]),
        _asst([{"type": "thinking", "thinking": "fresh"}]),
    ]
    r = _scan_pollution(seq)
    assert r is not None and "fresh" in r and "old" not in r, r
    print("OK newest-first: свежий thinking приоритетнее старого tool_use")

    # ── чистый транскрипт → None ──
    assert _scan_pollution([]) is None
    assert _scan_pollution([_user("просто текст")]) is None
    assert _scan_pollution(
        [_asst([{"type": "text", "text": "hello"}]),
         _user([{"type": "text", "text": "hi"}])]
    ) is None
    print("OK чистый транскрипт → None")

    # ── _block_snippet: схлопывает пробелы и режет по лимиту ──
    s = _block_snippet({"type": "text", "text": "a  b\n\n  c"}, limit=10)
    assert s == "a b c", repr(s)
    long = _block_snippet({"type": "text", "text": "x" * 50}, limit=10)
    assert long == "xxxxxxxxxx…", repr(long)
    print("OK _block_snippet: схлопывает и режет")

    print("ALL POLLUTION OK")


if __name__ == "__main__":
    main()
