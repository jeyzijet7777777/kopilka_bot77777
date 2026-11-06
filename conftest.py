import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("MPLBACKEND", "Agg")


def _install_telegram_stub():
    """python-telegram-bot нужен только для импорта bot.py; логика защиты данных его не использует.
    Если библиотека установлена, заглушка не включается."""
    class _Any:
        def __init__(self, *a, **k):
            self.args, self.kwargs = a, k

    class _Markup(_Any):
        def __init__(self, keyboard=None, *a, **k):
            super().__init__(keyboard, *a, **k)
            self.keyboard = keyboard or []
            self.inline_keyboard = keyboard or []

    class TelegramError(Exception):
        pass

    class Forbidden(TelegramError):
        pass

    class _Filter:
        def __and__(self, o): return self
        def __invert__(self): return self

    filters = types.SimpleNamespace(TEXT=_Filter(), COMMAND=_Filter(), Document=types.SimpleNamespace(ALL=_Filter()))

    def escape_markdown(text, version=1):
        for ch in "_*`[":
            text = text.replace(ch, "\\" + ch)
        return text

    tg = types.ModuleType("telegram")
    for name in ("BotCommand", "InlineKeyboardButton", "InputMediaPhoto", "Update"):
        setattr(tg, name, type(name, (_Any,), {}))
    tg.InlineKeyboardMarkup = type("InlineKeyboardMarkup", (_Markup,), {})
    tg.ReplyKeyboardMarkup = type("ReplyKeyboardMarkup", (_Markup,), {})
    err = types.ModuleType("telegram.error")
    err.TelegramError, err.Forbidden = TelegramError, Forbidden
    ext = types.ModuleType("telegram.ext")
    for name in ("Application", "CallbackQueryHandler", "CommandHandler", "MessageHandler", "PicklePersistence"):
        setattr(ext, name, type(name, (_Any,), {}))
    ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    ext.filters = filters
    helpers = types.ModuleType("telegram.helpers")
    helpers.escape_markdown = escape_markdown
    tg.error, tg.ext, tg.helpers = err, ext, helpers
    sys.modules.update({"telegram": tg, "telegram.error": err, "telegram.ext": ext, "telegram.helpers": helpers})


try:
    import telegram  # noqa: F401
except ImportError:
    _install_telegram_stub()
