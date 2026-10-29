import calendar
import datetime
import io
import logging
import os
import re
import sqlite3
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import openpyxl

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- Загрузка настроек из .env ---
def load_env(path=".env"):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

load_env()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
TZ_NAME = os.environ.get("TZ_NAME", "Europe/Moscow")
DB_PATH = os.environ.get("DB_PATH", "budget.db")
TEMPLATE_PATH = "template.xlsx"

TZ = ZoneInfo(TZ_NAME)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("budget-bot")


# --- База данных (Многопользовательская) ---
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER,
                key TEXT,
                value TEXT,
                PRIMARY KEY (user_id, key)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mandatory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                amount REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                spent_date TEXT NOT NULL,
                amount REAL NOT NULL,
                category TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS savings_topups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                topup_date TEXT NOT NULL,
                amount REAL NOT NULL,
                comment TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)


def get_setting(user_id: int, key: str, default=None):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE user_id = ? AND key = ?", 
            (user_id, key)
        ).fetchone()
        return row["value"] if row else default


def set_setting(user_id: int, key: str, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, str(value)),
        )


def today_date() -> datetime.date:
    return datetime.datetime.now(TZ).date()


def ensure_start_day(user_id: int):
    if not get_setting(user_id, "start_date"):
        set_setting(user_id, "start_date", today_date().isoformat())


def get_budget_info(user_id: int, ref_date: datetime.date = None):
    if ref_date is None:
        ref_date = today_date()
    
    real_days = calendar.monthrange(ref_date.year, ref_date.month)[1]
    custom_days = get_setting(user_id, "custom_days_in_month")
    days_in_month = int(custom_days) if custom_days and str(custom_days).isdigit() else real_days

    income_mode = get_setting(user_id, "income_mode", "daily")
    raw_income = float(get_setting(user_id, "daily_income", 1000) or 1000)

    if income_mode == "daily":
        income = raw_income * days_in_month
        display_income_text = f"по {fmt_money(raw_income)} в день"
    elif income_mode == "monthly":
        income = raw_income
        display_income_text = f"{fmt_money(raw_income)} 1 раз в месяц"
    elif income_mode == "twice_monthly":
        income = raw_income * 2
        display_income_text = f"{fmt_money(raw_income)} 2 раза в месяц"
    elif income_mode == "weekly":
        income = raw_income * (days_in_month / 7)
        display_income_text = f"{fmt_money(raw_income)} раз в неделю"
    else:
        income = raw_income * days_in_month
        display_income_text = f"по {fmt_money(raw_income)} в день"

    save_advance = float(get_setting(user_id, "save", 0) or 0)
    
    month_prefix = f"{ref_date.year:04d}-{ref_date.month:02d}-"
    with get_conn() as conn:
        mand_rows = conn.execute(
            "SELECT id, name, amount FROM mandatory WHERE user_id = ? ORDER BY id", 
            (user_id,)
        ).fetchall()
        topup_row = conn.execute(
            "SELECT SUM(amount) as total FROM savings_topups WHERE user_id = ? AND topup_date LIKE ?",
            (user_id, month_prefix + "%")
        ).fetchone()
    
    topups_total = topup_row["total"] if topup_row and topup_row["total"] else 0.0
    total_save_effective = save_advance + topups_total

    mandatory_total = sum(r["amount"] for r in mand_rows)
    available = income - mandatory_total - total_save_effective
    daily_limit = available / days_in_month if days_in_month else 0.0

    return {
        "raw_income": raw_income,
        "income_mode": income_mode,
        "display_income_text": display_income_text,
        "income": income,
        "save": save_advance,
        "topups_total": topups_total,
        "total_save_effective": total_save_effective,
        "mandatory_list": mand_rows,
        "mandatory_total": mandatory_total,
        "days_in_month": days_in_month,
        "is_custom_days": bool(custom_days),
        "available": available,
        "daily_limit": max(0.0, daily_limit),
    }


def calc_accumulated_change_pool(user_id: int, up_to_date: datetime.date = None) -> float:
    if up_to_date is None:
        up_to_date = today_date()
    ensure_start_day(user_id)
    start_str = get_setting(user_id, "start_date", up_to_date.isoformat())
    try:
        start_d = datetime.date.fromisoformat(start_str)
    except ValueError:
        start_d = up_to_date

    first_day_of_month = datetime.date(up_to_date.year, up_to_date.month, 1)
    effective_start = max(start_d, first_day_of_month)

    if effective_start >= up_to_date:
        return 0.0

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT spent_date, SUM(amount) as total FROM expenses "
            "WHERE user_id = ? AND spent_date >= ? AND spent_date < ? GROUP BY spent_date",
            (user_id, effective_start.isoformat(), up_to_date.isoformat()),
        ).fetchall()
    spent_by_day = {r["spent_date"]: r["total"] for r in rows}

    change_pool = 0.0
    cur = effective_start
    while cur < up_to_date:
        d_info = get_budget_info(user_id, cur)
        spent = spent_by_day.get(cur.isoformat(), 0.0)
        diff = d_info["daily_limit"] - spent
        change_pool += diff
        cur += datetime.timedelta(days=1)

    return change_pool


