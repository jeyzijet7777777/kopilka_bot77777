import asyncio
import calendar
import datetime
import difflib
import gzip
import io
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zlib
from contextlib import contextmanager
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
from matplotlib.colors import to_rgba
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Rectangle
from matplotlib.ticker import FuncFormatter
import openpyxl

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PicklePersistence,
    filters,
)
from telegram.helpers import escape_markdown

import datasafe
import migrations
from kopilka.services.mandatory_service import (
    get_month_mandatory_status,
    record_mandatory_payment,
    find_matching_mandatory,
    remove_mandatory_payment,
)
from kopilka.services.reports_service import build_bills_card
from kopilka.keyboards.inline import bills_inline_kb, bills_history_kb
from kopilka.utils.parsers import parse_mandatory_payment_message
from kopilka.database.repository import (
    add_mandatory_payment,
    get_mandatory_payments_for_day,
    get_mandatory_payments_for_month,
    delete_mandatory_payment,
)
from kopilka.services.categorizer import (
    classify_expense,
    save_user_category_rule,
    CATEGORIES_CATALOG,
    recategorize_user_expenses,
)
from kopilka.services.receipt_scanner import (
    scan_receipt_image,
    parse_bank_notification,
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
# Постоянный том. Всё, что должно пережить пересборку контейнера, лежит здесь. Пути по умолчанию
# строятся от DATA_DIR, чтобы запуск без Dockerfile (или без переменных хостинга) не клал базу
# в рабочую папку контейнера, откуда она пропадает при пересборке.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.environ.get("DB_PATH") or os.path.join(DATA_DIR, "budget.db")
STATE_PATH = os.environ.get("STATE_PATH") or os.path.join(DATA_DIR, "bot_state.pkl")
TEMPLATE_PATH = "template.xlsx"
# Telegram ID администраторов через запятую (узнать свой ID можно у @userinfobot)
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}

TZ = ZoneInfo(TZ_NAME)
TZ_LABEL = "МСК" if TZ_NAME == "Europe/Moscow" else TZ_NAME
MAX_AMOUNT = 10_000_000

# --- Сохранность данных ---
# Задайте EXPECT_EXISTING_DATA=1 у хостинга, когда в базе уже есть данные: тогда пустая база при запуске
# считается потерей (работает, даже если потерян весь том вместе с маркером).
EXPECT_EXISTING_DATA = datasafe.env_flag(os.environ.get("EXPECT_EXISTING_DATA"))
# Локальная разработка без тома: ALLOW_EPHEMERAL_DB=1 отключает тревогу про путь вне DATA_DIR.
ALLOW_EPHEMERAL_DB = datasafe.env_flag(os.environ.get("ALLOW_EPHEMERAL_DB"))
# STRICT_DATA_PATH=1: бот не запустится, если DB_PATH не внутри DATA_DIR (вместо предупреждения админу).
STRICT_DATA_PATH = datasafe.env_flag(os.environ.get("STRICT_DATA_PATH"))
SKIP_MOUNT_CHECK = datasafe.env_flag(os.environ.get("SKIP_MOUNT_CHECK"))
STARTUP_NOTICE = datasafe.env_flag(os.environ.get("STARTUP_NOTICE"), True)

# Автоматическая резервная копия: раз в неделю в указанный день и время (часовой пояс TZ_NAME).
BACKUP_ENABLED = datasafe.env_flag(os.environ.get("BACKUP_ENABLED"), True)
BACKUP_WEEKDAY = datasafe.parse_weekday(os.environ.get("BACKUP_WEEKDAY"), 6)  # 0 = понедельник ... 6 = воскресенье
BACKUP_HOUR, BACKUP_MINUTE = datasafe.parse_hhmm(os.environ.get("BACKUP_TIME"), (3, 0))
try:
    BACKUP_KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "8")))  # сколько автокопий хранить на томе
except ValueError:
    BACKUP_KEEP = 8
BACKUP_DIR = os.environ.get("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
# Где искать базу старой раскладки (budget.db / bot_state.pkl рядом с bot.py): её переносят на том при первом
# запуске. Дополнительная папка задаётся LEGACY_DIR (например, /app, если рабочая папка у хостинга другая).
LEGACY_DIRS = [os.getcwd(), os.path.dirname(os.path.abspath(__file__)), os.environ.get("LEGACY_DIR", "")]
# Автоотчёт за прошлый месяц: 1-го числа после MONTH_REPORT_TIME (часовой пояс TZ_NAME). Если бот в этот
# момент не работал, отчёт догоняется в течение первых MONTH_REPORT_WINDOW_DAYS дней месяца (один раз).
MONTH_REPORT_ENABLED = datasafe.env_flag(os.environ.get("MONTH_REPORT_ENABLED"), True)
MONTH_REPORT_HOUR, MONTH_REPORT_MINUTE = datasafe.parse_hhmm(os.environ.get("MONTH_REPORT_TIME"), (9, 0))
try:
    MONTH_REPORT_WINDOW_DAYS = min(28, max(1, int(os.environ.get("MONTH_REPORT_WINDOW_DAYS", "3"))))
except ValueError:
    MONTH_REPORT_WINDOW_DAYS = 3
# Недельная сводка: в понедельник после WEEK_SUMMARY_TIME приходят итоги прошлой недели (пн–вс).
# Если бот в понедельник не работал, сводка догоняется в течение WEEK_SUMMARY_WINDOW_DAYS первых дней недели.
WEEK_SUMMARY_ENABLED = datasafe.env_flag(os.environ.get("WEEK_SUMMARY_ENABLED"), True)
WEEK_SUMMARY_HOUR, WEEK_SUMMARY_MINUTE = datasafe.parse_hhmm(os.environ.get("WEEK_SUMMARY_TIME"), (9, 0))
try:
    WEEK_SUMMARY_WINDOW_DAYS = min(6, max(1, int(os.environ.get("WEEK_SUMMARY_WINDOW_DAYS", "2"))))
except ValueError:
    WEEK_SUMMARY_WINDOW_DAYS = 2
# Предупреждение «потрачено 80% дневного лимита» (доля лимита, при пересечении которой бот предупреждает)
LIMIT_WARN_RATIO = 0.8
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024  # бот не может скачать файл больше 20 МБ
TG_UPLOAD_LIMIT = 49 * 1024 * 1024  # и отправить больше 50 МБ

# Состояние, которое не должно попадать в PicklePersistence (рестарт его сбрасывает, и это нормально)
STARTUP_REPORT = None
_RUNTIME = {}

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
# httpx пишет в лог полные URL запросов, а в них лежит токен бота
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("budget-bot")

# --- Цвета графиков ---
BLUE = "#2a78d6"
ORANGE = "#eb6834"
GREEN = "#1baf7a"
GREEN_D = "#16a34a"
RED = "#e34948"
GRAY = "#898781"
INK = "#1f2937"

MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

INCOME_MODES = {
    "daily": "📅 Каждый день (например, 1000₽/день)",
    "monthly": "📅 1 раз в месяц (зарплата)",
    "twice_monthly": "📅 2 раза в месяц (аванс + зп)",
    "weekly": "📅 Раз в неделю",
}
INCOME_MODE_SHORT = {
    "daily": "в день",
    "monthly": "в месяц",
    "twice_monthly": "2 раза в месяц",
    "weekly": "в неделю",
}
INCOME_UNIT_PROMPT = {
    "daily": "в день",
    "monthly": "в месяц",
    "twice_monthly": "за один раз (выплат две в месяц)",
    "weekly": "в неделю",
}


# --- База данных ---
@contextmanager
def db():
    """Соединение с коммитом и гарантированным закрытием."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Приводит базу к актуальной схеме версионными миграциями (см. migrations.py)."""
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    migrations.migrate(DB_PATH, today_date(), backup_dir=BACKUP_DIR)


def get_setting(user_id: int, key: str, default=None):
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
        return row["value"] if row else default


LIMIT_KEYS = {"daily_income", "income_mode", "save", "custom_days_in_month"}


def set_setting(user_id: int, key: str, value):
    if key in LIMIT_KEYS:
        freeze_past_limits(user_id)  # прошлые дни остаются при старом лимите
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, str(value)),
        )


# --- Вспомогательные функции ---
def today_date() -> datetime.date:
    return datetime.datetime.now(TZ).date()


def fmt_money(val: float) -> str:
    return f"{val:,.0f}".replace(",", " ") + " ₽"


def fmt_num(val: float) -> str:
    return f"{int(round(val)):,}".replace(",", " ")


def esc(s) -> str:
    """Экранирование пользовательского текста для parse_mode=Markdown."""
    return escape_markdown(str(s), version=1)


def cap(s: str) -> str:
    s = s.strip()
    return s[:1].upper() + s[1:]


_CURRENCY_TAIL_RE = re.compile(r"(?:₽|руб\.?|р\.?|rub)$", re.IGNORECASE)
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?")
_THOUSANDS_COMMA_RE = re.compile(r"\d{1,3}(?:,\d{3})+")


def parse_amount(s: str, allow_zero: bool = False):
    """Число из строки или None: «1500», «1 500», «1,000», «300₽», «1500р», «10,5».
    Отсекает inf/nan, экспоненту, отрицательные и слишком большие."""
    if not isinstance(s, str):
        return None
    t = s.replace("\u00a0", "").replace(" ", "").replace("₽", "")
    t = _CURRENCY_TAIL_RE.sub("", t)
    if _THOUSANDS_COMMA_RE.fullmatch(t):
        t = t.replace(",", "")
    else:
        t = t.replace(",", ".")
    if not _AMOUNT_RE.fullmatch(t):
        return None
    v = float(t)
    if not math.isfinite(v) or v > MAX_AMOUNT:
        return None
    if v == 0 and not allow_zero:
        return None
    return v


def merge_thousands(tokens):
    """«1 500 такси» -> «1500 такси»: склеивает группы цифр, разделённые пробелом."""
    out = []
    for tok in tokens:
        if (
            out
            and re.fullmatch(r"\d{3}(?:[.,]\d{1,2})?", tok)
            and re.fullmatch(r"\d{1,3}(?:\d{3})*", out[-1])
        ):
            out[-1] += tok
        else:
            out.append(tok)
    return out


def parse_time(text: str):
    m = re.match(r"^(\d{1,2}):(\d{2})$", text.strip())
    if not m:
        return None
    h, mn = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mn <= 59):
        return None
    return h, mn


def save_reminder_time(user_id: int, h: int, mn: int):
    set_setting(user_id, "reminder_time", f"{h:02d}:{mn:02d}")
    now = datetime.datetime.now(TZ)
    # если время на сегодня уже прошло, отчёт придёт только завтра
    if now.hour * 60 + now.minute >= h * 60 + mn:
        set_setting(user_id, "last_report", today_date().isoformat())


def ensure_start_day(user_id: int):
    if not get_setting(user_id, "start_date"):
        set_setting(user_id, "start_date", today_date().isoformat())


def get_period_start(user_id: int, ref_date: datetime.date) -> datetime.date:
    """С какого дня считаем месяц: начало месяца или день старта, если он позже."""
    ensure_start_day(user_id)
    try:
        start_d = datetime.date.fromisoformat(get_setting(user_id, "start_date"))
    except (ValueError, TypeError):
        start_d = ref_date
    return max(start_d, ref_date.replace(day=1))


# --- Архив месяцев ---
def _month_key(d: datetime.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _next_month(d: datetime.date) -> datetime.date:
    return (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)


def archive_closed_months(user_id: int):
    """Сохраняет настройки каждого завершённого месяца, у которого ещё нет снимка.
    Вызывается ПЕРЕД изменением настроек (из freeze_past_limits), при смене даты и при открытии
    «Месяцев», поэтому снимок всегда делается по тем настройкам, что действовали в месяце."""
    if get_setting(user_id, "daily_income") is None:
        return
    try:
        start = datetime.date.fromisoformat(get_setting(user_id, "start_date") or "")
    except ValueError:
        return
    this_month = today_date().replace(day=1)
    with db() as conn:
        have = {
            r["month"]
            for r in conn.execute("SELECT month FROM month_settings WHERE user_id = ?", (user_id,))
        }
    missing, cur = [], start.replace(day=1)
    while cur < this_month:
        if _month_key(cur) not in have:
            missing.append(cur)
        cur = _next_month(cur)
    if not missing:
        return

    mode = get_setting(user_id, "income_mode", "daily") or "daily"
    raw = float(get_setting(user_id, "daily_income", 1000) or 1000)
    save = float(get_setting(user_id, "save", 0) or 0)
    custom = get_setting(user_id, "custom_days_in_month")
    try:
        launch = datetime.date.fromisoformat(get_setting(0, "archive_launch") or "").replace(day=1)
    except ValueError:
        launch = this_month
    with db() as conn:
        mand = [
            [r["name"], r["amount"]]
            for r in conn.execute(
                "SELECT name, amount FROM mandatory WHERE user_id = ? ORDER BY id", (user_id,)
            )
        ]
        mand_json = json.dumps(mand, ensure_ascii=False)
        for m in missing:
            real = calendar.monthrange(m.year, m.month)[1]
            days = int(custom) if custom and str(custom).isdigit() else real
            conn.execute(
                "INSERT OR IGNORE INTO month_settings "
                "(user_id, month, income_mode, raw_income, save, days_in_month, mandatory_json, estimated) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, _month_key(m), mode, raw, save, days, mand_json, 1 if m < launch else 0),
            )
    logger.info("Архив месяцев: пользователь %s, сохранено месяцев: %s", user_id, len(missing))


# --- Бюджет ---
def get_budget_info(user_id: int, ref_date: datetime.date = None):
    if ref_date is None:
        ref_date = today_date()

    real_days = calendar.monthrange(ref_date.year, ref_date.month)[1]
    today = today_date()
    with db() as conn:
        snap_rows = {
            r["month"]: r
            for r in conn.execute("SELECT * FROM month_settings WHERE user_id = ?", (user_id,))
        }
    # завершённый месяц считаем по настройкам, действовавшим в нём, а не по сегодняшним
    snap = snap_rows.get(_month_key(ref_date)) if (ref_date.year, ref_date.month) < (today.year, today.month) else None

    if snap:
        days_in_month = int(snap["days_in_month"])
        custom_days = days_in_month if days_in_month != real_days else None
        income_mode = snap["income_mode"]
        raw_income = float(snap["raw_income"])
    else:
        custom_days = get_setting(user_id, "custom_days_in_month")
        days_in_month = int(custom_days) if custom_days and str(custom_days).isdigit() else real_days
        income_mode = get_setting(user_id, "income_mode", "daily")
        raw_income = float(get_setting(user_id, "daily_income", 1000) or 1000)

    if income_mode == "monthly":
        income = raw_income
        display_income_text = f"{fmt_money(raw_income)} 1 раз в месяц"
    elif income_mode == "twice_monthly":
        income = raw_income * 2
        display_income_text = f"{fmt_money(raw_income)} 2 раза в месяц"
    elif income_mode == "weekly":
        income = raw_income * (days_in_month / 7)
        display_income_text = f"{fmt_money(raw_income)} раз в неделю"
    else:
        income_mode = "daily"
        income = raw_income * days_in_month
        display_income_text = f"по {fmt_money(raw_income)} в день"

    current_save = float(get_setting(user_id, "save", 0) or 0)
    save_advance = float(snap["save"]) if snap else current_save

    month_prefix = f"{ref_date.year:04d}-{ref_date.month:02d}-"
    with db() as conn:
        if snap:
            mand_rows = [
                {"id": None, "name": n, "amount": a} for n, a in json.loads(snap["mandatory_json"])
            ]
        else:
            mand_rows = conn.execute(
                "SELECT id, name, amount FROM mandatory WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        topup_row = conn.execute(
            "SELECT SUM(amount) AS total FROM savings_topups "
            "WHERE user_id = ? AND topup_date LIKE ? AND amount > 0",
            (user_id, month_prefix + "%"),
        ).fetchone()
        all_topups_row = conn.execute(
            "SELECT SUM(amount) AS total FROM savings_topups WHERE user_id = ? AND topup_date <= ?",
            (user_id, ref_date.isoformat()),
        ).fetchone()

    topups_total = topup_row["total"] if topup_row and topup_row["total"] else 0.0
    all_topups = all_topups_row["total"] if all_topups_row and all_topups_row["total"] else 0.0
    # накопленная копилка: плановые отчисления за каждый месяц с начала учёта + все пополнения/снятия
    try:
        start_d = datetime.date.fromisoformat(get_setting(user_id, "start_date") or "")
    except ValueError:
        start_d = ref_date
    # плановые отчисления каждого прошедшего месяца берутся из его снимка: смена плана
    # сегодня не должна менять накопленное в прошлом
    piggy_plan, cur_m = 0.0, start_d.replace(day=1)
    while cur_m <= ref_date.replace(day=1):
        row = snap_rows.get(_month_key(cur_m))
        piggy_plan += float(row["save"]) if row else current_save
        cur_m = _next_month(cur_m)
    piggy_total = piggy_plan + all_topups
    total_save_effective = save_advance + topups_total

    mandatory_total = sum(r["amount"] for r in mand_rows)
    available = income - mandatory_total - total_save_effective
    limit_base = available / days_in_month if days_in_month else 0.0

    with db() as conn:
        snap_rows = conn.execute(
            "SELECT day, daily_limit FROM day_limits WHERE user_id = ? AND day LIKE ?",
            (user_id, month_prefix + "%"),
        ).fetchall()
    limit_snaps = {int(r["day"][-2:]): r["daily_limit"] for r in snap_rows}
    first_counted_day = 1
    try:
        sd = datetime.date.fromisoformat(get_setting(user_id, "start_date") or "")
        if (sd.year, sd.month) == (ref_date.year, ref_date.month):
            first_counted_day = sd.day
    except ValueError:
        pass
    series = _month_limit_series(limit_base, days_in_month, limit_snaps, first_counted_day, ref_date.day)
    daily_limit = series.get(ref_date.day, max(0.0, limit_base))

    return {
        "raw_income": raw_income,
        "income_mode": income_mode,
        "display_income_text": display_income_text,
        "income": income,
        "save": save_advance,
        "topups_total": topups_total,
        "total_save_effective": total_save_effective,
        "piggy_total": piggy_total,
        "mandatory_list": mand_rows,
        "mandatory_total": mandatory_total,
        "days_in_month": days_in_month,
        "real_days": real_days,
        "is_custom_days": bool(custom_days),
        "available": available,
        "daily_limit": max(0.0, daily_limit),
        "limit_base": limit_base,
        "limit_snaps": limit_snaps,
        "first_counted_day": first_counted_day,
    }


def _month_limit_series(base: float, n: int, snaps: dict, first_day: int, last_day: int) -> dict:
    """Лимит по каждому дню месяца. Дни со снимком держат старый лимит, остальные
    делят оставшийся бюджет поровну: так смена настроек посреди месяца не меняет прошлое,
    а сумма лимитов за месяц по-прежнему равна «доход − обязательные − копилка»."""
    out, delta = {}, 0.0
    for dn in range(first_day, last_day + 1):
        if dn in snaps:
            lim = snaps[dn]
        else:
            lim = max(0.0, base - delta / max(1, n - (dn - 1)))
        out[dn] = lim
        delta += lim - base
    return out


def limits_between(user_id: int, start: datetime.date, end: datetime.date) -> dict:
    """{дата: лимит} для диапазона дат (дни до начала учёта в результат не попадают)."""
    out = {}
    cur = start.replace(day=1)
    while cur <= end:
        last_dn = calendar.monthrange(cur.year, cur.month)[1]
        info = get_budget_info(user_id, cur)
        series = _month_limit_series(
            info["limit_base"], info["days_in_month"], info["limit_snaps"],
            info["first_counted_day"], last_dn,
        )
        for dn, lim in series.items():
            out[datetime.date(cur.year, cur.month, dn)] = lim
        cur = (cur.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return {d: v for d, v in out.items() if start <= d <= end}


def freeze_past_limits(user_id: int):
    """Фиксирует лимит всех прошедших дней. Вызывается ПЕРЕД любым изменением,
    влияющим на лимит (доход, копилка, обязательные расходы, число дней)."""
    archive_closed_months(user_id)  # снимок настроек закрытых месяцев — до их изменения
    raw = get_setting(user_id, "start_date")
    if not raw or get_setting(user_id, "daily_income") is None:
        return  # бюджет ещё не настроен — фиксировать нечего (иначе заморозится лимит по умолчанию)
    try:
        start = datetime.date.fromisoformat(raw)
    except ValueError:
        return
    yesterday = today_date() - datetime.timedelta(days=1)
    if start > yesterday:
        return
    lims = limits_between(user_id, start, yesterday)
    with db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO day_limits (user_id, day, daily_limit) VALUES (?, ?, ?)",
            [(user_id, d.isoformat(), v) for d, v in lims.items()],
        )


def month_limits(user_id: int, today: datetime.date, start: datetime.date) -> dict:
    """{номер дня: лимит} от начала учёта до конца текущего месяца."""
    real_days = calendar.monthrange(today.year, today.month)[1]
    lims = limits_between(user_id, start, datetime.date(today.year, today.month, real_days))
    return {d.day: v for d, v in lims.items()}


def upsert_mandatory(user_id: int, name: str, amount: float) -> bool:
    """Добавляет обязательный расход; расход с тем же названием обновляет. True, если обновлён."""
    freeze_past_limits(user_id)
    with db() as conn:
        rows = conn.execute("SELECT id, name FROM mandatory WHERE user_id = ?", (user_id,)).fetchall()
        for r in rows:
            if r["name"].strip().lower() == name.strip().lower():
                conn.execute("UPDATE mandatory SET amount = ? WHERE id = ?", (amount, r["id"]))
                return True
        conn.execute(
            "INSERT INTO mandatory (user_id, name, amount) VALUES (?, ?, ?)",
            (user_id, name, amount),
        )
    return False


def calc_change_pool(user_id: int, through_date: datetime.date) -> float:
    """Банк сдачи: лимит за все дни периода по through_date включительно минус траты."""
    period_start = get_period_start(user_id, through_date)
    if period_start > through_date:
        return 0.0
    total_limit = sum(limits_between(user_id, period_start, through_date).values())
    with db() as conn:
        row = conn.execute(
            "SELECT SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_date >= ? AND spent_date <= ?",
            (user_id, period_start.isoformat(), through_date.isoformat()),
        ).fetchone()
    spent = row["total"] or 0.0
    return total_limit - spent


def delete_expense(user_id: int, exp_id: int):
    """Удаляет трату вместе со связанным снятием из копилки. Возвращает дату траты или None."""
    with db() as conn:
        row = conn.execute(
            "SELECT spent_date FROM expenses WHERE id = ? AND user_id = ?",
            (exp_id, user_id),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "DELETE FROM savings_topups WHERE expense_id = ? AND user_id = ?",
            (exp_id, user_id),
        )
        conn.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (exp_id, user_id))
        return row["spent_date"]


