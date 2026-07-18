"""Регрессия ретранслятора ошибок API: строгий баннер + классификация, без ложных алертов.

Раньше ретранслятор грепел claude.log по словам «rate-limit»/«api error» и ловил
собственный текст Клода — диагностику чужой сессии («9× ratelimit»), описание
самой фичи («ретрансляция ошибок API/rate-limit») — как ложный алерт о лимите.
Теперь триггер — только настоящий баннер TUI «API Error: <код> <детали>»,
класс определяет текст подсказки (rate-limit→/model, 400-протокол→/clear).

Запуск: .venv/bin/python tests/error_relay_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from bot import _API_ERR_BANNER_RE, _classify_api_error, _detect_log_signals  # noqa: E402


def _hit(chunk: str) -> tuple[str, str] | None:
    """Сымитировать шаг ретранслятора: (code, klass) либо None (нет баннера)."""
    m = _API_ERR_BANNER_RE.search(chunk.encode())
    if not m:
        return None
    return m.group(1).decode(), _classify_api_error(m.group(1), m.group(2))


def main():
    # ── Реальные баннеры из TUI классифицируются верно ──
    assert _hit("●API Error: 400 messages.7.content.2.server_tool_use.id: "
                "String should match pattern '^srvtoolu_[a-zA-Z0-9_]+$'") == ("400", "protocol")
    assert _hit("●API Error: 400 messages.5: `tool_result` blocks can only be "
                "in `user` messages") == ("400", "protocol")
    # thinking-400: чужой (неподписанный) thinking в истории — корень cross-provider
    # мусора; теперь тоже классифицируется как protocol (→подсказка /clear + эксцепт).
    assert _hit("●API Error: 400 messages.3.content.1.thinking: must be passed back "
                "to the API") == ("400", "protocol")
    assert _hit("●API Error: 429 {\"type\":\"rate_limit_error\"}") == ("429", "ratelimit")
    assert _hit("●API Error: 529 {\"type\":\"overloaded_error\"}") == ("529", "ratelimit")
    assert _hit("●API Error: 503 service unavailable") == ("503", "generic")
    print("OK classify: 400+tool/thinking→protocol, 429/529→ratelimit, 5xx→generic")

    # ── Главный регресс: проза модели НЕ триггерит (никакого «API Error: <код>») ──
    # Реальные строки из claude.log сессии tg-claude-orchestrator:
    assert _hit("Результат расследования: Журнал сессии noos содержит 13×«APIerror»,"
                " 9×«ratelimit», 7×«exitcode»") is None
    assert _hit("Ретрансляция ошибок API/rate-limit в чат с подсказкой /model") is None
    assert _hit("Скажи, бывает ли api error 429 у Anthropic?") is None
    assert _hit("rate-limit relay, overloaded, 429, internal server error — "
                "просто перечисление слов без баннера") is None
    print("OK no false positives on model prose (был ложный алерт о лимите)")

    # ── Дедуп по сигнатуре code:class: корень один — сигнатура одна ──
    sigs = set()
    for c in ["API Error: 400 messages.7.content.2.server_tool_use.id: x",
              "API Error: 400 messages.9.server_tool_use.id: y",
              "API Error: 429 rate_limit_error"]:
        code, klass = _hit(c)
        sigs.add(f"{code}:{klass}")
    assert sigs == {"400:protocol", "429:ratelimit"}, sigs
    print("OK signatures: корень 400-protocol схлопывается в одну сигнатуру")

    # ── _detect_log_signals: разбор трёх классов сигналов из куска лога ──
    none = _detect_log_signals("●Думаю над задачей… ✻Crunched for 12s".encode())
    assert none["api_error"] is None and none["retry"] is None and none["restarts"] == 0, none
    print("OK detect: чистый лог → никаких сигналов")

    # retry: «Retrying in 1s · attempt 47/100» (живой прогресс ретраев)
    r = _detect_log_signals("API Error: 400 … ✻Retrying in 1s · attempt 47/100".encode())
    assert r["retry"] == (47, 100), r["retry"]
    assert r["api_error"] is not None  # в том же куске виден и баннер ошибки
    print("OK detect: retry attempt K/M + сопутствующая API-ошибка")

    # restart-loop: баннер «Resume this session» mid-хода = краш-рестарт
    rs = _detect_log_signals("Resume this session with:\nclaude --resume x\n…\nResume this session with:\nclaude --resume y".encode())
    assert rs["restarts"] == 2, rs["restarts"]
    print("OK detect: рестарт-баннеры считаются")

    print("ALL ERROR-RELAY OK")


if __name__ == "__main__":
    main()
