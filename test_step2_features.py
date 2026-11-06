"""Улучшения бота: правка трат, графики прошлых месяцев, предупреждения и недельная сводка, категории."""
import asyncio
import datetime
import sqlite3
import types

import pytest

import bot
from test_month_report import FakeBot, at, freeze, menv, run  # noqa: F401  (menv — фикстура)

D = datetime.date
NOW = at(2026, 11, 4, 12, 0)  # среда


# ---------- вспомогательное ----------
def add_user(db, uid=1, start="2026-10-01", spend=()):
    """Доход 1000 в день, без обязательных и копилки: лимит ровно 1000 ₽ в день."""
    c = sqlite3.connect(db)
    c.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?, ?)", [
        (uid, "daily_income", "1000"), (uid, "income_mode", "daily"), (uid, "start_date", start), (uid, "save", "0"),
    ])
    c.executemany("INSERT INTO expenses (user_id,spent_date,amount,category) VALUES (?,?,?,?)",
                  [(uid, d, a, cat) for d, a, cat in spend])
    c.commit()
    c.close()


def cbs(markup):
    """callback_data всех кнопок (и для настоящей библиотеки, и для заглушки из conftest)."""
    out = []
    for row in markup.inline_keyboard:
        for b in row:
            out.append(getattr(b, "callback_data", None) or b.kwargs.get("callback_data"))
    return out


def labels(markup):
    out = []
    for row in markup.inline_keyboard:
        for b in row:
            out.append(getattr(b, "text", None) or b.args[0])
    return out


class FakeMsg:
    def __init__(self, text=None, photo=()):
        self.text, self.photo = text, photo
        self.replies, self.edits, self.photos, self.media = [], [], [], []

    async def reply_text(self, text, **kw):
        self.replies.append((text, kw))

    async def edit_text(self, text, **kw):
        self.edits.append((text, kw))

    async def reply_photo(self, photo, **kw):
        self.photos.append((photo, kw))

    async def edit_media(self, media, **kw):
        self.media.append((media, kw))

    async def edit_reply_markup(self, **kw):
        self.edits.append((None, kw))


def make_update(uid=1, text=None, data=None, photo=()):
    msg = FakeMsg(text, photo)
    q = None
    if data is not None:
        async def answer(*a, **k):
            return None
        q = types.SimpleNamespace(data=data, message=msg, answer=answer)
    return types.SimpleNamespace(effective_user=types.SimpleNamespace(id=uid), message=msg, callback_query=q), msg


def make_ctx(user_data=None):
    return types.SimpleNamespace(user_data=user_data if user_data is not None else {},
                                 application=types.SimpleNamespace(bot_data={}), bot=FakeBot())


