"""Мягкая деградация при обновлении Claude Code.

Если формат Claude Code поменялся и что-то не парсится — не падать молча, а
деградировать с понятным сигналом:
  * read_stats на «чужом» транскрипте (валидный JSONL, но неизвестная схема) →
    stale_schema=True, а не тихие нули;
  * read_stats не падает на битых строках/несуществующем файле;
  * Telegram: кнопка ⏭ показывается только когда есть что разблокировать.

(Разбор /cost — parse_cost — переехал в tests/reports_test.py.)

Запуск: .venv/bin/python tests/degradation_test.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from orchestrator.core.transcript import read_stats  # noqa: E402


def test_read_stats_stale_schema():
    tmp = Path(tempfile.mkdtemp())
    # Валидный JSONL, но НИ одной ожидаемой записи (схема поменялась):
    # много строк, заметный размер, но нет type=user/assistant с usage.
    p = tmp / "t.jsonl"
    p.write_text("\n".join(
        '{"kind": "somethingNew", "payload": {"x": ' + str(i) + ', "pad": "' + "y" * 200 + '"}}'
        for i in range(40)
    ))
    st = read_stats(p)
    assert st is not None and st["stale_schema"] is True, st
    assert st["turns"] == 0 and st["output_tokens"] == 0
    print("OK read_stats: непонятная схема → stale_schema=True (не тихие нули)")

    # Нормальный транскрипт — stale_schema=False.
    p2 = tmp / "ok.jsonl"
    p2.write_text(
        '{"type": "user", "message": {"content": "привет"}}\n'
        '{"type": "assistant", "message": {"model": "sonnet", '
        '"usage": {"input_tokens": 100, "output_tokens": 20}}}\n'
    )
    st2 = read_stats(p2)
    assert st2["stale_schema"] is False and st2["turns"] == 1 and st2["model"] == "sonnet"
    print("OK read_stats: нормальный транскрипт → stale_schema=False, поля извлечены")

    # Битые строки и мелкий файл — не падаем, не ложный stale.
    p3 = tmp / "broken.jsonl"
    p3.write_text("{битый json\nне json вообще\n")
    st3 = read_stats(p3)
    assert st3 is not None and st3["stale_schema"] is False  # мелкий → не stale
    print("OK read_stats: битые строки не роняют, мелкий файл не ложно-stale")

    # Несуществующий файл → None.
    assert read_stats(tmp / "nope.jsonl") is None
    print("OK read_stats: нет файла → None")


def test_telegram_unblock_button_hidden():
    import os
    os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:fake")
    from orchestrator.adapters.telegram.adapter import TelegramAdapter
    a = TelegramAdapter.__new__(TelegramAdapter)
    a.t = lambda k, **kw: k
    # Всегда 3 кнопки на своих местах (ряд не «прыгает», не промахнёшься). Нечего
    # разблокировать → средняя = дефис-заглушка.
    m = a._stop_markup(7, unblock_active=False)
    labels = [b.text for row in m.inline_keyboard for b in row]
    assert labels == ["bubble_stop", "bubble_unblock_idle", "bubble_esc"], labels
    # Есть что → та же позиция становится ⏭ (место не меняется).
    m2 = a._stop_markup(7, unblock_active=True)
    labels2 = [b.text for row in m2.inline_keyboard for b in row]
    assert labels2 == ["bubble_stop", "bubble_unblock", "bubble_esc"], labels2
    # callback_data средней кнопки одинаковый в обоих состояниях (стабильный ряд).
    assert m.inline_keyboard[0][1].callback_data == m2.inline_keyboard[0][1].callback_data
    print("OK Telegram: ⏭ всегда на месте (дефис при простое, ⏭ когда есть что)")


def main():
    test_read_stats_stale_schema()
    test_telegram_unblock_button_hidden()
    print("ALL DEGRADATION OK")


def test_degradation():
    main()


if __name__ == "__main__":
    main()
