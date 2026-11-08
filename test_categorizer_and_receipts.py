import asyncio
import datetime
import io
import os
import sqlite3
from PIL import Image, ImageDraw

import pytest
import zxingcpp

import bot
from kopilka.database.connection import db
from kopilka.services.categorizer import (
    classify_expense,
    save_user_category_rule,
    get_user_category_rule,
    CATEGORIES_CATALOG,
)
from kopilka.services.receipt_scanner import (
    parse_fns_qr,
    parse_bank_notification,
    scan_receipt_image,
)

D = datetime.date
NOW = datetime.datetime(2026, 11, 7, 12, 0)


class FakeMessage:
    def __init__(self, text=None, photo=(), document=None, caption=""):
        self.text = text
        self.photo = photo
        self.document = document
        self.caption = caption
        self.replies = []
        self.edits = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        self.replies.append((text, {"parse_mode": parse_mode, "reply_markup": reply_markup}))
        msg = self

        class SentMsg:
            def __init__(self, t):
                self.t = t

            async def edit_text(self, new_text, parse_mode=None, reply_markup=None):
                msg.edits.append((new_text, {"parse_mode": parse_mode, "reply_markup": reply_markup}))

        return SentMsg(text)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        self.edits.append((text, {"parse_mode": parse_mode, "reply_markup": reply_markup}))


class FakePhotoSize:
    def __init__(self, image_bytes):
        self._bytes = image_bytes

    async def get_file(self):
        outer = self

        class F:
            async def download_to_memory(self, out_buf):
                out_buf.write(outer._bytes)

        return F()


class FakeDocument:
    def __init__(self, image_bytes, file_name="receipt.jpg", mime_type="image/jpeg"):
        self._bytes = image_bytes
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = len(image_bytes)

    async def get_file(self):
        outer = self

        class F:
            async def download_to_memory(self, out_buf):
                out_buf.write(outer._bytes)

        return F()


def make_update(uid=1, text=None, data=None, photo=(), document=None, caption=""):
    msg = FakeMessage(text=text, photo=photo, document=document, caption=caption)
    cq = None
    if data:
        class CQ:
            def __init__(self, d):
                self.data = d
                self.message = msg
            async def answer(self): pass
        cq = CQ(data)

    class Upd:
        effective_user = type("U", (), {"id": uid})()
        message = msg
        callback_query = cq

    return Upd(), msg


