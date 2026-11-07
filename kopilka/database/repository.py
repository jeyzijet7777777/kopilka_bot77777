import calendar
import datetime
import json
import logging
from kopilka.database.connection import db
from kopilka.utils.formatting import today_date

logger = logging.getLogger("budget-bot")

# --- Settings ---
def get_setting(user_id: int, key: str, default=None):
    with db() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE user_id = ? AND key = ?",
            (user_id, key),
        ).fetchone()
        return row["value"] if row else default

def set_setting(user_id: int, key: str, value):
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, str(value)),
        )

def delete_setting(user_id: int, key: str):
    with db() as conn:
        conn.execute("DELETE FROM settings WHERE user_id = ? AND key = ?", (user_id, key))

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

def _month_key(d: datetime.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"

def _next_month(d: datetime.date) -> datetime.date:
    return (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)

def save_reminder_time(user_id: int, h: int, mn: int):
    val = f"{h:02d}:{mn:02d}"
    set_setting(user_id, "reminder_time", val)
    now = datetime.datetime.now(datetime.timezone.utc)
    from kopilka.config import TZ
    now = datetime.datetime.now(TZ)
    if now.hour * 60 + now.minute >= h * 60 + mn:
        set_setting(user_id, "last_report", today_date().isoformat())

# --- Day Limits ---
def clear_limits_from(user_id: int, from_d: datetime.date):
    """Сбрасывает сохранённые снимки лимита начиная с from_d."""
    with db() as conn:
        conn.execute(
            "DELETE FROM day_limits WHERE user_id = ? AND day >= ?",
            (user_id, from_d.isoformat()),
        )

def snapshot_day_limit(user_id: int, day_d: datetime.date, limit_val: float):
    with db() as conn:
        conn.execute(
            "INSERT INTO day_limits (user_id, day, daily_limit) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, day) DO UPDATE SET daily_limit = excluded.daily_limit",
            (user_id, day_d.isoformat(), float(limit_val)),
        )

# --- Mandatory Items ---
def get_mandatory_items(user_id: int) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, amount FROM mandatory WHERE user_id = ? ORDER BY id",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]

def upsert_mandatory(user_id: int, name: str, amount: float) -> bool:
    """True если обновлена, False если создана новая."""
    with db() as conn:
        rows = conn.execute("SELECT id, name FROM mandatory WHERE user_id = ?", (user_id,)).fetchall()
        for r in rows:
            if r["name"].strip().lower() == name.strip().lower():
                conn.execute("UPDATE mandatory SET amount = ? WHERE id = ?", (amount, r["id"]))
                return True
        conn.execute(
            "INSERT INTO mandatory (user_id, name, amount) VALUES (?, ?, ?)",
            (user_id, name.strip(), amount),
        )
        return False

def delete_mandatory(user_id: int, mand_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM mandatory WHERE id = ? AND user_id = ?", (mand_id, user_id))
        return cur.rowcount > 0

# --- Mandatory Payments (NEW in V2) ---
def add_mandatory_payment(
    user_id: int,
    mandatory_id: int,
    month: str,
    amount: float,
    payment_date: str,
    comment: str = "",
) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO mandatory_payments (user_id, mandatory_id, month, amount, payment_date, comment) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, mandatory_id, month, float(amount), payment_date, comment.strip()),
        )
        return cur.lastrowid

def get_mandatory_payments_for_month(user_id: int, month: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT p.id, p.mandatory_id, p.month, p.amount, p.payment_date, p.comment, p.created_at, "
            "m.name as mandatory_name, m.amount as planned_amount "
            "FROM mandatory_payments p "
            "LEFT JOIN mandatory m ON p.mandatory_id = m.id "
            "WHERE p.user_id = ? AND p.month = ? "
            "ORDER BY p.payment_date, p.id",
            (user_id, month),
        ).fetchall()
        return [dict(r) for r in rows]

def get_mandatory_payments_for_day(user_id: int, day_str: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT p.id, p.mandatory_id, p.month, p.amount, p.payment_date, p.comment, "
            "m.name as mandatory_name "
            "FROM mandatory_payments p "
            "LEFT JOIN mandatory m ON p.mandatory_id = m.id "
            "WHERE p.user_id = ? AND p.payment_date = ? "
            "ORDER BY p.id",
            (user_id, day_str),
        ).fetchall()
        return [dict(r) for r in rows]

def get_mandatory_payment_by_id(user_id: int, payment_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT p.id, p.mandatory_id, p.month, p.amount, p.payment_date, p.comment, "
            "m.name as mandatory_name, m.amount as planned_amount "
            "FROM mandatory_payments p "
            "LEFT JOIN mandatory m ON p.mandatory_id = m.id "
            "WHERE p.id = ? AND p.user_id = ?",
            (payment_id, user_id),
        ).fetchone()
        return dict(row) if row else None

def delete_mandatory_payment(user_id: int, payment_id: int) -> bool:
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM mandatory_payments WHERE id = ? AND user_id = ?",
            (payment_id, user_id),
        )
        return cur.rowcount > 0

# --- Month Settings Archive ---
def archive_closed_months(user_id: int):
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


# --- User Category Rules ---
def get_user_category_rules(user_id: int) -> dict[str, str]:
    with db() as conn:
        rows = conn.execute(
            "SELECT key, value FROM settings WHERE user_id = ? AND key LIKE 'cat_rule_%'",
            (user_id,),
        ).fetchall()
        return {r["key"][9:]: r["value"] for r in rows}


def save_user_category_rule(user_id: int, keyword: str, category: str):
    k = " ".join(str(keyword).casefold().replace("ё", "е").split())
    if not k or not category:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, f"cat_rule_{k}", str(category).strip()),
        )


def delete_user_category_rule(user_id: int, keyword: str):
    k = " ".join(str(keyword).casefold().replace("ё", "е").split())
    with db() as conn:
        conn.execute(
            "DELETE FROM settings WHERE user_id = ? AND key = ?",
            (user_id, f"cat_rule_{k}"),
        )

