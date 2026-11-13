"""Тесты динамического перерасчёта дневного бюджета."""
import datetime
import sqlite3
import types
import pytest

import bot
from test_month_report import FakeBot, at, freeze, menv, run  # noqa: F401
from test_step2_features import FakeMsg, make_update, make_ctx, add_user


def test_fixed_mode_does_not_recalculate_tomorrow(menv, monkeypatch):
    """По умолчанию (фиксированный режим) перерасход сегодня НЕ меняет завтрашний лимит."""
    add_user(menv.db, uid=10, start="2026-11-01")
    # Доход 1000/день, 30 дней в ноябре -> available = 30000, base = 1000
    freeze(monkeypatch, at(2026, 11, 5, 12, 0))
    today = bot.today_date()
    tomorrow = today + datetime.timedelta(days=1)
    
    # Добавляем перерасход: потратили 2500 на 5 ноября
    c = sqlite3.connect(menv.db)
    c.execute("INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
              (10, today.isoformat(), 2500.0, "Ресторан"))
    c.commit()
    c.close()
    
    info_today = bot.get_budget_info(10, today)
    info_tomorrow = bot.get_budget_info(10, tomorrow)
    
    assert info_today["daily_limit"] == 1000.0
    assert info_tomorrow["daily_limit"] == 1000.0  # Не изменился в фиксированном режиме!


def test_dynamic_mode_overrun_recalculates_tomorrow(menv, monkeypatch):
    """При включённом динамическом перерасчёте перерасход распределяется на оставшиеся дни."""
    add_user(menv.db, uid=11, start="2026-11-01")
    bot.set_setting(11, "recalc_mode", "1")
    
    freeze(monkeypatch, at(2026, 11, 5, 12, 0))
    today = bot.today_date()
    tomorrow = today + datetime.timedelta(days=1)
    
    # Дни 1..4 потратили ровно по 1000
    c = sqlite3.connect(menv.db)
    for d in range(1, 5):
        c.execute("INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
                  (11, f"2026-11-{d:02d}", 1000.0, "Еда"))
    # 5 ноября перерасход: потратили 2500 (перерасход на 1500)
    c.execute("INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
              (11, today.isoformat(), 2500.0, "Ресторан"))
    c.commit()
    c.close()
    
    info_today = bot.get_budget_info(11, today)
    assert info_today["daily_limit"] == 1000.0  # Лимит на сегодня был зафиксирован в начале дня
    
    info_tomorrow = bot.get_budget_info(11, tomorrow)
    # Оставшийся бюджет: 30000 - 4000 - 2500 = 23500
    # Оставшиеся дни: 30 - 5 = 25 дней
    # Новый лимит: 23500 / 25 = 940.0 ₽
    assert round(info_tomorrow["daily_limit"], 2) == 940.0


def test_dynamic_mode_savings_recalculates_tomorrow(menv, monkeypatch):
    """При экономии лимит на оставшиеся дни увеличивается."""
    add_user(menv.db, uid=12, start="2026-11-01")
    bot.set_setting(12, "recalc_mode", "1")
    
    freeze(monkeypatch, at(2026, 11, 5, 12, 0))
    today = bot.today_date()
    tomorrow = today + datetime.timedelta(days=1)
    
    c = sqlite3.connect(menv.db)
    for d in range(1, 5):
        c.execute("INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
                  (12, f"2026-11-{d:02d}", 1000.0, "Еда"))
    # 5 ноября сэкономили: потратили 0
    c.commit()
    c.close()
    
    info_today = bot.get_budget_info(12, today)
    assert info_today["daily_limit"] == 1000.0
    
    info_tomorrow = bot.get_budget_info(12, tomorrow)
    # Оставшийся бюджет: 30000 - 4000 = 26000
    # Оставшиеся дни: 30 - 5 = 25 дней
    # Новый лимит: 26000 / 25 = 1040.0 ₽
    assert round(info_tomorrow["daily_limit"], 2) == 1040.0