# --- Категории: единое написание и подсказки ---
DEFAULT_CATEGORY = "Разное"


def cat_key(name: str) -> str:
    """Ключ сравнения категорий: без регистра, ё = е, лишние пробелы схлопнуты."""
    return " ".join(str(name).casefold().replace("ё", "е").split())


_DYNAMIC_CATEGORY_TOKENS: dict[str, str] = {}


def cat_token(name: str) -> str:
    """Короткий отпечаток категории для callback_data (лимит Telegram 64 байта, а названия длинные и русские)."""
    tok = format(zlib.crc32(cat_key(name).encode("utf-8")) & 0xFFFFFFFF, "08x")
    _DYNAMIC_CATEGORY_TOKENS[tok] = str(name).strip()
    return tok


def used_categories(user_id: int, limit: int = None, exclude=()) -> list:
    """Уже использованные категории: сначала самые частые, при равенстве — самые свежие.
    Варианты одного слова в разном регистре («продукты» и «Продукты») склеиваются в самое частое написание."""
    with db() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) AS n, MAX(spent_date) AS last FROM expenses "
            "WHERE user_id = ? GROUP BY category",
            (user_id,),
        ).fetchall()
    skip = {cat_key(x) for x in exclude}
    merged = {}
    for r in rows:
        k = cat_key(r["category"])
        if not k or k in skip:
            continue
        m = merged.setdefault(k, {"n": 0, "last": "", "names": {}})
        m["n"] += r["n"]
        m["last"] = max(m["last"], r["last"] or "")
        m["names"][r["category"]] = m["names"].get(r["category"], 0) + r["n"]
    # чаще — выше, при равенстве свежее — выше
    ordered = sorted(merged.values(), key=lambda m: (m["n"], m["last"]), reverse=True)
    names = [max(m["names"].items(), key=lambda kv: (kv[1], kv[0]))[0] for m in ordered]
    return names[:limit] if limit else names


def canonical_category(user_id: int, name: str) -> str:
    """Если такая категория уже есть (в любом регистре), берём её написание: «ПРОДУКТЫ» -> «Продукты»."""
    k = cat_key(name)
    for existing in used_categories(user_id):
        if cat_key(existing) == k:
            return existing
    return " ".join(name.split())


def similar_category(user_id: int, name: str):
    """Похожая на name уже использованная категория (опечатка, окончание) или None. Только подсказка."""
    k = cat_key(name)
    if not k or k == cat_key(DEFAULT_CATEGORY):
        return None
    known = {cat_key(c): c for c in used_categories(user_id)}
    if k in known:
        return None
    close = difflib.get_close_matches(k, list(known), n=1, cutoff=0.8)
    if close:
        return known[close[0]]
    # «продукт» / «продуктов» против «продукты»: общий корень от 5 букв
    for kk, original in known.items():
        if min(len(k), len(kk)) >= 5 and (kk.startswith(k) or k.startswith(kk)):
            return original
    return None


def find_category_by_token(user_id: int, token: str):
    for c in used_categories(user_id):
        if cat_token(c) == token:
            return c
    for c in CATEGORIES_CATALOG:
        if cat_token(c) == token:
            return c
    if token in _DYNAMIC_CATEGORY_TOKENS:
        return _DYNAMIC_CATEGORY_TOKENS[token]
    return None


def category_chips(user_id: int, exp_id: int, names, per_row: int = 2) -> list:
    """Кнопки «сменить категорию у траты exp_id на …»."""
    btns = [
        InlineKeyboardButton(f"🏷 {n}"[:30], callback_data=f"xc_{exp_id}_{cat_token(n)}")
        for n in names
    ]
    return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]


def suggestion_rows(user_id: int, exp_id: int, category: str, was_default: bool, similar=None, candidates=None) -> list:
    """Подсказки под только что записанной тратой: без категории — частые из использованных;
    новая категория, похожая на существующую (similar, найдена ДО записи), — «возможно, это …»."""
    if was_default:
        return category_chips(user_id, exp_id, used_categories(user_id, limit=6, exclude=[category]))
    if similar:
        return category_chips(user_id, exp_id, [similar], per_row=1)
    if candidates:
        return category_chips(user_id, exp_id, candidates, per_row=2)
    return []


# --- Правка трат ---
def get_expense(user_id: int, exp_id: int):
    with db() as conn:
        return conn.execute(
            "SELECT id, spent_date, amount, category FROM expenses WHERE id = ? AND user_id = ?",
            (exp_id, user_id),
        ).fetchone()


def update_expense(user_id: int, exp_id: int, *, amount=None, category=None, spent_date=None):
    """Меняет только переданные поля. Возвращает обновлённую запись или None, если траты нет."""
    sets, args = [], []
    if amount is not None:
        sets.append("amount = ?")
        args.append(float(amount))
    if category is not None:
        sets.append("category = ?")
        args.append(category)
    if spent_date is not None:
        sets.append("spent_date = ?")
        args.append(spent_date.isoformat())
    with db() as conn:
        if sets:
            conn.execute(
                f"UPDATE expenses SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
                (*args, exp_id, user_id),
            )
        return conn.execute(
            "SELECT id, spent_date, amount, category FROM expenses WHERE id = ? AND user_id = ?",
            (exp_id, user_id),
        ).fetchone()


def day_spent(user_id: int, day: datetime.date) -> float:
    with db() as conn:
        row = conn.execute(
            "SELECT SUM(amount) AS total FROM expenses WHERE user_id = ? AND spent_date = ?",
            (user_id, day.isoformat()),
        ).fetchone()
    return row["total"] or 0.0


def build_expense_card(user_id: int, exp_id: int, note: str = ""):
    row = get_expense(user_id, exp_id)
    if not row:
        return "ℹ️ Эта трата уже удалена.", None
    d = datetime.date.fromisoformat(row["spent_date"])
    text = (
        f"{note}✏️ *Редактирование траты*\n\n"
        f"💰 Сумма: *{fmt_money(row['amount'])}*\n"
        f"🏷 Категория: *{esc(row['category'])}*\n"
        f"📅 Дата: *{d.strftime('%d.%m.%Y')}*\n\n"
        "Что изменить?"
    )
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 Сумму", callback_data=f"xe_amt_{exp_id}"),
            InlineKeyboardButton("🏷 Категорию", callback_data=f"xe_cat_{exp_id}"),
            InlineKeyboardButton("📅 Дату", callback_data=f"xe_dt_{exp_id}"),
        ],
        [InlineKeyboardButton("🗑 Удалить трату", callback_data=f"hdelexp_{exp_id}")],
        [
            InlineKeyboardButton(f"📊 Отчёт за {d.strftime('%d.%m')}", callback_data=f"xe_day_{exp_id}"),
            InlineKeyboardButton("🧾 К списку трат", callback_data="xe_list"),
        ],
    ])
    return text, markup


def parse_date_text(text: str, today: datetime.date = None):
    """Дата из «вчера», «28.10», «28.10.2026». Короткая дата в будущем трактуется как прошлогодняя."""
    today = today or today_date()
    t = text.strip().lower()
    if t in DATE_WORDS:
        return today + datetime.timedelta(days=DATE_WORDS[t])
    m = DATE_FULL_RE.match(t)
    if m:
        return _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = DATE_SHORT_RE.match(t)
    if m:
        dt = _make_date(int(m.group(1)), int(m.group(2)), today.year)
        if dt and dt > today:
            dt = _make_date(int(m.group(1)), int(m.group(2)), today.year - 1)
        return dt
    return None


def limit_warning(user_id: int, day: datetime.date, before: float, after: float) -> str:
    """Предупреждение, когда трата впервые доводит день до 80% лимита (и лимит ещё не превышен).
    Превышение лимита уже показывает дневной отчёт. Только для сегодняшнего дня."""
    if get_setting(user_id, "warn_limit", "1") == "0" or day != today_date():
        return ""
    limit = get_budget_info(user_id, day)["daily_limit"]
    if limit <= 0 or after > limit + 0.005:
        return ""
    if before < limit * LIMIT_WARN_RATIO <= after + 1e-9:
        return (
            f"⚠️ *Потрачено {after / limit * 100:.0f}% дневного лимита:* "
            f"{fmt_money(after)} из {fmt_money(limit)}, осталось {fmt_money(limit - after)}.\n\n"
        )
    return ""


# --- Экспорт в Excel ---
def update_excel_file(user_id: int, target_month: datetime.date = None) -> io.BytesIO:
    if target_month is None:
        target_month = today_date()

    first_day = datetime.date(target_month.year, target_month.month, 1)
    info = get_budget_info(user_id, target_month)
    real_days = info["real_days"]

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
    if info["is_custom_days"]:
        ws["B23"] = info["days_in_month"]  # иначе Excel делит на реальные дни и расходится с ботом

    for row_idx in range(8, 18):
        ws[f"A{row_idx}"] = None
        ws[f"B{row_idx}"] = None

    mand = list(info["mandatory_list"])
    if len(mand) > 10:
        shown = mand[:9]
        rest = mand[9:]
        for idx, item in enumerate(shown):
            ws[f"A{8 + idx}"] = item["name"]
            ws[f"B{8 + idx}"] = item["amount"]
        ws["A17"] = f"Остальные обязательные ({len(rest)} шт.)"
        ws["B17"] = sum(r["amount"] for r in rest)
    else:
        for idx, item in enumerate(mand):
            ws[f"A{8 + idx}"] = item["name"]
            ws[f"B{8 + idx}"] = item["amount"]

    for row_idx in range(6, 37):
        ws[f"G{row_idx}"] = None
        ws[f"H{row_idx}"] = None

    month_prefix = f"{first_day.year:04d}-{first_day.month:02d}-"
    with db() as conn:
        rows = conn.execute(
            "SELECT spent_date, amount, category FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? ORDER BY spent_date, id",
            (user_id, month_prefix + "%"),
        ).fetchall()

    by_day = {}
    for r in rows:
        d_num = int(r["spent_date"].split("-")[2])
        by_day.setdefault(d_num, []).append((r["amount"], r["category"]))

    lims = limits_between(user_id, first_day, datetime.date(first_day.year, first_day.month, real_days))
    t_today = today_date()
    try:
        start_d = datetime.date.fromisoformat(get_setting(user_id, "start_date", t_today.isoformat()))
    except ValueError:
        start_d = t_today

    for day_num in range(1, real_days + 1):
        cur_date = datetime.date(first_day.year, first_day.month, day_num)
        row_idx = 5 + day_num
        if day_num <= info["days_in_month"]:
            ws[f"F{row_idx}"] = round(lims.get(cur_date, info["daily_limit"]), 2)
        if day_num in by_day:
            items = by_day[day_num]
            ws[f"G{row_idx}"] = sum(x[0] for x in items)
            ws[f"H{row_idx}"] = " | ".join(
                f"{cat}: {int(amt) if float(amt).is_integer() else amt}₽" for amt, cat in items
            )
        elif start_d <= cur_date < t_today:
            ws[f"G{row_idx}"] = 0
            ws[f"H{row_idx}"] = "Без трат"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# --- Клавиатуры ---
MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📊 Сегодня", "📅 Месяц"],
        ["🏠 Счета", "🏦 Копилка"],
        ["🧾 Траты", "🏷 Категории"],
        ["⚙️ Меню & Ещё", "↩️ Отменить"],
    ],
    resize_keyboard=True,
)
MENU_TEXTS = {
    b for row in MAIN_KB.keyboard for b in (x.text if hasattr(x, "text") else x for x in row)
} | {
    "🏦 В копилку",
    "💸 Из копилки",
    "📈 График",
    "📥 Скачать Excel",
    "⚙️ Настройки",
    "↩️ Отменить последнюю",
    "📚 Месяцы",
    "🏷 Категории",
    "🧾 Траты",
    "🏠 Счета",
    "⚙️ Меню & Ещё",
    "⚙️ Меню",
    "⚙️ Ещё",
    "↩️ Отменить",
}


def mode_markup(prefix: str):
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=prefix + key)] for key, label in INCOME_MODES.items()]
    )


def _chart_month(month: datetime.date = None):
    """Месяц графика: None для текущего (в том числе для ссылки на текущий месяц), иначе 1-е число прошлого месяца."""
    if month is None:
        return None
    t = today_date()
    return None if (month.year, month.month) >= (t.year, t.month) else month.replace(day=1)


def chart_kb(user_id: int = None, kind: str = "overview", month: datetime.date = None):
    """Кнопки графика. Выбранный месяц помнится в callback_data (chart_<вид>_<ГГГГ-ММ>); без него — текущий."""
    month = _chart_month(month)
    sfx = f"_{_month_key(month)}" if month else ""
    rows = [
        [
            InlineKeyboardButton("📊 Лимит и банк", callback_data=f"chart_overview{sfx}"),
            InlineKeyboardButton("🗓 Календарь", callback_data=f"chart_calendar{sfx}"),
        ],
        [
            InlineKeyboardButton("🔮 Прогноз" if not month else "🔮 Итог", callback_data=f"chart_forecast{sfx}"),
            InlineKeyboardButton("🍩 Структура", callback_data=f"chart_structure{sfx}"),
        ],
        [InlineKeyboardButton("💧 Итог месяца", callback_data=f"chart_waterfall{sfx}")],
    ]
    if user_id is not None:
        months = _list_months(user_id, limit=1200)  # от нового к старому
        cur = month or today_date().replace(day=1)
        if cur in months:
            i = months.index(cur)
            nav = []
            if i + 1 < len(months):
                older = months[i + 1]
                nav.append(InlineKeyboardButton(f"◀️ {MONTHS_RU[older.month - 1][:3]} {older.year}",
                                                callback_data=f"chart_{kind}_{_month_key(older)}"))
            if len(months) > 2:
                nav.append(InlineKeyboardButton("📅 Месяц", callback_data=f"chartm_{kind}_{_month_key(cur)}"))
            if i > 0:
                newer = months[i - 1]
                nsfx = "" if i - 1 == 0 else f"_{_month_key(newer)}"
                nav.append(InlineKeyboardButton(f"{MONTHS_RU[newer.month - 1][:3]} {newer.year} ▶️",
                                                callback_data=f"chart_{kind}{nsfx}"))
            if nav:
                rows.append(nav)
    return InlineKeyboardMarkup(rows)


def settings_inline_kb(user_id: int):
    info = get_budget_info(user_id)
    reminder_time = get_setting(user_id, "reminder_time", "21:00")
    days_label = (
        f"{info['days_in_month']} дн. (авто)"
        if not info["is_custom_days"]
        else f"{info['days_in_month']} дн. (ручное)"
    )
    chart_on = get_setting(user_id, "report_chart", "1") == "1"
    month_report_on = get_setting(user_id, "month_report", "1") != "0"
    warn_on = get_setting(user_id, "warn_limit", "1") != "0"
    week_on = get_setting(user_id, "week_summary", "1") != "0"
    auto_cat_on = get_setting(user_id, "auto_category", "1") != "0"
    mode_label = INCOME_MODE_SHORT.get(info["income_mode"], info["income_mode"])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏦 Доход: {fmt_money(info['raw_income'])} ({mode_label})", callback_data="set_daily_income")],
        [InlineKeyboardButton("🔄 Изменить график получения дохода", callback_data="set_income_mode")],
        [InlineKeyboardButton(f"📅 Дней в месяце: {days_label}", callback_data="set_days_count")],
        [InlineKeyboardButton(f"⏰ Время отчёта, {TZ_LABEL} (сейчас {reminder_time})", callback_data="set_reminder_time")],
        [InlineKeyboardButton(f"🪄 Умные категории трат: {'вкл' if auto_cat_on else 'выкл'}", callback_data="toggle_auto_category")],
        [InlineKeyboardButton("🪄 Исправить старые категории трат", callback_data="run_fix_categories")],
        [InlineKeyboardButton(f"📈 График в вечернем отчёте: {'вкл' if chart_on else 'выкл'}", callback_data="toggle_report_chart")],
        [InlineKeyboardButton(f"📅 Отчёт за месяц 1-го числа: {'вкл' if month_report_on else 'выкл'}", callback_data="toggle_month_report")],
        [InlineKeyboardButton(f"📆 Сводка за неделю по понедельникам: {'вкл' if week_on else 'выкл'}", callback_data="toggle_week_summary")],
        [InlineKeyboardButton(f"⚠️ Предупреждение на 80% лимита: {'вкл' if warn_on else 'выкл'}", callback_data="toggle_warn_limit")],
        [InlineKeyboardButton("💰 Плановая копилка", callback_data="set_save")],
        [InlineKeyboardButton("🏦 История копилки", callback_data="piggy_history")],
        [
            InlineKeyboardButton("➕ Добавить обяз. расход", callback_data="add_mand"),
            InlineKeyboardButton("🗑 Удалить обяз. расход", callback_data="del_mand_menu"),
        ],
        [InlineKeyboardButton("🔄 Запустить мастера настройки", callback_data="restart_wizard")],
        [InlineKeyboardButton("🧹 Сбросить текущий ввод", callback_data="restart_bot_action")],
        [InlineKeyboardButton("⚠️ Полный сброс всех данных", callback_data="full_reset_action")],
    ])


def build_settings_text(user_id: int) -> str:
    ensure_start_day(user_id)
    info = get_budget_info(user_id)
    mand_lines = [f"  • {esc(r['name'])}: {fmt_money(r['amount'])}" for r in info["mandatory_list"]]
    mand_block = "\n".join(mand_lines) if mand_lines else "  (пока не добавлены)"

    topup_str = f" (+{fmt_money(info['topups_total'])} доп.)" if info["topups_total"] > 0 else ""
    reminder_time = get_setting(user_id, "reminder_time", "21:00")
    days_mode = "авто по календарю" if not info["is_custom_days"] else "ручной выбор"

    text = (
        "⚙️ *Настройки бюджета (🏦 Умная модель)*\n\n"
        f"🏦 *Доход:* {fmt_money(info['raw_income'])} ({info['display_income_text']})\n"
        f"📅 *Дней в месяце:* {info['days_in_month']} ({days_mode})\n"
        f"⏰ *Вечерний авто-отчёт:* в {reminder_time} ({TZ_LABEL})\n"
        f"🏠 *Обязательные расходы ({fmt_money(info['mandatory_total'])}):*\n{mand_block}\n\n"
        f"💰 *Плановая копилка:* {fmt_money(info['save'])}{topup_str}\n"
        f"🏦 *Копилка (всего накоплено):* {fmt_money(info['piggy_total'])}\n"
        f"🎯 *Чистый лимит на день:* *{fmt_money(info['daily_limit'])}*\n"
    )
    if info["available"] < 0:
        text += (
            f"\n⚠️ Обязательные расходы и копилка больше дохода на "
            f"*{fmt_money(-info['available'])}*, поэтому лимит на день равен 0.\n"
        )
    return text + "\n👇 Нажмите нужную кнопку для изменения:"


