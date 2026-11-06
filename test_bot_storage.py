import asyncio
import datetime
import os
import shutil
import sqlite3
import types

import pytest

import datasafe
import migrations
from test_migrations import dump, make_legacy_db

import bot

MSK = bot.TZ
ADMIN = 4242


class FakeBot:
    def __init__(self, fail_documents=False):
        self.messages, self.documents = [], []
        self.fail_documents = fail_documents

    async def send_message(self, chat_id, text, **kw):
        self.messages.append((chat_id, text))

    async def send_document(self, chat_id, document, filename=None, caption=None, **kw):
        if self.fail_documents:
            raise bot.TelegramError("chat not found")
        self.documents.append((chat_id, filename, document.read(), caption))


@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(bot, "DATA_DIR", str(data))
    monkeypatch.setattr(bot, "DB_PATH", str(data / "budget.db"))
    monkeypatch.setattr(bot, "BACKUP_DIR", str(data / "backups"))
    monkeypatch.setattr(bot, "ADMIN_IDS", {ADMIN})
    monkeypatch.setattr(bot, "BACKUP_KEEP", 2)
    monkeypatch.setattr(bot, "BACKUP_WEEKDAY", 6)
    monkeypatch.setattr(bot, "BACKUP_HOUR", 3)
    monkeypatch.setattr(bot, "BACKUP_MINUTE", 0)
    monkeypatch.setattr(bot, "STARTUP_REPORT", None)
    monkeypatch.setattr(bot, "_RUNTIME", {})
    bot.init_db()
    return types.SimpleNamespace(data=data, db=str(data / "budget.db"), tmp=tmp_path)


def add_data(db, user=1, n=3):
    c = sqlite3.connect(db)
    c.execute("INSERT OR REPLACE INTO settings VALUES (?, 'daily_income', '1000')", (user,))
    for i in range(n):
        c.execute("INSERT INTO expenses (user_id,spent_date,amount,category) VALUES (?, '2026-11-01', ?, 'Еда')", (user, 10 + i))
    c.commit()
    c.close()


def ctx(fake):
    return types.SimpleNamespace(bot=fake, application=types.SimpleNamespace(bot_data={}))


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- хранение как раньше
def test_init_db_and_budget_calcs_identical_to_legacy(env, monkeypatch):
    """Новый init_db создаёт ту же схему, а расчёты на тех же данных дают те же числа, что и старый код."""
    import legacy_bot
    legacy_db = str(env.tmp / "legacy.db")
    make_legacy_db(legacy_db)
    new_db = str(env.tmp / "new.db")
    shutil.copyfile(legacy_db, new_db)        # одни и те же данные
    monkeypatch.setattr(legacy_bot, "DB_PATH", legacy_db)
    monkeypatch.setattr(bot, "DB_PATH", new_db)
    bot.init_db()                              # миграция v0 -> v1 на копии «боевой» базы
    day = datetime.date(2026, 10, 20)
    for uid in (111, 222):
        old = legacy_bot.get_budget_info(uid, day)
        new = bot.get_budget_info(uid, day)
        assert old == new
        assert legacy_bot.calc_change_pool(uid, day) == bot.calc_change_pool(uid, day)
    assert legacy_bot.build_day_report(111, day) == bot.build_day_report(111, day)


def test_make_db_backup_is_valid_database(env):
    add_data(env.db)
    out = env.tmp / "b.db"
    out.write_bytes(bot.make_db_backup().read())
    assert dump(str(out)) == dump(env.db)


def test_admin_stats_shows_path_and_marker(env):
    datasafe.check_startup(env.db, str(env.data), check_mount=False)
    text = bot.build_admin_stats()
    assert "Файл:" in text and "/restore" in text and "запусков" in text


# ---------------------------------------------------------------- стартовые уведомления
def test_startup_message_for_lost_data(env):
    add_data(env.db)
    datasafe.check_startup(env.db, str(env.data), check_mount=False)
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(env.db + ext):
            os.remove(env.db + ext)
    bot.init_db()
    rep = datasafe.check_startup(env.db, str(env.data), check_mount=False)
    msg = bot.build_startup_message(rep)
    assert msg.startswith("🚨") and "БАЗА ПУСТА" in msg and "запуск №" in msg
    fake = FakeBot()
    bot.STARTUP_REPORT = rep
    run(bot.send_startup_notices(types.SimpleNamespace(bot=fake)))
    assert fake.messages and fake.messages[0][0] == ADMIN


