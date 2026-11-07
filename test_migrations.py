import datetime
import os
import re
import sqlite3

import pytest

import datasafe
import migrations

TODAY = datetime.date(2026, 11, 3)


def schema(path):
    """Нормализованная схема: (тип, имя) -> sql без лишних пробелов."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ).fetchall()
    finally:
        conn.close()
    return {(t, n): re.sub(r"\s+", " ", (s or "")).strip() for t, n, s in rows}


def dump(path):
    conn = sqlite3.connect(path)
    try:
        out = {}
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            out[t] = sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr)
        return out
    finally:
        conn.close()


def init_legacy_schema(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            expense_id INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS day_limits (
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            daily_limit REAL NOT NULL,
            PRIMARY KEY (user_id, day)
        )
    """)
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
    conn.execute(
        "INSERT OR IGNORE INTO settings (user_id, key, value) VALUES (0, 'archive_launch', ?)",
        (TODAY.isoformat(),),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exp_user_date ON expenses (user_id, spent_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_top_user_date ON savings_topups (user_id, topup_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mand_user ON mandatory (user_id)")
    conn.commit()
    conn.close()


def make_legacy_db(path):
    init_legacy_schema(path)
    conn = sqlite3.connect(path)
    conn.executemany("INSERT INTO settings (user_id,key,value) VALUES (?,?,?)", [
        (111, "daily_income", "1000"), (111, "income_mode", "daily"), (111, "start_date", "2026-10-01"),
        (111, "save", "5000"), (222, "daily_income", "50000"), (222, "income_mode", "monthly"),
    ])
    conn.execute("INSERT INTO mandatory (user_id,name,amount) VALUES (111,'Аренда',15000)")
    conn.executemany("INSERT INTO expenses (user_id,spent_date,amount,category) VALUES (?,?,?,?)", [
        (111, "2026-10-02", 450.0, "Продукты"), (111, "2026-10-03", 300.5, "Такси"), (222, "2026-11-01", 99.0, "Кофе"),
    ])
    conn.execute("INSERT INTO savings_topups (user_id,topup_date,amount,comment) VALUES (111,'2026-10-05',500,'Подработка')")
    conn.execute("INSERT INTO day_limits (user_id,day,daily_limit) VALUES (111,'2026-10-02',800)")
    conn.execute(
        "INSERT INTO month_settings (user_id,month,income_mode,raw_income,save,days_in_month,mandatory_json,estimated) "
        "VALUES (111,'2026-10','daily',1000,5000,31,'[[\"Аренда\", 15000]]',0)"
    )
    conn.commit()
    conn.close()
    # старый код не знает про user_version
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    c.close()


def test_fresh_db_schema_equals_legacy_init_db(tmp_path):
    old, new = str(tmp_path / "old.db"), str(tmp_path / "new.db")
    init_legacy_schema(old)
    applied = migrations.migrate(new, TODAY, backup_dir=str(tmp_path / "bk"))
    assert applied == [1, 2]
    old_s, new_s = schema(old), schema(new)
    assert all(k in new_s for k in old_s)  # содержит все таблицы и индексы легаси
    assert ("table", "mandatory_payments") in new_s
    c = sqlite3.connect(new)
    assert c.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()
    # на пустой базе копию перед миграцией не делаем
    assert not os.path.exists(tmp_path / "bk")


def test_legacy_db_upgrades_without_data_loss(tmp_path):
    path = str(tmp_path / "budget.db")
    make_legacy_db(path)
    before = dump(path)
    applied = migrations.migrate(path, TODAY, backup_dir=str(tmp_path / "bk"))
    assert applied == [1, 2]
    after = dump(path)
    assert after.get("mandatory_payments") == []
    assert {k: v for k, v in after.items() if k != "mandatory_payments"} == before  # ни одна строка легаси не изменилась
    copies = os.listdir(tmp_path / "bk")
    assert len(copies) == 1 and copies[0].startswith(f"pre_migration_v0_to_v{migrations.SCHEMA_VERSION}_")
    assert dump(str(tmp_path / "bk" / copies[0])) == before  # копия содержит всё, что было


def test_migrate_is_idempotent(tmp_path):
    path = str(tmp_path / "budget.db")
    make_legacy_db(path)
    migrations.migrate(path, TODAY, backup_dir=str(tmp_path / "bk"))
    snap = dump(path)
    assert migrations.migrate(path, TODAY, backup_dir=str(tmp_path / "bk")) == []
    assert dump(path) == snap
    assert len(os.listdir(tmp_path / "bk")) == 1  # повторный запуск копий не плодит


def test_database_newer_than_code_is_not_touched(tmp_path):
    path = str(tmp_path / "budget.db")
    make_legacy_db(path)
    c = sqlite3.connect(path)
    c.execute(f"PRAGMA user_version = {migrations.SCHEMA_VERSION + 5}")
    c.close()
    before = dump(path)
    with pytest.raises(migrations.SchemaTooNewError):
        migrations.migrate(path, TODAY, backup_dir=str(tmp_path / "bk"))
    assert dump(path) == before


def test_new_migration_applies_and_keeps_data(tmp_path, monkeypatch):
    path = str(tmp_path / "budget.db")
    make_legacy_db(path)
    migrations.migrate(path, TODAY, backup_dir=str(tmp_path / "bk"))
    before = dump(path)

    next_v = migrations.SCHEMA_VERSION + 1

    def v_next(conn, ctx):
        conn.execute("ALTER TABLE expenses ADD COLUMN note TEXT")

    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [(next_v, "тест", v_next)])
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", next_v)
    assert migrations.migrate(path, TODAY, backup_dir=str(tmp_path / "bk")) == [next_v]
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == next_v
    rows = c.execute("SELECT id,user_id,spent_date,amount,category,created_at FROM expenses").fetchall()
    assert sorted(rows, key=repr) == before["expenses"]
    c.close()


def test_failed_migration_rolls_back_schema_and_version(tmp_path, monkeypatch):
    path = str(tmp_path / "budget.db")
    make_legacy_db(path)
    migrations.migrate(path, TODAY, backup_dir=str(tmp_path / "bk"))
    before = dump(path)

    next_v = migrations.SCHEMA_VERSION + 1

    def bad(conn, ctx):
        conn.execute("CREATE TABLE half_done (x INTEGER)")
        conn.execute("DELETE FROM expenses")
        raise RuntimeError("сбой посреди миграции")

    monkeypatch.setattr(migrations, "MIGRATIONS", migrations.MIGRATIONS + [(next_v, "сломанная", bad)])
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", next_v)
    with pytest.raises(RuntimeError):
        migrations.migrate(path, TODAY, backup_dir=str(tmp_path / "bk"))
    c = sqlite3.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == next_v - 1
    assert c.execute("SELECT name FROM sqlite_master WHERE name='half_done'").fetchone() is None
    c.close()
    assert dump(path) == before