def build_day_report(user_id: int, target_date: datetime.date, extra_rows=None):
    info = get_budget_info(user_id, target_date)
    with db() as conn:
        rows = conn.execute(
            "SELECT id, amount, category FROM expenses WHERE user_id = ? AND spent_date = ? ORDER BY id",
            (user_id, target_date.isoformat()),
        ).fetchall()

    spent = sum(r["amount"] for r in rows)
    rem = info["daily_limit"] - spent
    change_pool = calc_change_pool(user_id, target_date)

    date_str = target_date.strftime("%d.%m.%Y")
    title = (
        f"📊 *Траты за сегодня ({date_str})*"
        if target_date == today_date()
        else f"📊 *Траты за {date_str}*"
    )

    if rows:
        exp_text = "\n".join(
            f"{i + 1}. *{fmt_money(r['amount'])}* — {esc(r['category'])}" for i, r in enumerate(rows)
        )
    else:
        exp_text = "_Пока нет записанных трат_"

    if rem < 0:
        status_line = f"⚠️ *ЛИМИТ ПРЕВЫШЕН на {fmt_money(-rem)}!*"
        if change_pool >= 0:
            advice = "💡 Перерасход покрыт накопленной сдачей."
        else:
            advice = "💡 Банк сдачи ушёл в минус. Попробуйте завтра потратить меньше."
    else:
        status_line = f"✅ *Остаток сдачи за день:* +{fmt_money(rem)}"
        advice = f"💡 Сдача (+{fmt_money(rem)}) добавлена в ваш банк сдачи."

    base_daily_income = info["income"] / info["days_in_month"] if info["days_in_month"] else 0.0

    text = (
        f"{title}\n\n"
        f"{exp_text}\n\n"
        f"🏦 *Доход за день (расчётный):* +{fmt_money(base_daily_income)}\n"
        f"💸 *Потрачено за день:* {fmt_money(spent)} из {fmt_money(info['daily_limit'])}\n"
        f"{status_line}\n\n"
        f"{advice}\n\n"
        f"🪙 *Банк сдачи:* {fmt_money(change_pool)}\n"
        f"💰 *Копилка (всего):* {fmt_money(info['piggy_total'])}"
    )
    if info["available"] < 0:
        text += (
            f"\n\n⚠️ Обязательные расходы и копилка больше дохода на "
            f"{fmt_money(-info['available'])}. Лимит на день: 0 ₽."
        )

    day_mand = get_mandatory_payments_for_day(user_id, target_date.isoformat())
    if day_mand:
        mand_spent = sum(p["amount"] for p in day_mand)
        items_str = ", ".join(f"{esc(p.get('mandatory_name') or 'Счёт')} ({fmt_money(p['amount'])})" for p in day_mand)
        text += (
            f"\n\n🏠 *Оплачено счетов за день ({fmt_money(mand_spent)}):*\n"
            f"  • {items_str}\n"
            f"  _(не вычитается из дневного лимита)_"
        )

    kb_rows = []
    for r in rows:
        btn_title = f"✏️ {r['category']} ({int(r['amount'])}₽)"
        if len(btn_title) > 32:
            btn_title = btn_title[:29] + "..."
        kb_rows.append([
            InlineKeyboardButton(btn_title, callback_data=f"xe_open_{r['id']}"),
            InlineKeyboardButton("❌", callback_data=f"delexp_{r['id']}"),
        ])
    kb_rows.extend(extra_rows or [])
    markup = InlineKeyboardMarkup(kb_rows) if kb_rows else None
    return text, markup


# --- Разбор сообщений ---
DATE_WORDS = {"сегодня": 0, "вчера": -1, "позавчера": -2}
DATE_FULL_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4})$")
DATE_SHORT_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})$")


def _make_date(d: int, m: int, y: int):
    if y < 100:
        y += 2000
    try:
        return datetime.date(y, m, d)
    except ValueError:
        return None


def parse_expense_message(text: str):
    """«450 продукты», «такси 300 вчера», «450 кофе 28.10». Возвращает (сумма, категория, дата)."""
    tokens = merge_thousands(text.strip().split())
    if not tokens:
        return None

    base = today_date()
    target = base
    rest = []

    for tok in tokens:
        low = tok.lower()
        if low in DATE_WORDS:
            target = base + datetime.timedelta(days=DATE_WORDS[low])
            continue
        m = DATE_FULL_RE.match(tok)
        if m:
            dt = _make_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if dt:
                target = dt
                continue
        rest.append(tok)

    def short_date(tok):
        m = DATE_SHORT_RE.match(tok)
        if not m:
            return None
        dt = _make_date(int(m.group(1)), int(m.group(2)), base.year)
        if dt and dt > base:
            dt = _make_date(int(m.group(1)), int(m.group(2)), base.year - 1)
        # «1.5» в «2 молоко 1.5» — скорее число, чем дата многомесячной давности
        if dt and 0 <= (base - dt).days <= 45:
            return dt
        return None

    # «10.5» это дата, только если в сообщении есть ещё и обычная сумма; иначе это 10,5 ₽
    if any(parse_amount(t) is not None and short_date(t) is None for t in rest):
        remaining = []
        for tok in rest:
            sd = short_date(tok)
            if sd:
                target = sd
            else:
                remaining.append(tok)
        rest = remaining

    if not rest:
        return None

    first = parse_amount(rest[0])
    last = parse_amount(rest[-1]) if len(rest) > 1 else None

    if first is not None:
        amount, cat_tokens = first, rest[1:]
    elif last is not None:
        amount, cat_tokens = last, rest[:-1]
    else:
        return None

    category = cap(" ".join(cat_tokens)) or "Разное"
    return amount, category[:60], target


def parse_label_amount(text: str):
    """«Аренда 15000». Возвращает (сумма, название) без разбора дат."""
    tokens = merge_thousands(text.strip().split())
    if not tokens:
        return None
    first = parse_amount(tokens[0])
    last = parse_amount(tokens[-1]) if len(tokens) > 1 else None
    if first is not None:
        amount, name_tokens = first, tokens[1:]
    elif last is not None:
        amount, name_tokens = last, tokens[:-1]
    else:
        return None
    return amount, (cap(" ".join(name_tokens)) or "Разное")[:60]


def parse_amount_comment(text: str, default_comment: str):
    """«500 Подработка». Возвращает (сумма, комментарий) или None."""
    tokens = merge_thousands(text.strip().split())
    if not tokens:
        return None
    amount = parse_amount(tokens[0])
    if amount is None:
        return None
    comment = cap(" ".join(tokens[1:])) if len(tokens) > 1 else default_comment
    return amount, comment[:60]


# --- Графики ---
def collect_month(user_id: int, month: datetime.date = None):
    """Данные месяца для графиков. Для прошлого месяца «сегодня» — его последний день."""
    today = today_date()
    month = _chart_month(month)
    if month:
        today = month.replace(day=calendar.monthrange(month.year, month.month)[1])
    info = get_budget_info(user_id, today)
    start = get_period_start(user_id, today)
    prefix = f"{today.year:04d}-{today.month:02d}-"
    with db() as conn:
        day_rows = conn.execute(
            "SELECT spent_date, SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY spent_date",
            (user_id, prefix + "%"),
        ).fetchall()
        cat_rows = conn.execute(
            "SELECT category, SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY category ORDER BY total DESC",
            (user_id, prefix + "%"),
        ).fetchall()
    by_day = {int(r["spent_date"].split("-")[2]): r["total"] for r in day_rows}
    return today, info, start, by_day, cat_rows


def _to_buf(fig) -> io.BytesIO:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="white")
    buf.seek(0)
    return buf


def _style(ax):
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.grid(axis="y", linestyle=":", alpha=0.5)
    ax.set_axisbelow(True)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_num(v)))


def _is_past(day: datetime.date) -> bool:
    """True, если график строится за уже закончившийся месяц."""
    t = today_date()
    return (day.year, day.month) < (t.year, t.month)