def make_ctx(user_data=None):
    class Ctx:
        def __init__(self):
            self.user_data = user_data if user_data is not None else {}
            self.bot_data = {}
    return Ctx()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def clean_db(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setattr(bot, "DB_PATH", db_file)
    import kopilka.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", db_file)

    bot.init_db()
    # Настраиваем пользователя
    bot.set_setting(1, "start_date", "2026-11-01")
    bot.set_setting(1, "daily_income", "1000")
    bot.set_setting(1, "income_mode", "daily")
    return db_file


# ---------- 1. Тесты автоматического распределения категорий ----------

def test_classify_expense_catalog():
    # Продукты
    cat, kw = classify_expense(1, "молоко")
    assert cat == "Продукты"
    assert kw == "молоко"

    # Кафе
    cat, kw = classify_expense(1, "шаурма")
    assert cat == "Кафе и рестораны"

    # Магазины
    cat, _ = classify_expense(1, "пятерочка")
    assert cat == "Продукты"

    # АЗС
    cat, _ = classify_expense(1, "лукойл")
    assert cat == "Авто"

    # Аптека
    cat, _ = classify_expense(1, "аптека")
    assert cat == "Здоровье"

    # Неизвестное
    cat, kw = classify_expense(1, "неизвестныйтовар777")
    assert cat == "Разное"
    assert kw is None


def test_classify_expense_adapts_to_user_synonym(clean_db):
    # Если у пользователя уже есть категория «Еда»
    user_cats = ["Еда", "Такси"]
    cat, _ = classify_expense(1, "молоко", user_cats)
    assert cat == "Еда"

    cat, _ = classify_expense(1, "яндекс go", user_cats)
    assert cat == "Такси"


def test_user_category_rule_persists_and_overrides(clean_db):
    # Добавляем персональное правило пользователя
    save_user_category_rule(1, "vape", "Вредные привычки")
    assert get_user_category_rule(1, "vape") == "Вредные привычки"

    cat, kw = classify_expense(1, "vape", [])
    assert cat == "Вредные привычки"
    assert kw == "vape"


def test_bot_auto_categorizes_expense_and_shows_chip(clean_db, monkeypatch):
    upd, msg = make_update(text="500 молоко")
    ctx = make_ctx()
    run(bot.text_handler(upd, ctx))

    with db() as conn:
        row = conn.execute("SELECT amount, category FROM expenses WHERE user_id = 1").fetchone()
    assert row["amount"] == 500
    assert row["category"] == "Продукты"

    # Сообщение содержит пояснение и кнопку оригинального ввода
    reply_text = msg.replies[0][0]
    assert "Продукты" in reply_text
    assert "молоко" in reply_text.lower()

    markup = msg.replies[0][1]["reply_markup"]
    tokens = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert any(c.startswith("xc_") for c in tokens)


def test_bot_auto_categorize_can_be_toggled_off(clean_db):
    # Отключаем автокатегории в настройках
    bot.set_setting(1, "auto_category", "0")

    upd, msg = make_update(text="500 молоко")
    ctx = make_ctx()
    run(bot.text_handler(upd, ctx))

    with db() as conn:
        row = conn.execute("SELECT amount, category FROM expenses WHERE user_id = 1").fetchone()
    # При отключённой настройке категория остаётся как ввёл пользователь
    assert row["category"] == "Молоко"


# ---------- 2. Тесты чтения чеков и уведомлений ----------

def test_parse_fns_qr():
    qr_str = "t=20261107T1435&s=1240.50&fn=9960440300999999&i=12345&fp=1234567890&n=1"
    parsed = parse_fns_qr(qr_str)
    assert parsed is not None
    assert parsed["amount"] == 1240.50
    assert parsed["spent_date"] == D(2026, 11, 7)
    assert parsed["method"] == "qr"


def test_parse_bank_notification():
    n1 = parse_bank_notification("Покупка 450р Пятерочка")
    assert n1 is not None
    assert n1["amount"] == 450.0
    assert n1["merchant"] == "Пятерочка"

    n2 = parse_bank_notification("СберБанк: Оплата 1250.50 руб Додо Пицца. Баланс 10000р")
    assert n2 is not None
    assert n2["amount"] == 1250.50
    assert "Додо Пицца" in n2["merchant"]

    n3 = parse_bank_notification("Оплата 300 ₽, Яндекс Такси")
    assert n3 is not None
    assert n3["amount"] == 300.0


def test_bot_handles_bank_notification_message(clean_db):
    upd, msg = make_update(text="Покупка 450р Пятерочка")
    ctx = make_ctx()
    run(bot.text_handler(upd, ctx))

    with db() as conn:
        row = conn.execute("SELECT amount, category FROM expenses WHERE user_id = 1").fetchone()
    assert row["amount"] == 450.0
    assert row["category"] == "Продукты"
    assert "Распознана оплата" in msg.replies[0][0]


def test_scan_receipt_qr_image(clean_db):
    qr_data = "t=20261107T1500&s=890.00&fn=9960440300999999&i=54321&fp=9876543210&n=1"
    bc = zxingcpp.create_barcode(qr_data, zxingcpp.BarcodeFormat.QRCode)
    qr_arr = zxingcpp.write_barcode_to_image(bc, scale=4)
    qr_img = Image.fromarray(qr_arr).convert("RGB")

    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    res = scan_receipt_image(img_bytes, user_id=1)
    assert res["success"] is True
    assert res["amount"] == 890.00
    assert res["spent_date"] == D(2026, 11, 7)
    assert res["method"] == "qr"


def test_bot_photo_handler_records_receipt(clean_db):
    qr_data = "t=20261107T1600&s=1500.00&fn=9960440300999999&i=11111&fp=2222222222&n=1"
    bc = zxingcpp.create_barcode(qr_data, zxingcpp.BarcodeFormat.QRCode)
    qr_arr = zxingcpp.write_barcode_to_image(bc, scale=4)
    qr_img = Image.fromarray(qr_arr).convert("RGB")

    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    fake_photo = FakePhotoSize(img_bytes)
    upd, msg = make_update(photo=[fake_photo])
    ctx = make_ctx()
    run(bot.photo_handler(upd, ctx))

    with db() as conn:
        row = conn.execute("SELECT amount, category FROM expenses WHERE user_id = 1").fetchone()
    assert row["amount"] == 1500.0
    assert "Чек успешно распознан" in msg.edits[0][0]
    assert "1 500" in msg.edits[0][0]


def test_bot_document_handler_records_receipt_file(clean_db):
    qr_data = "t=20261107T1700&s=620.00&fn=9960440300999999&i=33333&fp=4444444444&n=1"
    bc = zxingcpp.create_barcode(qr_data, zxingcpp.BarcodeFormat.QRCode)
    qr_arr = zxingcpp.write_barcode_to_image(bc, scale=4)
    qr_img = Image.fromarray(qr_arr).convert("RGB")

    buf = io.BytesIO()
    qr_img.save(buf, format="PNG")
    img_bytes = buf.getvalue()

    fake_doc = FakeDocument(img_bytes, file_name="receipt.png", mime_type="image/png")
    upd, msg = make_update(document=fake_doc)
    ctx = make_ctx()
    run(bot.document_handler(upd, ctx))

    with db() as conn:
        row = conn.execute("SELECT amount, category FROM expenses WHERE user_id = 1").fetchone()
    assert row["amount"] == 620.0
    assert "Чек успешно распознан" in msg.edits[0][0]


# ---------- 3. Тесты исправления существующих категорий в базе ----------

def test_recategorize_user_expenses(clean_db):
    with db() as conn:
        conn.executemany(
            "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
            [
                (1, "2026-11-01", 100.0, "молоко"),
                (1, "2026-11-02", 250.0, "шаурма"),
                (1, "2026-11-03", 2000.0, "лукойл"),
                (1, "2026-11-04", 50.0, "редкийнеизвестныйтовар"),
            ],
        )

    from kopilka.services.categorizer import recategorize_user_expenses
    res = recategorize_user_expenses(user_id=1)

    assert res["total_scanned"] == 4
    assert res["updated_count"] == 3

    with db() as conn:
        rows = {r["amount"]: r["category"] for r in conn.execute("SELECT amount, category FROM expenses WHERE user_id = 1").fetchall()}

    assert rows[100.0] == "Продукты"
    assert rows[250.0] == "Кафе и рестораны"
    assert rows[2000.0] == "Авто"
    assert rows[50.0] == "редкийнеизвестныйтовар"


def test_fix_categories_cmd_and_button(clean_db):
    with db() as conn:
        conn.executemany(
            "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
            [
                (1, "2026-11-01", 450.0, "пятерочка"),
                (1, "2026-11-02", 350.0, "яндекс такси"),
            ],
        )

    upd, msg = make_update(text="/fix_categories")
    ctx = make_ctx()
    ctx.args = []
    run(bot.fix_categories_cmd(upd, ctx))

    assert "Категории успешно обновлены" in msg.replies[0][0]
    assert "пятерочка" in msg.replies[0][0]
    assert "Продукты" in msg.replies[0][0]

    with db() as conn:
        cats = [r["category"] for r in conn.execute("SELECT category FROM expenses WHERE user_id = 1").fetchall()]
    assert "Продукты" in cats
    assert "Транспорт" in cats

    # Повторный вызов — сообщает что всё в порядке
    upd2, msg2 = make_update(text="/fix_categories")
    run(bot.fix_categories_cmd(upd2, ctx))
    assert "Все категории уже в полном порядке" in msg2.replies[0][0]


def test_classify_expense_morphology_and_prepositions():
    # Проверяем морфологический стемминг и предлоги
    assert classify_expense(1, "шаурму")[0] == "Кафе и рестораны"
    assert classify_expense(1, "пиццу")[0] == "Кафе и рестораны"
    assert classify_expense(1, "аптеку")[0] == "Здоровье"
    assert classify_expense(1, "в аптеке")[0] == "Здоровье"
    assert classify_expense(1, "на такси")[0] == "Транспорт"
    assert classify_expense(1, "купил курицу")[0] == "Продукты"
    assert classify_expense(1, "картошку")[0] == "Продукты"
    assert classify_expense(1, "в пятерочке")[0] == "Продукты"
    assert classify_expense(1, "за бензин")[0] == "Авто"
    assert classify_expense(1, "одежду")[0] == "Покупки"
    assert classify_expense(1, "книгу")[0] == "Развлечения"


def test_bot_auto_categorizes_even_with_prior_history(clean_db):
    # Пользователь уже имеет в истории категорию "шаурму"
    with db() as conn:
        conn.execute("INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (1, '2026-11-01', 250, 'шаурму')")

    upd, msg = make_update(text="300 шаурму")
    ctx = make_ctx()
    run(bot.text_handler(upd, ctx))

    with db() as conn:
        last = conn.execute("SELECT amount, category FROM expenses WHERE user_id = 1 ORDER BY id DESC LIMIT 1").fetchone()

    # Должно автоматически назначить Кафе и рестораны, несмотря на наличие старой записи
    assert last["amount"] == 300
    assert last["category"] == "Кафе и рестораны"
    assert "Кафе и рестораны" in msg.replies[0][0]


def test_ask_category_for_number_flow(clean_db):
    # Включаем запрос категории для чисел
    bot.set_setting(1, "ask_cat_on_num", "1")

    upd, msg = make_update(text="500")
    ctx = make_ctx()
    run(bot.text_handler(upd, ctx))

    # Бот спросил категорию
    assert "Выберите категорию" in msg.replies[0][0]
    assert ctx.user_data.get("state") == "wait_pick_category_for_amount"

    # Пользователь нажал кнопку (например Продукты)
    markup = msg.replies[0][1]["reply_markup"]
    btn_cb = [b.callback_data for row in markup.inline_keyboard for b in row if "Продукты" in b.text][0]
    upd2, msg2 = make_update(data=btn_cb)
    run(bot.callback_handler(upd2, ctx))

    with db() as conn:
        row = conn.execute("SELECT amount, category FROM expenses WHERE user_id = 1").fetchone()
    assert row["amount"] == 500
    assert row["category"] == "Продукты"


def test_menu_history_and_menu_undo_callbacks_when_update_message_is_none(clean_db):
    """В реальном Telegram при нажатии инлайн-кнопки update.message == None, а сообщение лежит в update.callback_query.message."""
    with db() as conn:
        for i in range(15):
            conn.execute(
                "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
                (1, "2026-11-04", 100.0 + i, "Продукты"),
            )

    # Нажатие «🧾 Траты» (menu_history) из инлайн-меню, когда update.message is None
    upd, msg = make_update(data="menu_history")
    upd.message = None
    upd.effective_message = msg
    ctx = make_ctx()
    run(bot.callback_handler(upd, ctx))
    assert len(msg.replies) == 1
    assert "Последние траты" in msg.replies[0][0]
    markup = msg.replies[0][1]["reply_markup"]
    cbs_list = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "hist_page_1" in cbs_list

    # Переход на 2-ю страницу истории
    upd_p2, msg_p2 = make_update(data="hist_page_1")
    upd_p2.message = None
    run(bot.callback_handler(upd_p2, ctx))
    assert "стр. 2" in msg_p2.edits[0][0]

    # Нажатие «↩️ Отменить последнюю» (menu_undo) из инлайн-меню, когда update.message is None
    upd_undo, msg_undo = make_update(data="menu_undo")
    upd_undo.message = None
    upd_undo.effective_message = msg_undo
    run(bot.callback_handler(upd_undo, ctx))
    assert len(msg_undo.replies) == 1
    assert "Удалена последняя трата" in msg_undo.replies[0][0]


def test_xe_del_callback_deletes_expense(clean_db):
    with db() as conn:
        eid = conn.execute(
            "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (1, '2026-11-04', 450.0, 'Продукты')"
        ).lastrowid

    upd, msg = make_update(data=f"xe_del_{eid}")
    upd.message = None
    run(bot.callback_handler(upd, make_ctx()))
    assert bot.get_expense(1, eid) is None
    assert "удалена" in msg.edits[0][0]


def test_classify_everyday_items_and_context_phrases():
    assert classify_expense(1, "Помело")[0] == "Продукты"
    assert classify_expense(1, "От обветривания")[0] == "Здоровье"
    assert classify_expense(1, "Почтовые конверты")[0] == "Дом"
    assert classify_expense(1, "светофор")[0] == "Продукты"
    assert classify_expense(1, "творожная масса")[0] == "Продукты"
    assert classify_expense(1, "супа")[0] == "Продукты"
    assert classify_expense(1, "жвачка")[0] == "Продукты"
    assert classify_expense(1, "мороженка")[0] == "Продукты"
    assert classify_expense(1, "для стирки")[0] == "Дом"
    assert classify_expense(1, "от комаров")[0] == "Здоровье"


def test_init_db_auto_recategorizes_existing_expenses(clean_db):
    with db() as conn:
        conn.executemany(
            "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
            [
                (1, "2026-11-01", 280.0, "Помело"),
                (1, "2026-11-02", 148.0, "От обветривания"),
                (1, "2026-11-03", 120.0, "Почтовые конверты"),
            ],
        )

    bot.init_db()

    with db() as conn:
        rows = {r["amount"]: r["category"] for r in conn.execute("SELECT amount, category FROM expenses WHERE user_id = 1").fetchall()}

    assert rows[280.0] == "Продукты"
    assert rows[148.0] == "Здоровье"
    assert rows[120.0] == "Дом"