def test_normal_startup_notice_and_opt_out(env, monkeypatch):
    add_data(env.db)
    rep = datasafe.check_startup(env.db, str(env.data), check_mount=False)
    bot.STARTUP_REPORT = rep
    fake = FakeBot()
    run(bot.send_startup_notices(types.SimpleNamespace(bot=fake)))
    assert fake.messages[0][1].startswith("🟢")
    monkeypatch.setattr(bot, "STARTUP_NOTICE", False)
    fake2 = FakeBot()
    run(bot.send_startup_notices(types.SimpleNamespace(bot=fake2)))
    assert fake2.messages == []
    # но тревога приходит всегда
    rep_bad = datasafe.check_startup(str(env.tmp / "budget.db"), str(env.data), check_mount=False)
    bot.STARTUP_REPORT = rep_bad
    run(bot.send_startup_notices(types.SimpleNamespace(bot=fake2)))
    assert fake2.messages and fake2.messages[0][1].startswith("🚨")


# ---------------------------------------------------------------- еженедельная копия
def at(y, m, d, h, mi):
    return datetime.datetime(y, m, d, h, mi, tzinfo=MSK)


def test_weekly_backup_sent_once_per_slot_and_rotated(env, monkeypatch):
    add_data(env.db)
    datasafe.check_startup(env.db, str(env.data), check_mount=False)
    fake = FakeBot()
    start = at(2026, 11, 3, 22, 47)

    class FixedDT(datetime.datetime):
        cur = start

        @classmethod
        def now(cls, tz=None):
            return cls.cur.astimezone(tz) if tz else cls.cur

    monkeypatch.setattr(bot.datetime, "datetime", FixedDT)

    run(bot.maintenance_job(ctx(fake)))
    assert len(fake.documents) == 1
    chat, name, payload, caption = fake.documents[0]
    assert chat == ADMIN and name == "auto_2026-11-03_22-47.db" and "Восстановление" in caption
    copy = env.tmp / "got.db"
    copy.write_bytes(payload)
    assert dump(str(copy)) == dump(env.db)                       # прислана согласованная копия
    assert os.path.exists(env.data / "backups" / name)           # и копия на томе
    assert datasafe.read_marker(str(env.data))["last_backup"]    # факт копии записан в маркер

    run(bot.maintenance_job(ctx(fake)))                           # в тот же слот повторно не шлём
    assert len(fake.documents) == 1

    # недели идут, копии копятся, на томе остаётся BACKUP_KEEP=2
    for week_start in (at(2026, 11, 9, 22, 47), at(2026, 11, 16, 22, 47), at(2026, 11, 23, 22, 47)):
        FixedDT.cur = week_start
        run(bot.maintenance_job(ctx(fake)))
    assert len(fake.documents) == 4
    autos = sorted(n for n in os.listdir(env.data / "backups") if n.startswith("auto_"))
    assert autos == ["auto_2026-11-16_22-47.db", "auto_2026-11-23_22-47.db"]


def test_backup_not_due_before_slot_time(env, monkeypatch):
    add_data(env.db)
    fake = FakeBot()
    datasafe.check_startup(env.db, str(env.data), check_mount=False)
    datasafe.refresh_marker(str(env.data), env.db, last_backup="2026-11-01T00:10:00Z")  # воскр. 03:10 МСК
    fixed = at(2026, 11, 3, 10, 0)

    class FixedDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed

    monkeypatch.setattr(bot.datetime, "datetime", FixedDT)
    run(bot.maintenance_job(ctx(fake)))
    assert fake.documents == []


def test_empty_database_is_never_sent_as_backup(env, monkeypatch):
    fake = FakeBot()
    run(bot.run_weekly_backup(ctx(fake), at(2026, 11, 3, 22, 47)))
    assert fake.documents == [] and not os.path.exists(env.data / "backups")


def test_failed_delivery_keeps_local_copy_and_retries_later(env):
    add_data(env.db)
    datasafe.check_startup(env.db, str(env.data), check_mount=False)
    bad = FakeBot(fail_documents=True)
    now = at(2026, 11, 3, 22, 47)
    run(bot.run_weekly_backup(ctx(bad), now))
    assert os.listdir(env.data / "backups") == ["auto_2026-11-03_22-47.db"]       # копия есть на томе
    assert "last_backup" not in datasafe.read_marker(str(env.data))               # но доставка не засчитана
    run(bot.run_weekly_backup(ctx(bad), now + datetime.timedelta(minutes=10)))    # слишком рано для повтора
    good = FakeBot()
    run(bot.run_weekly_backup(ctx(good), now + datetime.timedelta(hours=2)))
    assert len(good.documents) == 1
    assert os.listdir(env.data / "backups") == ["auto_2026-11-03_22-47.db"]       # новую копию не плодили
    assert datasafe.read_marker(str(env.data))["last_backup"]