def _xticks(ax, days):
    step = max(1, len(days) // 12)
    ax.set_xticks(days[::step])


def chart_overview(user_id: int, month: datetime.date = None) -> io.BytesIO:
    today, info, start, by_day, _ = collect_month(user_id, month)
    limit = info["daily_limit"]
    days = list(range(start.day, today.day + 1)) or [today.day]
    spent = [by_day.get(d, 0.0) for d in days]
    lm = month_limits(user_id, today, start)
    lim_d = [lm.get(d, limit) for d in days]

    fig = Figure(figsize=(8, 6.4))
    ax1, ax2 = fig.subplots(2, 1)

    ax1.bar(days, spent, color=[RED if s > l else BLUE for s, l in zip(spent, lim_d)], width=0.7)
    ax1.step(days, lim_d, where="mid", color=GREEN, linestyle="--", linewidth=2)
    ax1.set_title("Траты по дням и дневной лимит, ₽", fontsize=11, fontweight="bold", loc="left")
    ax1.legend(
        handles=[
            Patch(facecolor=BLUE, label="В лимите"),
            Patch(facecolor=RED, label="Перерасход"),
            Line2D([0], [0], color=GREEN, linestyle="--", linewidth=2, label=f"Лимит {fmt_num(limit)} ₽"),
        ],
        frameon=False,
        fontsize=9,
        ncol=3,
        loc="upper left",
        bbox_to_anchor=(0, 1.0),
    )
    _style(ax1)
    _xticks(ax1, days)

    cum, run = [], 0.0
    for s, l in zip(spent, lim_d):
        run += l - s
        cum.append(run)
    ax2.axhline(0, color=GRAY, linewidth=1)
    ax2.fill_between(days, cum, 0, where=[c >= 0 for c in cum], color=GREEN, alpha=0.18, interpolate=True)
    ax2.fill_between(days, cum, 0, where=[c < 0 for c in cum], color=RED, alpha=0.18, interpolate=True)
    ax2.plot(days, cum, color=BLUE, linewidth=2, marker="o", markersize=3)
    sign = "+" if cum[-1] >= 0 else "−"
    ax2.set_title(
        f"Банк сдачи нарастающим итогом: {sign}{fmt_num(abs(cum[-1]))} ₽",
        fontsize=11, fontweight="bold", loc="left",
    )
    _style(ax2)
    _xticks(ax2, days)
    ax2.set_xlabel("День месяца", color=GRAY)

    fig.tight_layout()
    return _to_buf(fig)


def chart_calendar(user_id: int, month: datetime.date = None) -> io.BytesIO:
    today, info, start, by_day, _ = collect_month(user_id, month)
    limit = info["daily_limit"]
    weeks = calendar.monthcalendar(today.year, today.month)
    n = len(weeks)
    lm = month_limits(user_id, today, start)

    fig = Figure(figsize=(7.6, 2.0 + 0.95 * n))
    ax = fig.add_subplot(111)
    ax.set_xlim(0, 7)
    ax.set_ylim(n + 0.8, -0.8)
    ax.axis("off")

    for c, name in enumerate(["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]):
        ax.text(c + 0.5, -0.35, name, ha="center", va="center", fontsize=10, color=GRAY)

    ok_days = active_days = 0
    saved = 0.0
    for r, week in enumerate(weeks):
        for c, d in enumerate(week):
            if d == 0:
                continue
            dt = datetime.date(today.year, today.month, d)
            label = ""
            if start <= dt <= today:
                lim_d = lm.get(d, limit)
                diff = lim_d - by_day.get(d, 0.0)
                active_days += 1
                saved += diff
                if diff >= 0:
                    ok_days += 1
                intensity = min(abs(diff) / lim_d, 1.0) if lim_d > 0 else 1.0
                face = to_rgba(GREEN_D if diff >= 0 else RED, 0.22 + 0.55 * intensity)
                label = f"{'+' if diff >= 0 else '−'}{fmt_num(abs(diff))}"
            else:
                face = to_rgba("#9ca3af", 0.12)
            is_today = dt == today_date()
            ax.add_patch(Rectangle(
                (c + 0.04, r + 0.04), 0.92, 0.92,
                facecolor=face,
                edgecolor=INK if is_today else "none",
                linewidth=1.8 if is_today else 0,
            ))
            ax.text(c + 0.12, r + 0.22, str(d), fontsize=9, color="#374151", va="center")
            if label:
                ax.text(c + 0.5, r + 0.63, label, ha="center", va="center",
                        fontsize=10, fontweight="bold", color=INK)

    streak, cur = 0, today
    while cur >= start and by_day.get(cur.day, 0.0) <= lm.get(cur.day, limit):
        streak += 1
        cur -= datetime.timedelta(days=1)

    sign = "+" if saved >= 0 else "−"
    ax.text(
        3.5, n + 0.4,
        f"Серия в лимите: {streak} дн.    В лимите: {ok_days} из {active_days} дн.    "
        f"Итог: {sign}{fmt_num(abs(saved))} ₽",
        ha="center", va="center", fontsize=10, color=INK,
    )
    fig.suptitle(
        f"{MONTHS_RU[today.month - 1]} {today.year}: отклонение от лимита {fmt_num(limit)} ₽/день",
        fontsize=12, fontweight="bold",
    )
    return _to_buf(fig)


def chart_forecast(user_id: int, month: datetime.date = None) -> io.BytesIO:
    today, info, start, by_day, _ = collect_month(user_id, month)
    limit = info["daily_limit"]
    horizon = max(info["days_in_month"], today.day)
    days_all = list(range(start.day, horizon + 1)) or [today.day]
    lm = month_limits(user_id, today, start)
    limit_line, acc = [], 0.0
    for d in days_all:
        acc += lm.get(d, limit)
        limit_line.append(acc)

    fact_days = list(range(start.day, today.day + 1)) or [today.day]
    cum, run = [], 0.0
    for d in fact_days:
        run += by_day.get(d, 0.0)
        cum.append(run)
    spent_total = cum[-1]
    n_elapsed = max(1, today.day - start.day + 1)
    avg = spent_total / n_elapsed

    past = _is_past(today)
    # у закончившегося месяца прогнозировать нечего: показываем итог
    fc_days = [today.day] if past else list(range(today.day, horizon + 1))
    fc = [spent_total + avg * (d - today.day) for d in fc_days]
    rest = limit_line[-1] - (spent_total if past else fc[-1])
    fc_color = BLUE if rest >= 0 else RED

    fig = Figure(figsize=(8, 4.8))
    ax = fig.add_subplot(111)
    ax.plot(days_all, limit_line, color=GRAY, linewidth=2)
    ax.fill_between(fact_days, cum, color=BLUE, alpha=0.10)
    ax.plot(fact_days, cum, color=BLUE, linewidth=2)
    if not past:
        ax.plot(fc_days, fc, color=fc_color, linewidth=2, linestyle="--")
    ax.scatter([today.day], [spent_total], s=50, color=BLUE, zorder=5, edgecolor="white", linewidth=1.5)
    if len(fc_days) > 1:
        ax.scatter([fc_days[-1]], [fc[-1]], s=50, color=fc_color, zorder=5, edgecolor="white", linewidth=1.5)
    ax.set_ylim(bottom=0)
    ax.set_xlim(days_all[0] - 0.5, days_all[-1] + 0.5)
    _style(ax)
    ax.set_xlabel("День месяца", color=GRAY)
    handles = [Line2D([0], [0], color=BLUE, linewidth=2, label="Факт")]
    if not past:
        handles.append(Line2D([0], [0], color=fc_color, linewidth=2, linestyle="--", label="Прогноз"))
    handles.append(Line2D([0], [0], color=GRAY, linewidth=2, label="Допустимо"))
    ax.legend(handles=handles, frameon=False, fontsize=9, ncol=3, loc="upper left")
    if past:
        title = (
            f"Итог: потрачено {fmt_num(spent_total)} из {fmt_num(limit_line[-1])} ₽, "
            f"{'сдача' if rest >= 0 else 'перерасход'} {fmt_num(abs(rest))} ₽"
        )
    else:
        verdict = "останется ≈" if rest >= 0 else "перерасход ≈"
        title = f"Прогноз: при темпе {fmt_num(avg)} ₽/день к {horizon}-му {verdict} {fmt_num(abs(rest))} ₽"
    ax.set_title(title, fontsize=11, fontweight="bold", loc="left")
    fig.tight_layout()
    return _to_buf(fig)


def chart_structure(user_id: int, month: datetime.date = None) -> io.BytesIO:
    today, info, start, by_day, cats = collect_month(user_id, month)
    fig = Figure(figsize=(9.5, 4.6))
    ax1, ax2 = fig.subplots(1, 2, gridspec_kw={"width_ratios": [1, 1.25]})

    parts = [
        ("Обязательные", info["mandatory_total"], BLUE),
        ("Копилка", max(0.0, info["total_save_effective"]), GREEN),
        ("Свободные деньги", max(0.0, info["available"]), ORANGE),
    ]
    parts = [p for p in parts if p[1] > 0]
    if parts:
        ax1.pie(
            [p[1] for p in parts],
            colors=[p[2] for p in parts],
            startangle=90,
            counterclock=False,
            wedgeprops={"width": 0.34, "edgecolor": "white", "linewidth": 2},
        )
        ax1.text(0, 0, f"{fmt_num(info['daily_limit'])} ₽\nв день",
                 ha="center", va="center", fontsize=13, fontweight="bold")
        ax1.legend(
            handles=[Patch(facecolor=c, label=f"{n}: {fmt_num(v)} ₽") for n, v, c in parts],
            loc="upper center", bbox_to_anchor=(0.5, 0.02), frameon=False, fontsize=9,
        )
    else:
        ax1.axis("off")
        ax1.text(0.5, 0.5, "Нет данных", ha="center", va="center", transform=ax1.transAxes)
    title1 = "Структура дохода"
    if info["available"] < 0:
        title1 += f" (дефицит {fmt_num(-info['available'])} ₽)"
    ax1.set_title(title1, fontsize=11, fontweight="bold")

    top = [(r["category"], r["total"]) for r in cats[:7]]
    other = sum(r["total"] for r in cats[7:])
    if other > 0:
        top.append(("Прочее", other))
    if top:
        names = [n if len(n) <= 22 else n[:21] + "…" for n, _ in top]
        vals = [v for _, v in top]
        ax2.barh(range(len(vals)), vals, color=BLUE, height=0.6)
        ax2.set_yticks(range(len(vals)))
        ax2.set_yticklabels(names, fontsize=10)
        ax2.invert_yaxis()
        ax2.set_xlim(0, max(vals) * 1.2)
        for i, v in enumerate(vals):
            ax2.text(v + max(vals) * 0.015, i, fmt_num(v), va="center", fontsize=9, color=INK)
        for side in ("top", "right"):
            ax2.spines[side].set_visible(False)
        ax2.xaxis.set_major_formatter(FuncFormatter(lambda v, _: fmt_num(v)))
        ax2.grid(axis="x", linestyle=":", alpha=0.5)
        ax2.set_axisbelow(True)
    else:
        ax2.axis("off")
        ax2.text(0.5, 0.5, "Трат за этот месяц нет", ha="center", va="center", transform=ax2.transAxes)
    ax2.set_title("Куда уходят траты, ₽", fontsize=11, fontweight="bold", loc="left")

    fig.tight_layout()
    return _to_buf(fig)


def chart_waterfall(user_id: int, month: datetime.date = None) -> io.BytesIO:
    today, info, start, by_day, _ = collect_month(user_id, month)
    spent = sum(by_day.values())
    steps = [
        ("Обязательные", info["mandatory_total"]),
        ("Копилка", max(0.0, info["total_save_effective"])),
        ("Траты", spent),
    ]
    income = info["income"]
    final = income - sum(v for _, v in steps)

    fig = Figure(figsize=(8, 4.8))
    ax = fig.add_subplot(111)
    labels = ["Доход"] + [n for n, _ in steps] + ["Остаток"]
    ax.bar(0, income, color=BLUE, width=0.6)
    ax.text(0, income, fmt_num(income), ha="center", va="bottom", fontsize=10, fontweight="bold")

    level = income
    for i, (_, v) in enumerate(steps, start=1):
        new_level = level - v
        ax.bar(i, abs(level - new_level), bottom=min(level, new_level), color=ORANGE, width=0.6)
        ax.text(i, max(level, new_level), f"−{fmt_num(v)}", ha="center", va="bottom",
                fontsize=10, fontweight="bold")
        level = new_level

    last = len(labels) - 1
    ax.bar(last, abs(final), bottom=min(0, final), color=GREEN if final >= 0 else RED, width=0.6)
    ax.text(last, max(0, final), f"{'' if final >= 0 else '−'}{fmt_num(abs(final))}",
            ha="center", va="bottom", fontsize=10, fontweight="bold")
    ax.axhline(0, color=GRAY, linewidth=1)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels)
    _style(ax)
    top = max(income, 1)
    ax.set_ylim(min(0, final) - top * 0.05, top * 1.12)
    ax.set_title(
        f"{MONTHS_RU[today.month - 1]}: от дохода до остатка (на {today.strftime('%d.%m')}), ₽",
        fontsize=11, fontweight="bold", loc="left",
    )
    fig.tight_layout()
    return _to_buf(fig)


CHARTS = {
    "overview": (chart_overview, "📊 Траты по дням и банк сдачи"),
    "calendar": (chart_calendar, "🗓 Календарь месяца: зелёный день в лимите, красный перерасход"),
    "forecast": (chart_forecast, "🔮 Прогноз на конец месяца по текущему темпу трат"),
    "structure": (chart_structure, "🍩 Структура дохода и категории трат"),
    "waterfall": (chart_waterfall, "💧 От дохода до остатка"),
}


def build_chart(kind: str, user_id: int, month: datetime.date = None) -> io.BytesIO:
    return CHARTS[kind][0](user_id, month)


async def send_chart_kind(message, user_id: int, kind: str, edit: bool = False, month: datetime.date = None):
    month = _chart_month(month)
    try:
        buf = await asyncio.to_thread(build_chart, kind, user_id, month)
    except Exception:
        logger.exception("Ошибка построения графика %s", kind)
        await message.reply_text("⚠️ Не удалось построить график.", reply_markup=MAIN_KB)
        return

    caption = CHARTS[kind][1] + (f"\n📅 {_month_title(month)}" if month else "")
    markup = chart_kb(user_id, kind, month)
    if edit:
        try:
            await message.edit_media(media=InputMediaPhoto(media=buf, caption=caption), reply_markup=markup)
            return
        except TelegramError as e:
            if "not modified" in str(e).lower():
                return
            buf.seek(0)
    await message.reply_photo(photo=buf, caption=caption, reply_markup=markup)


# --- Фоновая задача: вечерний отчёт ---
async def daily_reminder_job(context: ContextTypes.DEFAULT_TYPE):
    now_dt = datetime.datetime.now(TZ)
    t_date = now_dt.date()
    now_min = now_dt.hour * 60 + now_dt.minute

    with db() as conn:
        user_rows = conn.execute(
            "SELECT DISTINCT user_id FROM settings WHERE key = 'daily_income'"
        ).fetchall()

    # раз в сутки (и после перезапуска бота) сохраняем настройки закрывшегося месяца
    if context.application.bot_data.get("archived_on") != t_date.isoformat():
        for r in user_rows:
            try:
                archive_closed_months(r["user_id"])
            except Exception:
                logger.exception("Не удалось архивировать месяцы пользователя %s", r["user_id"])
        context.application.bot_data["archived_on"] = t_date.isoformat()

    for r in user_rows:
        uid = r["user_id"]
        parsed = parse_time(get_setting(uid, "reminder_time", "21:00") or "21:00") or (21, 0)
        target_min = parsed[0] * 60 + parsed[1]

        # отчёт уходит после целевого времени, если в этот день ещё не отправлялся
        # (так он придёт и после простоя бота, а не потеряется)
        if now_min < target_min:
            continue
        if get_setting(uid, "last_report") == t_date.isoformat():
            continue
        if get_setting(uid, "blocked") == "1":
            continue

        sent = False
        try:
            text, markup = build_day_report(uid, t_date)
            header = "⏰ *Ваш автоматический вечерний отчёт за день!*\n\n"
            await context.bot.send_message(
                chat_id=uid,
                text=header + text,
                parse_mode="Markdown",
                reply_markup=markup or MAIN_KB,
            )
            sent = True
            set_setting(uid, "last_report", t_date.isoformat())  # только после успешной отправки
            logger.info("Вечерний отчёт отправлен пользователю %s", uid)
        except Forbidden:
            logger.info("Пользователь %s заблокировал бота — отчёты приостановлены", uid)
            set_setting(uid, "last_report", t_date.isoformat())
            set_setting(uid, "blocked", "1")
        except Exception as e:
            fails = context.application.bot_data.setdefault("report_fails", {})
            day, cnt = fails.get(uid, (None, 0))
            cnt = cnt + 1 if day == t_date.isoformat() else 1
            fails[uid] = (t_date.isoformat(), cnt)
            logger.error("Не удалось отправить отчёт пользователю %s (попытка %s): %s", uid, cnt, e)
            if cnt >= 5:  # не долбим бесконечно
                set_setting(uid, "last_report", t_date.isoformat())

        if sent and get_setting(uid, "report_chart", "1") == "1":
            try:
                buf = await asyncio.to_thread(build_chart, "overview", uid)
                await context.bot.send_photo(
                    chat_id=uid, photo=buf, caption=CHARTS["overview"][1], reply_markup=chart_kb(uid, "overview")
                )
            except Exception as e:
                logger.error("Не удалось отправить график пользователю %s: %s", uid, e)
        await asyncio.sleep(0.05)


# --- Автоотчёт за прошлый месяц ---
def prev_month_first(d: datetime.date) -> datetime.date:
    return (d.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)


def month_report_window_open(now_dt: datetime.datetime) -> bool:
    """1-го числа отчёт уходит после MONTH_REPORT_TIME; со 2-го дня и до конца окна догоняется без привязки к часу."""
    day = now_dt.day
    if day > MONTH_REPORT_WINDOW_DAYS:
        return False
    if day == 1:
        return (now_dt.hour, now_dt.minute) >= (MONTH_REPORT_HOUR, MONTH_REPORT_MINUTE)
    return True


def month_report_pending(uid: int, month_first: datetime.date) -> bool:
    """Нужно ли пользователю отправить отчёт за month_first. Только читает данные."""
    if get_setting(uid, "month_report", "1") == "0" or get_setting(uid, "blocked") == "1":
        return False
    if get_setting(uid, "month_report_sent") == _month_key(month_first):
        return False
    try:
        start = datetime.date.fromisoformat(get_setting(uid, "start_date") or "")
    except ValueError:
        return False
    return month_first >= start.replace(day=1)  # учёт начался не позже этого месяца


async def send_monthly_reports(bot, now_dt: datetime.datetime, bot_data: dict) -> int:
    """Шлёт каждому настроенному пользователю итоги прошлого месяца (один раз на месяц).
    Расчёты те же, что в «Истории месяцев» (build_month_detail). В базу пишется только отметка
    month_report_sent в таблицу settings (схема не меняется)."""
    if not MONTH_REPORT_ENABLED or not month_report_window_open(now_dt):
        return 0
    month_first = prev_month_first(now_dt.date())
    key = _month_key(month_first)
    with db() as conn:
        uids = [r["user_id"] for r in conn.execute(
            "SELECT DISTINCT user_id FROM settings WHERE key = 'daily_income' AND user_id != 0"
        )]
    sent_count = 0
    for uid in uids:
        if not month_report_pending(uid, month_first):
            continue
        fails = bot_data.setdefault("month_report_fails", {})
        day, cnt = fails.get(uid, (None, 0))
        if day == now_dt.date().isoformat() and cnt >= 5:
            continue  # на сегодня хватит попыток, завтра ещё раз (пока открыто окно)
        try:
            archive_closed_months(uid)  # снимок настроек этого месяца должен существовать до расчёта
            text, markup = build_month_detail(uid, month_first)
            header = "📅 *Ваш автоматический отчёт за прошлый месяц*\n\n"
            await bot.send_message(
                chat_id=uid, text=header + text, parse_mode="Markdown", reply_markup=markup or MAIN_KB,
            )
        except Forbidden:
            logger.info("Пользователь %s заблокировал бота — отчёты приостановлены", uid)
            set_setting(uid, "month_report_sent", key)
            set_setting(uid, "blocked", "1")
        except Exception as e:
            cnt = cnt + 1 if day == now_dt.date().isoformat() else 1
            fails[uid] = (now_dt.date().isoformat(), cnt)
            logger.error("Не удалось отправить отчёт за месяц пользователю %s (попытка %s): %s", uid, cnt, e)
        else:
            set_setting(uid, "month_report_sent", key)  # только после успешной отправки
            sent_count += 1
            logger.info("Отчёт за %s отправлен пользователю %s", key, uid)
        await asyncio.sleep(0.05)
    return sent_count


async def monthly_report_job(context: ContextTypes.DEFAULT_TYPE):
    await send_monthly_reports(context.bot, datetime.datetime.now(TZ), context.application.bot_data)


# --- Недельная сводка ---
WEEKDAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def week_monday(d: datetime.date) -> datetime.date:
    return d - datetime.timedelta(days=d.weekday())


def week_summary_window_open(now_dt: datetime.datetime) -> bool:
    """В понедельник сводка уходит после WEEK_SUMMARY_TIME; во вторник и далее (до конца окна) догоняется."""
    wd = now_dt.weekday()
    if wd >= WEEK_SUMMARY_WINDOW_DAYS:
        return False
    if wd == 0:
        return (now_dt.hour, now_dt.minute) >= (WEEK_SUMMARY_HOUR, WEEK_SUMMARY_MINUTE)
    return True


def week_summary_pending(uid: int, week_start: datetime.date) -> bool:
    """Нужна ли пользователю сводка за неделю, начавшуюся week_start. Только читает данные."""
    if get_setting(uid, "week_summary", "1") == "0" or get_setting(uid, "blocked") == "1":
        return False
    if get_setting(uid, "week_summary_sent") == week_start.isoformat():
        return False
    try:
        start = datetime.date.fromisoformat(get_setting(uid, "start_date") or "")
    except ValueError:
        return False
    return start <= week_start + datetime.timedelta(days=6)  # учёт начался не позже конца той недели


def _week_totals(user_id: int, start: datetime.date, end: datetime.date):
    """(потрачено, сумма лимитов, {дата: потрачено}, {дата: лимит}) за [start, end]; дни до начала учёта не считаются."""
    lims = limits_between(user_id, start, end) if start <= end else {}
    with db() as conn:
        rows = conn.execute(
            "SELECT spent_date, SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_date >= ? AND spent_date <= ? GROUP BY spent_date",
            (user_id, start.isoformat(), end.isoformat()),
        ).fetchall()
    by_day = {datetime.date.fromisoformat(r["spent_date"]): r["total"] for r in rows}
    return sum(by_day.values()), sum(lims.values()), by_day, lims


def build_week_summary(user_id: int, week_start: datetime.date):
    """Итоги недели пн–вс. None, если за неделю не записано ни одной траты (тогда сводку не шлём)."""
    week_end = week_start + datetime.timedelta(days=6)
    try:
        acc_start = datetime.date.fromisoformat(get_setting(user_id, "start_date") or "")
    except ValueError:
        acc_start = week_start
    start = max(week_start, acc_start)
    spent, total_limit, by_day, lims = _week_totals(user_id, start, week_end)
    if not by_day:
        return None
    with db() as conn:
        top = conn.execute(
            "SELECT category, SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_date >= ? AND spent_date <= ? "
            "GROUP BY category ORDER BY total DESC LIMIT 3",
            (user_id, week_start.isoformat(), week_end.isoformat()),
        ).fetchall()

    def dm(d):
        return d.strftime("%d.%m")

    lines = [f"📆 *Итоги недели {dm(week_start)} – {dm(week_end)}*", ""]
    lines.append(f"💸 *Потрачено:* {fmt_money(spent)} из {fmt_money(total_limit)} лимита")
    diff = total_limit - spent
    lines.append(
        f"🪙 *Итог недели:* сдача +{fmt_money(diff)}" if diff >= 0 else f"🪙 *Итог недели:* перерасход −{fmt_money(-diff)}"
    )
    over = sorted(d for d, lim in lims.items() if by_day.get(d, 0.0) > lim + 0.5)
    if lims:
        over_txt = ", ".join(f"{WEEKDAYS_RU[d.weekday()]} {dm(d)}" for d in over)
        lines.append(f"🔴 *Дней с превышением:* {len(over)} из {len(lims)}" + (f" ({over_txt})" if over else ""))
    top_day = max(by_day.items(), key=lambda kv: kv[1])
    lines.append(f"📈 *Самый дорогой день:* {WEEKDAYS_RU[top_day[0].weekday()]} {dm(top_day[0])} — {fmt_money(top_day[1])}")

    prev_start = week_start - datetime.timedelta(days=7)
    if prev_start + datetime.timedelta(days=6) >= acc_start:
        prev_spent = _week_totals(user_id, max(prev_start, acc_start), week_start - datetime.timedelta(days=1))[0]
        if prev_spent > 0:
            pct = (spent - prev_spent) / prev_spent * 100
            arrow = "↑" if pct > 0.5 else ("↓" if pct < -0.5 else "≈")
            lines.append(f"↔️ *К прошлой неделе:* {arrow} {abs(pct):.0f}% (было {fmt_money(prev_spent)})")
    if top:
        lines.append("\n🏆 *Топ категорий недели:*")
        for i, r in enumerate(top, 1):
            lines.append(f"{i}. {esc(r['category'])} — {fmt_money(r['total'])}")
    info = get_budget_info(user_id, week_end)
    lines.append(f"\n🪙 *Банк сдачи на {dm(week_end)}:* {fmt_money(calc_change_pool(user_id, week_end))}")
    lines.append(f"💰 *Копилка (всего):* {fmt_money(info['piggy_total'])}")
    return "\n".join(lines)


async def send_week_summaries(bot, now_dt: datetime.datetime, bot_data: dict) -> int:
    """Раз в неделю шлёт каждому настроенному пользователю итоги прошлой недели. Отметка week_summary_sent
    хранится в settings (схема базы не менялась)."""
    if not WEEK_SUMMARY_ENABLED or not week_summary_window_open(now_dt):
        return 0
    week_start = week_monday(now_dt.date()) - datetime.timedelta(days=7)
    key = week_start.isoformat()
    with db() as conn:
        uids = [r["user_id"] for r in conn.execute(
            "SELECT DISTINCT user_id FROM settings WHERE key = 'daily_income' AND user_id != 0"
        )]
    sent_count = 0
    for uid in uids:
        if not week_summary_pending(uid, week_start):
            continue
        fails = bot_data.setdefault("week_summary_fails", {})
        day, cnt = fails.get(uid, (None, 0))
        if day == now_dt.date().isoformat() and cnt >= 5:
            continue
        try:
            text = build_week_summary(uid, week_start)
            if text is None:
                set_setting(uid, "week_summary_sent", key)  # трат не было: молчим, но неделю закрываем
                continue
            await bot.send_message(chat_id=uid, text=text, parse_mode="Markdown", reply_markup=MAIN_KB)
        except Forbidden:
            logger.info("Пользователь %s заблокировал бота — сводки приостановлены", uid)
            set_setting(uid, "week_summary_sent", key)
            set_setting(uid, "blocked", "1")
        except Exception as e:
            cnt = cnt + 1 if day == now_dt.date().isoformat() else 1
            fails[uid] = (now_dt.date().isoformat(), cnt)
            logger.error("Не удалось отправить недельную сводку пользователю %s (попытка %s): %s", uid, cnt, e)
        else:
            set_setting(uid, "week_summary_sent", key)  # только после успешной отправки
            sent_count += 1
            logger.info("Недельная сводка за %s отправлена пользователю %s", key, uid)
        await asyncio.sleep(0.05)
    return sent_count


async def week_summary_job(context: ContextTypes.DEFAULT_TYPE):
    await send_week_summaries(context.bot, datetime.datetime.now(TZ), context.application.bot_data)


# --- Команды ---
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_start_day(user_id)
    if get_setting(user_id, "blocked") == "1":
        set_setting(user_id, "blocked", "0")
    context.user_data.clear()
    context.user_data["wizard_step"] = "wizard_mode"
    welcome = (
        "👋 *Привет! Добро пожаловать в твой персональный бот для бюджетирования.*\n\n"
        "🧙‍♂️ Давай настроим твой личный бюджет за пару шагов.\n\n"
        "📥 *Шаг 1 из 5:* Как ты получаешь деньги?"
    )
    await update.message.reply_text(welcome, parse_mode="Markdown", reply_markup=mode_markup("wiz_mode_"))


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    _discard_pending_restore(update.effective_user.id)
    await update.message.reply_text("Ввод отменён.", reply_markup=MAIN_KB)


def menu_hub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 Графика трат", callback_data="chart_overview"),
            InlineKeyboardButton("📥 Скачать Excel", callback_data="menu_excel"),
        ],
        [
            InlineKeyboardButton("🏷 Траты по категориям", callback_data="menu_categories"),
            InlineKeyboardButton("📚 Архив месяцев", callback_data="menu_months"),
        ],
        [
            InlineKeyboardButton("⚙️ Настройки бюджета", callback_data="menu_settings"),
            InlineKeyboardButton("↩️ Отменить последнюю", callback_data="menu_undo"),
        ],
    ])


async def show_menu_hub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "⚙️ *Меню и дополнительные сервисы*\n\n"
        "Выберите нужный раздел или отчёт:\n"
        "• 📈 *Графика* — визуальные диаграммы расходов и остатка\n"
        "• 📥 *Excel* — полная выгрузка персональной таблицы\n"
        "• 🏷 *Категории* — подробная разбивка расходов по статьям\n"
        "• 📚 *Архив* — сравнение и история прошлых месяцев\n"
        "• ⚙️ *Настройки* — доходы, плановая копилка и время отчётов\n"
        "• ↩️ *Отмена* — быстрый откат последней добавленной траты\n\n"
        "💡 _Подсказка: чтобы записать трату, просто отправьте, например: `450 продукты`._"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=menu_hub_kb())
    else:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=menu_hub_kb())


async def show_today(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    today_d = today_date()
    today_nav = [
        [
            InlineKeyboardButton("🔄 Обновить", callback_data="today_refresh"),
            InlineKeyboardButton("📈 График", callback_data="chart_overview"),
            InlineKeyboardButton("🏠 Счета", callback_data="bills_refresh"),
        ]
    ]
    text, markup = build_day_report(user_id, today_d, extra_rows=today_nav)
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
    lims = limits_between(user_id, start_d, end_d)
    with db() as conn:
        rows = conn.execute(
            "SELECT spent_date, SUM(amount) AS total FROM expenses "
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
        diff = lims.get(cur, info["daily_limit"]) - sp
        icon = "🟢" if diff >= 0 else "🔴"
        lines.append(f"{icon} *{cur.strftime('%d.%m')}*: {fmt_money(sp)} (остаток: {fmt_money(diff)})")

    lines.append(f"\n💸 *Всего за 7 дней:* {fmt_money(total_week)}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KB)


async def show_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now_d = today_date()
    month_prefix = f"{now_d.year:04d}-{now_d.month:02d}-"
    info = get_budget_info(user_id, now_d)

    with db() as conn:
        day_rows = conn.execute(
            "SELECT spent_date, SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY spent_date ORDER BY spent_date",
            (user_id, month_prefix + "%"),
        ).fetchall()
        top_rows = conn.execute(
            "SELECT category, SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY category ORDER BY total DESC LIMIT 5",
            (user_id, month_prefix + "%"),
        ).fetchall()

    expenses_spent = sum(r["total"] for r in day_rows)
    mand_status = get_month_mandatory_status(user_id, now_d)
    mand_paid = mand_status["total_paid"]
    total_spent = expenses_spent + mand_paid
    change_pool = calc_change_pool(user_id, now_d)

    lines = [f"📅 *Сводка за {now_d.strftime('%m.%Y')}*\n"]

    if mand_status["items"]:
        lines.append("🏠 *Обязательные счета (План / Оплачено):*")
        for m in mand_status["items"]:
            lines.append(f"• {m['status_emoji']} {esc(m['name'])}: {fmt_money(m['paid'])} / {fmt_money(m['plan'])} _{m['status_text']}_")
        lines.append(f"  *Итого счетов:* план {fmt_money(mand_status['total_plan'])}, оплачено {fmt_money(mand_status['total_paid'])}")
        if mand_status["total_overrun"] > 0:
            lines.append(f"  ⚠️ _Превышение плана счетов: +{fmt_money(mand_status['total_overrun'])}_")
        lines.append("")
    elif info["mandatory_list"]:
        lines.append("🏠 *Обязательные расходы (учтены):*")
        for m in info["mandatory_list"]:
            lines.append(f"• {esc(m['name'])}: {fmt_money(m['amount'])}")
        lines.append("")

    if day_rows:
        lines.append("🛍 *Обычные траты по дням:*")
        for r in day_rows[-10:]:
            d_fmt = datetime.date.fromisoformat(r["spent_date"]).strftime("%d.%m")
            lines.append(f"• {d_fmt}: *{fmt_money(r['total'])}*")
    else:
        lines.append("_В этом месяце обычных трат пока нет_")

    lines.append(f"\n💸 *Всего потрачено (с учётом обязательных):* *{fmt_money(total_spent)}*")
    lines.append(f"🪙 *Банк сдачи:* {fmt_money(change_pool)}")
    lines.append(f"💰 *Копилка (всего):* {fmt_money(info['piggy_total'])}")

    if top_rows:
        lines.append("\n🏆 *Топ-5 категорий расходов:*")
        for i, r in enumerate(top_rows, 1):
            lines.append(f"{i}. {esc(r['category'])} — {fmt_money(r['total'])}")

    month_kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 Графики", callback_data="chart_overview"),
            InlineKeyboardButton("📥 Excel", callback_data="menu_excel"),
        ],
        [
            InlineKeyboardButton("🏷 Категории", callback_data="menu_categories"),
            InlineKeyboardButton("📚 Архив месяцев", callback_data="menu_months"),
        ],
        [
            InlineKeyboardButton("🏠 Обязательные счета", callback_data="bills_refresh"),
        ],
    ])
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=month_kb)


