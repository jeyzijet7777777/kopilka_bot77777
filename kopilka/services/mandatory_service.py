import datetime
from kopilka.database.repository import (
    _month_key,
    add_mandatory_payment,
    delete_mandatory,
    delete_mandatory_payment,
    get_mandatory_items,
    get_mandatory_payments_for_day,
    get_mandatory_payments_for_month,
    upsert_mandatory,
)
from kopilka.utils.formatting import today_date

def get_month_mandatory_status(user_id: int, ref_date: datetime.date = None) -> dict:
    """Полный статус исполнения обязательных платежей за указанный месяц."""
    ref_date = ref_date or today_date()
    month_str = _month_key(ref_date)

    items = get_mandatory_items(user_id)
    payments = get_mandatory_payments_for_month(user_id, month_str)

    # Группируем оплаты по mandatory_id
    paid_by_item = {}
    payments_by_item = {}
    for p in payments:
        mid = p["mandatory_id"]
        paid_by_item[mid] = paid_by_item.get(mid, 0.0) + p["amount"]
        payments_by_item.setdefault(mid, []).append(p)

    item_statuses = []
    total_plan = 0.0
    total_paid = 0.0
    total_remaining = 0.0
    total_overrun = 0.0

    for item in items:
        mid = item["id"]
        plan = float(item["amount"])
        paid = float(paid_by_item.get(mid, 0.0))
        rem = max(0.0, plan - paid)
        overrun = max(0.0, paid - plan)

        total_plan += plan
        total_paid += paid
        total_remaining += rem
        total_overrun += overrun

        if paid == 0:
            emoji = "⚪"
            status_text = "Не оплачено"
        elif paid > plan:
            emoji = "⚠️"
            status_text = f"Переплата на {overrun:,.0f} ₽"
        elif paid >= plan:
            emoji = "🟢"
            status_text = "Оплачено"
        else:
            emoji = "🟡"
            status_text = f"Частично (ост. {rem:,.0f} ₽)"

        item_statuses.append({
            "id": mid,
            "name": item["name"],
            "plan": plan,
            "paid": paid,
            "remaining": rem,
            "overrun": overrun,
            "is_paid": paid >= plan,
            "is_overrun": paid > plan,
            "status_emoji": emoji,
            "status_text": status_text,
            "payments": payments_by_item.get(mid, []),
        })

    return {
        "month": month_str,
        "items": item_statuses,
        "total_plan": total_plan,
        "total_paid": total_paid,
        "total_remaining": total_remaining,
        "total_overrun": total_overrun,
        "all_payments": payments,
    }

def record_mandatory_payment(
    user_id: int,
    mandatory_id: int,
    amount: float,
    payment_date: datetime.date = None,
    comment: str = "",
) -> int:
    d = payment_date or today_date()
    month_str = _month_key(d)
    return add_mandatory_payment(
        user_id=user_id,
        mandatory_id=mandatory_id,
        month=month_str,
        amount=amount,
        payment_date=d.isoformat(),
        comment=comment,
    )

def find_matching_mandatory(user_id: int, query: str) -> dict | None:
    """Ищет обязательный расход по названию (точное совпадение или вхождение без учёта регистра)."""
    items = get_mandatory_items(user_id)
    q = query.strip().lower()
    # 1. Точное совпадение
    for it in items:
        if it["name"].strip().lower() == q:
            return it
    # 2. Вхождение
    for it in items:
        name = it["name"].strip().lower()
        if q in name or name in q:
            return it
    return None

def remove_mandatory_payment(user_id: int, payment_id: int) -> bool:
    return delete_mandatory_payment(user_id, payment_id)