# --- Экспорт в персональный Excel с детальной стоимостью каждой вещи ---
def update_excel_file(user_id: int, target_month: datetime.date = None) -> str:
    if target_month is None:
        target_month = today_date()

    first_day = datetime.date(target_month.year, target_month.month, 1)
    info = get_budget_info(user_id, target_month)
    days_count = info["days_in_month"]

    if os.path.exists(TEMPLATE_PATH):
        wb = openpyxl.load_workbook(TEMPLATE_PATH)
        ws = wb.active
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Бюджет"

    ws["B4"] = datetime.datetime(first_day.year, first_day.month, 1)
    ws["B5"] = info["income"]
    ws["B20"] = info["total_save_effective"]

    for row_idx in range(8, 18):
        ws[f"A{row_idx}"] = None
        ws[f"B{row_idx}"] = None

    for idx, item in enumerate(info["mandatory_list"][:10]):
        row_idx = 8 + idx
        ws[f"A{row_idx}"] = item["name"]
        ws[f"B{row_idx}"] = item["amount"]

    for row_idx in range(6, 37):
        ws[f"G{row_idx}"] = None
        ws[f"H{row_idx}"] = None

    month_prefix = f"{first_day.year:04d}-{first_day.month:02d}-"
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT spent_date, amount, category FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? ORDER BY spent_date, id",
            (user_id, month_prefix + "%"),
        ).fetchall()

    by_day = {}
    for r in rows:
        d_num = int(r["spent_date"].split("-")[2])
        by_day.setdefault(d_num, []).append((r["amount"], r["category"]))

    t_today = today_date()
    start_str = get_setting(user_id, "start_date", t_today.isoformat())
    try:
        start_d = datetime.date.fromisoformat(start_str)
    except ValueError:
        start_d = t_today

    for day_num in range(1, days_count + 1):
        cur_date = datetime.date(first_day.year, first_day.month, min(day_num, days_count))
        row_idx = 5 + day_num
        if day_num in by_day:
            items = by_day[day_num]
            total_day = sum(x[0] for x in items)
            details_list = [f"{cat}: {int(amt)}₽" if amt.is_integer() else f"{cat}: {amt}₽" for amt, cat in items]
            ws[f"G{row_idx}"] = total_day
            ws[f"H{row_idx}"] = " | ".join(details_list)
        elif start_d <= cur_date < t_today:
            ws[f"G{row_idx}"] = 0
            ws[f"H{row_idx}"] = "Без трат"

    user_export_path = f"Бюджет_{user_id}.xlsx"
    wb.save(user_export_path)
    return user_export_path


# --- Клавиатуры ---
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📊 Сегодня", "📅 Месяц", "🏦 В копилку"],
        ["💸 Из копилки", "📈 График", "📥 Скачать Excel"],
        ["⚙️ Настройки", "↩️ Отменить последнюю"],
    ],
    resize_keyboard=True,
)


def settings_inline_kb(user_id: int):
    info = get_budget_info(user_id)
    reminder_time = get_setting(user_id, "reminder_time", "21:00")
    days_label = f"{info['days_in_month']} дн. (авто)" if not info['is_custom_days'] else f"{info['days_in_month']} дн. (ручное)"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏦 Ежедневный доход ({int(info['raw_income'])}₽ / {info['income_mode']})", callback_data="set_daily_income")],
        [InlineKeyboardButton("🔄 Изменить график получения дохода", callback_data="set_income_mode")],
        [InlineKeyboardButton(f"📅 Дней в месяце: {days_label}", callback_data="set_days_count")],
        [InlineKeyboardButton(f"⏰ Время отчёта МСК (сейчас {reminder_time})", callback_data="set_reminder_time")],
        [InlineKeyboardButton("💰 Плановая копилка", callback_data="set_save")],
        [
            InlineKeyboardButton("➕ Добавить обяз. расход", callback_data="add_mand"),
            InlineKeyboardButton("🗑 Удалить обяз. расход", callback_data="del_mand_menu"),
        ],
        [InlineKeyboardButton("🔄 Запустить мастера настройки", callback_data="restart_wizard")],
        [InlineKeyboardButton("🔄 Перезагрузить бота (сброс кэша)", callback_data="restart_bot_action")],
        [InlineKeyboardButton("⚠️ Полный сброс всех данных", callback_data="full_reset_action")],
    ])


# Универсальная функция: теперь доступна ВСЕМ пользователям без ограничений
def is_allowed(update: Update) -> bool:
    return True


def fmt_money(val: float) -> str:
    return f"{val:,.0f}".replace(",", " ") + " ₽"


def build_settings_text(user_id: int) -> str:
    ensure_start_day(user_id)
    info = get_budget_info(user_id)
    mand_lines = []
    for r in info["mandatory_list"]:
        mand_lines.append(f"  • {r['name']}: {fmt_money(r['amount'])}")
    mand_block = "\n".join(mand_lines) if mand_lines else "  (пока не добавлены)"
    
    topup_str = f" (+{fmt_money(info['topups_total'])} доп.)" if info['topups_total'] > 0 else ""
    reminder_time = get_setting(user_id, "reminder_time", "21:00")
    days_mode = "авто по календарю" if not info['is_custom_days'] else "ручной выбор"

    return (
        "⚙️ *Настройки бюджета (🏦 Умная модель)*\n\n"
        f"🏦 *Доход:* {fmt_money(info['raw_income'])} ({info['display_income_text']})\n"
        f"📅 *Дней в месяце:* {info['days_in_month']} ({days_mode})\n"
        f"⏰ *Вечерний авто-отчёт:* в {reminder_time} по МСК\n"
        f"🏠 *Обязательные расходы ({fmt_money(info['mandatory_total'])}):*\n{mand_block}\n\n"
        f"💰 *Плановая копилка:* {fmt_money(info['save'])}{topup_str}\n"
        f"🎯 *Чистый лимит на день:* *{fmt_money(info['daily_limit'])}*\n\n"
        "👇 Нажмите нужную кнопку для изменения:"
    )