async def send_chart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_chart_kind(update.message, update.effective_user.id, "overview")


async def send_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    try:
        buf = await asyncio.to_thread(update_excel_file, user_id)
        await update.message.reply_document(
            document=buf,
            filename=f"Бюджет_{today_date().strftime('%m_%Y')}.xlsx",
            caption="📥 Ваша персональная таблица бюджета с деталями трат",
            reply_markup=MAIN_KB,
        )
    except Exception:
        logger.exception("Ошибка при формировании Excel:")
        await update.message.reply_text("⚠️ Не удалось сформировать Excel. Попробуйте позже.", reply_markup=MAIN_KB)


async def undo_last(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with db() as conn:
        exp = conn.execute(
            "SELECT id, amount, category, spent_date, created_at FROM expenses "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        top = conn.execute(
            "SELECT id, amount, comment, topup_date, created_at FROM savings_topups "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
    if not exp and not top:
        await update.message.reply_text("Операций для отмены пока нет.", reply_markup=MAIN_KB)
        return

    if top and (not exp or (top["created_at"] or "") >= (exp["created_at"] or "")):
        freeze_past_limits(user_id)
        with db() as conn:
            conn.execute("DELETE FROM savings_topups WHERE id = ? AND user_id = ?", (top["id"], user_id))
        sign = "+" if top["amount"] >= 0 else "−"
        await update.message.reply_text(
            f"↩️ Отменена операция с копилкой: *{sign}{fmt_money(abs(top['amount']))}* "
            f"({esc(top['comment'])}, {top['topup_date']})",
            parse_mode="Markdown",
            reply_markup=MAIN_KB,
        )
        return

    delete_expense(user_id, exp["id"])
    await update.message.reply_text(
        f"↩️ Удалена последняя трата: *{fmt_money(exp['amount'])}* ({esc(exp['category'])}, {exp['spent_date']})",
        parse_mode="Markdown",
        reply_markup=MAIN_KB,
    )


def month_summary(user_id: int, first_day: datetime.date) -> dict:
    """Итоги месяца: настройки из снимка (для прошлых) + траты и пополнения, посчитанные по датам."""
    today = today_date()
    is_current = (first_day.year, first_day.month) == (today.year, today.month)
    last_dn = calendar.monthrange(first_day.year, first_day.month)[1]
    end = today if is_current else datetime.date(first_day.year, first_day.month, last_dn)
    info = get_budget_info(user_id, end)
    key = _month_key(first_day)
    prefix = key + "-"
    with db() as conn:
        day_rows = conn.execute(
            "SELECT spent_date, SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY spent_date",
            (user_id, prefix + "%"),
        ).fetchall()
        cat_rows = conn.execute(
            "SELECT category, SUM(amount) AS total FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY category ORDER BY total DESC LIMIT 5",
            (user_id, prefix + "%"),
        ).fetchall()
        wd = conn.execute(
            "SELECT SUM(amount) AS total FROM savings_topups "
            "WHERE user_id = ? AND topup_date LIKE ? AND amount < 0",
            (user_id, prefix + "%"),
        ).fetchone()
        snap = conn.execute(
            "SELECT estimated FROM month_settings WHERE user_id = ? AND month = ?",
            (user_id, key),
        ).fetchone()
    by_day = {r["spent_date"]: r["total"] for r in day_rows}
    spent = sum(by_day.values())
    period_start = get_period_start(user_id, end)
    lims = limits_between(user_id, period_start, end) if period_start <= end else {}
    total_limit = sum(lims.values())
    return {
        "info": info,
        "is_current": is_current,
        "estimated": bool(snap and snap["estimated"]),
        "spent": spent,
        "total_limit": total_limit,
        "change_pool": total_limit - spent,
        "over_days": sum(1 for d, lim in lims.items() if by_day.get(d.isoformat(), 0.0) > lim + 0.5),
        "counted_days": len(lims),
        "withdrawn": -(wd["total"] or 0.0) if wd else 0.0,
        "top": cat_rows,
    }


def _list_months(user_id: int, limit: int = 24):
    """Месяцы от текущего назад до начала учёта (первые дни месяцев)."""
    ensure_start_day(user_id)
    try:
        start = datetime.date.fromisoformat(get_setting(user_id, "start_date") or "").replace(day=1)
    except ValueError:
        start = today_date().replace(day=1)
    out, cur = [], today_date().replace(day=1)
    while cur >= start and len(out) < limit:
        out.append(cur)
        cur = (cur - datetime.timedelta(days=1)).replace(day=1)
    return out


def _month_title(d: datetime.date) -> str:
    return f"{MONTHS_RU[d.month - 1]} {d.year}"


def build_months_overview(user_id: int):
    if get_setting(user_id, "daily_income") is None:
        return "Бюджет ещё не настроен. Нажмите /start, и после первого месяца здесь появится история.", None
    archive_closed_months(user_id)
    months = _list_months(user_id)
    lines = ["📚 *История месяцев*", "_Потрачено из лимита → сдача (+) или перерасход (−), 🏦 копилка на конец месяца_", ""]
    buttons = []
    for m in months:
        s = month_summary(user_id, m)
        pool = s["change_pool"]
        sign = "+" if pool >= 0 else "−"
        tag = " _(идёт)_" if s["is_current"] else (" ≈" if s["estimated"] else "")
        lines.append(
            f"• *{MONTHS_RU[m.month - 1][:3]} {m.year}*{tag}: {fmt_num(s['spent'])} из {fmt_num(s['total_limit'])} "
            f"→ {sign}{fmt_money(abs(pool))}, 🏦 {fmt_money(s['info']['piggy_total'])}"
        )
        buttons.append(InlineKeyboardButton(_month_title(m), callback_data=f"month_{_month_key(m)}"))
    if any(" ≈" in ln for ln in lines):
        lines.append("\n_≈ — месяц закрыт до появления архива: доход и обязательные взяты из текущих настроек._")
    lines.append("\n👇 Выберите месяц, чтобы увидеть подробности и скачать Excel.")
    return "\n".join(lines), InlineKeyboardMarkup([buttons[i:i + 2] for i in range(0, len(buttons), 2)])


def build_month_detail(user_id: int, first_day: datetime.date):
    s = month_summary(user_id, first_day)
    info = s["info"]
    lines = [f"📚 *{_month_title(first_day)}*" + (" _(идёт сейчас)_" if s["is_current"] else "")]
    if s["estimated"]:
        lines.append("_≈ Месяц закрыт до появления архива: доход и обязательные взяты из текущих настроек._")
    lines.append("")
    income_line = f"💵 *Доход за месяц:* {fmt_money(info['income'])}"
    if info["income_mode"] != "monthly":
        income_line += f" ({info['display_income_text']})"
    lines.append(income_line)
    if info["mandatory_list"]:
        lines.append(f"🏠 *Обязательные ({fmt_money(info['mandatory_total'])}):*")
        for m in info["mandatory_list"]:
            lines.append(f"  • {esc(m['name'])}: {fmt_money(m['amount'])}")
    else:
        lines.append("🏠 *Обязательные:* нет")
    save_line = f"💰 *Плановая копилка:* {fmt_money(info['save'])}"
    if info["topups_total"] > 0:
        save_line += f", +{fmt_money(info['topups_total'])} доп."
    if s["withdrawn"] > 0:
        save_line += f", снято {fmt_money(s['withdrawn'])}"
    lines.append(save_line)
    lines.append(f"🎯 *Бюджет на ежедневные траты:* {fmt_money(max(0.0, info['available']))}")
    lines.append("")
    lines.append(f"💸 *Потрачено:* {fmt_money(s['spent'])} из {fmt_money(s['total_limit'])} лимита")
    pool = s["change_pool"]
    if pool >= 0:
        lines.append(f"🪙 *Итог:* сдача +{fmt_money(pool)}")
    else:
        lines.append(f"🪙 *Итог:* перерасход −{fmt_money(-pool)}")
    if s["counted_days"]:
        lines.append(f"🔴 *Дней с превышением лимита:* {s['over_days']} из {s['counted_days']}")
    lines.append(
        f"🏦 *Копилка {'сегодня' if s['is_current'] else 'на конец месяца'}:* {fmt_money(info['piggy_total'])}"
    )
    if s["top"]:
        lines.append("\n🏆 *Топ-5 категорий:*")
        for i, r in enumerate(s["top"], 1):
            lines.append(f"{i}. {esc(r['category'])} — {fmt_money(r['total'])}")
    key = _month_key(first_day)
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📈 Графики", callback_data=f"chart_overview_{key}"),
            InlineKeyboardButton("📥 Excel", callback_data=f"monthxls_{key}"),
        ],
        [InlineKeyboardButton("🏷 Категории месяца", callback_data=f"cat_{key}")],
        [InlineKeyboardButton("◀️ К списку месяцев", callback_data="months_list")],
    ])
    return "\n".join(lines), markup


# --- Расходы по категориям по месяцам ---
def category_totals(user_id: int, month_first: datetime.date) -> list:
    """[(название, сумма, число трат)] за месяц, по убыванию суммы. Написания одной категории в разном регистре склеены."""
    with db() as conn:
        rows = conn.execute(
            "SELECT category, SUM(amount) AS total, COUNT(*) AS n FROM expenses "
            "WHERE user_id = ? AND spent_date LIKE ? GROUP BY category",
            (user_id, _month_key(month_first) + "-%"),
        ).fetchall()
    merged = {}
    for r in rows:
        m = merged.setdefault(cat_key(r["category"]), {"total": 0.0, "n": 0, "best": (0.0, "")})
        m["total"] += r["total"]
        m["n"] += r["n"]
        m["best"] = max(m["best"], (r["total"], r["category"]))
    out = [(m["best"][1], m["total"], m["n"]) for m in merged.values()]
    return sorted(out, key=lambda x: (-x[1], x[0]))


def build_categories_report(user_id: int, month_first: datetime.date):
    """Расходы по категориям за месяц: сумма, доля, изменение к прошлому месяцу. Кнопки: соседние месяцы и таблица по месяцам."""
    if get_setting(user_id, "daily_income") is None:
        return "Бюджет ещё не настроен. Нажмите /start.", None
    months = _list_months(user_id, limit=1200)  # от нового к старому
    month_first = month_first.replace(day=1)
    cur = category_totals(user_id, month_first)
    prev_first = prev_month_first(month_first)
    prev = {cat_key(n): t for n, t, _ in category_totals(user_id, prev_first)}
    total = sum(t for _, t, _ in cur)
    lines = [f"🏷 *Расходы по категориям: {_month_title(month_first)}*",
             "_Обычные траты, без обязательных расходов и копилки_", ""]
    if not cur:
        lines.append("_В этом месяце трат нет._")
    else:
        shown, rest = cur[:20], cur[20:]
        for i, (name, t, n) in enumerate(shown, 1):
            line = f"{i}. {esc(name)} — *{fmt_money(t)}* · {t / total * 100:.0f}%"
            p = prev.get(cat_key(name))
            if p:
                pct = (t - p) / p * 100
                if abs(pct) >= 1:
                    line += f" · {'↑' if pct > 0 else '↓'}{abs(pct):.0f}%"
            elif prev:
                line += " · новая"
            lines.append(line)
        if rest:
            lines.append(f"… и ещё {len(rest)}: {fmt_money(sum(t for _, t, _ in rest))}")
        lines.append(f"\n💸 *Итого:* {fmt_money(total)}")
        if prev:
            lines.append(f"_Стрелки: изменение к {_month_title(prev_first)}._")
    rows = []
    if month_first in months:
        i = months.index(month_first)
        nav = []
        if i + 1 < len(months):
            o = months[i + 1]
            nav.append(InlineKeyboardButton(f"◀️ {MONTHS_RU[o.month - 1][:3]} {o.year}", callback_data=f"cat_{_month_key(o)}"))
        if i > 0:
            n_ = months[i - 1]
            nav.append(InlineKeyboardButton(f"{MONTHS_RU[n_.month - 1][:3]} {n_.year} ▶️", callback_data=f"cat_{_month_key(n_)}"))
        if nav:
            rows.append(nav)
    rows.append([
        InlineKeyboardButton("📊 По месяцам", callback_data=f"catx_{_month_key(month_first)}"),
        InlineKeyboardButton("📚 Месяцы", callback_data="months_list"),
    ])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def _short_amount(v: float) -> str:
    if v <= 0:
        return "·"
    if v >= 10000:
        return f"{v / 1000:.0f}к"
    if v >= 1000:
        return f"{v / 1000:.1f}к".replace(".", ",")
    return f"{v:.0f}"


def build_categories_matrix(user_id: int, upto: datetime.date, n_months: int = 5, n_cats: int = 8):
    """Таблица «категория × месяц» за последние n_months месяцев по upto (включительно)."""
    months = [m for m in _list_months(user_id, limit=1200) if m <= upto.replace(day=1)][:n_months][::-1]
    if not months:
        return "Пока нет данных по месяцам.", None
    per_month = [{cat_key(n): (n, t) for n, t, _ in category_totals(user_id, m)} for m in months]
    sums = {}
    for pm in per_month:
        for k, (n, t) in pm.items():
            cur = sums.setdefault(k, [n, 0.0])
            cur[1] += t
    top = sorted(sums.items(), key=lambda kv: -kv[1][1])
    shown, rest = top[:n_cats], top[n_cats:]
    if not shown:
        return "🏷 В этих месяцах трат нет.", InlineKeyboardMarkup(
            [[InlineKeyboardButton("◀️ К категориям", callback_data=f"cat_{_month_key(upto)}")]]
        )

    def clip(name, w=9):
        name = name.replace("`", "'")
        return name if len(name) <= w else name[: w - 1] + "…"

    header = f"{'':9} " + " ".join(f"{MONTHS_RU[m.month - 1][:3]:>5}" for m in months)
    out = [header]
    for k, (name, _) in shown:
        out.append(f"{clip(name):9} " + " ".join(f"{_short_amount(pm.get(k, (0, 0.0))[1]):>5}" for pm in per_month))
    if rest:
        rest_keys = [k for k, _ in rest]
        out.append(f"{'Прочее':9} " + " ".join(
            f"{_short_amount(sum(pm.get(k, (0, 0.0))[1] for k in rest_keys)):>5}" for pm in per_month))
    out.append(f"{'Итого':9} " + " ".join(
        f"{_short_amount(sum(t for _, t in pm.values())):>5}" for pm in per_month))
    text = (
        "🏷 *Категории по месяцам, ₽*\n_к — тысячи, «·» — трат не было_\n\n"
        "```\n" + "\n".join(out) + "\n```"
    )
    return text, InlineKeyboardMarkup([[InlineKeyboardButton("◀️ К категориям", callback_data=f"cat_{_month_key(upto)}")]])


async def show_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = build_categories_report(update.effective_user.id, today_date())
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup or MAIN_KB)


async def fix_categories_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    is_adm = is_admin(update)
    target_uid = None if is_adm and context.args and context.args[0].lower() == "all" else user_id

    res = recategorize_user_expenses(user_id=target_uid)
    scanned = res["total_scanned"]
    updated = res["updated_count"]
    changes = res["changes"]

    if updated == 0:
        await update.message.reply_text(
            "✨ *Все категории уже в полном порядке!*\n"
            f"Проверено трат: {scanned}. Неправильных или разрозненных категорий не обнаружено.",
            parse_mode="Markdown",
            reply_markup=MAIN_KB,
        )
        return

    lines = [
        "🪄 *Категории успешно обновлены!*",
        f"• Всего проверено трат: *{scanned}*",
        f"• Исправлено категорий: *{updated}*\n",
        "*Произведённые изменения:*",
    ]
    sorted_changes = sorted(changes.items(), key=lambda kv: kv[1], reverse=True)[:15]
    for (old_c, new_c), count in sorted_changes:
        lines.append(f"• `{esc(old_c)}` → *{esc(new_c)}* ({count} шт.)")

    if len(changes) > 15:
        lines.append(f"_... и ещё {len(changes) - 15} категорий_")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=MAIN_KB)


def parse_month_key(user_id: int, key: str):
    """'2026-10' -> дата 1-го числа, если месяц есть в истории пользователя, иначе None."""
    if not re.fullmatch(r"\d{4}-\d{2}", key or ""):
        return None
    try:
        d = datetime.date(int(key[:4]), int(key[5:]), 1)
    except ValueError:
        return None
    return d if d in set(_list_months(user_id, limit=1200)) else None


