from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from kopilka.utils.formatting import fmt_money

def bills_inline_kb(status: dict) -> InlineKeyboardMarkup:
    """Инлайн-кнопки для экрана обязательных платежей."""
    rows = []
    items = status.get("items", [])

    for it in items:
        mid = it["id"]
        name = it["name"]
        rem = it["remaining"]
        if not it["is_paid"]:
            rows.append([
                InlineKeyboardButton(
                    f"💳 Оплатить {name[:18]} ({fmt_money(rem)})",
                    callback_data=f"paymand_q_{mid}",
                ),
                InlineKeyboardButton("✏️ Сумма", callback_data=f"paymand_c_{mid}"),
            ])
        else:
            rows.append([
                InlineKeyboardButton(f"➕ Доплатить {name[:20]}", callback_data=f"paymand_c_{mid}")
            ])

    nav_row = [
        InlineKeyboardButton("📜 История оплат", callback_data="bills_history"),
        InlineKeyboardButton("⚙️ Добавить счёт", callback_data="add_mand"),
    ]
    rows.append(nav_row)
    rows.append([InlineKeyboardButton("🔄 Обновить статус", callback_data="bills_refresh")])
    return InlineKeyboardMarkup(rows)

def bills_history_kb(payments: list[dict]) -> InlineKeyboardMarkup:
    """Кнопки отмены оплат в истории."""
    rows = []
    for p in payments[-10:]:
        pid = p["id"]
        name = p.get("mandatory_name") or "Счёт"
        amt = p["amount"]
        dt = p["payment_date"][-5:]  # MM-DD
        rows.append([
            InlineKeyboardButton(
                f"🗑 Отменить: {name[:12]} {fmt_money(amt)} ({dt})",
                callback_data=f"del_paymand_{pid}",
            )
        ])
    rows.append([InlineKeyboardButton("◀️ Назад к счетам", callback_data="bills_refresh")])
    return InlineKeyboardMarkup(rows)