def build_day_report(user_id: int, target_date: datetime.date):
    info = get_budget_info(user_id, target_date)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, amount, category FROM expenses WHERE user_id = ? AND spent_date = ? ORDER BY id",
            (user_id, target_date.isoformat()),
        ).fetchall()

    spent = sum(r["amount"] for r in rows)
    rem = info["daily_limit"] - spent
    change_pool = calc_accumulated_change_pool(user_id, target_date + datetime.timedelta(days=1))

    date_str = target_date.strftime("%d.%m.%Y")
    title = f"📊 *Траты за сегодня ({date_str})*" if target_date == today_date() else f"📊 *Траты за {date_str}*"

    if rows:
        lines = [f"{i+1}. *{fmt_money(r['amount'])}* — {r['category']}" for i, r in enumerate(rows)]
        exp_text = "\n".join(lines)
    else:
        exp_text = "_Пока нет записанных трат_"

    if rem < 0:
        overspend = abs(rem)
        status_line = f"⚠️ *ЛИМИТ ПРЕВЫШЕН на {fmt_money(overspend)}!*"
        advice = f"💡 Перерасход покрыт за счёт накопленной сдачи прошлых дней."
    else:
        status_line = f"✅ *Остаток сдачи за день:* +{fmt_money(rem)}"
        advice = f"💡 Сдача (+{fmt_money(rem)}) добавлена в ваш банк сдачи."

    base_daily_income = info["daily_limit"] if info["income_mode"] == "daily" else (info["income"] / info["days_in_month"])

    text = (
        f"{title}\n\n"
        f"{exp_text}\n\n"
        f"🏦 *Доход за день (расчётный):* +{fmt_money(base_daily_income)}\n"
        f"💸 *Потрачено за день:* {fmt_money(spent)} из {fmt_money(info['daily_limit'])}\n"
        f"{status_line}\n\n"
        f"{advice}\n\n"
        f"🪙 *Банк сдачи:* {fmt_money(change_pool)}\n"
        f"💰 *Плановая копилка:* {fmt_money(info['total_save_effective'])}"
    )

    buttons = []
    for r in rows:
        btn_title = f"❌ {r['category']} ({int(r['amount'])}₽)"
        if len(btn_title) > 35:
            btn_title = btn_title[:32] + "..."
        buttons.append(InlineKeyboardButton(btn_title, callback_data=f"delexp_{r['id']}"))
    
    kb_rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    markup = InlineKeyboardMarkup(kb_rows) if kb_rows else None
    return text, markup


DATE_WORDS = {
    "сегодня": 0,
    "вчера": -1,
    "позавчера": -2,
}


def parse_expense_message(text: str):
    tokens = text.strip().split()
    if not tokens:
        return None

    target_d = today_date()
    remaining_tokens = []

    for tok in tokens:
        low = tok.lower()
        if low in DATE_WORDS:
            target_d = today_date() + datetime.timedelta(days=DATE_WORDS[low])
            continue
        m_date = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?$", tok)
        if m_date:
            d, m, y = int(m_date.group(1)), int(m_date.group(2)), m_date.group(3)
            year = int(y) if y else target_d.year
            if year < 100:
                year += 2000
            try:
                target_d = datetime.date(year, m, d)
                continue
            except ValueError:
                pass
        remaining_tokens.append(tok)

    if not remaining_tokens:
        return None

    amount = None
    cat_tokens = []

    def try_float(s):
        s_clean = s.replace(",", ".").replace("₽", "").strip()
        try:
            return float(s_clean)
        except ValueError:
            return None

    first_val = try_float(remaining_tokens[0])
    last_val = try_float(remaining_tokens[-1]) if len(remaining_tokens) > 1 else None

    if first_val is not None and first_val > 0:
        amount = first_val
        cat_tokens = remaining_tokens[1:]
    elif last_val is not None and last_val > 0:
        amount = last_val
        cat_tokens = remaining_tokens[:-1]

    if amount is None:
        return None

    category = " ".join(cat_tokens).strip() or "Разное"
    return amount, category.capitalize(), target_d


# --- Фоновая задача для многопользовательской рассылки ---
async def daily_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now_dt = datetime.datetime.now(TZ)
    t_date = now_dt.date()

    # Автоматически находим всех пользователей, у которых настроен бот в базе данных
    with get_conn() as conn:
        user_rows = conn.execute("SELECT DISTINCT user_id FROM settings").fetchall()
    users = [r["user_id"] for r in user_rows]
    if not users:
        return

    for uid in users:
        reminder_time_str = get_setting(uid, "reminder_time", "21:00")
        try:
            r_hour, r_min = map(int, reminder_time_str.split(":"))
        except ValueError:
            r_hour, r_min = 21, 0

        current_total_minutes = now_dt.hour * 60 + now_dt.minute
        target_total_minutes = r_hour * 60 + r_min

        if current_total_minutes == target_total_minutes:
            text, markup = build_day_report(uid, t_date)
            header = "⏰ *Ваш автоматический вечерний отчёт за день!*\n\n"
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=header + text,
                    parse_mode="Markdown",
                    reply_markup=markup or MAIN_KB
                )
                logger.info("Вечерний отчёт успешно отправлен пользователю %s", uid)
            except Exception as e:
                logger.error("Не удалось отправить вечерний отчёт пользователю %s: %s", uid, e)