def is_admin(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id in ADMIN_IDS


def build_admin_stats() -> str:
    """Только агрегаты: личные траты и суммы пользователей админу не показываются."""
    with db() as conn:
        one = lambda q: conn.execute(q).fetchone()[0] or 0
        users = one("SELECT COUNT(DISTINCT user_id) FROM settings WHERE key = 'daily_income' AND user_id != 0")
        blocked = one("SELECT COUNT(*) FROM settings WHERE key = 'blocked' AND value = '1' AND user_id != 0")
        active7 = one("SELECT COUNT(DISTINCT user_id) FROM expenses WHERE created_at >= datetime('now', '-7 days')")
        new7 = one("SELECT COUNT(*) FROM settings WHERE key = 'start_date' AND user_id != 0 AND value >= date('now', '-7 days')")
        exp_total = one("SELECT COUNT(*) FROM expenses")
        exp7 = one("SELECT COUNT(*) FROM expenses WHERE created_at >= datetime('now', '-7 days')")
        topups = one("SELECT COUNT(*) FROM savings_topups")
        months = one("SELECT COUNT(*) FROM month_settings")
    try:
        size_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    except OSError:
        size_mb = 0.0
    return (
        "🛠 *Админ-панель*\n\n"
        f"👥 Пользователей: *{users}* (заблокировали бота: {blocked})\n"
        f"🆕 Новых за 7 дней: *{new7}*\n"
        f"🔥 Активных за 7 дней (были траты): *{active7}*\n\n"
        f"🧾 Трат всего: *{exp_total}* (за 7 дней: {exp7})\n"
        f"🏦 Операций с копилкой: *{topups}*\n"
        f"📚 Архивных месяцев: *{months}*\n"
        f"💾 База данных: *{size_mb:.2f} МБ*\n"
        f"📁 Файл: {esc(os.path.abspath(DB_PATH))}\n"
        f"🔗 {esc(datasafe.format_marker_summary(DATA_DIR))}\n\n"
        "Резервная копия базы: /backup, восстановление: /restore"
    )


def make_db_backup() -> io.BytesIO:
    """Согласованная копия SQLite (с учётом WAL) через штатный backup API."""
    return io.BytesIO(datasafe.backup_bytes(DB_PATH))


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return  # для остальных команды как будто не существует
    await update.message.reply_text(build_admin_stats(), parse_mode="Markdown", reply_markup=MAIN_KB)


async def backup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    try:
        buf = await asyncio.to_thread(make_db_backup)
        stamp = datetime.datetime.now(TZ).strftime("%Y-%m-%d_%H-%M")
        await update.message.reply_document(
            document=buf, filename=f"budget_backup_{stamp}.db",
            caption="💾 Резервная копия базы. В ней личные данные пользователей, не пересылайте её.",
        )
    except Exception:
        logger.exception("Ошибка резервной копии:")
        await update.message.reply_text("⚠️ Не удалось сделать копию, подробности в логах.")


# --- Сохранность данных: уведомления админам, автокопии, восстановление ---
def _utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


async def notify_admins(bot, text: str) -> int:
    """Простой текст без разметки (в путях бывают «_»). Возвращает, скольким админам дошло."""
    delivered = 0
    for admin_id in sorted(ADMIN_IDS):
        try:
            await bot.send_message(chat_id=admin_id, text=text[:4000])
            delivered += 1
        except TelegramError as e:
            logger.error("Не удалось написать администратору %s: %s", admin_id, e)
    return delivered


def log_startup_report(rep) -> None:
    logger.info(
        "Хранилище: база %s, %s; том %s (id %s, запуск №%s)",
        rep.db_path, datasafe.stats_line(rep.stats), rep.data_dir, rep.install_id, rep.starts,
    )
    for lvl, text in rep.problems:
        {"info": logger.info, "warn": logger.warning, "critical": logger.critical}[lvl]("Хранилище: %s", text)
    if not ADMIN_IDS and rep.level != "info":
        logger.critical("ADMIN_IDS не задан: предупредить некого, смотрите логи.")


def build_startup_message(rep) -> str:
    facts = (
        f"База: {rep.db_path}\n"
        f"В ней: {datasafe.stats_line(rep.stats)}\n"
        f"Том: id {rep.install_id}, запуск №{rep.starts}"
    )
    if rep.level == "critical":
        head = "🚨 Копилка: ПРОБЛЕМА С ДАННЫМИ"
    elif rep.level == "warn":
        head = "⚠️ Копилка: предупреждение о хранении данных"
    else:
        head = "🟢 Копилка запущена"
    body = "\n\n".join(rep.texts("warn")) if rep.level != "info" else ""
    tail = ""
    if rep.level == "info" and rep.starts == 1 and not rep.notes:
        tail = "\n\nЭто первый запуск с этим томом. Если бот уже работал раньше, значит том новый или пустой."
    notes = ("\n\n" + "\n".join("• " + n for n in rep.notes)) if rep.notes else ""
    return head + ("\n\n" + body if body else "") + notes + "\n\n" + facts + tail


async def send_startup_notices(app) -> None:
    rep = STARTUP_REPORT
    if rep is None or not ADMIN_IDS:
        return
    if rep.level == "info" and not STARTUP_NOTICE and not rep.notes:
        return
    await notify_admins(app.bot, build_startup_message(rep))


def _lost_still_active() -> bool:
    marker = datasafe.read_marker(DATA_DIR) or {}
    return bool(marker.get("lost_since")) and datasafe.db_stats(os.path.abspath(DB_PATH))["empty"]


async def _send_backup_file(bot, admin_id: int, path: str, caption: str) -> bool:
    """Шлёт файл копии; если он больше лимита Telegram, сжимает gzip (восстановление понимает .gz)."""
    send_path, tmp_gz = path, None
    try:
        if os.path.getsize(path) > TG_UPLOAD_LIMIT:
            tmp_gz = path + ".gz.tmp"
            with open(path, "rb") as fin, gzip.open(tmp_gz, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout)
            send_path = tmp_gz
            if os.path.getsize(send_path) > TG_UPLOAD_LIMIT:
                await bot.send_message(
                    chat_id=admin_id,
                    text=f"💾 Копия базы слишком большая для Telegram. Она лежит на томе: {path}",
                )
                return False
        name = os.path.basename(path) + (".gz" if tmp_gz else "")
        with open(send_path, "rb") as f:
            await bot.send_document(chat_id=admin_id, document=f, filename=name, caption=caption)
        return True
    except TelegramError as e:
        logger.error("Не удалось отправить копию администратору %s: %s", admin_id, e)
        return False
    finally:
        if tmp_gz and os.path.exists(tmp_gz):
            os.remove(tmp_gz)


def _make_scheduled_backup(stamp: str):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = datasafe.backup_to_file(DB_PATH, os.path.join(BACKUP_DIR, f"auto_{stamp}.db"))
    removed = datasafe.rotate_auto_backups(BACKUP_DIR, BACKUP_KEEP)
    return dest, removed


async def run_weekly_backup(context: ContextTypes.DEFAULT_TYPE, now: datetime.datetime) -> None:
    if _RUNTIME.get("backup_next_try", 0) > now.timestamp():
        return
    stats = datasafe.db_stats(os.path.abspath(DB_PATH))
    if stats["empty"]:
        return  # пустую базу не шлём: она бы вытеснила нормальные копии
    try:
        dest = _RUNTIME.get("pending_backup")
        if not dest or not os.path.isfile(dest):
            dest, removed = await asyncio.to_thread(_make_scheduled_backup, now.strftime("%Y-%m-%d_%H-%M"))
            _RUNTIME["pending_backup"] = dest
            logger.info("Автокопия: %s (удалено старых: %s)", dest, len(removed))
    except Exception:
        logger.exception("Не удалось сделать автокопию базы")
        _RUNTIME["backup_next_try"] = now.timestamp() + 3600
        await notify_admins(context.bot, "⚠️ Не удалось сделать еженедельную копию базы, подробности в логах.")
        return

    caption = (
        f"💾 Еженедельная копия базы ({datasafe.stats_line(stats)}). "
        "В ней личные данные пользователей, не пересылайте её. Восстановление: /restore"
    )
    delivered = 0
    for admin_id in sorted(ADMIN_IDS):
        if await _send_backup_file(context.bot, admin_id, dest, caption):
            delivered += 1
    if delivered:
        stamp = _utc_iso()
        _RUNTIME["last_backup"] = stamp
        _RUNTIME.pop("pending_backup", None)
        datasafe.refresh_marker(DATA_DIR, DB_PATH, last_backup=stamp)
        logger.info("Автокопия отправлена администраторам: %s", delivered)
    else:
        _RUNTIME["backup_next_try"] = now.timestamp() + 3600  # копия уже на томе, повторяем только отправку


async def maintenance_job(context: ContextTypes.DEFAULT_TYPE):
    """Раз в 10 минут: обновляет маркер тома, раз в сутки повторяет тревогу, делает еженедельную копию."""
    now = datetime.datetime.now(TZ)
    try:
        if _RUNTIME.get("marker_at", 0) + 3600 <= now.timestamp():
            await asyncio.to_thread(datasafe.refresh_marker, DATA_DIR, DB_PATH)
            _RUNTIME["marker_at"] = now.timestamp()

        rep = STARTUP_REPORT
        persistent_problem = bool(rep and rep.path_bad and not ALLOW_EPHEMERAL_DB) or _lost_still_active()
        if persistent_problem and ADMIN_IDS and _RUNTIME.get("data_alert_on") != now.date().isoformat():
            _RUNTIME["data_alert_on"] = now.date().isoformat()
            if rep and rep.problems and rep.level == "critical":
                await notify_admins(context.bot, "🚨 Напоминание: проблема с данными не устранена.\n\n"
                                    + "\n\n".join(rep.texts("critical")))
    except Exception:
        logger.exception("Ошибка обслуживания маркера")

    if not (BACKUP_ENABLED and ADMIN_IDS):
        return
    try:
        marker = datasafe.read_marker(DATA_DIR) or {}
        last = _RUNTIME.get("last_backup") or marker.get("last_backup")
        if datasafe.backup_due(now, last, BACKUP_WEEKDAY, BACKUP_HOUR, BACKUP_MINUTE):
            await run_weekly_backup(context, now)
    except Exception:
        logger.exception("Ошибка еженедельной копии")


def _discard_pending_restore(admin_id: int) -> None:
    pend = _RUNTIME.get("restore_pending", {}).pop(admin_id, None)
    if pend:
        shutil.rmtree(os.path.dirname(pend["path"]), ignore_errors=True)


async def restore_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    context.user_data.clear()
    context.user_data["state"] = "wait_restore_file"
    await update.message.reply_text(
        "♻️ Восстановление базы\n\n"
        "Пришлите файл .db документом (копию из /backup или еженедельную копию; .gz тоже подойдёт). "
        "Я проверю целостность и состав файла и покажу, что в нём. Ничего не заменяется без вашего "
        "подтверждения, а текущая база перед заменой сохраняется.\n\n"
        "Лимит Telegram на скачивание файлов ботом: 20 МБ. Отмена: /cancel."
    )


async def _process_receipt_bytes(msg, user_id: int, image_bytes: bytes, caption: str = ""):
    ensure_start_day(user_id)
    status_msg = await msg.reply_text("🔍 Читаю чек, секунду...")
    try:
        result = scan_receipt_image(
            image_bytes,
            user_id=user_id,
            caption=caption or "",
            user_existing_categories=used_categories(user_id),
        )

        if not result or not result.get("success") or not result.get("amount"):
            await status_msg.edit_text(
                "🤔 Не удалось распознать сумму или QR-код на чеке.\n\n"
                "💡 Попробуйте:\n"
                "• Сфотографировать чек при хорошем освещении\n"
                "• Чтобы QR-код или строка «ИТОГО» были чётко видны\n"
                "• Либо запишите трату текстом, например: `450 продукты`.",
                parse_mode="Markdown",
            )
            return

        amount = result["amount"]
        spent_d = result.get("spent_date") or today_date()
        if spent_d > today_date():
            spent_d = today_date()

        merchant = result.get("merchant")
        category = result.get("category") or DEFAULT_CATEGORY
        category = canonical_category(user_id, category)
        method = result.get("method", "чек")

        before = day_spent(user_id, spent_d)
        with db() as conn:
            exp_id = conn.execute(
                "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
                (user_id, spent_d.isoformat(), amount, category),
            ).lastrowid

        warning = limit_warning(user_id, spent_d, before, before + amount)
        top_cats = used_categories(user_id, limit=6, exclude=[category])
        chips = category_chips(user_id, exp_id, top_cats) if top_cats else []
        chips.append([
            InlineKeyboardButton("✏️ Сменить категорию", callback_data=f"xe_cat_{exp_id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"xe_del_{exp_id}"),
        ])
        report_text, markup = build_day_report(user_id, spent_d, extra_rows=chips)

        merch_line = f"\n🏪 Магазин: *{esc(merchant)}*" if merchant else ""
        items_line = ""
        if result.get("items"):
            preview = ", ".join(result["items"][:3])
            if len(result["items"]) > 3:
                preview += "..."
            items_line = f"\n🛒 Товары: _{esc(preview)}_"

        method_name = "QR-код" if method == "qr" else "текст чека"
        header = (
            f"🧾 *Чек успешно распознан ({method_name})!*\n\n"
            f"💰 Сумма: *{fmt_money(amount)}*\n"
            f"🏷 Категория: *{esc(category)}*\n"
            f"📅 Дата: *{spent_d.strftime('%d.%m.%Y')}*"
            f"{merch_line}"
            f"{items_line}\n\n"
        )
        await status_msg.edit_text(
            f"{header}{warning}{report_text}",
            parse_mode="Markdown",
            reply_markup=markup or MAIN_KB,
        )
    except Exception as e:
        logger.exception("Ошибка при обработке фото чека: %s", e)
        await status_msg.edit_text(
            "⚠️ Произошла ошибка при чтении чека. Попробуйте отправить фото ещё раз или введите трату текстом.",
        )


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.photo:
        return
    user_id = update.effective_user.id
    photo_file = await msg.photo[-1].get_file()
    buf = io.BytesIO()
    await photo_file.download_to_memory(buf)
    await _process_receipt_bytes(msg, user_id, buf.getvalue(), msg.caption or "")


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.document:
        return
    doc = msg.document
    admin_id = update.effective_user.id

    if is_admin(update) and context.user_data.get("state") == "wait_restore_file":
        if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
            await msg.reply_text("❌ Файл больше 20 МБ: Telegram не даёт боту его скачать. "
                                 "Положите базу на том вручную средствами хостинга.")
            return

        tmpdir = tempfile.mkdtemp(prefix="upload_")
        try:
            upload = os.path.join(tmpdir, "upload.bin")
            tg_file = await doc.get_file()
            await tg_file.download_to_drive(upload)
            candidate, info = await asyncio.to_thread(datasafe.prepare_restore, upload, today_date())
        except datasafe.RestoreError as e:
            await msg.reply_text(f"❌ Файл не подходит: {e}\n\nПришлите другой файл или нажмите /cancel. База не тронута.")
            return
        except Exception:
            logger.exception("Ошибка при проверке файла для восстановления")
            await msg.reply_text("❌ Не удалось проверить файл (подробности в логах). База не тронута.")
            return
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

        _discard_pending_restore(admin_id)
        token = uuid.uuid4().hex[:10]
        _RUNTIME.setdefault("restore_pending", {})[admin_id] = {"token": token, "path": candidate}
        current = datasafe.db_stats(os.path.abspath(DB_PATH))
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Заменить базу", callback_data=f"restore_ok_{token}"),
            InlineKeyboardButton("Отмена", callback_data=f"restore_no_{token}"),
        ]])
        await msg.reply_text(
            "✅ Файл прошёл проверку (целостность, таблицы, версия схемы).\n\n"
            f"В файле: {datasafe.stats_line(info)}\n"
            f"Сейчас в боте: {datasafe.stats_line(current)}\n\n"
            "При замене ВСЕ текущие данные будут заменены данными из файла. Копия текущей базы "
            "сохранится на томе и придёт вам файлом. Заменить?",
            reply_markup=markup,
        )
        return

    # Проверка: отправлен ли документ с изображением чека
    mime = (getattr(doc, "mime_type", None) or "").lower()
    fn = (getattr(doc, "file_name", None) or getattr(doc, "src", None) or "").lower()
    if mime.startswith("image/") or fn.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
        user_id = update.effective_user.id
        if doc.file_size and doc.file_size > TG_DOWNLOAD_LIMIT:
            await msg.reply_text("⚠️ Файл слишком большой (больше 20 МБ).")
            return
        tg_file = await doc.get_file()
        buf = io.BytesIO()
        await tg_file.download_to_memory(buf)
        await _process_receipt_bytes(msg, user_id, buf.getvalue(), msg.caption or "")


async def restore_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    query = update.callback_query
    admin_id = update.effective_user.id
    action, _, token = data.removeprefix("restore_").partition("_")
    pend = _RUNTIME.get("restore_pending", {}).get(admin_id)
    if not pend or pend["token"] != token or not os.path.isfile(pend["path"]):
        await query.message.edit_text("ℹ️ Запрос устарел. Начните заново: /restore")
        return
    if action == "no":
        _discard_pending_restore(admin_id)
        context.user_data.clear()
        await query.message.edit_text("Отменено, база не тронута.")
        return
    if action != "ok":
        return

    await query.message.edit_text("⏳ Восстанавливаю базу…")
    try:
        result = await asyncio.to_thread(datasafe.apply_restore, pend["path"], DB_PATH, BACKUP_DIR)
    except Exception:
        logger.exception("Ошибка восстановления базы")
        _discard_pending_restore(admin_id)
        context.user_data.clear()
        await query.message.edit_text(
            "❌ Восстановление не удалось (подробности в логах). Замена выполняется одной транзакцией, "
            "поэтому текущая база осталась прежней."
        )
        return
    _discard_pending_restore(admin_id)
    context.user_data.clear()
    context.application.bot_data.pop("archived_on", None)
    await asyncio.to_thread(datasafe.refresh_marker, DATA_DIR, DB_PATH, last_restore=_utc_iso())
    _RUNTIME.pop("marker_at", None)
    text = f"✅ База восстановлена. Теперь: {datasafe.stats_line(result['stats'])}."
    if result["before_restore"]:
        text += f"\nКопия прежней базы на томе: {result['before_restore']}"
    await query.message.edit_text(text)
    if result["before_restore"]:
        await _send_backup_file(context.bot, admin_id, result["before_restore"], "💾 Прежняя база (до восстановления)")


async def show_months(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = build_months_overview(update.effective_user.id)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup or MAIN_KB)


def build_history(user_id: int):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, spent_date, amount, category FROM expenses WHERE user_id = ? "
            "ORDER BY spent_date DESC, id DESC LIMIT 12",
            (user_id,),
        ).fetchall()
    if not rows:
        return "Трат пока нет. Запишите первую, например: `450 продукты`.", None
    lines = ["🧾 *Последние траты* (✏️ — изменить сумму, категорию или дату, ❌ — удалить):", ""]
    buttons = []
    for r in rows:
        d = datetime.date.fromisoformat(r["spent_date"]).strftime("%d.%m")
        lines.append(f"• {d} — *{fmt_money(r['amount'])}* {esc(r['category'])}")
        title = f"✏️ {d} {int(r['amount'])}₽ {r['category']}"
        buttons.append([
            InlineKeyboardButton(title[:36], callback_data=f"xe_open_{r['id']}"),
            InlineKeyboardButton("❌", callback_data=f"hdelexp_{r['id']}"),
        ])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = build_history(update.effective_user.id)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup or MAIN_KB)


async def show_bills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text, markup = build_bills_card(user_id)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


def piggy_undo_kb(topup_id: int):
    return InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Отменить", callback_data=f"deltop_{topup_id}")]])


def build_piggy_history(user_id: int):
    info = get_budget_info(user_id)
    with db() as conn:
        rows = conn.execute(
            "SELECT id, topup_date, amount, comment FROM savings_topups "
            "WHERE user_id = ? ORDER BY id DESC LIMIT 10",
            (user_id,),
        ).fetchall()
    lines = [
        f"🏦 *Копилка (всего):* {fmt_money(info['piggy_total'])}",
        f"_Плановые отчисления: {fmt_money(info['save'])} в месяц_",
        "",
    ]
    action_row = [
        InlineKeyboardButton("➕ Пополнить", callback_data="piggy_topup_btn"),
        InlineKeyboardButton("💸 Снять", callback_data="piggy_withdraw_btn"),
    ]
    buttons = [action_row]
    if rows:
        lines.append("*Последние операции* (нажмите, чтобы отменить):")
        for r in rows:
            d = datetime.date.fromisoformat(r["topup_date"]).strftime("%d.%m")
            sign = "+" if r["amount"] >= 0 else "−"
            lines.append(f"• {d} {sign}{fmt_money(abs(r['amount']))} — {esc(r['comment'])}")
            title = f"❌ {d} {sign}{int(abs(r['amount']))}₽ {r['comment']}"
            buttons.append([InlineKeyboardButton(title[:40], callback_data=f"hdeltop_{r['id']}")])
    else:
        lines.append("_Операций пока нет. Нажмите «➕ Пополнить» или отправьте `+500`._")
    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def show_piggy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, markup = build_piggy_history(update.effective_user.id)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=markup or MAIN_KB)


async def piggy_add(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, amount: float, comment: str):
    freeze_past_limits(user_id)
    with db() as conn:
        tid = conn.execute(
            "INSERT INTO savings_topups (user_id, topup_date, amount, comment) VALUES (?, ?, ?, ?)",
            (user_id, today_date().isoformat(), amount, comment),
        ).lastrowid
    context.user_data.clear()
    info = get_budget_info(user_id)
    await update.message.reply_text(
        f"💰 В копилку зачислено: *+{fmt_money(amount)}* ({esc(comment)})!\n"
        f"🏦 Всего в копилке: *{fmt_money(info['piggy_total'])}*\n"
        f"_Лимит на сегодня и дальше: {fmt_money(info['daily_limit'])} (прошлые дни не менялись)._",
        parse_mode="Markdown",
        reply_markup=piggy_undo_kb(tid),
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *Как пользоваться*\n\n"
        "Трата: `450 продукты`, `такси 300 вчера`, `1 500 одежда 28.10`\n"
        "Пополнить копилку: `+500 подработка`\n"
        "Снять из копилки — кнопка «💸 Из копилки» (на дневной лимит не влияет)\n\n"
        "Ошибся в трате? /history или «🧾 Траты»: ✏️ меняет сумму, категорию или дату, ❌ удаляет.\n"
        "Без категории (`450`) бот предложит одну из уже использованных.\n"
        "Расходы по категориям и месяцам: /categories. Графики прошлых месяцев: /chart, кнопки ◀️ ▶️.\n\n"
        "Бот предупредит, когда потрачено 80% дневного лимита, и по понедельникам пришлёт итоги недели; "
        "1-го числа — итоги месяца (всё отключается в /settings).\n"
        "Команды: /today /week /month /months /history /categories /chart /excel /piggy /settings /undo /cancel",
        parse_mode="Markdown",
        reply_markup=MAIN_KB,
    )


async def post_init(app):
    try:
        await app.bot.set_my_commands([
            BotCommand("today", "📊 Сводка за сегодня"),
            BotCommand("month", "📅 Сводка за месяц"),
            BotCommand("bills", "🏠 Счета и обязательные траты"),
            BotCommand("piggy", "🏦 Копилка и операции"),
            BotCommand("history", "🧾 Последние траты: правка/удаление"),
            BotCommand("categories", "🏷 Расходы по категориям"),
            BotCommand("menu", "⚙️ Меню и сервисы"),
            BotCommand("chart", "📈 Графика и диаграммы"),
            BotCommand("excel", "📥 Скачать Excel-отчет"),
            BotCommand("week", "📆 Траты за 7 дней"),
            BotCommand("months", "📚 Архив прошлых месяцев"),
            BotCommand("undo", "↩️ Отменить последнюю операцию"),
            BotCommand("settings", "⚙️ Настройки бюджета"),
            BotCommand("help", "ℹ️ Справка и подсказки"),
        ])
    except TelegramError:
        logger.warning("Не удалось установить меню команд")
    try:
        await send_startup_notices(app)
    except Exception:
        logger.exception("Не удалось отправить стартовое уведомление")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Необработанная ошибка", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("⚠️ Что-то пошло не так. Попробуйте ещё раз.")
        except TelegramError:
            pass


