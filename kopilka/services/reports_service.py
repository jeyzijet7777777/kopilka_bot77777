import datetime
from telegram import InlineKeyboardMarkup
from kopilka.config import MONTHS_RU
from kopilka.keyboards.inline import bills_inline_kb
from kopilka.services.mandatory_service import get_month_mandatory_status
from kopilka.utils.formatting import esc, fmt_money, today_date

def build_bills_card(user_id: int, ref_date: datetime.date = None) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует карточку статуса обязательных счетов за месяц с инлайн-кнопками."""
    ref_date = ref_date or today_date()
    status = get_month_mandatory_status(user_id, ref_date)
    month_name = MONTHS_RU[ref_date.month - 1]

    lines = [f"🏠 *Обязательные счета — {month_name} {ref_date.year}*\n"]

    if not status["items"]:
        lines.append("_Обязательные расходы пока не настроены._")
        lines.append("Добавь их кнопкой ниже или через Настройки (например: «Аренда 25000»).")
    else:
        for it in status["items"]:
            lines.append(
                f"{it['status_emoji']} *{esc(it['name'])}*: {fmt_money(it['paid'])} / {fmt_money(it['plan'])} "
                f"_{it['status_text']}_"
            )
        lines.append("\n━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📊 *План на месяц:* {fmt_money(status['total_plan'])}")
        lines.append(f"✅ *Фактически оплачено:* {fmt_money(status['total_paid'])}")
        if status["total_remaining"] > 0:
            lines.append(f"⏳ *Осталось оплатить:* {fmt_money(status['total_remaining'])}")
        if status["total_overrun"] > 0:
            lines.append(f"⚠️ *Превышение плана:* +{fmt_money(status['total_overrun'])}")
        lines.append("\n💡 _Оплата счетов не уменьшает твой дневной лимит трат!_")
        lines.append("_Для оплаты нажми кнопку ниже или напиши в чат:_ `оплата аренда 15000`")

    text = "\n".join(lines)
    markup = bills_inline_kb(status)
    return text, markup
