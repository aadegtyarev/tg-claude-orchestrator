"""Прозрачность фоновых процессов сессии (из Stop-payload харнесса).

- _update_background: снимок background_tasks/session_crons на сессию + уведомление
  ТОЛЬКО о новых задачах (дедуп по id, без спама на каждый ход);
- bg_text: рендер /bg (пусто / задачи+кроны, HTML-экранирование команды).

Запуск: .venv/bin/python tests/bg_tasks_test.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.app import OrchestratorCore  # noqa: E402
from orchestrator.core.sessions import Session  # noqa: E402
from orchestrator.core.texts import get_texts  # noqa: E402


def _session() -> Session:
    return Session(name="s", port=0, session_dir=Path("/tmp/s"),
                   claude_session_id="x", title="s")


def _core(notices: list) -> OrchestratorCore:
    c = OrchestratorCore.__new__(OrchestratorCore)
    texts = get_texts("ru")
    c.t = lambda k, **kw: texts.get(k, k).format(**kw) if kw else texts.get(k, k)

    async def notice(session, text):
        notices.append(text)

    c.notice = notice
    return c


async def run():
    # bg_text: пусто
    c = _core([])
    s = _session()
    assert "нет" in c.bg_text(s).lower()
    print("OK bg_text: пустая сессия → «нет задач/кронов»")

    # _update_background: новая задача → уведомление, снимок сохранён
    notices: list = []
    c = _core(notices)
    s = _session()
    payload = {"background_tasks": [
        {"id": "b1", "type": "shell", "status": "running",
         "description": "Sleep 600", "command": "sleep 600"}
    ], "session_crons": []}
    await c._update_background(s, payload)
    assert len(notices) == 1 and "sleep 600" in notices[0]
    assert s.background_tasks and s.background_tasks[0]["id"] == "b1"
    print("OK _update_background: новая задача → уведомление + снимок")

    # тот же id на следующем Stop → НЕ уведомляем повторно (дедуп)
    await c._update_background(s, payload)
    assert len(notices) == 1, notices
    print("OK дедуп: та же задача на следующем ходу → без повторного спама")

    # новая задача с новым id → уведомляем
    payload2 = {"background_tasks": payload["background_tasks"] + [
        {"id": "b2", "type": "shell", "status": "running", "command": "npm run watch"}
    ], "session_crons": []}
    await c._update_background(s, payload2)
    assert len(notices) == 2 and "npm run watch" in notices[1]
    print("OK новая задача (новый id) → новое уведомление")

    # bg_text с задачами: заголовок, id, команда, экранирование
    s2 = _session()
    s2.background_tasks = [
        {"id": "b3", "type": "shell", "status": "running",
         "description": "echo <b> & test", "command": "echo '<b> & x'"}
    ]
    txt = c.bg_text(s2)
    assert "Задачи (1)" in txt and "b3" in txt
    assert "&lt;b&gt;" in txt and "<b>" not in txt  # HTML экранирован
    print("OK bg_text: рендер задач + HTML-экранирование команды")

    # задачи пусто, но есть крон
    s3 = _session()
    s3.session_crons = [{"schedule": "*/30 * * * *", "description": "poll"}]
    txt = c.bg_text(s3)
    assert "Задач нет" in txt and "Кроны (1)" in txt and "poll" in txt
    print("OK bg_text: только кроны")


def test_tool_line_bg_marker():
    from orchestrator.core.toolline import tool_line
    texts = get_texts("ru")
    t = lambda k, **kw: texts.get(k, k).format(**kw) if kw else texts.get(k, k)  # noqa: E731
    # обычный Bash — без метки
    normal = tool_line("Bash", {"command": "ls -la"}, t)
    assert "в фон" not in normal
    # Bash run_in_background — с меткой «в фон»
    bg = tool_line("Bash", {"command": "npm run watch", "run_in_background": True}, t)
    assert "в фон" in bg and "🔧" in bg
    print("OK tool_line: run_in_background → метка «в фон» в бабле")


def test_bubble_bg_label():
    from orchestrator.core.bubble import Bubble, BubbleLine, BubbleManager
    texts = get_texts("ru")
    m = BubbleManager.__new__(BubbleManager)
    m._t = lambda k, **kw: texts.get(k, k).format(**kw) if kw else texts.get(k, k)
    b = Bubble()
    b.entries.append(BubbleLine(html="⚡ <b>Bash</b>"))
    # без сессии/фона — метки нет
    assert "/bg" not in m._render_text(b, None)
    # сессия с фоновой задачей → метка-футер «N в фоне · /bg»
    s = _session()
    s.background_tasks = [{"id": "b1", "type": "shell", "status": "running"}]
    out = m._render_text(b, s)
    assert "1 в фоне" in out and "/bg" in out and "🔧" in out
    print("OK бабл: метка «N в фоне · /bg» в конце по снимку сессии")


def main():
    asyncio.run(run())
    test_tool_line_bg_marker()
    test_bubble_bg_label()
    print("ALL BG-TASKS OK")


if __name__ == "__main__":
    main()
