"""Перенос старой базы (budget.db рядом с bot.py) на постоянный том."""
import datetime
import hashlib
import os
import sqlite3
import types

import pytest

import datasafe
import migrations
from test_migrations import dump, make_legacy_db

TODAY = datetime.date(2026, 11, 3)


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture
def lay(tmp_path):
    """app/ — старая раскладка (рабочая папка контейнера), data/ — том."""
    app, data = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    data.mkdir()
    return types.SimpleNamespace(
        app=app, data=data, legacy=str(app / "budget.db"), legacy_state=str(app / "bot_state.pkl"),
        db=str(data / "budget.db"), state=str(data / "bot_state.pkl"),
    )


def adopt(lay, **kw):
    return datasafe.adopt_legacy_files(lay.db, lay.state, str(lay.data), [str(lay.app)], **kw)


def test_legacy_db_and_state_are_copied_and_original_untouched(lay):
    make_legacy_db(lay.legacy)
    with open(lay.legacy_state, "wb") as f:
        f.write(b"pickle-bytes")
    before_sha, before = sha(lay.legacy), dump(lay.legacy)

    res = adopt(lay)

    assert res.changed and res.db_from == lay.legacy and res.state_from == lay.legacy_state
    assert dump(lay.db) == before                      # данные идентичны, ничего не потеряно
    assert open(lay.state, "rb").read() == b"pickle-bytes"
    assert sha(lay.legacy) == before_sha               # оригинал не изменён и на месте
    assert os.path.exists(lay.legacy_state)
    assert any("перенесена" in n for n in res.notes)


def test_copy_includes_uncheckpointed_wal_data(lay):
    make_legacy_db(lay.legacy)
    conn = sqlite3.connect(lay.legacy)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("INSERT INTO expenses (user_id,spent_date,amount,category) VALUES (111,'2026-11-02',777,'WAL')")
    conn.commit()  # запись только в -wal, основной файл её ещё не содержит
    try:
        assert os.path.getsize(lay.legacy + "-wal") > 0
        adopt(lay)
    finally:
        conn.close()
    c = sqlite3.connect(lay.db)
    assert c.execute("SELECT COUNT(*) FROM expenses WHERE category='WAL'").fetchone()[0] == 1
    c.close()


def test_adopted_db_migrates_without_loss_and_startup_sees_no_loss(lay):
    make_legacy_db(lay.legacy)
    before = dump(lay.legacy)
    adopt(lay)
    rep = datasafe.check_startup(lay.db, str(lay.data), check_mount=False)
    assert not rep.lost and not rep.new_install and not rep.stats["empty"]
    migrations.migrate(lay.db, TODAY, backup_dir=str(lay.data / "backups"))
    after = dump(lay.db)
    for table, rows in before.items():
        assert after[table] == rows, table
    c = sqlite3.connect(lay.db)
    assert c.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
    c.close()


def test_never_overwrites_nonempty_volume_db(lay):
    make_legacy_db(lay.legacy)
    make_legacy_db(lay.db)
    c = sqlite3.connect(lay.db)
    c.execute("INSERT INTO expenses (user_id,spent_date,amount,category) VALUES (111,'2026-11-03',1,'Новая')")
    c.commit()
    c.close()
    before = sha(lay.db)
    res = adopt(lay)
    assert not res.changed and sha(lay.db) == before
    assert any("уже есть своя база" in n for n in res.notes)


def test_no_repeat_notice_after_adoption(lay):
    make_legacy_db(lay.legacy)
    res = adopt(lay)
    datasafe.check_startup(lay.db, str(lay.data), check_mount=False)
    datasafe.refresh_marker(str(lay.data), lay.db, adopted_from=res.db_from)
    again = adopt(lay)
    assert not again.changed and again.notes == []


