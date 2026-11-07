from telegram import ReplyKeyboardMarkup

MAIN_KB = ReplyKeyboardMarkup(
    [
        ["📊 Сегодня", "📅 Месяц"],
        ["🏠 Счета", "⚙️ Меню"],
    ],
    resize_keyboard=True,
)

MENU_TEXTS = {
    b for row in MAIN_KB.keyboard
    for b in (x.text if hasattr(x, "text") else x for x in row)
} | {
    "🏦 Копилка",
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
