"""Автоотчёт 1-го числа за прошлый месяц."""
import asyncio
import datetime
import shutil
import sqlite3
import types

import pytest

import bot
from test_migrations import dump, make_legacy_db

MSK = bot.TZ
FINANCE_TABLES = ("expenses", "savings_topups", "mandatory", "day_limits")


class FakeBot:
    def __init__(self, fail=None):
        self.sent = []
        self.fail = fail  # исключение, которое нужно бросать при отправке

    async def send_message(self, chat_id, text, **kw):
        if self.fail:
            raise self.fail
        self.sent.append((chat_id, text, kw))


def run(coro):
    return asyncio.run(coro)


def at(y, m, d, h=10, mi=0):
    return datetime.datetime(y, m, d, h, mi, tzinfo=MSK)


@pytest.fixture
def menv(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(bot, "DATA_DIR", str(data))
    monkeypatch.setattr(bot, "DB_PATH", str(data / "budget.db"))
    monkeypatch.setattr(bot, "BACKUP_DIR", str(data / "backups"))
    monkeypatch.setattr(bot, "MONTH_REPORT_ENABLED", True)
    monkeypatch.setattr(bot, "MONTH_REPORT_HOUR", 9)
    monkeypatch.setattr(bot, "MONTH_REPORT_MINUTE", 0)
    monkeypatch.setattr(bot, "MONTH_REPORT_WINDOW_DAYS", 3)
    bot.init_db()
    return types.SimpleNamespace(db=str(data / "budget.db"), tmp=tmp_path)


def freeze(monkeypatch, moment):
    class FixedDT(datetime.datetime):
        cur = moment

        @classmethod
        def now(cls, tz=None):
            return cls.cur.astimezone(tz) if tz else cls.cur

    monkeypatch.setattr(bot.datetime, "datetime", FixedDT)
    return FixedDT


def add_user(db, uid=1, start="2026-10-01", spend=(("2026-10-02", 450, "Продукты"), ("2026-10-03", 300, "Такси"))):
    c = sqlite3.connect(db)
    c.executemany("INSERT OR REPLACE INTO settings VALUES (?, ?, ?)", [
        (uid, "daily_income", "1000"), (uid, "income_mode", "daily"), (uid, "start_date", start), (uid, "save", "5000"),
    ])
    c.execute("INSERT INTO mandatory (user_id,name,amount) VALUES (?, 'Аренда', 15000)", (uid,))
    c.executemany("INSERT INTO expenses (user_id,spent_date,amount,category) VALUES (?,?,?,?)",
                  [(uid, d, a, cat) for d, a, cat in spend])
    c.execute("INSERT INTO savings_topups (user_id,topup_date,amount,comment) VALUES (?, '2026-10-10', 700, 'подработка')", (uid,))
    c.commit()
    c.close()


def sent_marker(uid=1):
    return bot.get_setting(uid, "month_report_sent")


def test_sends_summary_on_first_day_after_report_time_once(menv, monkeypatch):
    add_user(menv.db)
    freeze(monkeypatch, at(2026, 11, 1, 9, 0))
    fake, data = FakeBot(), {}
    assert run(bot.send_monthly_reports(fake, at(2026, 11, 1, 9, 0), data)) == 1
    chat, text, kw = fake.sent[0]
    assert chat == 1 and "отчёт за прошлый месяц" in text and "Октябрь 2026" in text
    assert "Потрачено" in text and "750 ₽" in text      # 450 + 300
    assert "сдача" in text or "перерасход" in text
    assert "Копилка на конец месяца" in text
    assert kw["parse_mode"] == "Markdown" and kw["reply_markup"] is not None
    assert sent_marker() == "2026-10"
    # повторные запуски job (он идёт каждые 2 минуты) ничего не шлют
    for t in (at(2026, 11, 1, 9, 2), at(2026, 11, 1, 23, 0), at(2026, 11, 2, 10, 0)):
        assert run(bot.send_monthly_reports(fake, t, data)) == 0
    assert len(fake.sent) == 1


def test_report_body_equals_history_detail(menv, monkeypatch):
    """Цифры отчёта соответствуют истории месяцев."""
    from test_migrations import make_legacy_db
    legacy_db = str(menv.tmp / "legacy.db")
    make_legacy_db(legacy_db)
    shutil.copyfile(legacy_db, menv.db)
    bot.init_db()
    freeze(monkeypatch, at(2026, 11, 1, 9, 30))
    first = datetime.date(2026, 10, 1)
    expected_text, _ = bot.build_month_detail(111, first)
    assert "Октябрь 2026" in expected_text
    assert "Доход за месяц:" in expected_text
    fake = FakeBot()
    run(bot.send_monthly_reports(fake, at(2026, 11, 1, 9, 30), {}))
    by_user = {chat: text for chat, text, _ in fake.sent}
    assert 111 in by_user and by_user[111].endswith(expected_text)


def test_not_before_report_time_on_first_day(menv, monkeypatch):
    add_user(menv.db)
    freeze(monkeypatch, at(2026, 11, 1, 8, 59))
    fake = FakeBot()
    assert run(bot.send_monthly_reports(fake, at(2026, 11, 1, 8, 59), {})) == 0
    assert fake.sent == [] and sent_marker() is None


def test_catch_up_within_window_after_downtime_but_not_later(menv, monkeypatch):
    add_user(menv.db)
    fake = FakeBot()
    freeze(monkeypatch, at(2026, 11, 4, 12, 0))
    assert run(bot.send_monthly_reports(fake, at(2026, 11, 4, 12, 0), {})) == 0   # окно закрыто
    assert sent_marker() is None
    freeze(monkeypatch, at(2026, 11, 3, 0, 5))                                      # бот поднялся 3-го
    assert run(bot.send_monthly_reports(fake, at(2026, 11, 3, 0, 5), {})) == 1


def test_users_without_history_for_that_month_are_skipped(menv, monkeypatch):
    add_user(menv.db, uid=1, start="2026-11-01", spend=())       # учёт начался в ноябре
    add_user(menv.db, uid=2)
    freeze(monkeypatch, at(2026, 11, 1, 10))
    fake = FakeBot()
    run(bot.send_monthly_reports(fake, at(2026, 11, 1, 10), {}))
    assert [c for c, _, _ in fake.sent] == [2]
    assert sent_marker(1) is None and sent_marker(2) == "2026-10"


def test_opt_out_and_blocked_users_get_nothing(menv, monkeypatch):
    add_user(menv.db, uid=1)
    add_user(menv.db, uid=2)
    bot.set_setting(1, "month_report", "0")
    bot.set_setting(2, "blocked", "1")
    freeze(monkeypatch, at(2026, 11, 1, 10))
    fake = FakeBot()
    run(bot.send_monthly_reports(fake, at(2026, 11, 1, 10), {}))
    assert fake.sent == []


def test_disabled_by_env(menv, monkeypatch):
    add_user(menv.db)
    monkeypatch.setattr(bot, "MONTH_REPORT_ENABLED", False)
    freeze(monkeypatch, at(2026, 11, 1, 10))
    fake = FakeBot()
    assert run(bot.send_monthly_reports(fake, at(2026, 11, 1, 10), {})) == 0 and fake.sent == []


def test_year_boundary_reports_december(menv, monkeypatch):
    add_user(menv.db, start="2026-12-01", spend=(("2026-12-05", 1000, "Подарки"),))
    freeze(monkeypatch, at(2027, 1, 1, 9, 5))
    fake = FakeBot()
    run(bot.send_monthly_reports(fake, at(2027, 1, 1, 9, 5), {}))
    assert "Декабрь 2026" in fake.sent[0][1] and sent_marker() == "2026-12"


def test_delivery_failure_retries_then_stops_for_the_day_and_never_marks_sent(menv, monkeypatch):
    add_user(menv.db)
    freeze(monkeypatch, at(2026, 11, 1, 10))
    data = {}
    broken = FakeBot(fail=RuntimeError("network"))
    for _ in range(8):
        run(bot.send_monthly_reports(broken, at(2026, 11, 1, 10), data))
    assert data["month_report_fails"][1][1] == 5          # после 5 попыток за день больше не долбим
    assert sent_marker() is None
    good = FakeBot()
    run(bot.send_monthly_reports(good, at(2026, 11, 1, 10, 30), data))
    assert good.sent == []                                  # сегодня лимит попыток исчерпан
    freeze(monkeypatch, at(2026, 11, 2, 10))
    assert run(bot.send_monthly_reports(good, at(2026, 11, 2, 10), data)) == 1   # завтра отчёт всё же доходит
    assert sent_marker() == "2026-10"


def test_forbidden_marks_user_blocked_and_not_retried(menv, monkeypatch):
    add_user(menv.db)
    freeze(monkeypatch, at(2026, 11, 1, 10))
    run(bot.send_monthly_reports(FakeBot(fail=bot.Forbidden("blocked")), at(2026, 11, 1, 10), {}))
    assert bot.get_setting(1, "blocked") == "1" and sent_marker() == "2026-10"


def test_only_marker_and_archive_rows_are_written_finance_untouched(menv, monkeypatch):
    add_user(menv.db)
    freeze(monkeypatch, at(2026, 11, 1, 10))
    before = dump(menv.db)
    run(bot.send_monthly_reports(FakeBot(), at(2026, 11, 1, 10), {}))
    after = dump(menv.db)
    for t in FINANCE_TABLES:
        assert after[t] == before[t], t                    # траты, копилка, обязательные и лимиты не тронуты
    assert [r for r in after["settings"] if r not in before["settings"]] == [(1, "month_report_sent", "2026-10")]
    assert [r for r in before["settings"] if r not in after["settings"]] == []
    c = sqlite3.connect(menv.db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == bot.migrations.SCHEMA_VERSION   # схема не менялась
    c.close()


def test_toggle_in_settings_keyboard(menv):
    add_user(menv.db)
    kb = bot.settings_inline_kb(1)
    labels = [
        getattr(b, "text", None) or (b.args[0] if getattr(b, "args", None) else getattr(b, "kwargs", {}).get("text"))
        for row in kb.inline_keyboard for b in row
    ]
    assert any("Отчёт за месяц 1-го числа: вкл" in str(l) for l in labels)
    bot.set_setting(1, "month_report", "0")
    kb = bot.settings_inline_kb(1)
    labels = [
        getattr(b, "text", None) or (b.args[0] if getattr(b, "args", None) else getattr(b, "kwargs", {}).get("text"))
        for row in kb.inline_keyboard for b in row
    ]
    assert any("Отчёт за месяц 1-го числа: выкл" in str(l) for l in labels)
