import os
import re
from zoneinfo import ZoneInfo
import datasafe

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
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.environ.get("DB_PATH") or os.path.join(DATA_DIR, "budget.db")
STATE_PATH = os.environ.get("STATE_PATH") or os.path.join(DATA_DIR, "bot_state.pkl")
TEMPLATE_PATH = "template.xlsx"
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}

TZ = ZoneInfo(TZ_NAME)
TZ_LABEL = "МСК" if TZ_NAME == "Europe/Moscow" else TZ_NAME
MAX_AMOUNT = 10_000_000

# Сохранность данных
EXPECT_EXISTING_DATA = datasafe.env_flag(os.environ.get("EXPECT_EXISTING_DATA"))
ALLOW_EPHEMERAL_DB = datasafe.env_flag(os.environ.get("ALLOW_EPHEMERAL_DB"))
STRICT_DATA_PATH = datasafe.env_flag(os.environ.get("STRICT_DATA_PATH"))
SKIP_MOUNT_CHECK = datasafe.env_flag(os.environ.get("SKIP_MOUNT_CHECK"))
STARTUP_NOTICE = datasafe.env_flag(os.environ.get("STARTUP_NOTICE"), True)

BACKUP_ENABLED = datasafe.env_flag(os.environ.get("BACKUP_ENABLED"), True)
BACKUP_WEEKDAY = datasafe.parse_weekday(os.environ.get("BACKUP_WEEKDAY"), 6)
BACKUP_HOUR, BACKUP_MINUTE = datasafe.parse_hhmm(os.environ.get("BACKUP_TIME"), (3, 0))
try:
    BACKUP_KEEP = max(1, int(os.environ.get("BACKUP_KEEP", "8")))
except ValueError:
    BACKUP_KEEP = 8
BACKUP_DIR = os.environ.get("BACKUP_DIR") or os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "backups")
LEGACY_DIRS = [os.getcwd(), os.path.dirname(os.path.abspath(__file__)), os.environ.get("LEGACY_DIR", "")]

MONTH_REPORT_ENABLED = datasafe.env_flag(os.environ.get("MONTH_REPORT_ENABLED"), True)
MONTH_REPORT_HOUR, MONTH_REPORT_MINUTE = datasafe.parse_hhmm(os.environ.get("MONTH_REPORT_TIME"), (9, 0))
try:
    MONTH_REPORT_WINDOW_DAYS = min(28, max(1, int(os.environ.get("MONTH_REPORT_WINDOW_DAYS", "3"))))
except ValueError:
    MONTH_REPORT_WINDOW_DAYS = 3

WEEK_SUMMARY_ENABLED = datasafe.env_flag(os.environ.get("WEEK_SUMMARY_ENABLED"), True)
WEEK_SUMMARY_HOUR, WEEK_SUMMARY_MINUTE = datasafe.parse_hhmm(os.environ.get("WEEK_SUMMARY_TIME"), (9, 0))
try:
    WEEK_SUMMARY_WINDOW_DAYS = min(6, max(1, int(os.environ.get("WEEK_SUMMARY_WINDOW_DAYS", "2"))))
except ValueError:
    WEEK_SUMMARY_WINDOW_DAYS = 2

LIMIT_WARN_RATIO = 0.8
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024
TG_UPLOAD_LIMIT = 49 * 1024 * 1024

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
