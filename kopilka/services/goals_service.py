"""Сервис управления финансовыми целями в копилке."""
from kopilka.database.repository import delete_setting, get_setting, set_setting
from kopilka.utils.formatting import fmt_money


def get_piggy_goal(user_id: int) -> dict | None:
    """Возвращает информацию о текущей цели накопления или None."""
    title = get_setting(user_id, "piggy_goal_title")
    target_str = get_setting(user_id, "piggy_goal_target")
    if not title or not target_str:
        return None
    try:
        target = float(target_str)
        if target <= 0:
            return None
        return {"title": title, "target": target}
    except (ValueError, TypeError):
        return None


def set_piggy_goal(user_id: int, title: str, target: float) -> None:
    """Устанавливает или обновляет цель накопления."""
    set_setting(user_id, "piggy_goal_title", title.strip())
    set_setting(user_id, "piggy_goal_target", float(target))


def clear_piggy_goal(user_id: int) -> None:
    """Удаляет цель накопления."""
    delete_setting(user_id, "piggy_goal_title")
    delete_setting(user_id, "piggy_goal_target")


def format_goal_progress(current_savings: float, goal: dict) -> str:
    """Формирует красивый текст с прогресс-баром для карточки копилки."""
    target = goal["target"]
    title = goal["title"]
    pct = max(0.0, (current_savings / target) * 100) if target > 0 else 0.0
    capped_pct = min(100.0, pct)
    filled = int(round(capped_pct / 10))
    bar = "▓" * filled + "░" * (10 - filled)

    if current_savings >= target:
        status = "🎉 *Цель достигнута! Поздравляем!*"
    else:
        left = target - current_savings
        status = f"⏳ Осталось накопить: *{fmt_money(left)}*"

    return (
        f"🎯 *Цель: {title}*\n"
        f"[{bar}] *{pct:.1f}%* ({fmt_money(current_savings)} из {fmt_money(target)})\n"
        f"{status}"
    )
