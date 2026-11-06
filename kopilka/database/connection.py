import os
import sqlite3
import sys
from contextlib import contextmanager
import kopilka.config
from kopilka.utils.formatting import today_date
import migrations

def get_current_db_path() -> str:
    if "bot" in sys.modules and hasattr(sys.modules["bot"], "DB_PATH"):
        return sys.modules["bot"].DB_PATH
    return kopilka.config.DB_PATH

@contextmanager
def db(db_path: str = None):
    """Соединение с коммитом и гарантированным закрытием."""
    path = db_path or get_current_db_path()
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db(target_db_path: str = None):
    """Приводит базу к актуальной схеме версионными миграциями (см. migrations.py)."""
    path = target_db_path or get_current_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    backup_dir = kopilka.config.BACKUP_DIR
    if "bot" in sys.modules and hasattr(sys.modules["bot"], "BACKUP_DIR"):
        backup_dir = sys.modules["bot"].BACKUP_DIR
    migrations.migrate(path, today_date(), backup_dir=backup_dir)