# --- Обработчики команд и кнопок ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_start_day(user_id)
    
    context.user_data["wizard_step"] = "wizard_mode"
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Каждый день (например, 1000₽/день)", callback_data="wiz_mode_daily")],
        [InlineKeyboardButton("📅 1 раз в месяц (зарплата)", callback_data="wiz_mode_monthly")],
        [InlineKeyboardButton("📅 2 раза в месяц (аванс + зп)", callback_data="wiz_mode_twice_monthly")],
        [InlineKeyboardButton("📅 Раз в неделю", callback_data="wiz_mode_weekly")],
    ])
    welcome = (
        "👋 *Привет! Добро пожаловать в твой персональный бот для бюджетирования.*\n\n"
        "🧙‍♂️ Давай настроим твой личный бюджет за пару шагов.\n\n"
        "📥 *Шаг 1 из 5:* Как ты получаешь деньги?"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown", reply_markup=markup)


async def show_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text, markup = build_day_report(user_id, today_date())
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup or MAIN_KB)


async def show_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    context.user_data.clear()
    await update.message.reply_text(
        build_settings_text(user_id),
        parse_mode="Markdown",
        reply_markup=settings_inline_kb(user_id),
    )


async def show_week(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    end_d = today_date()
    start_d = end_d - datetime.timedelta(days=6)
    info = get_budget_info(user_id, end_d)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT spent_date, SUM(amount) as total FROM expenses "
            "WHERE user_id = ? AND spent_date >= ? AND spent_date <= ? GROUP BY spent_date",
            (user_id, start_d.isoformat(), end_d.isoformat()),
        ).fetchall()
    by_day = {r["spent_date"]: r["total"] for r in rows}

    lines = ["📆 *Траты за последние 7 дней:*\n"]
    total_week = 0.0
    for i in range(7):
        cur = start_d + datetime.timedelta(days=i)
        sp = by_day.get(cur.isoformat(), 0.0)
        total_week += sp
        diff = info["daily_limit"] - sp
        icon = "🟢" if diff >= 0 else "🔴"
        lines.append(f"{icon} *{cur.strftime('%d.%m')}*: {fmt_money(sp)} (остаток: {fmt_money(diff)})")

    lines.append(f"\n💸 *Всего за 7 дней:* {fmt_money(total_week)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KB)


async def show_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now_d = today_date()
    month_prefix = f"{now_d.year:04d}-{now_d.month:02d}-"
    info = get_budget_info(user_id, now_d)

    with get_conn() as conn:
        day_rows = conn.execute(
            "SELECT spent_date, SUM(amount) as total FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY spent_date ORDER BY spent_date",
            (user_id, month_prefix + "%"),
        ).fetchall()
        top_rows = conn.execute(
            "SELECT category, SUM(amount) as total FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY lower(category) ORDER BY total DESC LIMIT 5",
            (user_id, month_prefix + "%"),
        ).fetchall()

    expenses_spent = sum(r["total"] for r in day_rows)
    total_spent = expenses_spent + info["mandatory_total"]
    change_pool = calc_accumulated_change_pool(user_id, now_d + datetime.timedelta(days=1))

    lines = [f"📅 *Сводка за {now_d.strftime('%m.%Y')}*\n"]

    if info["mandatory_list"]:
        lines.append("🏠 *Обязательные расходы (учтены):*")
        for m in info["mandatory_list"]:
            lines.append(f"• {m['name']}: {fmt_money(m['amount'])}")
        lines.append("")

    if day_rows:
        lines.append("🛍 *Обычные траты по дням:*")
        for r in day_rows[-10:]:
            d_fmt = datetime.date.fromisoformat(r["spent_date"]).strftime("%d.%m")
            lines.append(f"• {d_fmt}: *{fmt_money(r['total'])}*")
    else:
        lines.append("_В этом месяце обычных трат пока нет_")

    lines.append(f"\n💸 *Всего потрачено (с учетом обязательных):* *{fmt_money(total_spent)}*")
    lines.append(f"🪙 *Банк сдачи:* {fmt_money(change_pool)}")
    lines.append(f"💰 *Плановая копилка:* {fmt_money(info['total_save_effective'])}")

    if top_rows:
        lines.append("\n🏆 *Топ-5 категорий расходов:*")
        for i, r in enumerate(top_rows, 1):
            lines.append(f"{i}. {r['category']} — {fmt_money(r['total'])}")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KB)