def test_dynamic_mode_exhaustion(menv, monkeypatch):
    """При полном исчерпании бюджета лимит не уходит в минус, а равен 0.0."""
    add_user(menv.db, uid=13, start="2026-11-01")
    bot.set_setting(13, "recalc_mode", "1")
    
    freeze(monkeypatch, at(2026, 11, 5, 12, 0))
    today = bot.today_date()
    tomorrow = today + datetime.timedelta(days=1)
    
    c = sqlite3.connect(menv.db)
    c.execute("INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
              (13, today.isoformat(), 50000.0, "Покупка авто"))
    c.commit()
    c.close()
    
    info_tomorrow = bot.get_budget_info(13, tomorrow)
    assert info_tomorrow["daily_limit"] == 0.0


def test_toggle_recalc_callback(menv):
    """Проверка переключения настройки динамического перерасчёта через callback."""
    add_user(menv.db, uid=14, start="2026-11-01")
    assert bot.get_setting(14, "recalc_mode", "0") == "0"
    
    upd, msg = make_update(uid=14, data="toggle_recalc_mode")
    ctx = make_ctx()
    run(bot.callback_handler(upd, ctx))
    
    assert bot.get_setting(14, "recalc_mode", "0") == "1"
    assert any("Динамический перерасчёт лимита" in r[0] for r in msg.replies)
    
    # Повторный клик выключает
    upd2, msg2 = make_update(uid=14, data="toggle_recalc_mode")
    run(bot.callback_handler(upd2, ctx))
    assert bot.get_setting(14, "recalc_mode", "0") == "0"


def test_recalc_command(menv):
    """Команда /recalc переключает режим и выводит статус."""
    add_user(menv.db, uid=15, start="2026-11-01")
    upd, msg = make_update(uid=15, text="/recalc")
    ctx = make_ctx()
    
    run(bot.recalc_cmd(upd, ctx))
    assert bot.get_setting(15, "recalc_mode", "0") == "1"
    assert any("Динамический перерасчёт лимита включён" in r[0] for r in msg.replies)
    
    run(bot.recalc_cmd(upd, ctx))
    assert bot.get_setting(15, "recalc_mode", "0") == "0"
    assert any("Фиксированный лимит включён" in r[0] for r in msg.replies)


def test_build_day_report_dynamic(menv, monkeypatch):
    """Отчёт дня при перерасходе показывает перерасчитанный завтрашний лимит."""
    add_user(menv.db, uid=16, start="2026-11-01")
    bot.set_setting(16, "recalc_mode", "1")
    
    freeze(monkeypatch, at(2026, 11, 5, 12, 0))
    today = bot.today_date()
    c = sqlite3.connect(menv.db)
    c.execute("INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
              (16, today.isoformat(), 2500.0, "Кафе"))
    c.commit()
    c.close()
    
    text, _ = bot.build_day_report(16, today)
    assert "ЛИМИТ ПРЕВЫШЕН" in text
    assert "Бюджет пересчитан" in text
    assert "Лимит с завтра:" in text


def test_limits_between_dynamic(menv, monkeypatch):
    """limits_between возвращает корректную динамическую серию по всем дням."""
    add_user(menv.db, uid=17, start="2026-11-01")
    bot.set_setting(17, "recalc_mode", "1")
    
    freeze(monkeypatch, at(2026, 11, 3, 12, 0))
    c = sqlite3.connect(menv.db)
    c.execute("INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
              (17, "2026-11-01", 2000.0, "Шоппинг"))
    c.execute("INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
              (17, "2026-11-02", 500.0, "Обед"))
    c.commit()
    c.close()
    
    lims = bot.limits_between(17, datetime.date(2026, 11, 1), datetime.date(2026, 11, 4))
    # День 1: 1000.0
    assert round(lims[datetime.date(2026, 11, 1)], 1) == 1000.0
    # День 2: (30000 - 2000) / 29 = 965.52
    assert round(lims[datetime.date(2026, 11, 2)], 1) == 965.5
    # День 3: (28000 - 500) / 28 = 982.14
    assert round(lims[datetime.date(2026, 11, 3)], 1) == 982.1
    # День 4: проекция на будущее с учётом экономии за день 3 (потрачено 0): (27500 / 27) = 1018.52
    assert round(lims[datetime.date(2026, 11, 4)], 1) == 1018.5
