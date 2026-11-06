import datetime
import pytest
import bot
from kopilka.services.mandatory_service import (
    find_matching_mandatory,
    get_month_mandatory_status,
    record_mandatory_payment,
    remove_mandatory_payment,
)
from kopilka.services.reports_service import build_bills_card
from kopilka.utils.parsers import parse_mandatory_payment_message

TODAY = datetime.date(2026, 11, 7)

def setup_user_with_bills(db_path, user_id=101):
    bot.DB_PATH = db_path
    bot.init_db()
    bot.set_setting(user_id, "daily_income", "60000")
    bot.set_setting(user_id, "income_mode", "monthly")
    bot.set_setting(user_id, "start_date", "2026-11-01")
    bot.set_setting(user_id, "save", "10000")
    bot.upsert_mandatory(user_id, "Аренда", 30000)
    bot.upsert_mandatory(user_id, "ЖКХ", 5000)
    bot.upsert_mandatory(user_id, "Интернет", 800)
    bot.upsert_mandatory(user_id, "Связь", 500)

def test_main_keyboard_includes_bills_button():
    assert "🏠 Счета" in bot.MENU_TEXTS
    assert {"🏷 Категории", "🧾 Траты", "🏠 Счета"} <= bot.MENU_TEXTS

def test_parse_mandatory_payment_message():
    assert parse_mandatory_payment_message("оплата интернет 800") == (800.0, "Интернет")
    assert parse_mandatory_payment_message("оплатил жкх 4850") == (4850.0, "Жкх")
    assert parse_mandatory_payment_message("счет аренда 30000") == (30000.0, "Аренда")
    assert parse_mandatory_payment_message("обяз 1200 свет") == (1200.0, "Свет")
    assert parse_mandatory_payment_message("просто 450 продукты") is None

def test_mandatory_payments_lifecycle(tmp_path):
    db_file = str(tmp_path / "budget.db")
    setup_user_with_bills(db_file, user_id=101)

    status = get_month_mandatory_status(101, TODAY)
    assert status["total_plan"] == 36300.0
    assert status["total_paid"] == 0.0
    assert status["total_remaining"] == 36300.0

    items_dict = {it["name"]: it for it in status["items"]}
    assert items_dict["Интернет"]["status_emoji"] == "⚪"
    assert items_dict["Интернет"]["is_paid"] is False

    # 1. Полная оплата Интернета
    internet = find_matching_mandatory(101, "интернет")
    assert internet is not None
    pay_id1 = record_mandatory_payment(101, internet["id"], 800.0, TODAY)
    assert pay_id1 > 0

    status = get_month_mandatory_status(101, TODAY)
    items_dict = {it["name"]: it for it in status["items"]}
    assert items_dict["Интернет"]["paid"] == 800.0
    assert items_dict["Интернет"]["remaining"] == 0.0
    assert items_dict["Интернет"]["is_paid"] is True
    assert items_dict["Интернет"]["status_emoji"] == "🟢"

    # 2. Частичная оплата ЖКХ (3500 из 5000)
    jkh = find_matching_mandatory(101, "жкх")
    pay_id2 = record_mandatory_payment(101, jkh["id"], 3500.0, TODAY)
    status = get_month_mandatory_status(101, TODAY)
    items_dict = {it["name"]: it for it in status["items"]}
    assert items_dict["ЖКХ"]["paid"] == 3500.0
    assert items_dict["ЖКХ"]["remaining"] == 1500.0
    assert items_dict["ЖКХ"]["is_paid"] is False
    assert items_dict["ЖКХ"]["status_emoji"] == "🟡"

    # 3. Доплата ЖКХ остатка (1500)
    record_mandatory_payment(101, jkh["id"], 1500.0, TODAY)
    status = get_month_mandatory_status(101, TODAY)
    items_dict = {it["name"]: it for it in status["items"]}
    assert items_dict["ЖКХ"]["paid"] == 5000.0
    assert items_dict["ЖКХ"]["remaining"] == 0.0
    assert items_dict["ЖКХ"]["is_paid"] is True
    assert items_dict["ЖКХ"]["status_emoji"] == "🟢"

    # 4. Удаление оплаты
    assert remove_mandatory_payment(101, pay_id1) is True
    status = get_month_mandatory_status(101, TODAY)
    items_dict = {it["name"]: it for it in status["items"]}
    assert items_dict["Интернет"]["paid"] == 0.0
    assert items_dict["Интернет"]["is_paid"] is False
    assert items_dict["Интернет"]["status_emoji"] == "⚪"

def test_overrun_payment_does_not_affect_daily_limit(tmp_path):
    db_file = str(tmp_path / "budget.db")
    setup_user_with_bills(db_file, user_id=102)

    info_before = bot.get_budget_info(102, TODAY)
    limit_before = info_before["daily_limit"]
    pool_before = bot.calc_change_pool(102, TODAY)

    # Оплата с превышением (650 вместо 500)
    svyaz = find_matching_mandatory(102, "связь")
    record_mandatory_payment(102, svyaz["id"], 650.0, TODAY)

    status = get_month_mandatory_status(102, TODAY)
    items_dict = {it["name"]: it for it in status["items"]}
    assert items_dict["Связь"]["paid"] == 650.0
    assert items_dict["Связь"]["overrun"] == 150.0
    assert items_dict["Связь"]["is_overrun"] is True
    assert items_dict["Связь"]["status_emoji"] == "⚠️"
    assert status["total_overrun"] == 150.0

    # Проверяем, что дневной лимит и банк сдачи НЕ ИЗМЕНИЛИСЬ
    info_after = bot.get_budget_info(102, TODAY)
    assert info_after["daily_limit"] == limit_before
    assert bot.calc_change_pool(102, TODAY) == pool_before

def test_build_bills_card_rendering(tmp_path):
    db_file = str(tmp_path / "budget.db")
    setup_user_with_bills(db_file, user_id=103)

    text, markup = build_bills_card(103, TODAY)
    assert "Обязательные счета" in text
    assert "Аренда" in text
    assert "ЖКХ" in text
    assert "План на месяц" in text
    assert markup is not None
    # Кнопки для быстрой оплаты должны присутствовать
    cb_data = [b.callback_data for row in markup.inline_keyboard for b in row if hasattr(b, "callback_data")]
    assert any(c.startswith("paymand_q_") for c in cb_data)
    assert any(c.startswith("paymand_c_") for c in cb_data)
    assert "bills_history" in cb_data
