import datetime
import math
import re
from telegram.helpers import escape_markdown
from kopilka.config import TZ, MAX_AMOUNT

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
    """Число из строки или None: «1500», «1 500», «1,000», «300₽», «1500р», «10,5»."""
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