def only_expense_id(db, uid=1):
    c = sqlite3.connect(db)
    try:
        return c.execute("SELECT id FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()[0]
    finally:
        c.close()


# ---------- 2. правка трат ----------
def test_update_expense_changes_only_given_fields_and_checks_owner(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db, spend=[("2026-11-03", 450, "Продукты")])
    add_user(menv.db, uid=2, spend=[("2026-11-03", 100, "Кофе")])
    eid = only_expense_id(menv.db, 1)

    row = bot.update_expense(1, eid, amount=500)
    assert (row["amount"], row["category"], row["spent_date"]) == (500, "Продукты", "2026-11-03")
    row = bot.update_expense(1, eid, category="Еда", spent_date=D(2026, 11, 1))
    assert (row["amount"], row["category"], row["spent_date"]) == (500, "Еда", "2026-11-01")
    # чужую трату изменить нельзя
    assert bot.update_expense(2, eid, amount=1) is None
    assert bot.get_expense(1, eid)["amount"] == 500
    assert bot.get_expense(1, 9999) is None


@pytest.mark.parametrize("text, expected", [
    ("вчера", D(2026, 11, 3)),
    ("Сегодня", D(2026, 11, 4)),
    ("позавчера", D(2026, 11, 2)),
    ("28.10", D(2026, 10, 28)),
    ("28.10.2026", D(2026, 10, 28)),
    ("1.9.26", D(2026, 9, 1)),
    ("15.12", D(2025, 12, 15)),   # в будущем этого года -> прошлогодняя
    ("31.02", None),
    ("завтра?", None),
    ("abc", None),
])
def test_parse_date_text(menv, monkeypatch, text, expected):
    freeze(monkeypatch, NOW)
    assert bot.parse_date_text(text) == expected


def test_day_report_and_history_have_edit_and_delete_buttons(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db, spend=[("2026-11-04", 450, "Продукты")])
    eid = only_expense_id(menv.db)
    _, markup = bot.build_day_report(1, D(2026, 11, 4))
    assert f"xe_open_{eid}" in cbs(markup) and f"delexp_{eid}" in cbs(markup)
    _, markup = bot.build_history(1)
    assert f"xe_open_{eid}" in cbs(markup) and f"hdelexp_{eid}" in cbs(markup)
    assert all(len(c.encode()) <= 64 for c in cbs(markup))


def test_edit_card_shows_values_and_buttons(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db, spend=[("2026-11-03", 450, "Продукты")])
    eid = only_expense_id(menv.db)
    text, markup = bot.build_expense_card(1, eid)
    assert "450" in text and "Продукты" in text and "03.11.2026" in text
    assert {f"xe_amt_{eid}", f"xe_cat_{eid}", f"xe_dt_{eid}", f"hdelexp_{eid}"} <= set(cbs(markup))
    text, markup = bot.build_expense_card(1, 9999)
    assert "удалена" in text and markup is None


def test_edit_amount_flow(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db, spend=[("2026-11-04", 450, "Продукты")])
    eid = only_expense_id(menv.db)
    ctx = make_ctx()

    upd, msg = make_update(data=f"xe_amt_{eid}")
    run(bot.callback_handler(upd, ctx))
    assert ctx.user_data["state"] == "wait_edit_amount" and ctx.user_data["edit_exp_id"] == eid

    upd, msg = make_update(text="не число")
    run(bot.text_handler(upd, ctx))
    assert "числом" in msg.replies[0][0]
    assert ctx.user_data["state"] == "wait_edit_amount"      # ждём дальше
    assert bot.get_expense(1, eid)["amount"] == 450

    upd, msg = make_update(text="1 200")
    run(bot.text_handler(upd, ctx))
    assert bot.get_expense(1, eid)["amount"] == 1200
    assert not ctx.user_data                                  # состояние сброшено
    assert "Сумма изменена" in msg.replies[0][0]


def test_edit_date_flow_validates(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db, spend=[("2026-11-04", 450, "Продукты")])
    eid = only_expense_id(menv.db)
    ctx = make_ctx({"state": "wait_edit_date", "edit_exp_id": eid})

    upd, msg = make_update(text="завтра")
    run(bot.text_handler(upd, ctx))
    assert "Не понял" in msg.replies[0][0]
    upd, msg = make_update(text="05.11.2026")       # будущее
    run(bot.text_handler(upd, ctx))
    assert "не наступила" in msg.replies[0][0]
    assert bot.get_expense(1, eid)["spent_date"] == "2026-11-04"

    upd, msg = make_update(text="вчера")
    run(bot.text_handler(upd, ctx))
    assert bot.get_expense(1, eid)["spent_date"] == "2026-11-03"
    assert "03.11.2026" in msg.replies[0][0]


def test_edit_category_by_text_uses_existing_spelling_and_by_button(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db, spend=[("2026-11-01", 100, "Продукты"), ("2026-11-02", 100, "Продукты"), ("2026-11-04", 50, "Кофе")])
    eid = only_expense_id(menv.db)                         # «Кофе»
    ctx = make_ctx({"state": "wait_edit_cat", "edit_exp_id": eid})

    upd, msg = make_update(text="ПРОДУКТЫ")
    run(bot.text_handler(upd, ctx))
    assert bot.get_expense(1, eid)["category"] == "Продукты"

    # кнопки выбора категории: показаны использованные, кроме текущей
    bot.update_expense(1, eid, category="Кофе")
    upd, msg = make_update(data=f"xe_cat_{eid}")
    run(bot.callback_handler(upd, make_ctx()))
    text, kw = msg.replies[0]
    assert "Кофе" in text                                      # текущая названа в тексте
    buttons = cbs(kw["reply_markup"])
    assert f"xc_{eid}_{bot.cat_token('Продукты')}" in buttons and f"xc_{eid}_{bot.cat_token('Кофе')}" not in buttons
    assert f"xe_open_{eid}" in buttons and all(len(b.encode()) <= 64 for b in buttons)

    # нажатие на категорию
    upd, msg = make_update(data=f"xc_{eid}_{bot.cat_token('Продукты')}")
    run(bot.callback_handler(upd, make_ctx()))
    assert bot.get_expense(1, eid)["category"] == "Продукты"
    assert "Категория изменена" in msg.edits[0][0]


def test_edit_of_deleted_expense_is_handled(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db, spend=[("2026-11-04", 450, "Продукты")])
    eid = only_expense_id(menv.db)
    bot.delete_expense(1, eid)
    ctx = make_ctx({"state": "wait_edit_amount", "edit_exp_id": eid})
    upd, msg = make_update(text="500")
    run(bot.text_handler(upd, ctx))
    assert "удалена" in msg.replies[0][0] and not ctx.user_data
    upd, msg = make_update(data=f"xe_open_{eid}")
    run(bot.callback_handler(upd, make_ctx()))
    assert "удалена" in msg.replies[0][0]


def test_menu_button_cancels_edit_state(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db, spend=[("2026-11-04", 450, "Продукты")])
    eid = only_expense_id(menv.db)
    ctx = make_ctx({"state": "wait_edit_amount", "edit_exp_id": eid})
    upd, msg = make_update(text="📊 Сегодня")
    run(bot.text_handler(upd, ctx))
    assert not ctx.user_data and bot.get_expense(1, eid)["amount"] == 450


# ---------- 3. графики за прошлые месяцы ----------
def chart_data(menv):
    add_user(menv.db, spend=[
        ("2026-10-02", 450, "Продукты"), ("2026-10-02", 700, "Такси"), ("2026-10-15", 1500, "Одежда"),
        ("2026-10-31", 300, "Кофе"), ("2026-11-01", 200, "Продукты"), ("2026-11-03", 900, "Такси"),
    ])
    bot.archive_closed_months(1)


@pytest.mark.parametrize("kind", list(bot.CHARTS))
@pytest.mark.parametrize("month", [None, D(2026, 10, 1), D(2026, 11, 1)])
def test_all_charts_render_for_past_and_current_month(menv, monkeypatch, kind, month):
    freeze(monkeypatch, NOW)
    chart_data(menv)
    buf = bot.build_chart(kind, 1, month)
    assert buf.read(8) == b"\x89PNG\r\n\x1a\n"


def test_past_month_chart_uses_that_months_data(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    chart_data(menv)
    today, info, start, by_day, cats = bot.collect_month(1, D(2026, 10, 1))
    assert today == D(2026, 10, 31) and start == D(2026, 10, 1)
    assert by_day == {2: 1150, 15: 1500, 31: 300}
    assert {r["category"] for r in cats} == {"Продукты", "Такси", "Одежда", "Кофе"}
    today, _, start, by_day, _ = bot.collect_month(1)            # текущий месяц
    assert today == D(2026, 11, 4) and by_day == {1: 200, 3: 900}
    # ссылка на текущий месяц равна «без месяца»
    assert bot.collect_month(1, D(2026, 11, 1))[0] == today


def test_chart_nav_buttons(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    chart_data(menv)
    cur = cbs(bot.chart_kb(1, "overview"))
    assert "chart_overview_2026-10" in cur                     # ◀️ Окт
    assert "chart_calendar" in cur and "chart_forecast" in cur
    past = cbs(bot.chart_kb(1, "calendar", D(2026, 10, 1)))
    assert "chart_calendar_2026-10" in past                    # вид сохраняет выбранный месяц
    assert "chart_calendar" in past                            # ▶️ Ноя = текущий месяц без ключа
    assert all(len(c.encode()) <= 64 for c in cur + past)
    assert bot.chart_kb().inline_keyboard                      # без пользователя (как раньше) работает


def test_chart_callback_for_past_month_and_unknown_month(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    chart_data(menv)
    upd, msg = make_update(data="chart_structure_2026-10", photo=())   # из текстовой карточки месяца
    run(bot.callback_handler(upd, make_ctx()))
    assert len(msg.photos) == 1 and "Октябрь 2026" in msg.photos[0][1]["caption"]

    upd, msg = make_update(data="chart_structure_2026-10", photo=("x",))  # из самой картинки
    run(bot.callback_handler(upd, make_ctx()))
    assert len(msg.media) == 1 and not msg.photos

    upd, msg = make_update(data="chart_structure_2019-01")
    run(bot.callback_handler(upd, make_ctx()))
    assert "нет в истории" in msg.replies[0][0] and not msg.photos


def test_month_picker_and_month_card_button(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    chart_data(menv)
    upd, msg = make_update(data="chartm_overview_2026-11", photo=("x",))
    run(bot.callback_handler(upd, make_ctx()))
    picker = msg.edits[0][1]["reply_markup"]
    assert "chart_overview_2026-10" in cbs(picker) and "chart_overview_2026-11" in cbs(picker)
    _, markup = bot.build_month_detail(1, D(2026, 10, 1))
    assert "chart_overview_2026-10" in cbs(markup) and "cat_2026-10" in cbs(markup)


# ---------- 4. предупреждения ----------
def test_limit_warning_crosses_80_percent_once(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db)
    today = D(2026, 11, 4)
    assert bot.limit_warning(1, today, 0, 500) == ""                 # 50%
    w = bot.limit_warning(1, today, 500, 850)                        # пересекли 80%
    assert "85%" in w and "850" in w and "150" in w
    assert bot.limit_warning(1, today, 850, 900) == ""               # уже предупреждали
    assert bot.limit_warning(1, today, 500, 1000).startswith("⚠️")   # ровно 100% — ещё в лимите
    assert bot.limit_warning(1, today, 500, 1200) == ""              # превышение показывает отчёт
    assert bot.limit_warning(1, D(2026, 11, 3), 0, 900) == ""        # не сегодня
    bot.set_setting(1, "warn_limit", "0")
    assert bot.limit_warning(1, today, 500, 850) == ""


def test_new_expense_message_contains_warning_only_when_crossing(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db)
    upd, msg = make_update(text="500 продукты")
    run(bot.text_handler(upd, make_ctx()))
    assert "дневного лимита:" not in msg.replies[0][0]
    upd, msg = make_update(text="350 такси")
    run(bot.text_handler(upd, make_ctx()))
    assert "85% дневного лимита" in msg.replies[0][0]
    upd, msg = make_update(text="10 жвачка")
    run(bot.text_handler(upd, make_ctx()))
    assert "дневного лимита:" not in msg.replies[0][0]
    # трата задним числом не предупреждает
    upd, msg = make_update(text="900 кино вчера")
    run(bot.text_handler(upd, make_ctx()))
    assert "дневного лимита:" not in msg.replies[0][0]


def test_edit_amount_can_trigger_warning(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db, spend=[("2026-11-04", 100, "Кофе")])
    eid = only_expense_id(menv.db)
    ctx = make_ctx({"state": "wait_edit_amount", "edit_exp_id": eid})
    upd, msg = make_update(text="900")
    run(bot.text_handler(upd, ctx))
    assert "90% дневного лимита" in msg.replies[0][0]


def test_settings_have_toggles(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    add_user(menv.db)
    assert {"toggle_week_summary", "toggle_warn_limit"} <= set(cbs(bot.settings_inline_kb(1)))
    upd, _ = make_update(data="toggle_warn_limit")
    run(bot.callback_handler(upd, make_ctx()))
    assert bot.get_setting(1, "warn_limit") == "0"
    upd, _ = make_update(data="toggle_week_summary")
    run(bot.callback_handler(upd, make_ctx()))
    assert bot.get_setting(1, "week_summary") == "0"


@pytest.fixture
def wenv(menv, monkeypatch):
    monkeypatch.setattr(bot, "WEEK_SUMMARY_ENABLED", True)
    monkeypatch.setattr(bot, "WEEK_SUMMARY_HOUR", 9)
    monkeypatch.setattr(bot, "WEEK_SUMMARY_MINUTE", 0)
    monkeypatch.setattr(bot, "WEEK_SUMMARY_WINDOW_DAYS", 2)
    return menv


WEEK_SPEND = [  # неделя 26.10 (пн) – 01.11 (вс) и предыдущая
    ("2026-10-19", 2000, "Продукты"),
    ("2026-10-26", 400, "Продукты"), ("2026-10-27", 1500, "Одежда"), ("2026-10-28", 300, "Такси"),
    ("2026-10-30", 200, "Продукты"),
]


def test_week_summary_content(wenv, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 2, 9, 5))
    add_user(wenv.db, start="2026-10-01", spend=WEEK_SPEND)
    text = bot.build_week_summary(1, D(2026, 10, 26))
    assert "26.10 – 01.11" in text
    assert "2 400 ₽ из 7 000 ₽" in text                       # 400+1500+300+200; лимит 7 × 1000
    assert "сдача +4 600 ₽" in text
    assert "Дней с превышением:* 1 из 7" in text and "Вт 27.10" in text
    assert "Самый дорогой день:* Вт 27.10 — 1 500 ₽" in text
    assert "Топ категорий" in text and "1. Одежда — 1 500 ₽" in text and "2. Продукты — 600 ₽" in text


def test_week_summary_compares_with_previous_week(wenv, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 2, 9, 5))
    add_user(wenv.db, start="2026-10-01", spend=WEEK_SPEND)
    text = bot.build_week_summary(1, D(2026, 10, 26))
    assert "↑ 20%" in text and "было 2 000 ₽" in text           # 2000 -> 2400


def test_week_summary_counts_only_days_since_start(wenv, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 2, 9, 5))
    add_user(wenv.db, start="2026-10-29", spend=[("2026-10-29", 500, "Кофе")])
    text = bot.build_week_summary(1, D(2026, 10, 26))
    assert "500 ₽ из 4 000 ₽" in text                         # 29, 30, 31.10 и 01.11 = 4 дня
    assert "из 4" in text


def test_week_summary_none_without_expenses(wenv, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 2, 9, 5))
    add_user(wenv.db, start="2026-10-01", spend=[("2026-10-10", 100, "Кофе")])
    assert bot.build_week_summary(1, D(2026, 10, 26)) is None


def test_week_summary_schedule_once_and_catch_up(wenv, monkeypatch):
    add_user(wenv.db, start="2026-10-01", spend=WEEK_SPEND)
    fake, data = FakeBot(), {}

    freeze(monkeypatch, at(2026, 11, 2, 8, 59))                # понедельник до 09:00
    assert run(bot.send_week_summaries(fake, at(2026, 11, 2, 8, 59), data)) == 0
    freeze(monkeypatch, at(2026, 11, 1, 20, 0))                # воскресенье
    assert run(bot.send_week_summaries(fake, at(2026, 11, 1, 20, 0), data)) == 0

    freeze(monkeypatch, at(2026, 11, 2, 9, 0))
    assert run(bot.send_week_summaries(fake, at(2026, 11, 2, 9, 0), data)) == 1
    chat, text, kw = fake.sent[0]
    assert chat == 1 and "Итоги недели 26.10 – 01.11" in text and kw["parse_mode"] == "Markdown"
    assert bot.get_setting(1, "week_summary_sent") == "2026-10-26"
    for t in (at(2026, 11, 2, 9, 2), at(2026, 11, 3, 12, 0)):  # job идёт каждые 2 минуты
        assert run(bot.send_week_summaries(fake, t, data)) == 0
    assert len(fake.sent) == 1

    # следующая неделя — снова одна сводка, потому что отметка про другую неделю
    add_user(wenv.db, start="2026-10-01", spend=[("2026-11-03", 100, "Кофе")])
    freeze(monkeypatch, at(2026, 11, 9, 10, 0))
    assert run(bot.send_week_summaries(fake, at(2026, 11, 9, 10, 0), data)) == 1


def test_week_summary_catch_up_window(wenv, monkeypatch):
    add_user(wenv.db, start="2026-10-01", spend=WEEK_SPEND)
    freeze(monkeypatch, at(2026, 11, 3, 15, 0))                # бот спал в понедельник, вторник — ещё можно
    assert run(bot.send_week_summaries(FakeBot(), at(2026, 11, 3, 15, 0), {})) == 1
    add_user(wenv.db, uid=2, start="2026-10-01", spend=WEEK_SPEND)
    freeze(monkeypatch, at(2026, 11, 4, 15, 0))                # среда: окно закрыто
    assert run(bot.send_week_summaries(FakeBot(), at(2026, 11, 4, 15, 0), {})) == 0


def test_week_summary_respects_switch_blocked_and_empty_week(wenv, monkeypatch):
    freeze(monkeypatch, at(2026, 11, 2, 10, 0))
    add_user(wenv.db, uid=1, spend=WEEK_SPEND)
    add_user(wenv.db, uid=2, spend=WEEK_SPEND)
    add_user(wenv.db, uid=3, spend=[("2026-10-10", 100, "Кофе")])   # на этой неделе трат нет
    bot.set_setting(1, "week_summary", "0")
    bot.set_setting(2, "blocked", "1")
    fake = FakeBot()
    assert run(bot.send_week_summaries(fake, at(2026, 11, 2, 10, 0), {})) == 0
    assert fake.sent == []
    assert bot.get_setting(3, "week_summary_sent") == "2026-10-26"   # неделя закрыта без сообщения


def test_week_summary_disabled_by_env(wenv, monkeypatch):
    monkeypatch.setattr(bot, "WEEK_SUMMARY_ENABLED", False)
    add_user(wenv.db, spend=WEEK_SPEND)
    freeze(monkeypatch, at(2026, 11, 2, 10, 0))
    assert run(bot.send_week_summaries(FakeBot(), at(2026, 11, 2, 10, 0), {})) == 0


def test_week_summary_forbidden_blocks_user(wenv, monkeypatch):
    add_user(wenv.db, spend=WEEK_SPEND)
    freeze(monkeypatch, at(2026, 11, 2, 10, 0))
    assert run(bot.send_week_summaries(FakeBot(fail=bot.Forbidden("x")), at(2026, 11, 2, 10, 0), {})) == 0
    assert bot.get_setting(1, "blocked") == "1"


def test_week_summary_send_error_retries(wenv, monkeypatch):
    add_user(wenv.db, spend=WEEK_SPEND)
    freeze(monkeypatch, at(2026, 11, 2, 10, 0))
    data = {}
    assert run(bot.send_week_summaries(FakeBot(fail=RuntimeError("net")), at(2026, 11, 2, 10, 0), data)) == 0
    assert bot.get_setting(1, "week_summary_sent") is None       # не отмечено: повторим
    assert run(bot.send_week_summaries(FakeBot(), at(2026, 11, 2, 10, 2), data)) == 1


# ---------- 5. категории ----------
def cat_data(menv):
    add_user(menv.db, start="2026-09-01", spend=[
        ("2026-09-10", 3000, "Продукты"),
        ("2026-10-01", 1000, "Продукты"), ("2026-10-02", 500, "продукты"), ("2026-10-03", 700, "Такси"),
        ("2026-10-04", 800, "Ёлка"),
        ("2026-11-01", 3000, "Продукты"), ("2026-11-02", 100, "Такси"), ("2026-11-03", 400, "Кино"),
    ])


def test_used_categories_order_and_merge(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    cat_data(menv)
    names = bot.used_categories(1)
    assert names[0] == "Продукты" and "продукты" not in names     # варианты регистра склеены
    assert names.index("Такси") < names.index("Кино")              # Такси: 2 траты, Кино: 1
    assert bot.used_categories(1, limit=2) == names[:2]
    assert "Такси" not in bot.used_categories(1, exclude=["такси"])


def test_canonical_and_similar_category(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    cat_data(menv)
    assert bot.canonical_category(1, "ПРОДУКТЫ") == "Продукты"
    assert bot.canonical_category(1, "ёлка") == "Ёлка" and bot.canonical_category(1, "елка") == "Ёлка"
    assert bot.canonical_category(1, "  Новая   штука ") == "Новая штука"
    assert bot.similar_category(1, "Продукт") == "Продукты"         # опечатка/окончание
    assert bot.similar_category(1, "Такcи") == "Такси"
    assert bot.similar_category(1, "Продукты") is None              # точное совпадение — не «похожая»
    assert bot.similar_category(1, "Рыбалка") is None


def test_category_token_is_stable_and_short(menv):
    assert bot.cat_token("Продукты") == bot.cat_token("продукты ") and len(bot.cat_token("x")) == 8
    cb = f"xc_{10 ** 9}_{bot.cat_token('Очень длинное название категории для проверки')}"
    assert len(cb.encode()) <= 64


def test_expense_without_category_offers_used_categories(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    cat_data(menv)
    upd, msg = make_update(text="450")
    run(bot.text_handler(upd, make_ctx()))
    eid = only_expense_id(menv.db)
    assert bot.get_expense(1, eid)["category"] == "Разное"
    markup = msg.replies[0][1]["reply_markup"]
    chips = [c for c in cbs(markup) if c.startswith("xc_")]
    assert chips and chips[0] == f"xc_{eid}_{bot.cat_token('Продукты')}"
    assert not any(c == f"xc_{eid}_{bot.cat_token('Разное')}" for c in chips)
    # нажатие на подсказку меняет категорию
    upd, msg = make_update(data=chips[0])
    run(bot.callback_handler(upd, make_ctx()))
    assert bot.get_expense(1, eid)["category"] == "Продукты"


def test_new_expense_is_normalized_and_typo_hint(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    cat_data(menv)
    upd, msg = make_update(text="100 ПРОДУКТЫ")
    run(bot.text_handler(upd, make_ctx()))
    eid = only_expense_id(menv.db)
    assert bot.get_expense(1, eid)["category"] == "Продукты"
    assert not [c for c in cbs(msg.replies[0][1]["reply_markup"]) if c.startswith("xc_")]  # известная — без подсказок

    upd, msg = make_update(text="100 продукт")
    run(bot.text_handler(upd, make_ctx()))
    eid = only_expense_id(menv.db)
    assert bot.get_expense(1, eid)["category"] == "Продукт"          # сохранили как ввёл…
    assert f"xc_{eid}_{bot.cat_token('Продукты')}" in cbs(msg.replies[0][1]["reply_markup"])  # …и предложили исправить


def test_category_totals_merge_spellings(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    cat_data(menv)
    totals = bot.category_totals(1, D(2026, 10, 1))
    assert len(totals) == 3                                       # «Продукты»+«продукты», Такси, Ёлка
    name, total, n = totals[0]
    assert name.lower() == "продукты" and total == 1500 and n == 2
    assert bot.category_totals(1, D(2020, 1, 1)) == []


def test_categories_report_shares_deltas_and_nav(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    cat_data(menv)
    text, markup = bot.build_categories_report(1, D(2026, 10, 1))
    assert "Октябрь 2026" in text
    assert "1. Продукты — *1 500 ₽* · 50%" in text
    assert "↓50%" in text                                       # Продукты: сентябрь 3000 -> октябрь 1500
    assert "Итого:* 3 000 ₽" in text
    assert "новая" in text                                      # Ёлка в сентябре не было
    b = cbs(markup)
    assert "cat_2026-09" in b and "cat_2026-11" in b and "catx_2026-10" in b

    text, markup = bot.build_categories_report(1, D(2026, 11, 1))
    assert "Ноябрь 2026" in text and "↑100%" in text            # Продукты: 1500 -> 3000
    assert "cat_2026-10" in cbs(markup) and "cat_2026-12" not in cbs(markup)


def test_categories_report_empty_month_and_unconfigured(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    text, markup = bot.build_categories_report(77, D(2026, 11, 1))
    assert "не настроен" in text and markup is None
    add_user(menv.db)
    text, markup = bot.build_categories_report(1, D(2026, 11, 1))
    assert "трат нет" in text and markup is not None


def test_categories_matrix(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    cat_data(menv)
    text, markup = bot.build_categories_matrix(1, D(2026, 11, 1))
    assert "```" in text and "Сен" in text and "Окт" in text and "Ноя" in text
    prod = [ln for ln in text.splitlines() if ln.startswith("Продукты")][0]
    assert "3,0к" in prod and "1,5к" in prod                    # сен 3000, окт 1500, ноя 3000
    assert any(ln.startswith("Итого") for ln in text.splitlines())
    assert "cat_2026-11" in cbs(markup)


def test_categories_callbacks_and_menu_button(menv, monkeypatch):
    freeze(monkeypatch, NOW)
    cat_data(menv)
    upd, msg = make_update(data="cat_2026-10")
    run(bot.callback_handler(upd, make_ctx()))
    assert "Октябрь 2026" in msg.edits[0][0]
    upd, msg = make_update(data="catx_2026-10")
    run(bot.callback_handler(upd, make_ctx()))
    assert "Категории по месяцам" in msg.edits[0][0]
    upd, msg = make_update(data="cat_2019-01")
    run(bot.callback_handler(upd, make_ctx()))
    assert "нет в истории" in msg.replies[0][0]
    upd, msg = make_update(text="🏷 Категории")
    run(bot.text_handler(upd, make_ctx()))
    assert "Ноябрь 2026" in msg.replies[0][0]
    upd, msg = make_update(text="🧾 Траты")
    run(bot.text_handler(upd, make_ctx()))
    assert "Последние траты" in msg.replies[0][0]


def test_main_keyboard_includes_new_buttons():
    assert {"🏷 Категории", "🧾 Траты"} <= bot.MENU_TEXTS
