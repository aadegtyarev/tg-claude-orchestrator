"""Регрессия форматирования вызовов инструментов в статус-бабле (_tool_line).

Главное — компактность в одну строку:
  * Bash показывает «голову» команды (имя + подкоманда), а не весь cd/&&/grep;
  * TaskCreate — тему, а не весь JSON-объект;
  * незнакомые тулы — первый осмысленный аргумент, а не сырой JSON;
  * Read/Write/Edit — basename пути.

Запуск: .venv/bin/python tests/tool_line_test.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.adapters.telegram.adapter import TelegramAdapter  # noqa: E402
from orchestrator.core.toolline import (  # noqa: E402
    bash_head as _bash_head,
    file_suffix as _file_suffix,
    first_meaningful as _first_meaningful,
    tool_line,
)


class StubBot:
    """Лёгкий носитель для tool_line без поднятия Telegram/сессий."""

    # бинтим реальный метод адаптера — логику склейки цитат проверяем как есть.
    _with_quote = TelegramAdapter._with_quote

    def t(self, key, **kw):
        return f"🤖 {kw.get('agent', 'agent')}"

    def _tool_line(self, tool, tool_input):
        return tool_line(tool, tool_input, self.t)


def main():
    b = StubBot()

    # ── _bash_head: показываем basename cd + голову команды в одну строку ──
    assert _bash_head('cd /a/b && grep -n "x" file.py') == "b · grep"
    assert _bash_head('cd /a/b/c && grep foo') == "c · grep foo"   # foo — словесный аргумент
    assert _bash_head('find /long/path -name "*.md" 2>/dev/null && echo ---- && ls') == "find"
    assert _bash_head('git log --oneline -5') == "git log"
    assert _bash_head('npm test') == "npm test"
    assert _bash_head('ls -la') == "ls"
    assert _bash_head('cd /some/where') == "where"       # вся команда — cd → basename
    assert _bash_head('cd worktrees/b5 && grep -rn "x" .') == "b5 · grep"
    print("OK _bash_head shows cd dir + command head")

    # ── _file_suffix: контекст строк для Read/Edit ──
    assert _file_suffix("Read", {"offset": 10, "limit": 24}) == " L10–34"
    assert _file_suffix("Read", {"offset": 100}) == " L100+"
    assert _file_suffix("Read", {}) == ""
    assert _file_suffix("Edit", {"old_string": "a\nb\nc"}) == " ±3 стр"
    assert _file_suffix("Edit", {"old_string": "x", "replace_all": True}) == " (все)"
    assert _file_suffix("Write", {"file_path": "/x"}) == ""
    print("OK _file_suffix: line context for Read/Edit")

    # ── _first_meaningful: первый строковый аргумент, не JSON ──
    assert _first_meaningful({"foo": "bar", "description": "d"}) == "d"
    assert _first_meaningful({"path": "/x/y.py", "x": 1}) == "/x/y.py"
    assert _first_meaningful({"a": 1, "b": 2}) == ""       # нет строковых полей
    assert _first_meaningful({"query": "  ", "text": "ok"}) == "ok"
    print("OK _first_meaningful picks meaningful field")

    # ── _tool_line: конец-в-конец, ключевые кейсы из скриншотов ──
    long_cmd = ('cd /home/adegtyarev/Develop/Hobby/noos/.ai-dev/worktrees/b5 && '
                'grep -n "_secret_gate\\|_scrub_str_list\\|render_placeholders"')
    line = b._tool_line("Bash", {"command": long_cmd})
    assert line == "⚡ <b>Bash</b> <code>b5 · grep</code>", line
    assert "&&" not in line   # в одну строку, без хвостов
    print("OK Bash → cd dir + head, no full command")

    tc = b._tool_line("TaskCreate", {
        "subject": "Fix B-5 blocker 1: distill_study_skill scrub",
        "description": "core/execute_control.py distill_study_skill calls store.insert_skill ...",
    })
    assert tc == ("🔧 <b>TaskCreate</b> "
                  "<code>Fix B-5 blocker 1: distill_study_skill scrub</code>"), tc
    assert "description" not in tc and "{" not in tc  # никакого JSON
    print("OK TaskCreate → subject only, no JSON")

    assert b._tool_line("Read", {"file_path": "/a/b/secret_scrub.py"}) == \
        "📖 <b>Read</b> <code>secret_scrub.py</code>"
    assert b._tool_line("Read", {"file_path": "/a/b/x.py", "offset": 10, "limit": 24}) == \
        "📖 <b>Read</b> <code>x.py L10–34</code>"
    assert b._tool_line("Write", {"file_path": "/a/b/c.py"}) == \
        "✍️ <b>Write</b> <code>c.py</code>"
    print("OK Read/Write → basename + line range")

    # незнакомый сторонний тул — осмысленный аргумент, не JSON
    ext = b._tool_line("mcp__repo__search", {"query": "how to foo", "limit": 5})
    assert ext == "🔧 <b>mcp__repo__search</b> <code>how to foo</code>", ext
    empty = b._tool_line("mcp__weird__ping", {"limit": 5})
    assert empty == "🔧 <b>mcp__weird__ping</b>", empty   # нет строкового поля → без детали
    print("OK unknown tools → meaningful arg, not JSON")

    # HTML-экранирование: тема с разметкой не ломает бабл
    safe = b._tool_line("TaskCreate", {"subject": "<b>x</b> & y"})
    assert "<b>x</b>" not in safe and "&lt;b&gt;x&lt;/b&gt;" in safe, safe
    print("OK detail HTML-escaped")

    # ── спавн сабагента: "Agent" — актуальное имя тула в Claude Code ≥2.1.
    # Раньше код искал только "Task" и падал в generic-фолбэк (description
    # без имени агента) — REVIEW.md, найдено разбором живого лога noos.
    agent_line = b._tool_line("Agent", {
        "subagent_type": "dev-reviewer", "description": "Fresh review slice 3",
    })
    assert agent_line == "<b>🤖 dev-reviewer</b>: <i>Fresh review slice 3</i>", agent_line
    print("OK Agent (актуальное имя) → узнан, имя агента показано")

    # "Task" (более старые версии CC) — тот же рендер, для совместимости.
    task_line = b._tool_line("Task", {
        "subagent_type": "dev-reviewer", "description": "Fresh review slice 3",
    })
    assert task_line == agent_line, (task_line, agent_line)
    print("OK Task (старое имя) → тот же рендер, что и Agent")

    # ── _with_quote: цитата reply склеивается с текстом для модели ──
    Q = SimpleNamespace
    # выделенный фрагмент (message.quote) приоритетнее полного reply_to
    m = Q(text="а это точно?", quote=Q(text="строка 1\nстрока 2"),
          reply_to_message=Q(text="весь пост целиком"))
    out = b._with_quote(m)
    assert out == "> строка 1\n> строка 2\n\nа это точно?", repr(out)
    # нет quote → берём весь reply_to
    m2 = Q(text="почему?", quote=None, reply_to_message=Q(text="прошлый ответ"))
    assert b._with_quote(m2) == "> прошлый ответ\n\nпочему?"
    # обычное сообщение без reply — без изменений
    m3 = Q(text="просто вопрос", quote=None, reply_to_message=None)
    assert b._with_quote(m3) == "просто вопрос"
    print("OK _with_quote: reply-цитата долетает до модели")

    print("ALL TOOL_LINE OK")


def test_tool_line():
    main()

if __name__ == "__main__":
    main()