async def send_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now_d = today_date()
    info = get_budget_info(user_id, now_d)
    days_count = info["days_in_month"]
    month_prefix = f"{now_d.year:04d}-{now_d.month:02d}-"

    with get_conn() as conn:
        rows = conn.execute(
            "SELECT spent_date, SUM(amount) as total FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY spent_date",
            (user_id, month_prefix + "%"),
        ).fetchall()
    by_day = {int(r["spent_date"].split("-")[2]): r["total"] for r in rows}

    days_x = list(range(1, now_d.day + 1))
    spent_y = [by_day.get(d, 0.0) for d in days_x]
    limit_y = [info["daily_limit"]] * len(days_x)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(8, 6), dpi=120)
    fig.patch.set_facecolor("#ffffff")

    colors = ["#ef4444" if s > info["daily_limit"] else "#3b82f6" for s in spent_y]
    ax1.bar(days_x, spent_y, color=colors, alpha=0.85, label="Потрачено за день")
    ax1.plot(days_x, limit_y, color="#10b981", linestyle="--", linewidth=2, label=f"Лимит ({int(info['daily_limit'])} ₽)")
    ax1.set_title("Траты по дням и дневной лимит, ₽", fontsize=11, fontweight="bold")
    ax1.set_xticks(range(1, days_count + 1, max(1, days_count // 10)))
    ax1.grid(axis="y", linestyle=":", alpha=0.6)
    ax1.legend()

    cum_change = []
    cur_c = 0.0
    for s in spent_y:
        cur_c += (info["daily_limit"] - s)
        cum_change.append(cur_c)

    ax2.plot(days_x, cum_change, marker="o", color="#3b82f6", linewidth=2, label="Банк сдачи")
    ax2.set_title("Динамика банка сдачи за месяц, ₽", fontsize=11, fontweight="bold")
    ax2.set_xticks(range(1, days_count + 1, max(1, days_count // 10)))
    ax2.grid(True, linestyle=":", alpha=0.6)
    ax2.legend()

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format="png")
    plt.close(fig)
    buf.seek(0)

    await update.message.reply_photo(
        photo=buf,
        caption="📈 *Ваш график бюджета и банка сдачи*",
        parse_mode="Markdown",
        reply_markup=MAIN_KB,
    )


async def send_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        path = update_excel_file(user_id)
        with open(path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=f"Бюджет_{today_date().strftime('%m_%Y')}.xlsx",
                caption="📥 *Ваша персональная таблица бюджета с деталями трат!*",
                parse_mode="Markdown",
                reply_markup=MAIN_KB,
            )
    except Exception as e:
        logger.exception("Ошибка при формировании Excel:")
        await update.message.reply_text(f"⚠️ Не удалось сформировать Excel: {e}", reply_markup=MAIN_KB)


async def undo_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, amount, category, spent_date FROM expenses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,)
        ).fetchone()
        if not row:
            await update.message.reply_text("Трат для отмены пока нет.", reply_markup=MAIN_KB)
            return
        conn.execute("DELETE FROM expenses WHERE id = ?", (row["id"],))

    update_excel_file(user_id)
    await update.message.reply_text(
        f"↩️ Удалена последняя трата: *{fmt_money(row['amount'])}* ({row['category']}, {row['spent_date']})",
        parse_mode="Markdown",
        reply_markup=MAIN_KB,
    )


# --- Обработка Inline-кнопок ---
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data == "restart_bot_action":
        context.user_data.clear()
        try:
            await query.message.edit_text(
                "🔄 *Кэш успешно сброшен, состояние очищено!*\n\n" + build_settings_text(user_id),
                parse_mode="Markdown",
                reply_markup=settings_inline_kb(user_id),
            )
        except Exception:
            await query.message.reply_text(
                "🔄 *Кэш успешно сброшен!*\n\n" + build_settings_text(user_id),
                parse_mode="Markdown",
                reply_markup=settings_inline_kb(user_id),
            )
        return

    if data == "full_reset_action":
        with get_conn() as conn:
            conn.execute("DELETE FROM settings WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM mandatory WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM expenses WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM savings_topups WHERE user_id = ?", (user_id,))
        
        context.user_data.clear()
        
        context.user_data["wizard_step"] = "wizard_mode"
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Каждый день (например, 1000₽/день)", callback_data="wiz_mode_daily")],
            [InlineKeyboardButton("📅 1 раз в месяц (зарплата)", callback_data="wiz_mode_monthly")],
            [InlineKeyboardButton("📅 2 раза в месяц (аванс + зп)", callback_data="wiz_mode_twice_monthly")],
            [InlineKeyboardButton("📅 Раз в неделю", callback_data="wiz_mode_weekly")],
        ])
        await query.message.edit_text(
            "⚠️ *Все ваши данные, настройки, траты и копилка успешно удалены!*\n\n"
            "🧙‍♂️ Давайте настроим ваш бюджет заново с чистого листа.\n\n"
            "📥 *Шаг 1 из 5:* Как вы получаете деньги?",
            parse_mode="Markdown",
            reply_markup=markup
        )
        return

    wizard_step = context.user_data.get("wizard_step")
    if wizard_step and data.startswith("wiz_mode_"):
        chosen_mode = data.replace("wiz_mode_", "")
        set_setting(user_id, "income_mode", chosen_mode)
        context.user_data["wizard_step"] = "wizard_income"
        await query.message.edit_text(
            "🏦 *Шаг 2 из 5:* Введи сумму твоего дохода (отправь число следующим сообщением, например: `1000` или `50000`):",
            parse_mode="Markdown"
        )
        return

    if data == "set_daily_income":
        context.user_data["state"] = "wait_daily_income"
        mode = get_setting(user_id, "income_mode", "daily")
        unit_text = "в день" if mode == "daily" else ("в месяц" if mode == "monthly" else ("за раз (2р/мес)" if mode == "twice_monthly" else "в неделю"))
        await query.message.reply_text(f"🏦 Введите сумму вашего дохода *{unit_text}* (например: `1000` или `50050`):", parse_mode="Markdown")

    elif data == "set_income_mode":
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Каждый день", callback_data="mode_daily")],
            [InlineKeyboardButton("📅 1 раз в месяц", callback_data="mode_monthly")],
            [InlineKeyboardButton("📅 2 раза в месяц", callback_data="mode_twice_monthly")],
            [InlineKeyboardButton("📅 Раз в неделю", callback_data="mode_weekly")],
        ])
        await query.message.reply_text("🔄 Выберите график получения дохода:", reply_markup=markup)

    elif data.startswith("mode_"):
        m_type = data.split("_")[1]
        if m_type == "twice_monthly":
            m_type = "twice_monthly"
        set_setting(user_id, "income_mode", m_type)
        await query.message.edit_text(
            f"✅ График дохода успешно изменён!\n\n" + build_settings_text(user_id),
            parse_mode="Markdown",
            reply_markup=settings_inline_kb(user_id),
        )

    elif data == "set_days_count":
        context.user_data["state"] = "wait_days_count"
        await query.message.reply_text(
            "📅 Сколько дней учитывать в месяце?\n"
            "• Введите число (например: `30`, `31`)\n"
            "• Или отправьте слово **`авто`**, чтобы бот брал реальное количество дней из календаря.",
            parse_mode="Markdown"
        )

    elif data == "set_reminder_time":
        context.user_data["state"] = "wait_reminder_time"
        await query.message.reply_text(
            "⏰ Введите время по МСК для вечернего отчёта (в формате `ЧЧ:ММ`, например: `03:48`):",
            parse_mode="Markdown"
        )

    elif data == "set_save":
        context.user_data["state"] = "wait_save"
        await query.message.reply_text("💰 Введите сумму плановой копилки за месяц (например: `5000`):", parse_mode="Markdown")

    elif data == "add_mand":
        context.user_data["state"] = "wait_mand"
        await query.message.reply_text(
            "➕ Напишите название и сумму обязательного расхода через пробел (например: `Аренда 15000`):",
            parse_mode="Markdown",
        )

    elif data == "del_mand_menu":
        info = get_budget_info(user_id)
        if not info["mandatory_list"]:
            await query.message.reply_text("Ваш список обязательных расходов пуст.")
            return
        btns = [
            [InlineKeyboardButton(f"❌ {r['name']} ({fmt_money(r['amount'])})", callback_data=f"delmand_{r['id']}")]
            for r in info["mandatory_list"]
        ]
        await query.message.reply_text(
            "🗑 Нажмите на обязательный расход, который нужно удалить:",
            reply_markup=InlineKeyboardMarkup(btns),
        )

    elif data.startswith("delmand_"):
        mand_id = int(data.split("_")[1])
        with get_conn() as conn:
            conn.execute("DELETE FROM mandatory WHERE id = ? AND user_id = ?", (mand_id, user_id))
        update_excel_file(user_id)
        await query.message.edit_text(
            "✅ Обязательный расход удалён!\n\n" + build_settings_text(user_id),
            parse_mode="Markdown",
            reply_markup=settings_inline_kb(user_id),
        )

    elif data == "restart_wizard":
        context.user_data["wizard_step"] = "wizard_mode"
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("📅 Каждый день", callback_data="wiz_mode_daily")],
            [InlineKeyboardButton("📅 1 раз в месяц", callback_data="wiz_mode_monthly")],
            [InlineKeyboardButton("📅 2 раза в месяц", callback_data="wiz_mode_twice_monthly")],
            [InlineKeyboardButton("📅 Раз в неделю", callback_data="wiz_mode_weekly")],
        ])
        await query.message.edit_text(
            "🔄 *Запуск мастера настройки*\n\n"
            "📥 *Шаг 1 из 5:* Как ты получаешь деньги?",
            parse_mode="Markdown",
            reply_markup=markup
        )

    elif data.startswith("delexp_"):
        exp_id = int(data.split("_")[1])
        with get_conn() as conn:
            row = conn.execute("SELECT spent_date FROM expenses WHERE id = ? AND user_id = ?", (exp_id, user_id)).fetchone()
            if row:
                spent_date_str = row["spent_date"]
                conn.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (exp_id, user_id))
            else:
                spent_date_str = None

        target_d = datetime.date.fromisoformat(spent_date_str) if spent_date_str else today_date()
        update_excel_file(user_id, target_d)
        
        text, markup = build_day_report(user_id, target_d)
        try:
            await query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            await query.answer("Трата удалена!", show_alert=False)
        return


