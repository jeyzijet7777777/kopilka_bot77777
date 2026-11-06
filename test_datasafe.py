import datetime
import gzip
import os
import shutil
import sqlite3
import threading
import zoneinfo

import pytest

import datasafe
import migrations
from test_migrations import dump, make_legacy_db

TODAY = datetime.date(2026, 11, 3)
MSK = zoneinfo.ZoneInfo("Europe/Moscow")


def make_db(path, users=2, expenses=3):
    migrations.migrate(path, TODAY)
    c = sqlite3.connect(path)
    for u in range(1, users + 1):
        c.execute("INSERT INTO settings VALUES (?, 'daily_income', '1000')", (u,))
    for i in range(expenses):
        c.execute("INSERT INTO expenses (user_id,spent_date,amount,category) VALUES (1,'2026-11-01',?, 'Еда')", (10 + i,))
    c.commit()
    c.close()


# ---------------------------------------------------------------- пути
def test_is_inside(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    assert datasafe.is_inside(str(d / "budget.db"), str(d))
    assert datasafe.is_inside(str(d / "sub" / "b.db"), str(d))
    assert not datasafe.is_inside(str(tmp_path / "budget.db"), str(d))
    assert not datasafe.is_inside(str(d / ".." / "budget.db"), str(d))
    assert not datasafe.is_inside(str(tmp_path / "data2" / "b.db"), str(d))  # префикс строки не считается
    link = tmp_path / "link"
    try:
        os.symlink(str(tmp_path), str(link))
        assert not datasafe.is_inside(str(link / "budget.db"), str(d))
    except (OSError, NotImplementedError):
        pass  # Windows без прав SeCreateSymbolicLinkPrivilege


# ---------------------------------------------------------------- проверка при запуске
def check(tmp_path, **kw):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    kw.setdefault("check_mount", False)
    return datasafe.check_startup(str(kw.pop("db", data / "budget.db")), str(data), **kw)


def test_new_install_is_warned_and_marker_created(tmp_path):
    rep = check(tmp_path)
    assert rep.new_install and rep.level == "warn" and not rep.lost
    assert rep.starts == 1 and rep.install_id
    assert datasafe.read_marker(str(tmp_path / "data"))["starts"] == 1


def test_existing_data_without_marker_adopts_quietly(tmp_path):
    (tmp_path / "data").mkdir()
    make_db(str(tmp_path / "data" / "budget.db"))
    rep = check(tmp_path)
    assert rep.level == "info" and not rep.lost and not rep.new_install
    assert datasafe.read_marker(str(tmp_path / "data"))["last_stats"]["expenses"] == 3


def test_lost_database_detected_by_marker_and_alarm_survives_restart(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "budget.db"
    make_db(str(db))
    assert check(tmp_path).level == "info"          # запуск №1: всё хорошо, маркер запомнил данные
    os.remove(db)                                   # пересборка без тома: базы нет
    for ext in ("-wal", "-shm"):
        if os.path.exists(str(db) + ext):
            os.remove(str(db) + ext)
    rep = check(tmp_path)
    assert rep.lost and rep.level == "critical"
    assert "БАЗА ПУСТА" in rep.texts("critical")[0]
    # бот создал пустую базу (как init_db), но тревога не исчезает при следующем запуске
    migrations.migrate(str(db), TODAY)
    rep2 = check(tmp_path)
    assert rep2.lost and rep2.level == "critical"
    assert datasafe.read_marker(str(data))["last_stats"]["expenses"] == 3   # «хорошее состояние» не затёрто
    assert rep2.starts == 3


def test_alarm_clears_after_data_returns(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "budget.db"
    make_db(str(db))
    check(tmp_path)
    os.remove(db)
    assert check(tmp_path).lost
    make_db(str(db), users=1, expenses=1)            # данные вернулись (например, /restore)
    rep = check(tmp_path)
    assert not rep.lost and rep.level == "info"
    m = datasafe.read_marker(str(data))
    assert "lost_since" not in m and m["last_stats"]["expenses"] == 1


def test_expect_existing_data_env_catches_lost_whole_volume(tmp_path):
    rep = check(tmp_path, expect_data=True)     # нет ни базы, ни маркера, но данные ожидались
    assert rep.lost and rep.level == "critical"


def test_db_path_outside_data_dir_is_critical(tmp_path):
    rep = check(tmp_path, db=tmp_path / "budget.db")
    assert rep.path_bad and rep.level == "critical"
    assert "DB_PATH" in rep.texts("critical")[0]
    assert check(tmp_path, db=tmp_path / "budget.db", allow_ephemeral=True).level != "critical"


def test_ephemeral_db_with_marker_on_volume_detects_loss_after_rebuild(tmp_path):
    """Типичный сценарий пользователя: DB_PATH=budget.db вне тома, а том с маркером на месте."""
    data = tmp_path / "data"
    data.mkdir()
    eph = tmp_path / "container" / "budget.db"
    eph.parent.mkdir()
    make_db(str(eph))
    first = check(tmp_path, db=eph)
    assert first.path_bad
    shutil.rmtree(eph.parent)                 # пересборка контейнера
    eph.parent.mkdir()
    second = check(tmp_path, db=eph)
    assert second.lost and second.path_bad


def test_mount_warning(tmp_path):
    rep = check(tmp_path, check_mount=True)    # tmp-папка не точка монтирования
    assert any("не похож на отдельный смонтированный том" in t for t in rep.texts("warn"))
    assert not any("смонтированный" in t for t in check(tmp_path, check_mount=False).texts())


def test_unwritable_volume_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(datasafe, "write_marker", lambda *a, **k: False)
    rep = check(tmp_path)
    assert rep.level == "critical" and not rep.marker_written


def test_corrupt_marker_does_not_crash(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / datasafe.MARKER_NAME).write_text("{not json")
    assert check(tmp_path).starts == 1


def test_db_stats_does_not_create_file(tmp_path):
    p = tmp_path / "nope.db"
    s = datasafe.db_stats(str(p))
    assert s["empty"] and not s["exists"] and not p.exists()


def test_refresh_marker_keeps_good_state_while_lost(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "budget.db"
    make_db(str(db))
    check(tmp_path)
    os.remove(db)
    migrations.migrate(str(db), TODAY)       # пустая новая база
    datasafe.refresh_marker(str(data), str(db))
    assert datasafe.read_marker(str(data))["last_stats"]["expenses"] == 3
    make_db(str(db), users=1, expenses=0)    # данные вернулись
    datasafe.refresh_marker(str(data), str(db))
    assert datasafe.read_marker(str(data))["last_stats"]["users"] == 1


# ---------------------------------------------------------------- резервные копии
def test_backup_includes_uncheckpointed_wal_data(tmp_path):
    path = str(tmp_path / "budget.db")
    migrations.migrate(path, TODAY)               # WAL
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA wal_autocheckpoint=0")  # данные остаются только в -wal
    writer.execute("INSERT INTO settings VALUES (7, 'daily_income', '123')")
    writer.commit()
    assert os.path.getsize(path + "-wal") > 0
    # убеждаемся, что «наивное» копирование файла базы данные потеряло бы
    plain = tmp_path / "plain.db"
    shutil.copyfile(path, plain)
    c = sqlite3.connect(plain)
    assert c.execute("SELECT COUNT(*) FROM settings WHERE user_id=7").fetchone()[0] == 0
    c.close()
    dest = datasafe.backup_to_file(path, str(tmp_path / "bk" / "auto_2026-11-01_03-00.db"))
    c = sqlite3.connect(dest)
    assert c.execute("SELECT value FROM settings WHERE user_id=7").fetchone()[0] == "123"
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"   # самодостаточный файл
    c.close()
    assert not os.path.exists(dest + "-wal")
    assert not any(n.endswith(".tmp") for n in os.listdir(tmp_path / "bk"))
    writer.close()


def test_backup_bytes_roundtrip(tmp_path):
    path = str(tmp_path / "budget.db")
    make_db(path)
    out = tmp_path / "x.db"
    out.write_bytes(datasafe.backup_bytes(path))
    assert dump(str(out)) == dump(path)


def test_rotation_keeps_only_newest_auto_and_never_touches_others(tmp_path):
    bk = tmp_path / "bk"
    bk.mkdir()
    autos = [f"auto_2026-10-{d:02d}_03-00.db" for d in range(1, 8)]
    others = ["before_restore_2026-10-01_00-00-00.db", "pre_migration_v0_to_v1_x.db", "my_notes.db", "auto_bad.db"]
    for n in autos + others:
        (bk / n).write_bytes(b"x")
    removed = datasafe.rotate_auto_backups(str(bk), keep=3)
    assert sorted(removed) == sorted(autos[:4])
    left = set(os.listdir(bk))
    assert set(autos[4:]) <= left and set(others) <= left
    datasafe.rotate_auto_backups(str(bk), keep=0)     # keep < 1 трактуется как 1: хотя бы одна копия остаётся
    assert len([n for n in os.listdir(bk) if datasafe.AUTO_BACKUP_RE.match(n)]) == 1
    assert datasafe.rotate_auto_backups(str(tmp_path / "missing"), 3) == []


def test_schedule_parsing():
    assert datasafe.parse_weekday("6") == 6 and datasafe.parse_weekday("пн") == 0
    assert datasafe.parse_weekday("Sunday") == 6 and datasafe.parse_weekday("пятница") == 4
    assert datasafe.parse_weekday("9", 2) == 2 and datasafe.parse_weekday("", 3) == 3
    assert datasafe.parse_hhmm("04:30") == (4, 30) and datasafe.parse_hhmm("25:00") == (3, 0)
    assert datasafe.parse_hhmm("abc", (1, 1)) == (1, 1)


def test_backup_due_logic():
    sun = 6
    now = datetime.datetime(2026, 11, 3, 22, 47, tzinfo=MSK)               # вторник
    slot = datasafe.last_backup_slot(now, sun, 3, 0)
    assert slot == datetime.datetime(2026, 11, 1, 3, 0, tzinfo=MSK)
    assert datasafe.backup_due(now, None, sun, 3, 0)                       # копий ещё не было
    assert datasafe.backup_due(now, "2026-10-25T00:05:00Z", sun, 3, 0)     # последняя до слота
    assert not datasafe.backup_due(now, "2026-11-01T00:30:00Z", sun, 3, 0) # после слота (03:00 МСК = 00:00 UTC)
    assert datasafe.backup_due(now, "garbage", sun, 3, 0)
    # в день слота до наступления времени ещё действует прошлый слот
    sunday_early = datetime.datetime(2026, 11, 1, 2, 0, tzinfo=MSK)
    assert datasafe.last_backup_slot(sunday_early, sun, 3, 0) == datetime.datetime(2026, 10, 25, 3, 0, tzinfo=MSK)


# ---------------------------------------------------------------- проверка файла
def test_validate_rejects_bad_files(tmp_path):
    empty = tmp_path / "e.db"
    empty.write_bytes(b"")
    with pytest.raises(datasafe.RestoreError, match="пустой"):
        datasafe.validate_db_file(str(empty))
    txt = tmp_path / "t.db"
    txt.write_text("hello world, not sqlite at all")
    with pytest.raises(datasafe.RestoreError, match="не файл SQLite"):
        datasafe.validate_db_file(str(txt))
    other = tmp_path / "o.db"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE foo (x)")
    c.commit()
    c.close()
    with pytest.raises(datasafe.RestoreError, match="нет нужных таблиц"):
        datasafe.validate_db_file(str(other))
    badcols = tmp_path / "c.db"
    c = sqlite3.connect(badcols)
    for t in datasafe.REQUIRED_TABLES:
        c.execute(f"CREATE TABLE {t} (id INTEGER)")
    c.commit()
    c.close()
    with pytest.raises(datasafe.RestoreError, match="не хватает колонок"):
        datasafe.validate_db_file(str(badcols))


def test_validate_detects_corruption(tmp_path):
    path = str(tmp_path / "budget.db")
    make_db(path, users=3, expenses=400)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=DELETE")
    c.close()
    bad = tmp_path / "bad.db"
    data = bytearray(open(path, "rb").read())
    for i in range(4096 * 2, min(len(data), 4096 * 3)):   # портим страницу данных
        data[i] = 0xFF
    bad.write_bytes(bytes(data))
    with pytest.raises(datasafe.RestoreError):
        datasafe.validate_db_file(str(bad))
    trunc = tmp_path / "trunc.db"
    trunc.write_bytes(open(path, "rb").read()[:5000])
    with pytest.raises(datasafe.RestoreError):
        datasafe.validate_db_file(str(trunc))


def test_validate_rejects_too_new_schema(tmp_path):
    path = str(tmp_path / "budget.db")
    make_db(path)
    c = sqlite3.connect(path)
    c.execute("PRAGMA user_version = 99")
    c.close()
    with pytest.raises(datasafe.RestoreError, match="более новой версией"):
        datasafe.validate_db_file(path, max_version=migrations.SCHEMA_VERSION)


# ---------------------------------------------------------------- восстановление
def test_restore_replaces_data_keeps_copy_and_survives_open_connections(tmp_path):
    live = str(tmp_path / "data" / "budget.db")
    os.makedirs(os.path.dirname(live))
    make_db(live, users=1, expenses=1)
    snapshot = str(tmp_path / "from_admin.db")
    src = str(tmp_path / "other.db")
    make_db(src, users=5, expenses=40)
    datasafe.backup_to_file(src, snapshot)

    held = sqlite3.connect(live)            # соединение бота, открытое во время восстановления
    held.execute("SELECT COUNT(*) FROM expenses").fetchall()
    before_live = dump(live)

    cand, info = datasafe.prepare_restore(snapshot, TODAY)
    assert info["users"] == 5 and info["expenses"] == 40
    assert dump(live) == before_live        # проверка файла живую базу не меняет
    res = datasafe.apply_restore(cand, live, str(tmp_path / "data" / "backups"))

    assert res["stats"]["expenses"] == 40
    assert held.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 40   # старые соединения видят новые данные
    held.close()
    assert dump(live) == dump(src)
    # копия прежней базы лежит на томе и содержит прежние данные
    assert dump(res["before_restore"])["expenses"] == before_live["expenses"]
    c = sqlite3.connect(live)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"      # режим WAL сохранился
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("PRAGMA user_version").fetchone()[0] == migrations.SCHEMA_VERSION
    c.close()
    shutil.rmtree(os.path.dirname(cand))


def test_restore_of_legacy_backup_migrates_candidate_first(tmp_path):
    """Копия, снятая СТАРЫМ ботом (user_version = 0), восстанавливается и доводится до текущей схемы."""
    legacy = str(tmp_path / "legacy.db")
    make_legacy_db(legacy)
    legacy_copy = str(tmp_path / "legacy_copy.db")
    datasafe.backup_to_file(legacy, legacy_copy)
    live = str(tmp_path / "budget.db")
    migrations.migrate(live, TODAY)
    cand, info = datasafe.prepare_restore(legacy_copy, TODAY)
    assert info["version"] == migrations.SCHEMA_VERSION
    datasafe.apply_restore(cand, live, str(tmp_path / "bk"))
    assert dump(live) == {**dump(legacy), "mandatory_payments": []}


def test_restore_accepts_gzip(tmp_path):
    src = str(tmp_path / "s.db")
    make_db(src, users=2, expenses=5)
    copy = datasafe.backup_to_file(src, str(tmp_path / "c.db"))
    gz = tmp_path / "c.db.gz"
    with open(copy, "rb") as f, gzip.open(gz, "wb") as g:
        g.write(f.read())
    cand, info = datasafe.prepare_restore(str(gz), TODAY)
    assert info["expenses"] == 5
    shutil.rmtree(os.path.dirname(cand))


def test_bad_upload_leaves_live_db_untouched_and_no_temp_dirs(tmp_path):
    live = str(tmp_path / "budget.db")
    make_db(live)
    before = dump(live)
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)
    with pytest.raises(datasafe.RestoreError):
        datasafe.prepare_restore(str(junk), TODAY)
    assert dump(live) == before


def test_restore_is_atomic_under_concurrent_writes(tmp_path):
    live = str(tmp_path / "budget.db")
    make_db(live, users=1, expenses=1)
    src = str(tmp_path / "src.db")
    make_db(src, users=3, expenses=30)
    cand, _ = datasafe.prepare_restore(datasafe.backup_to_file(src, str(tmp_path / "c.db")), TODAY)
    errors = []

    def writer():
        try:
            for i in range(30):
                c = sqlite3.connect(live, timeout=30)
                c.execute("INSERT INTO expenses (user_id,spent_date,amount,category) VALUES (9,'2026-11-02',1,'w')")
                c.commit()
                c.close()
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=writer)
    t.start()
    datasafe.apply_restore(cand, live, str(tmp_path / "bk"))
    t.join()
    assert not errors
    c = sqlite3.connect(live)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    n = c.execute("SELECT COUNT(*) FROM expenses").fetchone()[0]
    assert 30 <= n <= 60       # восстановленные 30 + сколько успел дописать писатель после замены
    c.close()


def test_list_extra_mounts_finds_volume_and_skips_system(tmp_path):
    f = tmp_path / "mounts"
    f.write_text(
        "overlay / overlay rw 0 0\n"
        "proc /proc proc rw 0 0\n"
        "tmpfs /dev tmpfs rw 0 0\n"
        "/dev/vdb /app/storage ext4 rw 0 0\n"
        "/dev/vda1 /etc/hosts ext4 rw 0 0\n"
        "/dev/vdc /my\\040disk ext4 rw 0 0\n"
    )
    assert datasafe.list_extra_mounts(str(f)) == ["/app/storage", "/my disk"]
    assert datasafe.list_extra_mounts(str(tmp_path / "none")) == []
