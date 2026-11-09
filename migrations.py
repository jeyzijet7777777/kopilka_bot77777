"""Версионные миграции базы (PRAGMA user_version).

Правила:
- Любое изменение схемы — только новой миграцией в конец MIGRATIONS (номер = предыдущий + 1).
- Уже вышедшие миграции не редактируются.
- Каждая миграция выполняется в своей транзакции вместе с повышением user_version:
  при ошибке откатываются и схема, и номер версии.
- Перед миграцией непустой базы делается копия в backup_dir (pre_migration_*.db).
- База НОВЕЕ, чем знает код (user_version больше SCHEMA_VERSION), не трогается: бот не запустится,
  чтобы старый код не испортил данные.
"""
import datetime
import logging
import os
import sqlite3

import datasafe

logger = logging.getLogger("budget-bot")


class SchemaTooNewError(RuntimeError):
    pass


def _v1_baseline(conn: sqlite3.Connection, ctx: dict) -> None:
    """Схема, которая была в боте до введения версий. Все команды идемпотентны, поэтому для уже
    работающих баз (user_version = 0) миграция ничего не теряет и не дублирует."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER,
            key TEXT,
            value TEXT,
            PRIMARY KEY (user_id, key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mandatory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            amount REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            spent_date TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS savings_topups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            topup_date TEXT NOT NULL,
            amount REAL NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # снимки дневного лимита: прошедшие дни не «пересчитываются» после смены настроек
    conn.execute("""
        CREATE TABLE IF NOT EXISTS day_limits (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            daily_limit REAL NOT NULL,
            PRIMARY KEY (user_id, day)
        )
    """)
    # связь «снятие из копилки» <-> трата, чтобы удалять их вместе
    cols = [r[1] for r in conn.execute("PRAGMA table_info(savings_topups)")]
    if "expense_id" not in cols:
        conn.execute("ALTER TABLE savings_topups ADD COLUMN expense_id INTEGER")
    # старая миграция бота: снятия из копилки больше не дублируются «тратой дня»
    conn.execute(
        "DELETE FROM expenses WHERE id IN "
        "(SELECT expense_id FROM savings_topups WHERE expense_id IS NOT NULL)"
    )
    conn.execute("UPDATE savings_topups SET expense_id = NULL WHERE expense_id IS NOT NULL")
    # снимки настроек по завершённым месяцам
    conn.execute("""
        CREATE TABLE IF NOT EXISTS month_settings (
            user_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            income_mode TEXT NOT NULL,
            raw_income REAL NOT NULL,
            save REAL NOT NULL,
            days_in_month INTEGER NOT NULL,
            mandatory_json TEXT NOT NULL,
            estimated INTEGER NOT NULL DEFAULT 0,
            closed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, month)
        )
    """)
    # месяцы, закончившиеся до первого запуска архива, восстанавливаются по текущим настройкам (≈)
    conn.execute(
        "INSERT OR IGNORE INTO settings (user_id, key, value) VALUES (0, 'archive_launch', ?)",
        (ctx["today"].isoformat(),),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_user_date ON expenses(user_id, spent_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_top_user_date ON savings_topups(user_id, topup_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mand_user ON mandatory(user_id)")


def _v2_mandatory_payments(conn: sqlite3.Connection, ctx: dict) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mandatory_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mandatory_id INTEGER NOT NULL,
            month TEXT NOT NULL,
            amount REAL NOT NULL,
            payment_date TEXT NOT NULL,
            comment TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mand_pay_user_month ON mandatory_payments(user_id, month)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mand_pay_item ON mandatory_payments(mandatory_id, month)")


def _v3_expense_comment(conn: sqlite3.Connection, ctx: dict) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(expenses)")]
    if "comment" not in cols:
        conn.execute("ALTER TABLE expenses ADD COLUMN comment TEXT DEFAULT ''")


# (версия, описание, функция). Новые миграции — только в конец списка.
MIGRATIONS = [
    (1, "базовая схема (всё, что было до введения версий)", _v1_baseline),
    (2, "учёт фактических оплат обязательных трат (mandatory_payments)", _v2_mandatory_payments),
    (3, "детализация трат: название товара / комментарий (comment)", _v3_expense_comment),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def get_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _has_user_tables(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None


def migrate(db_path: str, today: datetime.date, backup_dir: str | None = None) -> list[int]:
    """Приводит базу к актуальной версии. Возвращает номера применённых миграций."""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)  # транзакциями управляем сами
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        current = get_version(conn)
        if current > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"Версия базы ({current}) новее, чем знает этот код ({SCHEMA_VERSION}). "
                "Запустите более новую версию бота. База не изменена."
            )
        pending = [m for m in MIGRATIONS if m[0] > current]
        if not pending:
            return []

        if backup_dir and _has_user_tables(conn):
            os.makedirs(backup_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            dest = os.path.join(backup_dir, f"pre_migration_v{current}_to_v{SCHEMA_VERSION}_{stamp}.db")
            datasafe.backup_to_file(db_path, dest)
            logger.info("Копия перед миграцией: %s", dest)

        ctx = {"today": today}
        applied = []
        for ver, title, fn in pending:
            conn.execute("BEGIN IMMEDIATE")
            try:
                fn(conn, ctx)
                conn.execute(f"PRAGMA user_version = {int(ver)}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                logger.exception("Миграция %s (%s) не применена, изменения откатены", ver, title)
                raise
            applied.append(ver)
            logger.info("Применена миграция %s: %s", ver, title)
        return applied
    finally:
        conn.close()