# --- Обработка текстовых сообщений ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    wizard_step = context.user_data.get("wizard_step")
    if wizard_step:
        if wizard_step == "wizard_income":
            try:
                val = float(text.replace(" ", "").replace(",", ".").replace("₽", ""))
                set_setting(user_id, "daily_income", val)
                context.user_data["wizard_step"] = "wizard_save"
                await update.message.reply_text(
                    "💰 *Шаг 3 из 5:* Какую сумму ты хочешь откладывать в плановую копилку за месяц? (например: `5000`):",
                    parse_mode="Markdown"
                )
            except ValueError:
                await update.message.reply_text("⚠️ Введи число, например: `1000`")
            return

        elif wizard_step == "wizard_save":
            try:
                val = float(text.replace(" ", "").replace(",", ".").replace("₽", ""))
                set_setting(user_id, "save", val)
                context.user_data["wizard_step"] = "wizard_mand"
                await update.message.reply_text(
                    "🏠 *Шаг 4 из 5:* Добавь первый **обязательный расход** (название и сумму через пробел, например: `Аренда 15000` или напиши `нет`, чтобы пропустить):",
                    parse_mode="Markdown"
                )
            except ValueError:
                await update.message.reply_text("⚠️ Введи число, например: `5000`")
            return

        elif wizard_step == "wizard_mand":
            if text.lower() not in ["нет", "0", "пропустить", "-"]:
                parsed = parse_expense_message(text)
                if parsed:
                    amount, name, _ = parsed
                    with get_conn() as conn:
                        conn.execute("INSERT INTO mandatory (user_id, name, amount) VALUES (?, ?, ?)", (user_id, name, amount))
            
            context.user_data["wizard_step"] = "wizard_time"
            await update.message.reply_text(
                "⏰ *Шаг 5 из 5:* В какое **время по МСК** присылать вечерний отчёт? (например: `03:48`):",
                parse_mode="Markdown"
            )
            return

        elif wizard_step == "wizard_time":
            m = re.match(r"^(\d{1,2}):(\d{2})$", text)
            if m:
                set_setting(user_id, "reminder_time", f"{int(m.group(1)):02d}:{int(m.group(2)):02d}")
            else:
                set_setting(user_id, "reminder_time", "21:00")
            
            context.user_data.clear()
            update_excel_file(user_id)
            await update.message.reply_text(
                "🎉 *Настройка успешно завершена!*\n\n"
                "Бот полностью готов к работе. Теперь траты и стоимость каждой вещи записываются в Excel 👇",
                parse_mode="Markdown",
                reply_markup=MAIN_KB
            )
            return

    if text == "📊 Сегодня":
        await show_today(update, context)
        return
    if text == "📅 Месяц":
        await show_month(update, context)
        return
    if text == "🏦 В копилку":
        context.user_data["state"] = "wait_piggy"
        await update.message.reply_text(
            "💰 *Пополнение плановой копилки*\n\n"
            "Сколько хочешь отложить? (например: `500 Подработка` или просто `500`):",
            parse_mode="Markdown",
        )
        return
    if text == "💸 Из копилки":
        context.user_data["state"] = "wait_withdraw_piggy"
        await update.message.reply_text(
            "💸 *Снятие из копилки*\n\nВведите сумму и причину, например: `3000 ремонт телефона`:",
            parse_mode="Markdown",
        )
        return
    if text == "📈 График":
        await send_chart(update, context)
        return
    if text == "📥 Скачать Excel":
        await send_excel(update, context)
        return
    if text == "⚙️ Настройки":
        await show_settings(update, context)
        return
    if text == "↩️ Отменить последнюю":
        await undo_last(update, context)
        return

    state = context.user_data.get("state")
    if state == "wait_piggy":
        tokens = text.split()
        amount = None
        comment = "Пополнение"
        try:
            amount = float(tokens[0].replace(",", ".").replace("₽", "").strip())
            if len(tokens) > 1:
                comment = " ".join(tokens[1:]).capitalize()
        except (ValueError, IndexError):
            pass

        if amount is None or amount <= 0:
            await update.message.reply_text("⚠️ Введи сумму числом, например: `500 Подработка`")
            return

        with get_conn() as conn:
            conn.execute(
                "INSERT INTO savings_topups (user_id, topup_date, amount, comment) VALUES (?, ?, ?, ?)",
                (user_id, today_date().isoformat(), amount, comment)
            )

        context.user_data.clear()
        update_excel_file(user_id)
        info = get_budget_info(user_id)

        await update.message.reply_text(
            f"💰 В плановую копилку зачислено: *+{fmt_money(amount)}* ({comment})!\n"
            f"💰 Всего в копилке: *{fmt_money(info['total_save_effective'])}*",
            parse_mode="Markdown",
            reply_markup=MAIN_KB,
        )
        return

    if state == "wait_withdraw_piggy":
        tokens = text.split()
        amount = None
        comment = "Снятие"
        try:
            amount = float(tokens[0].replace(",", ".").replace("₽", "").strip())
            if len(tokens) > 1:
                comment = " ".join(tokens[1:]).capitalize()
        except (ValueError, IndexError):
            pass

        if amount is None or amount <= 0:
            await update.message.reply_text("⚠️ Введи сумму и причину, например: `3000 ремонт телефона`")
            return

        t_date = today_date()

        with get_conn() as conn:
            conn.execute(
                "INSERT INTO savings_topups (user_id, topup_date, amount, comment) VALUES (?, ?, ?, ?)",
                (user_id, t_date.isoformat(), -amount, f"Снятие: {comment}")
            )
            conn.execute(
                "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
                (user_id, t_date.isoformat(), amount, f"Из копилки: {comment}")
            )

        context.user_data.clear()
        update_excel_file(user_id, t_date)
        info = get_budget_info(user_id)

        await update.message.reply_text(
            f"💸 Снято из копилки: *-{fmt_money(amount)}* ({comment})!\n"
            f"💰 Всего в плановой копилке: *{fmt_money(info['total_save_effective'])}*",
            parse_mode="Markdown",
            reply_markup=MAIN_KB,
        )
        return

    if state == "wait_daily_income":
        val_str = text.replace(" ", "").replace(",", ".").replace("₽", "")
        try:
            val = float(val_str)
            set_setting(user_id, "daily_income", val)
            context.user_data.clear()
            update_excel_file(user_id)
            await update.message.reply_text(
                f"✅ Доход успешно обновлён!\n\n" + build_settings_text(user_id),
                parse_mode="Markdown",
                reply_markup=settings_inline_kb(user_id),
            )
        except ValueError:
            await update.message.reply_text("⚠️ Введите число, например: `1000`")
        return

    if state == "wait_days_count":
        if text.lower() == "авто":
            with get_conn() as conn:
                conn.execute("DELETE FROM settings WHERE user_id = ? AND key = 'custom_days_in_month'", (user_id,))
            context.user_data.clear()
            update_excel_file(user_id)
            await update.message.reply_text(
                "✅ Дней в месяце теперь берутся **автоматически по реальному календарю**.\n\n" + build_settings_text(user_id),
                parse_mode="Markdown",
                reply_markup=settings_inline_kb(user_id),
            )
        else:
            try:
                days_val = int(text)
                if 1 <= days_val <= 31:
                    set_setting(user_id, "custom_days_in_month", days_val)
                    context.user_data.clear()
                    update_excel_file(user_id)
                    await update.message.reply_text(
                        f"✅ Установлено фиксированное количество дней в месяце: *{days_val}*\n\n" + build_settings_text(user_id),
                        parse_mode="Markdown",
                        reply_markup=settings_inline_kb(user_id),
                    )
                else:
                    await update.message.reply_text("⚠️ Введи число от 1 до 31 или слово `авто`.")
            except ValueError:
                await update.message.reply_text("⚠️ Введи число или слово `авто`.")
        return

    if state == "wait_reminder_time":
        m = re.match(r"^(\d{1,2}):(\d{2})$", text)
        if not m:
            await update.message.reply_text("⚠️ Неверный формат. Введи время в формате `ЧЧ:ММ` (например: `03:48`):")
            return
        h, mn = int(m.group(1)), int(m.group(2))
        if not (0 <= h <= 23 and 0 <= mn <= 59):
            await update.message.reply_text("⚠️ Некорректное время. Часы от 0 до 23, минуты от 0 до 59.")
            return
        
        time_str = f"{h:02d}:{mn:02d}"
        set_setting(user_id, "reminder_time", time_str)
        context.user_data.clear()
        await update.message.reply_text(
            f"⏰ Время вечернего отчёта успешно изменено на *{time_str} МСК*!\n\n" + build_settings_text(user_id),
            parse_mode="Markdown",
            reply_markup=settings_inline_kb(user_id),
        )
        return

    if state == "wait_save":
        val_str = text.replace(" ", "").replace(",", ".").replace("₽", "")
        try:
            val = float(val_str)
            set_setting(user_id, "save", val)
            context.user_data.clear()
            update_excel_file(user_id)
            await update.message.reply_text(
                f"💰 Плановая копилка обновлена: *{fmt_money(val)}*\n\n" + build_settings_text(user_id),
                parse_mode="Markdown",
                reply_markup=settings_inline_kb(user_id),
            )
        except ValueError:
            await update.message.reply_text("⚠️ Введи число, например: `5000`")
        return

    if state == "wait_mand":
        parsed = parse_expense_message(text)
        if not parsed:
            await update.message.reply_text("⚠️ Напиши название и сумму через пробел, например: `Аренда 15000`")
            return
        amount, name, _ = parsed
        with get_conn() as conn:
            conn.execute("INSERT INTO mandatory (user_id, name, amount) VALUES (?, ?, ?)", (user_id, name, amount))
        context.user_data.clear()
        update_excel_file(user_id)
        await update.message.reply_text(
            f"✅ Добавлен обязательный расход: *{name} — {fmt_money(amount)}*\n\n" + build_settings_text(user_id),
            parse_mode="Markdown",
            reply_markup=settings_inline_kb(user_id),
        )
        return

    if text.startswith("+"):
        clean_text = text[1:].strip()
        tokens = clean_text.split()
        amount = None
        comment = "Пополнение"
        try:
            amount = float(tokens[0].replace(",", ".").replace("₽", "").strip())
            if len(tokens) > 1:
                comment = " ".join(tokens[1:]).capitalize()
        except (ValueError, IndexError):
            pass

        if amount is not None and amount > 0:
            with get_conn() as conn:
                conn.execute(
                    "INSERT INTO savings_topups (user_id, topup_date, amount, comment) VALUES (?, ?, ?, ?)",
                    (user_id, today_date().isoformat(), amount, comment)
                )
            update_excel_file(user_id)
            info = get_budget_info(user_id)
            await update.message.reply_text(
                f"💰 В плановую копилку зачислено: *+{fmt_money(amount)}* ({comment})!\n"
                f"💰 Всего в копилке: *{fmt_money(info['total_save_effective'])}*",
                parse_mode="Markdown",
                reply_markup=MAIN_KB,
            )
            return

    parsed = parse_expense_message(text)
    if not parsed:
        await update.message.reply_text(
            "🤔 Не понял сообщение.\n"
            "Чтобы записать трату: `450 продукты`.",
            parse_mode="Markdown",
            reply_markup=MAIN_KB,
        )
        return

    amount, category, spent_d = parsed
    ensure_start_day(user_id)
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
            (user_id, spent_d.isoformat(), amount, category),
        )

    try:
        update_excel_file(user_id, spent_d)
    except Exception as e:
        logger.warning("Не удалось автообновить Excel: %s", e)

    report_text, markup = build_day_report(user_id, spent_d)
    await update.message.reply_text(
        f"✅ Записано: *{fmt_money(amount)}* — {category} ({spent_d.strftime('%d.%m')})\n\n{report_text}",
        parse_mode="Markdown",
        reply_markup=markup or MAIN_KB,
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не указан BOT_TOKEN в файле .env")
    init_db()
    logger.info("Многопользовательский бот запущен для всех. Часовой пояс: %s, база: %s", TZ_NAME, DB_PATH)

    app = Application.builder().token(BOT_TOKEN).build()

    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(daily_reminder_job, interval=60, first=10)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("today", show_today))
    app.add_handler(CommandHandler("week", show_week))
    app.add_handler(CommandHandler("month", show_month))
    app.add_handler(CommandHandler("chart", send_chart))
    app.add_handler(CommandHandler("excel", send_excel))
    app.add_handler(CommandHandler("settings", show_settings))
    app.add_handler(CommandHandler("undo", undo_last))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    app.run_polling()


if __name__ == "__main__":
    main()