# --- Inline-кнопки ---
async def refresh_settings(query, user_id: int, prefix: str = ""):
    text = prefix + build_settings_text(user_id)
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=settings_inline_kb(user_id))
    except TelegramError:
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=settings_inline_kb(user_id))


async def safe_edit(query, text: str, markup=None):
    """Правит сообщение с кнопкой; если не вышло (старое сообщение и т. п.), присылает новое."""
    try:
        await query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    except TelegramError as e:
        if "not modified" in str(e).lower():
            return
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


async def expense_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Кнопки правки траты: xe_open_<id>, xe_amt_<id>, xe_cat_<id>, xe_dt_<id>, xe_day_<id>, xe_list, xc_<id>_<отпечаток>."""
    query = update.callback_query
    user_id = update.effective_user.id

    if data == "xe_list":
        context.user_data.clear()
        text, markup = build_history(user_id)
        await safe_edit(query, text, markup)
        return

    parts = data.split("_")
    try:
        if parts[0] == "xc":
            action, exp_id, token = "pick", int(parts[1]), parts[2]
        else:
            action, exp_id, token = parts[1], int(parts[2]), None
    except (ValueError, IndexError):
        return

    row = get_expense(user_id, exp_id)
    if not row:
        context.user_data.clear()
        await query.message.reply_text("ℹ️ Эта трата уже удалена.")
        return

    if action == "open":
        context.user_data.clear()
        text, markup = build_expense_card(user_id, exp_id)
        await safe_edit(query, text, markup)
    elif action == "amt":
        context.user_data.clear()
        context.user_data.update(state="wait_edit_amount", edit_exp_id=exp_id)
        await query.message.reply_text(
            f"💰 Сейчас: *{fmt_money(row['amount'])}* ({esc(row['category'])}).\n"
            "Напишите новую сумму числом, например `450`. Отмена: /cancel.",
            parse_mode="Markdown",
        )
    elif action == "dt":
        context.user_data.clear()
        context.user_data.update(state="wait_edit_date", edit_exp_id=exp_id)
        d = datetime.date.fromisoformat(row["spent_date"])
        await query.message.reply_text(
            f"📅 Сейчас: *{d.strftime('%d.%m.%Y')}*.\n"
            "Напишите новую дату: `вчера`, `28.10` или `28.10.2026`. Отмена: /cancel.",
            parse_mode="Markdown",
        )
    elif action == "cat":
        context.user_data.clear()
        context.user_data.update(state="wait_edit_cat", edit_exp_id=exp_id)
        names = used_categories(user_id, limit=10, exclude=[row["category"]])
        rows = category_chips(user_id, exp_id, names)
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"xe_open_{exp_id}")])
        hint = "Выберите из уже использованных или напишите новую категорию." if names else "Напишите новую категорию."
        await query.message.reply_text(
            f"🏷 Сейчас: *{esc(row['category'])}*.\n{hint} Отмена: /cancel.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows),
        )
    elif action == "day":
        context.user_data.clear()
        text, markup = build_day_report(user_id, datetime.date.fromisoformat(row["spent_date"]))
        await safe_edit(query, text, markup)
    elif action == "pick":
        name = find_category_by_token(user_id, token)
        if not name:
            await query.message.reply_text("ℹ️ Такой категории уже нет. Напишите название вручную.")
            return
        update_expense(user_id, exp_id, category=name)
        last_kw = context.user_data.get("last_auto_keyword")
        if last_kw:
            save_user_category_rule(user_id, last_kw, name)
        context.user_data.clear()
        text, markup = build_expense_card(user_id, exp_id, "✅ Категория изменена.\n\n")
        await safe_edit(query, text, markup)


async def handle_expense_edit_input(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, state: str, text: str):
    """Текст, введённый после «Изменить сумму / категорию / дату»."""
    exp_id = context.user_data.get("edit_exp_id")
    row = get_expense(user_id, exp_id) if exp_id else None
    if not row:
        context.user_data.clear()
        await update.message.reply_text("ℹ️ Эта трата уже удалена.", reply_markup=MAIN_KB)
        return
    day = datetime.date.fromisoformat(row["spent_date"])
    before = day_spent(user_id, day)
    warning = ""

    if state == "wait_edit_amount":
        val = parse_amount(text)
        if val is None:
            await update.message.reply_text("⚠️ Введите сумму числом, например: `450`", parse_mode="Markdown")
            return
        update_expense(user_id, exp_id, amount=val)
        warning = limit_warning(user_id, day, before, before - row["amount"] + val)
        note = f"✅ Сумма изменена: {fmt_money(row['amount'])} → *{fmt_money(val)}*.\n\n"
    elif state == "wait_edit_date":
        new_day = parse_date_text(text)
        if new_day is None:
            await update.message.reply_text(
                "⚠️ Не понял дату. Напишите `вчера`, `28.10` или `28.10.2026`.", parse_mode="Markdown"
            )
            return
        if new_day > today_date():
            await update.message.reply_text("⚠️ Эта дата ещё не наступила. Укажите сегодня или прошедшую дату.")
            return
        update_expense(user_id, exp_id, spent_date=new_day)
        warning = limit_warning(
            user_id, new_day, day_spent(user_id, new_day) - row["amount"], day_spent(user_id, new_day)
        )
        note = f"✅ Дата изменена: {day.strftime('%d.%m.%Y')} → *{new_day.strftime('%d.%m.%Y')}*.\n\n"
    else:  # wait_edit_cat
        name = canonical_category(user_id, cap(text))[:60]
        if not name or name[0].isdigit() and parse_amount(name) is not None:
            await update.message.reply_text("⚠️ Напишите название категории, например: `Продукты`", parse_mode="Markdown")
            return
        update_expense(user_id, exp_id, category=name)
        last_kw = context.user_data.get("last_auto_keyword")
        if last_kw:
            save_user_category_rule(user_id, last_kw, name)
        note = f"✅ Категория изменена: {esc(row['category'])} → *{esc(name)}*.\n\n"

    context.user_data.clear()
    card, markup = build_expense_card(user_id, exp_id, note + warning)
    await update.message.reply_text(card, parse_mode="Markdown", reply_markup=markup)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id

    if data.startswith("restore_"):
        if is_admin(update):
            await restore_callback(update, context, data)
        return

    if data.startswith("chartm_"):
        # выбор месяца: список месяцев вместо кнопок графика (сам график не перерисовывается)
        kind, _, key = data.removeprefix("chartm_").partition("_")
        if kind in CHARTS:
            months = _list_months(user_id, limit=24)
            btns = [
                InlineKeyboardButton(_month_title(m), callback_data=f"chart_{kind}_{_month_key(m)}") for m in months
            ]
            rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
            rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"chart_{kind}_{key}" if key else f"chart_{kind}")])
            try:
                await query.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(rows))
            except TelegramError:
                pass
        return

    if data.startswith("chart_"):
        kind, _, key = data.removeprefix("chart_").partition("_")
        if kind not in CHARTS:
            return
        month = None
        if key:
            month = parse_month_key(user_id, key)
            if not month:
                await query.message.reply_text("ℹ️ Этого месяца нет в истории.")
                return
        # из текстового сообщения (карточка месяца) картинку нужно прислать новым сообщением
        await send_chart_kind(query.message, user_id, kind, edit=bool(query.message.photo), month=month)
        return

    if data == "months_list":
        text, markup = build_months_overview(user_id)
        try:
            await query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except TelegramError:
            pass
        return

    if data.startswith("month_") or data.startswith("monthxls_"):
        first_day = parse_month_key(user_id, data.split("_", 1)[1])
        if not first_day:
            await query.message.reply_text("ℹ️ Этого месяца нет в истории.")
            return
        if data.startswith("monthxls_"):
            is_cur = (first_day.year, first_day.month) == (today_date().year, today_date().month)
            try:
                buf = await asyncio.to_thread(
                    update_excel_file, user_id, today_date() if is_cur else first_day
                )
                await query.message.reply_document(
                    document=buf,
                    filename=f"Бюджет_{first_day.strftime('%m_%Y')}.xlsx",
                    caption=f"📥 Таблица бюджета за {_month_title(first_day)}",
                )
            except Exception:
                logger.exception("Ошибка при формировании Excel за месяц:")
                await query.message.reply_text("⚠️ Не удалось сформировать Excel. Попробуйте позже.")
            return
        text, markup = build_month_detail(user_id, first_day)
        try:
            await query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except TelegramError:
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    if data.startswith("cat_") or data.startswith("catx_"):
        key = data.split("_", 1)[1]
        month = parse_month_key(user_id, key)
        if not month:
            await query.message.reply_text("ℹ️ Этого месяца нет в истории.")
            return
        if data.startswith("catx_"):
            text, markup = build_categories_matrix(user_id, month)
        else:
            text, markup = build_categories_report(user_id, month)
        await safe_edit(query, text, markup)
        return

    if data.startswith("xe_") or data.startswith("xc_"):
        await expense_edit_callback(update, context, data)
        return

    if data == "restart_bot_action":
        context.user_data.clear()
        await refresh_settings(query, user_id, "🧹 *Текущий ввод сброшен.*\n\n")
        return

    if data == "full_reset_action":
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("🗑 Да, удалить всё", callback_data="full_reset_confirm"),
            InlineKeyboardButton("Отмена", callback_data="full_reset_cancel"),
        ]])
        await query.message.reply_text(
            "⚠️ *Удалить все настройки, траты и копилку?* Это нельзя отменить.",
            parse_mode="Markdown",
            reply_markup=markup,
        )
        return

    if data == "full_reset_cancel":
        await query.message.edit_text("Отменено, данные не тронуты.")
        return

    if data == "full_reset_confirm":
        with db() as conn:
            conn.execute("DELETE FROM settings WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM mandatory WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM expenses WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM savings_topups WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM day_limits WHERE user_id = ?", (user_id,))
            conn.execute("DELETE FROM month_settings WHERE user_id = ?", (user_id,))
        context.user_data.clear()
        context.user_data["wizard_step"] = "wizard_mode"
        await query.message.edit_text(
            "⚠️ *Все ваши данные, настройки, траты и копилка удалены.*\n\n"
            "🧙‍♂️ Давайте настроим бюджет заново с чистого листа.\n\n"
            "📥 *Шаг 1 из 5:* Как вы получаете деньги?",
            parse_mode="Markdown",
            reply_markup=mode_markup("wiz_mode_"),
        )
        return

    if data.startswith("wiz_mode_"):
        chosen = data.removeprefix("wiz_mode_")
        if chosen not in INCOME_MODES:
            return
        set_setting(user_id, "income_mode", chosen)
        context.user_data["wizard_step"] = "wizard_income"
        await query.message.edit_text(
            "🏦 *Шаг 2 из 5:* Введи сумму дохода (число следующим сообщением, например: `1000` или `50000`):",
            parse_mode="Markdown",
        )
        return

    if data == "set_daily_income":
        context.user_data["state"] = "wait_daily_income"
        mode = get_setting(user_id, "income_mode", "daily")
        unit = INCOME_UNIT_PROMPT.get(mode, "в день")
        await query.message.reply_text(
            f"🏦 Введите сумму вашего дохода *{unit}* (например: `1000` или `50050`):",
            parse_mode="Markdown",
        )

    elif data == "set_income_mode":
        await query.message.reply_text("🔄 Выберите график получения дохода:", reply_markup=mode_markup("mode_"))

    elif data.startswith("mode_"):
        m_type = data.removeprefix("mode_")
        if m_type not in INCOME_MODES:
            return
        set_setting(user_id, "income_mode", m_type)
        await refresh_settings(query, user_id, "✅ График дохода изменён!\n\n")

    elif data == "toggle_report_chart":
        cur = get_setting(user_id, "report_chart", "1")
        set_setting(user_id, "report_chart", "0" if cur == "1" else "1")
        await refresh_settings(query, user_id)

    elif data == "toggle_month_report":
        cur = get_setting(user_id, "month_report", "1")
        set_setting(user_id, "month_report", "1" if cur == "0" else "0")
        await refresh_settings(query, user_id)

    elif data == "toggle_week_summary":
        cur = get_setting(user_id, "week_summary", "1")
        set_setting(user_id, "week_summary", "1" if cur == "0" else "0")
        await refresh_settings(query, user_id)

    elif data == "toggle_warn_limit":
        cur = get_setting(user_id, "warn_limit", "1")
        set_setting(user_id, "warn_limit", "1" if cur == "0" else "0")
        await refresh_settings(query, user_id)

    elif data == "toggle_auto_category":
        cur = get_setting(user_id, "auto_category", "1")
        set_setting(user_id, "auto_category", "0" if cur != "0" else "1")
        await refresh_settings(query, user_id)

    elif data == "run_fix_categories":
        res = recategorize_user_expenses(user_id=user_id)
        scanned = res["total_scanned"]
        updated = res["updated_count"]
        changes = res["changes"]
        if updated == 0:
            msg_text = "✨ *Все категории уже в полном порядке!*\nНеправильных или разрозненных категорий не найдено."
        else:
            lines = [
                "🪄 *Категории успешно обновлены!*",
                f"• Проверено трат: *{scanned}*",
                f"• Исправлено трат: *{updated}*\n",
                "*Произведённые изменения:*",
            ]
            sorted_changes = sorted(changes.items(), key=lambda kv: kv[1], reverse=True)[:10]
            for (old_c, new_c), count in sorted_changes:
                lines.append(f"• `{esc(old_c)}` → *{esc(new_c)}* ({count} шт.)")
            msg_text = "\n".join(lines)
        await query.message.reply_text(msg_text, parse_mode="Markdown")
        await refresh_settings(query, user_id)

    elif data == "set_days_count":
        context.user_data["state"] = "wait_days_count"
        await query.message.reply_text(
            "📅 Сколько дней учитывать в месяце?\n"
            "• Введите число (например: `30`, `31`)\n"
            "• Или отправьте слово `авто`, чтобы бот брал реальное количество дней из календаря.",
            parse_mode="Markdown",
        )

    elif data == "set_reminder_time":
        context.user_data["state"] = "wait_reminder_time"
        await query.message.reply_text(
            f"⏰ Введите время ({TZ_LABEL}) для вечернего отчёта (формат `ЧЧ:ММ`, например: `21:30`):",
            parse_mode="Markdown",
        )

    elif data == "set_save":
        context.user_data["state"] = "wait_save"
        await query.message.reply_text(
            "💰 Введите сумму плановой копилки за месяц (например: `5000`, или `0`, чтобы отключить):",
            parse_mode="Markdown",
        )

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
        try:
            mand_id = int(data.split("_")[1])
        except (ValueError, IndexError):
            return
        freeze_past_limits(user_id)
        with db() as conn:
            conn.execute("DELETE FROM mandatory WHERE id = ? AND user_id = ?", (mand_id, user_id))
        await refresh_settings(query, user_id, "✅ Обязательный расход удалён!\n\n")

    elif data.startswith("paymand_q_"):
        try:
            mid = int(data.split("_")[2])
        except (ValueError, IndexError):
            return
        status = get_month_mandatory_status(user_id)
        target_item = next((it for it in status["items"] if it["id"] == mid), None)
        if target_item:
            amt = target_item["remaining"] if target_item["remaining"] > 0 else target_item["plan"]
            record_mandatory_payment(user_id, mid, amt, today_date())
            await query.answer(f"Оплачено: {fmt_money(amt)} ✅")
            text_resp, markup = build_bills_card(user_id)
            try:
                await query.edit_message_text(text_resp, parse_mode="Markdown", reply_markup=markup)
            except Exception:
                pass
        return

    elif data.startswith("paymand_c_"):
        try:
            mid = int(data.split("_")[2])
        except (ValueError, IndexError):
            return
        status = get_month_mandatory_status(user_id)
        it = next((x for x in status["items"] if x["id"] == mid), None)
        name = it["name"] if it else "Счёт"
        context.user_data["state"] = f"wait_paymand_amount_{mid}"
        await query.answer()
        await query.message.reply_text(
            f"✏️ Введи фактически оплаченную сумму для *{esc(name)}* (например: `4850`):\n"
            f"_Для отмены напиши /cancel_",
            parse_mode="Markdown",
        )
        return

    elif data == "bills_history":
        status = get_month_mandatory_status(user_id)
        payments = status["all_payments"]
        if not payments:
            await query.answer("В этом месяце оплат счетов пока нет", show_alert=True)
            return
        lines = ["📜 *История оплат счетов за этот месяц:*\n"]
        for p in payments:
            name = p.get("mandatory_name") or "Счёт"
            lines.append(f"• {p['payment_date']}: *{esc(name)}* — {fmt_money(p['amount'])}")
        lines.append("\nНажми на кнопку ниже для отмены платежа:")
        await query.answer()
        try:
            await query.edit_message_text(
                "\n".join(lines),
                parse_mode="Markdown",
                reply_markup=bills_history_kb(payments),
            )
        except Exception:
            pass
        return

    elif data.startswith("del_paymand_"):
        try:
            pid = int(data.split("_")[2])
        except (ValueError, IndexError):
            return
        deleted = remove_mandatory_payment(user_id, pid)
        if deleted:
            await query.answer("Оплата отменена 🗑")
        else:
            await query.answer("Оплата не найдена")
        text_resp, markup = build_bills_card(user_id)
        try:
            await query.edit_message_text(text_resp, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            pass
        return

    elif data == "bills_refresh":
        text_resp, markup = build_bills_card(user_id)
        await query.answer("Обновлено")
        try:
            await query.edit_message_text(text_resp, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            pass
        return

    elif data == "restart_wizard":
        context.user_data.clear()
        context.user_data["wizard_step"] = "wizard_mode"
        await query.message.edit_text(
            "🔄 *Запуск мастера настройки*\n\n📥 *Шаг 1 из 5:* Как ты получаешь деньги?",
            parse_mode="Markdown",
            reply_markup=mode_markup("wiz_mode_"),
        )

    elif data.startswith("hdelexp_"):
        try:
            exp_id = int(data.split("_")[1])
        except (ValueError, IndexError):
            return
        delete_expense(user_id, exp_id)
        text, markup = build_history(user_id)
        try:
            await query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except TelegramError:
            pass

    elif data == "piggy_history":
        text, markup = build_piggy_history(user_id)
        await query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)

    elif data.startswith("deltop_") or data.startswith("hdeltop_"):
        try:
            tid = int(data.split("_")[1])
        except (ValueError, IndexError):
            return
        freeze_past_limits(user_id)
        with db() as conn:
            deleted = conn.execute(
                "DELETE FROM savings_topups WHERE id = ? AND user_id = ?", (tid, user_id)
            ).rowcount
        if data.startswith("hdeltop_"):
            text, markup = build_piggy_history(user_id)
            try:
                await query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
            except TelegramError:
                pass
        else:
            msg = "↩️ Операция с копилкой отменена." if deleted else "ℹ️ Эта операция уже отменена."
            try:
                await query.message.edit_text(msg)
            except TelegramError:
                pass

    elif data.startswith("delexp_"):
        try:
            exp_id = int(data.split("_")[1])
        except (ValueError, IndexError):
            return
        spent_date_str = delete_expense(user_id, exp_id)
        if not spent_date_str:
            await query.message.reply_text("ℹ️ Эта трата уже удалена.")
            return
        target_d = datetime.date.fromisoformat(spent_date_str)
        text, markup = build_day_report(user_id, target_d)
        try:
            await query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except TelegramError:
            pass

    elif data == "today_refresh":
        today_d = today_date()
        today_nav = [
            [
                InlineKeyboardButton("🔄 Обновить", callback_data="today_refresh"),
                InlineKeyboardButton("📈 График", callback_data="chart_overview"),
                InlineKeyboardButton("🏠 Счета", callback_data="bills_refresh"),
            ]
        ]
        text, markup = build_day_report(user_id, today_d, extra_rows=today_nav)
        await query.answer("Сводка обновлена ✅")
        try:
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=markup)
        except Exception:
            pass
        return

    elif data == "piggy_topup_btn":
        context.user_data["state"] = "wait_piggy"
        await query.answer()
        await query.message.reply_text(
            "💰 *Пополнение плановой копилки*\n\n"
            "Сколько хочешь отложить? (например: `500 Подработка` или просто `500`):\n"
            "_Для отмены отправьте /cancel_",
            parse_mode="Markdown",
        )
        return

    elif data == "piggy_withdraw_btn":
        context.user_data["state"] = "wait_withdraw_piggy"
        await query.answer()
        await query.message.reply_text(
            "💸 *Снятие из копилки*\n\n"
            "Введите сумму и причину, например: `3000 ремонт телефона`:\n"
            "_Для отмены отправьте /cancel_",
            parse_mode="Markdown",
        )
        return

    elif data == "menu_excel":
        await query.answer("Формирую Excel-файл...")
        try:
            buf = await asyncio.to_thread(update_excel_file, user_id)
            await query.message.reply_document(
                document=buf,
                filename=f"Бюджет_{today_date().strftime('%m_%Y')}.xlsx",
                caption="📥 Ваша персональная таблица бюджета с деталями трат",
            )
        except Exception:
            logger.exception("Ошибка при выгрузке Excel:")
            await query.message.reply_text("⚠️ Не удалось сформировать Excel. Попробуйте позже.")
        return

    elif data == "menu_categories":
        text, markup = build_categories_report(user_id, today_date())
        await query.answer()
        await safe_edit(query, text, markup)
        return

    elif data == "menu_months":
        text, markup = build_months_overview(user_id)
        await query.answer()
        try:
            await query.message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
        except TelegramError:
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=markup)
        return

    elif data == "menu_settings":
        context.user_data.clear()
        await query.answer()
        try:
            await query.message.edit_text(
                build_settings_text(user_id),
                parse_mode="Markdown",
                reply_markup=settings_inline_kb(user_id),
            )
        except TelegramError:
            await query.message.reply_text(
                build_settings_text(user_id),
                parse_mode="Markdown",
                reply_markup=settings_inline_kb(user_id),
            )
        return

    elif data == "menu_undo":
        await query.answer()
        last = get_last_expense(user_id)
        if not last:
            await query.message.reply_text("ℹ️ Нет трат для отмены.")
            return
        delete_expense(user_id, last["id"])
        day = datetime.date.fromisoformat(last["spent_date"])
        text, markup = build_day_report(user_id, day)
        await query.message.reply_text(
            f"↩️ Отменена трата: *{fmt_money(last['amount'])}* — {esc(last['category'])}\n\n{text}",
            parse_mode="Markdown",
            reply_markup=markup or MAIN_KB,
        )
        return


# --- Текстовые сообщения ---
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id
    if get_setting(user_id, "blocked") == "1":
        set_setting(user_id, "blocked", "0")

    # нажатие кнопки меню всегда прерывает незавершённый ввод
    if text in MENU_TEXTS:
        context.user_data.clear()

    wizard_step = context.user_data.get("wizard_step")
    if wizard_step:
        if wizard_step == "wizard_income":
            val = parse_amount(text)
            if val is None:
                await update.message.reply_text("⚠️ Введи положительное число, например: `1000`", parse_mode="Markdown")
                return
            set_setting(user_id, "daily_income", val)
            context.user_data["wizard_step"] = "wizard_save"
            await update.message.reply_text(
                "💰 *Шаг 3 из 5:* Сколько откладывать в плановую копилку за месяц? (например: `5000` или `0`):",
                parse_mode="Markdown",
            )
            return

        if wizard_step == "wizard_save":
            val = parse_amount(text, allow_zero=True)
            if val is None:
                await update.message.reply_text("⚠️ Введи число, например: `5000`", parse_mode="Markdown")
                return
            set_setting(user_id, "save", val)
            context.user_data["wizard_step"] = "wizard_mand"
            await update.message.reply_text(
                "🏠 *Шаг 4 из 5:* Добавь первый *обязательный расход* (название и сумма, например: "
                "`Аренда 15000`, или напиши `нет`, чтобы пропустить):",
                parse_mode="Markdown",
            )
            return

        if wizard_step == "wizard_mand":
            if text.lower() not in ["нет", "0", "пропустить", "-"]:
                parsed = parse_label_amount(text)
                if not parsed:
                    await update.message.reply_text(
                        "⚠️ Напиши название и сумму, например: `Аренда 15000`, или `нет`.",
                        parse_mode="Markdown",
                    )
                    return
                amount, name = parsed
                upsert_mandatory(user_id, name, amount)
            context.user_data["wizard_step"] = "wizard_time"
            await update.message.reply_text(
                f"⏰ *Шаг 5 из 5:* В какое *время ({TZ_LABEL})* присылать вечерний отчёт? (например: `21:00`):",
                parse_mode="Markdown",
            )
            return

        if wizard_step == "wizard_time":
            t = parse_time(text)
            if not t:
                await update.message.reply_text("⚠️ Введи время в формате `ЧЧ:ММ`, например: `21:00`", parse_mode="Markdown")
                return
            save_reminder_time(user_id, *t)
            context.user_data.clear()
            await update.message.reply_text(
                "🎉 *Настройка завершена!*\n\n"
                "Чтобы записать трату, напиши, например: `450 продукты`.",
                parse_mode="Markdown",
                reply_markup=MAIN_KB,
            )
            return

    if text == "📊 Сегодня":
        await show_today(update, context)
        return
    if text == "📅 Месяц":
        await show_month(update, context)
        return
    if text == "🏠 Счета":
        await show_bills(update, context)
        return
    if text == "🏦 Копилка":
        await show_piggy(update, context)
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
    if text in ["⚙️ Меню & Ещё", "⚙️ Меню", "⚙️ Ещё"]:
        await show_menu_hub(update, context)
        return
    if text in ["↩️ Отменить последнюю", "↩️ Отменить"]:
        await undo_last(update, context)
        return
    if text == "📈 График":
        await send_chart(update, context)
        return
    if text == "📥 Скачать Excel":
        await send_excel(update, context)
        return
    if text == "📚 Месяцы":
        await show_months(update, context)
        return
    if text == "🏷 Категории":
        await show_categories(update, context)
        return
    if text == "🧾 Траты":
        await show_history(update, context)
        return
    if text == "⚙️ Настройки":
        await show_settings(update, context)
        return

    state = context.user_data.get("state")

    if state and state.startswith("wait_paymand_amount_"):
        mid = int(state.split("_")[-1])
        amt = parse_amount(text)
        if amt is None or amt <= 0:
            await update.message.reply_text("⚠️ Введи сумму числом, например: `15000` или /cancel.")
            return
        context.user_data.pop("state", None)
        record_mandatory_payment(user_id, mid, amt, today_date())
        status = get_month_mandatory_status(user_id)
        it = next((x for x in status["items"] if x["id"] == mid), None)
        name = it["name"] if it else "Счёт"
        text_resp = (
            f"✅ *Оплата обязательного счёта учтена!*\n\n"
            f"🏠 *{esc(name)}*: {fmt_money(amt)}\n"
            f"📅 Дата: *{today_date().strftime('%d.%m.%Y')}*\n\n"
            f"💡 _Этот платёж НЕ уменьшает твой дневной лимит._"
        )
        await update.message.reply_text(text_resp, parse_mode="Markdown", reply_markup=MAIN_KB)
        return

    if state == "wait_restore_file":
        await update.message.reply_text("♻️ Жду файл базы (.db) документом. Отмена: /cancel.")
        return

    if state in ("wait_edit_amount", "wait_edit_cat", "wait_edit_date"):
        await handle_expense_edit_input(update, context, user_id, state, text)
        return

    if state == "wait_piggy":
        parsed = parse_amount_comment(text, "Пополнение")
        if not parsed:
            await update.message.reply_text("⚠️ Введи сумму числом, например: `500 Подработка`", parse_mode="Markdown")
            return
        await piggy_add(update, context, user_id, *parsed)
        return

    if state == "wait_withdraw_piggy":
        parsed = parse_amount_comment(text, "Снятие")
        if not parsed:
            await update.message.reply_text("⚠️ Введи сумму и причину, например: `3000 ремонт телефона`", parse_mode="Markdown")
            return
        amount, comment = parsed
        info = get_budget_info(user_id)
        if amount > info["piggy_total"] + 0.005:
            await update.message.reply_text(
                f"⚠️ В копилке только *{fmt_money(max(0.0, info['piggy_total']))}*, а вы хотите снять "
                f"*{fmt_money(amount)}*. Введите сумму поменьше или нажмите /cancel.",
                parse_mode="Markdown",
            )
            return
        with db() as conn:
            tid = conn.execute(
                "INSERT INTO savings_topups (user_id, topup_date, amount, comment) VALUES (?, ?, ?, ?)",
                (user_id, today_date().isoformat(), -amount, f"Снятие: {comment}"),
            ).lastrowid
        context.user_data.clear()
        info = get_budget_info(user_id)
        await update.message.reply_text(
            f"💸 Снято из копилки: *-{fmt_money(amount)}* ({esc(comment)})!\n"
            f"🏦 Осталось в копилке: *{fmt_money(info['piggy_total'])}*\n"
            "_На дневной лимит и банк сдачи снятие не влияет._",
            parse_mode="Markdown",
            reply_markup=piggy_undo_kb(tid),
        )
        return

    if state == "wait_daily_income":
        val = parse_amount(text)
        if val is None:
            await update.message.reply_text("⚠️ Введите положительное число, например: `1000`", parse_mode="Markdown")
            return
        set_setting(user_id, "daily_income", val)
        context.user_data.clear()
        await update.message.reply_text(
            "✅ Доход обновлён!\n\n" + build_settings_text(user_id),
            parse_mode="Markdown",
            reply_markup=settings_inline_kb(user_id),
        )
        return

    if state == "wait_days_count":
        if text.lower() == "авто":
            freeze_past_limits(user_id)
            with db() as conn:
                conn.execute(
                    "DELETE FROM settings WHERE user_id = ? AND key = 'custom_days_in_month'", (user_id,)
                )
            context.user_data.clear()
            await update.message.reply_text(
                "✅ Дней в месяце теперь берётся *автоматически по календарю*.\n\n" + build_settings_text(user_id),
                parse_mode="Markdown",
                reply_markup=settings_inline_kb(user_id),
            )
            return
        try:
            days_val = int(text)
        except ValueError:
            await update.message.reply_text("⚠️ Введи число или слово `авто`.", parse_mode="Markdown")
            return
        if not 1 <= days_val <= 31:
            await update.message.reply_text("⚠️ Введи число от 1 до 31 или слово `авто`.", parse_mode="Markdown")
            return
        set_setting(user_id, "custom_days_in_month", days_val)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ Фиксированное количество дней в месяце: *{days_val}*\n\n" + build_settings_text(user_id),
            parse_mode="Markdown",
            reply_markup=settings_inline_kb(user_id),
        )
        return

    if state == "wait_reminder_time":
        t = parse_time(text)
        if not t:
            await update.message.reply_text(
                "⚠️ Неверный формат. Введи время как `ЧЧ:ММ` (часы 0-23, минуты 0-59), например: `21:30`",
                parse_mode="Markdown",
            )
            return
        save_reminder_time(user_id, *t)
        context.user_data.clear()
        await update.message.reply_text(
            f"⏰ Время вечернего отчёта изменено на *{t[0]:02d}:{t[1]:02d} ({TZ_LABEL})*!\n\n" + build_settings_text(user_id),
            parse_mode="Markdown",
            reply_markup=settings_inline_kb(user_id),
        )
        return

    if state == "wait_save":
        val = parse_amount(text, allow_zero=True)
        if val is None:
            await update.message.reply_text("⚠️ Введи число, например: `5000`", parse_mode="Markdown")
            return
        set_setting(user_id, "save", val)
        context.user_data.clear()
        await update.message.reply_text(
            f"💰 Плановая копилка обновлена: *{fmt_money(val)}*\n\n" + build_settings_text(user_id),
            parse_mode="Markdown",
            reply_markup=settings_inline_kb(user_id),
        )
        return

    if state == "wait_mand":
        parsed = parse_label_amount(text)
        if not parsed:
            await update.message.reply_text("⚠️ Напиши название и сумму, например: `Аренда 15000`", parse_mode="Markdown")
            return
        amount, name = parsed
        updated = upsert_mandatory(user_id, name, amount)
        context.user_data.clear()
        await update.message.reply_text(
            f"✅ {'Обновлён' if updated else 'Добавлен'} обязательный расход: *{esc(name)} — {fmt_money(amount)}*\n\n" + build_settings_text(user_id),
            parse_mode="Markdown",
            reply_markup=settings_inline_kb(user_id),
        )
        return

    if text.startswith("+"):
        parsed = parse_amount_comment(text[1:], "Пополнение")
        if parsed:
            await piggy_add(update, context, user_id, *parsed)
            return

    # Распознавание оплаты обязательного счёта (например: «оплата интернет 800» или «счет жкх 4850»)
    mand_pay = parse_mandatory_payment_message(text)
    if mand_pay:
        amount, name = mand_pay
        matched = find_matching_mandatory(user_id, name)
        if matched:
            record_mandatory_payment(user_id, matched["id"], amount, today_date())
            text_resp = (
                f"✅ *Оплата обязательного счёта учтена!*\n\n"
                f"🏠 *{esc(matched['name'])}*: {fmt_money(amount)}\n"
                f"📅 Дата: *{today_date().strftime('%d.%m.%Y')}*\n\n"
                f"💡 _Этот платёж НЕ уменьшает твой дневной лимит._"
            )
            await update.message.reply_text(text_resp, parse_mode="Markdown", reply_markup=MAIN_KB)
            return
        else:
            await update.message.reply_text(
                f"🤔 Обязательный счёт «{esc(name)}» не найден в твоём списке счетов.\n"
                f"Проверь список счетов по кнопке «🏠 Счета» или добавь его в Настройках.",
                parse_mode="Markdown",
                reply_markup=MAIN_KB,
            )
            return

    # Распознавание банковских уведомлений, чеков и СМС («Покупка 450р Пятерочка»)
    bank_parsed = parse_bank_notification(text)
    if bank_parsed and bank_parsed.get("amount"):
        amount = bank_parsed["amount"]
        spent_d = bank_parsed.get("spent_date") or today_date()
        if spent_d > today_date():
            spent_d = today_date()
        merchant = bank_parsed.get("merchant") or ""
        ensure_start_day(user_id)
        classified_cat, _ = classify_expense(user_id, merchant, used_categories(user_id))
        category = canonical_category(user_id, classified_cat if classified_cat != "Разное" else (cap(merchant) or DEFAULT_CATEGORY))
        before = day_spent(user_id, spent_d)
        with db() as conn:
            exp_id = conn.execute(
                "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
                (user_id, spent_d.isoformat(), amount, category),
            ).lastrowid
        warning = limit_warning(user_id, spent_d, before, before + amount)
        top_cats = used_categories(user_id, limit=6, exclude=[category])
        chips = category_chips(user_id, exp_id, top_cats) if top_cats else []
        chips.append([
            InlineKeyboardButton("✏️ Сменить категорию", callback_data=f"xe_cat_{exp_id}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"xe_del_{exp_id}"),
        ])
        report_text, markup = build_day_report(user_id, spent_d, extra_rows=chips)
        merch_str = f" ({esc(merchant)})" if merchant else ""
        await update.message.reply_text(
            f"🧾 Распознана оплата: *{fmt_money(amount)}* — {esc(category)}{merch_str}\n\n"
            f"{warning}{report_text}",
            parse_mode="Markdown",
            reply_markup=markup or MAIN_KB,
        )
        return

    parsed = parse_expense_message(text)
    if not parsed:
        await update.message.reply_text(
            "🤔 Не понял сообщение.\nЧтобы записать трату: `450 продукты`.",
            parse_mode="Markdown",
            reply_markup=MAIN_KB,
        )
        return

    amount, category, spent_d = parsed
    if spent_d > today_date():
        await update.message.reply_text(
            f"⚠️ Дата {spent_d.strftime('%d.%m.%Y')} ещё не наступила, трату не записал. "
            "Укажите сегодня, вчера или прошедшую дату.",
            reply_markup=MAIN_KB,
        )
        return
    ensure_start_day(user_id)
    was_default = category == DEFAULT_CATEGORY
    similar = None if was_default else similar_category(user_id, category)  # до записи, иначе найдёт саму себя

    auto_cat_on = get_setting(user_id, "auto_category", "1") != "0"
    note_hint = ""
    candidate_chips = None

    if auto_cat_on and not was_default and not similar:
        known_keys = {cat_key(c) for c in used_categories(user_id)}
        if cat_key(category) not in known_keys:
            classified_cat, matched_kw = classify_expense(user_id, category, used_categories(user_id))
            if classified_cat and classified_cat != "Разное":
                raw_input = category
                category = classified_cat
                note_hint = f"\n💡 _Категория подобрана по слову «{esc(raw_input)}»_"
                candidate_chips = [raw_input]
                context.user_data["last_auto_keyword"] = raw_input

    category = canonical_category(user_id, category)  # «ПРОДУКТЫ» -> уже существующее «Продукты»
    before = day_spent(user_id, spent_d)
    with db() as conn:
        exp_id = conn.execute(
            "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
            (user_id, spent_d.isoformat(), amount, category),
        ).lastrowid

    warning = limit_warning(user_id, spent_d, before, before + amount)
    extra = suggestion_rows(user_id, exp_id, category, was_default, similar, candidates=candidate_chips)
    report_text, markup = build_day_report(user_id, spent_d, extra_rows=extra)
    await update.message.reply_text(
        f"✅ Записано: *{fmt_money(amount)}* — {esc(category)} ({spent_d.strftime('%d.%m')}){note_hint}\n\n"
        f"{warning}{report_text}",
        parse_mode="Markdown",
        reply_markup=markup or MAIN_KB,
    )


def main():
    global STARTUP_REPORT
    if not BOT_TOKEN:
        raise RuntimeError("Не указан BOT_TOKEN в файле .env")

    # Старая раскладка: база лежала рядом с bot.py. Копируем её на том (оригинал не трогаем) ДО проверки
    # хранилища, иначе бот стартовал бы с пустой базой на томе, а настоящая осталась бы лежать в стороне.
    try:
        adopted = datasafe.adopt_legacy_files(
            DB_PATH, STATE_PATH, DATA_DIR, LEGACY_DIRS, allow_ephemeral=ALLOW_EPHEMERAL_DB,
        )
    except Exception:
        logger.exception("Не удалось перенести старую базу на том. Оригинал не тронут, бот продолжит запуск.")
        adopted = datasafe.AdoptResult(notes=["Перенос старой базы на том завершился ошибкой (подробности в логах)."])
    for note in adopted.notes:
        logger.warning("Перенос данных: %s", note)

    # Сначала смотрим на хранилище и только потом создаём/мигрируем базу: так видно, что было до запуска
    report = datasafe.check_startup(
        DB_PATH, DATA_DIR,
        expect_data=EXPECT_EXISTING_DATA,
        allow_ephemeral=ALLOW_EPHEMERAL_DB,
        check_mount=not SKIP_MOUNT_CHECK,
    )
    report.notes.extend(adopted.notes)
    if adopted.db_from:
        datasafe.refresh_marker(DATA_DIR, DB_PATH, adopted_from=adopted.db_from, adopted_at=_utc_iso())
    STARTUP_REPORT = report
    log_startup_report(report)
    if STRICT_DATA_PATH and report.path_bad and not ALLOW_EPHEMERAL_DB:
        raise SystemExit(
            f"STRICT_DATA_PATH=1: DB_PATH ({report.db_path}) не внутри {DATA_DIR}. Бот не запущен, "
            "чтобы не писать данные туда, где они пропадут при пересборке."
        )

    init_db()
    os.makedirs(os.path.dirname(os.path.abspath(STATE_PATH)), exist_ok=True)
    logger.info("Бот запущен. Часовой пояс: %s, база: %s", TZ_NAME, DB_PATH)

    persistence = PicklePersistence(filepath=STATE_PATH)
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).post_init(post_init).build()

    if app.job_queue:
        app.job_queue.run_repeating(daily_reminder_job, interval=60, first=10)
        app.job_queue.run_repeating(maintenance_job, interval=600, first=45)
        app.job_queue.run_repeating(monthly_report_job, interval=120, first=20)
        app.job_queue.run_repeating(week_summary_job, interval=120, first=25)

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("today", show_today))
    app.add_handler(CommandHandler("week", show_week))
    app.add_handler(CommandHandler("month", show_month))
    app.add_handler(CommandHandler("months", show_months))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("backup", backup_cmd))
    app.add_handler(CommandHandler("restore", restore_cmd))
    app.add_handler(CommandHandler("chart", send_chart))
    app.add_handler(CommandHandler("excel", send_excel))
    app.add_handler(CommandHandler("settings", show_settings))
    app.add_handler(CommandHandler("undo", undo_last))
    app.add_handler(CommandHandler("piggy", show_piggy))
    app.add_handler(CommandHandler("history", show_history))
    app.add_handler(CommandHandler("bills", show_bills))
    app.add_handler(CommandHandler("mandatory", show_bills))
    app.add_handler(CommandHandler("categories", show_categories))
    app.add_handler(CommandHandler("fix_categories", fix_categories_cmd))
    app.add_handler(CommandHandler("recategorize", fix_categories_cmd))
    app.add_handler(CommandHandler("menu", show_menu_hub))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    app.run_polling()


if __name__ == "__main__":
    main()