def test_backup_disabled_without_admins(env, monkeypatch):
    add_data(env.db)
    monkeypatch.setattr(bot, "ADMIN_IDS", set())
    fake = FakeBot()
    run(bot.maintenance_job(ctx(fake)))
    assert fake.documents == [] and fake.messages == []


# ---------------------------------------------------------------- /restore через обработчики
class FakeMessage:
    def __init__(self):
        self.sent, self.edits, self.doc = [], [], None

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))

    async def edit_text(self, text, **kw):
        self.edits.append(text)


def make_update(user_id=ADMIN, document=None, data=None):
    msg = FakeMessage()
    msg.document = document
    q = types.SimpleNamespace(message=msg, data=data, answer=None)

    async def _answer(*a, **k):
        return None

    q.answer = _answer
    return types.SimpleNamespace(
        effective_user=types.SimpleNamespace(id=user_id), message=msg, callback_query=q
    )


class FakeDoc:
    def __init__(self, src, size=None):
        self.src, self.file_size = src, size or os.path.getsize(src)

    async def get_file(self):
        outer = self

        class F:
            async def download_to_drive(self, path):
                shutil.copyfile(outer.src, path)
        return F()


def full_ctx(fake=None):
    c = ctx(fake or FakeBot())
    c.user_data = {}
    return c


def test_restore_full_flow_via_handlers(env):
    add_data(env.db, user=1, n=1)
    other = str(env.tmp / "other.db")
    migrations.migrate(other, datetime.date(2026, 11, 3))
    add_data(other, user=9, n=25)
    snapshot = datasafe.backup_to_file(other, str(env.tmp / "snap.db"))

    c = full_ctx()
    run(bot.restore_cmd(make_update(), c))
    assert c.user_data["state"] == "wait_restore_file"

    up = make_update(document=FakeDoc(snapshot))
    run(bot.document_handler(up, c))
    text, kw = up.message.sent[-1]
    assert "прошёл проверку" in text and "трат: 25" in text and "трат: 1" in text
    token = bot._RUNTIME["restore_pending"][ADMIN]["token"]
    assert dump(env.db)["expenses"] != dump(other)["expenses"]      # пока ничего не заменено

    cb = make_update(data=f"restore_ok_{token}")
    run(bot.callback_handler(cb, c))
    assert any("База восстановлена" in e for e in cb.callback_query.message.edits)
    assert dump(env.db) == dump(other)
    assert c.user_data == {}
    assert c.bot.documents and c.bot.documents[0][3].startswith("💾 Прежняя база")
    backups = os.listdir(env.data / "backups")
    assert any(n.startswith("before_restore_") for n in backups)
    assert ADMIN not in bot._RUNTIME["restore_pending"]


def test_restore_cancel_and_garbage_and_non_admin(env, tmp_path):
    add_data(env.db)
    before = dump(env.db)
    c = full_ctx()
    run(bot.restore_cmd(make_update(), c))
    junk = env.tmp / "junk.db"
    junk.write_text("не база")
    up = make_update(document=FakeDoc(str(junk)))
    run(bot.document_handler(up, c))
    assert "не подходит" in up.message.sent[-1][0] and "не файл SQLite" in up.message.sent[-1][0]
    assert c.user_data["state"] == "wait_restore_file"             # можно прислать другой файл
    assert dump(env.db) == before

    good = datasafe.backup_to_file(env.db, str(env.tmp / "g.db"))
    up = make_update(document=FakeDoc(good))
    run(bot.document_handler(up, c))
    token = bot._RUNTIME["restore_pending"][ADMIN]["token"]
    pending_dir = os.path.dirname(bot._RUNTIME["restore_pending"][ADMIN]["path"])
    cb = make_update(data=f"restore_no_{token}")
    run(bot.callback_handler(cb, c))
    assert "Отменено" in cb.callback_query.message.edits[-1]
    assert not os.path.exists(pending_dir)                          # временные файлы убраны
    assert dump(env.db) == before

    # не админ: команда и кнопки молча игнорируются, файл не принимается
    intruder = full_ctx()
    run(bot.restore_cmd(make_update(user_id=1), intruder))
    assert intruder.user_data == {}
    intruder.user_data["state"] = "wait_restore_file"
    up = make_update(user_id=1, document=FakeDoc(good))
    run(bot.document_handler(up, intruder))
    assert up.message.sent == [] and not bot._RUNTIME.get("restore_pending", {}).get(1)
    cb = make_update(user_id=1, data="restore_ok_abc")
    run(bot.callback_handler(cb, intruder))
    assert cb.callback_query.message.edits == []


