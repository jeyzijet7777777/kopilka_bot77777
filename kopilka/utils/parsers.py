import datetime
import re
from kopilka.utils.formatting import (
    cap,
    merge_thousands,
    parse_amount,
    today_date,
)

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
        if dt and 0 <= (base - dt).days <= 45:
            return dt
        return None

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

MANDATORY_PAYMENT_PREFIXES = (
    "оплата", "оплатил", "оплатила", "счет", "счёт",
    "#счет", "#счёт", "обяз", "#обяз", "чек", "#оплата"
)

def parse_mandatory_payment_message(text: str):
    """Распознаёт команду/сообщение об оплате счета:
    «оплата интернет 800», «оплатил 15000 аренда», «счет жкх 4850».
    Возвращает (amount, name) или None.
    """
    cleaned = text.strip()
    if not cleaned:
        return None
    low = cleaned.lower()
    matched_prefix = None
    for p in MANDATORY_PAYMENT_PREFIXES:
        if low.startswith(p + " ") or low.startswith(p + ":"):
            matched_prefix = p
            break
    if not matched_prefix:
        return None
    remainder = cleaned[len(matched_prefix):].lstrip(" :")
    return parse_label_amount(remainder)