def test_lost_volume_does_not_silently_use_stale_legacy_file(lay):
    """Том раньше содержал данные и опустел: старый файл может быть старее, подставлять его молча нельзя."""
    make_legacy_db(lay.legacy)
    make_legacy_db(lay.db)
    datasafe.check_startup(lay.db, str(lay.data), check_mount=False)  # маркер запомнил данные
    os.remove(lay.db)
    res = adopt(lay)
    assert not res.changed and not os.path.exists(lay.db)
    assert any("/restore" in n for n in res.notes)
    rep = datasafe.check_startup(lay.db, str(lay.data), check_mount=False)
    assert rep.lost  # потеря по-прежнему видна


def test_empty_marker_from_previous_run_does_not_block_adoption(lay):
    """Бот уже запускался на пустом томе (маркер без данных), потом нашлась старая база."""
    datasafe.check_startup(lay.db, str(lay.data), check_mount=False)
    make_legacy_db(lay.legacy)
    res = adopt(lay)
    assert res.db_from == lay.legacy and not datasafe.db_stats(lay.db)["empty"]


def test_empty_stub_on_volume_is_replaced_and_stale_wal_removed(lay):
    migrations.migrate(lay.db, TODAY)  # пустая база с схемой, как после первого запуска без переноса
    with open(lay.db + "-wal", "wb") as f:
        f.write(b"stale")
    make_legacy_db(lay.legacy)
    before = dump(lay.legacy)
    res = adopt(lay)
    assert res.db_from and dump(lay.db) == before
    assert not os.path.exists(lay.db + "-wal")


def test_noop_when_db_path_outside_volume(lay):
    make_legacy_db(lay.legacy)
    outside = str(lay.app / "elsewhere.db")
    res = datasafe.adopt_legacy_files(outside, lay.state, str(lay.data), [str(lay.app)])
    assert not res.changed and not os.path.exists(outside) and not os.path.exists(lay.state)


def test_noop_when_legacy_is_the_target(lay):
    make_legacy_db(lay.legacy)
    res = datasafe.adopt_legacy_files(lay.legacy, lay.legacy_state, str(lay.app), [str(lay.app)])
    assert not res.changed and res.notes == []


def test_empty_or_missing_legacy_is_ignored(lay):
    assert not adopt(lay).changed                          # файла нет
    migrations.migrate(lay.legacy, TODAY)                  # файл есть, но данных нет
    res = adopt(lay)
    assert not res.changed and not os.path.exists(lay.db)


def test_corrupt_legacy_is_reported_and_not_copied(lay):
    with open(lay.legacy, "wb") as f:
        f.write(b"SQLite format 3\x00" + b"garbage" * 100)
    res = adopt(lay)
    assert not res.changed and not os.path.exists(lay.db)


def test_non_botdb_sqlite_file_is_not_adopted(lay):
    c = sqlite3.connect(lay.legacy)
    c.execute("CREATE TABLE expenses (x)")
    c.execute("INSERT INTO expenses VALUES (1)")
    c.commit()
    c.close()
    res = adopt(lay)
    assert not res.changed and not os.path.exists(lay.db)


def test_state_not_overwritten_if_exists_on_volume(lay):
    with open(lay.legacy_state, "wb") as f:
        f.write(b"old")
    with open(lay.state, "wb") as f:
        f.write(b"new")
    res = adopt(lay)
    assert res.state_from is None and open(lay.state, "rb").read() == b"new"


def test_picks_database_with_more_data_when_several_dirs(lay, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    make_legacy_db(lay.legacy)
    big = str(other / "budget.db")
    make_legacy_db(big)
    c = sqlite3.connect(big)
    for i in range(20):
        c.execute("INSERT INTO expenses (user_id,spent_date,amount,category) VALUES (111,'2026-10-10',?, 'x')", (i + 1,))
    c.commit()
    c.close()
    res = datasafe.adopt_legacy_files(lay.db, lay.state, str(lay.data), [str(lay.app), str(other)])
    assert res.db_from == big
    assert any("не использованы" in n for n in res.notes)