def test_restore_stale_token_and_oversized_file(env):
    add_data(env.db)
    before = dump(env.db)
    c = full_ctx()
    cb = make_update(data="restore_ok_deadbeef")
    run(bot.callback_handler(cb, c))
    assert "устарел" in cb.callback_query.message.edits[-1] and dump(env.db) == before

    c.user_data["state"] = "wait_restore_file"
    big = FakeDoc(env.db, size=bot.TG_DOWNLOAD_LIMIT + 1)
    up = make_update(document=big)
    run(bot.document_handler(up, c))
    assert "больше 20 МБ" in up.message.sent[-1][0]


def test_text_while_waiting_for_restore_file_is_not_recorded_as_expense(env):
    c = full_ctx()
    c.user_data["state"] = "wait_restore_file"
    up = make_update()
    up.message.text = "450 продукты"
    run(bot.text_handler(up, c))
    assert "Жду файл" in up.message.sent[-1][0]
    conn = sqlite3.connect(env.db)
    assert conn.execute("SELECT COUNT(*) FROM expenses").fetchone()[0] == 0
    conn.close()


def test_default_paths_follow_data_dir_not_working_directory(tmp_path):
    """Без DB_PATH/STATE_PATH (например, хостинг запускает bot.py без Dockerfile) путь НЕ должен
    оказаться «budget.db» в рабочей папке контейнера."""
    import subprocess
    import sys
    code = "import bot; print(bot.DB_PATH); print(bot.STATE_PATH); print(bot.BACKUP_DIR)"
    env = {k: v for k, v in os.environ.items() if k not in ("DB_PATH", "STATE_PATH", "DATA_DIR", "BACKUP_DIR")}
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    parent_dir = os.path.dirname(cur_dir)
    env["PYTHONPATH"] = os.pathsep.join([cur_dir, parent_dir, os.path.join(parent_dir, "tests")])
    prog = "import conftest\n" + code
    out = subprocess.run([sys.executable, "-c", prog], cwd=str(tmp_path), env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    out_paths = [p.replace("\\", "/").removeprefix("C:") for p in out.stdout.split()]
    assert out_paths == ["/data/budget.db", "/data/bot_state.pkl", "/data/backups"]
    env["DATA_DIR"] = "/mnt/vol"
    out = subprocess.run([sys.executable, "-c", prog], cwd=str(tmp_path), env=env, capture_output=True, text=True)
    out_paths2 = [p.replace("\\", "/").removeprefix("C:") for p in out.stdout.split()]
    assert out_paths2 == ["/mnt/vol/budget.db", "/mnt/vol/bot_state.pkl", "/mnt/vol/backups"]


# ---------------------------------------------------------------- перенос старой базы: сообщение админу
def test_adoption_note_reaches_admin_even_when_startup_notice_off(env, monkeypatch):
    legacy_dir = env.tmp / "app"
    legacy_dir.mkdir()
    make_legacy_db(str(legacy_dir / "budget.db"))
    os.remove(env.db)  # init_db из фикстуры создал пустую базу; на томе её быть не должно
    for ext in ("-wal", "-shm"):
        if os.path.exists(env.db + ext):
            os.remove(env.db + ext)
    adopted = datasafe.adopt_legacy_files(env.db, str(env.data / "bot_state.pkl"), str(env.data), [str(legacy_dir)])
    assert adopted.db_from
    bot.init_db()
    rep = datasafe.check_startup(env.db, str(env.data), check_mount=False)
    rep.notes.extend(adopted.notes)
    monkeypatch.setattr(bot, "STARTUP_NOTICE", False)
    monkeypatch.setattr(bot, "STARTUP_REPORT", rep)
    fake = FakeBot()
    run(bot.send_startup_notices(types.SimpleNamespace(bot=fake)))
    assert len(fake.messages) == 1
    text = fake.messages[0][1]
    assert fake.messages[0][0] == ADMIN and "перенесена" in text and "не изменён и не удалён" in text
    assert "том новый или пустой" not in text  # про «первый запуск» не пугаем
    assert not rep.lost and rep.level == "info"
