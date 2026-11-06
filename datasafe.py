"""Сохранность данных: проверка при запуске, маркер в томе, резервные копии, проверка и восстановление.

Модуль не зависит от Telegram, чтобы его можно было тестировать на временной SQLite-базе.

Как отличить «новая установка» от «база потерялась»:
1. Маркер в самом томе (DATA_DIR/.kopilka_marker.json) помнит, сколько данных было при прошлом запуске.
   Есть маркер с данными, а база пуста или пропала -> база потеряна.
2. Если потерян весь том (вместе с маркером), маркер не поможет. Для этого есть переменная хостинга
   EXPECT_EXISTING_DATA=1: её ставят один раз после первого запуска с данными, и она переживает пересборку.
3. Сообщение админу при каждом запуске содержит id установки и счётчик запусков: после потери тома
   счётчик возвращается к 1, а id меняется, это видно в переписке.
"""
import datetime
import gzip
import json
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger("budget-bot")

MARKER_NAME = ".kopilka_marker.json"
SQLITE_HEADER = b"SQLite format 3\x00"

# таблицы и колонки, без которых файл не считается базой бота
REQUIRED_TABLES = {
    "settings": {"user_id", "key", "value"},
    "mandatory": {"id", "user_id", "name", "amount"},
    "expenses": {"id", "user_id", "spent_date", "amount", "category"},
    "savings_topups": {"id", "user_id", "topup_date", "amount", "comment"},
}

AUTO_BACKUP_RE = re.compile(r"^auto_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}\.db$")
LEVELS = {"info": 0, "warn": 1, "critical": 2}


# ---------------------------------------------------------------- вспомогательное
def env_flag(value, default: bool = False) -> bool:
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "да"}


def is_inside(path: str, directory: str) -> bool:
    """Лежит ли path внутри directory (после раскрытия .. и симлинков)."""
    p = os.path.realpath(os.path.abspath(path))
    d = os.path.realpath(os.path.abspath(directory))
    try:
        return os.path.commonpath([p, d]) == d
    except ValueError:
        return False


def human_size(n: int | float) -> str:
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024 or unit == "ГБ":
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} ГБ"


def db_stats(db_path: str) -> dict:
    """Только чтение. Базу, которой нет, не создаёт."""
    stats = {"exists": False, "size": 0, "users": 0, "expenses": 0, "topups": 0, "mandatory": 0, "empty": True}
    if not os.path.isfile(db_path):
        return stats
    stats["exists"] = True
    stats["size"] = os.path.getsize(db_path)
    try:
        conn = sqlite3.connect(db_path, timeout=10)
    except sqlite3.Error:
        return stats
    try:
        def one(q):
            try:
                return conn.execute(q).fetchone()[0] or 0
            except sqlite3.Error:
                return 0
        stats["users"] = one("SELECT COUNT(DISTINCT user_id) FROM settings WHERE user_id != 0")
        stats["expenses"] = one("SELECT COUNT(*) FROM expenses")
        stats["topups"] = one("SELECT COUNT(*) FROM savings_topups")
        stats["mandatory"] = one("SELECT COUNT(*) FROM mandatory")
    finally:
        conn.close()
    stats["empty"] = not (stats["users"] or stats["expenses"] or stats["topups"] or stats["mandatory"])
    return stats


def stats_line(s: dict) -> str:
    return (
        f"пользователей: {s['users']}, трат: {s['expenses']}, "
        f"операций с копилкой: {s['topups']}, размер: {human_size(s['size'])}"
    )


# ---------------------------------------------------------------- маркер
def marker_path(data_dir: str) -> str:
    return os.path.join(data_dir, MARKER_NAME)


def read_marker(data_dir: str) -> dict | None:
    try:
        with open(marker_path(data_dir), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        logger.warning("Маркер %s повреждён, будет создан заново", marker_path(data_dir))
        return None


def write_marker(data_dir: str, marker: dict) -> bool:
    """Атомарная запись. False, если том недоступен для записи (это тоже проблема, её видно в логе)."""
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = marker_path(data_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(marker, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, marker_path(data_dir))
        return True
    except OSError as e:
        logger.error("Не удалось записать маркер в %s: %s", data_dir, e)
        return False


def _now_iso(now: datetime.datetime | None = None) -> str:
    return (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


# ---------------------------------------------------------------- проверка при запуске
_PSEUDO_FS = {
    "proc", "sysfs", "devpts", "tmpfs", "cgroup", "cgroup2", "mqueue", "devtmpfs", "overlay",
    "securityfs", "debugfs", "tracefs", "fusectl", "configfs", "pstore", "bpf", "autofs", "binfmt_misc",
}
_SYSTEM_MOUNTS = ("/proc", "/sys", "/dev", "/etc/hosts", "/etc/hostname", "/etc/resolv.conf", "/run")


def list_extra_mounts(mounts_file: str = "/proc/mounts") -> list[str]:
    """Точки монтирования, похожие на подключённые тома (не системные и не корень контейнера).
    Помогает найти путь, куда хостинг монтирует постоянный диск."""
    out = []
    try:
        with open(mounts_file, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                _, point, fstype = parts[:3]
                if fstype in _PSEUDO_FS or point == "/" or point.startswith(_SYSTEM_MOUNTS):
                    continue
                out.append(point.replace("\\040", " "))
    except OSError:
        pass
    return out


@dataclass
class StartupReport:
    db_path: str
    data_dir: str
    stats: dict
    problems: list = field(default_factory=list)  # [(уровень, текст)]
    new_install: bool = False
    lost: bool = False
    path_bad: bool = False
    install_id: str = ""
    starts: int = 0
    marker_written: bool = False
    notes: list = field(default_factory=list)  # информационные сообщения (например, «база перенесена»)

    @property
    def level(self) -> str:
        return max((lvl for lvl, _ in self.problems), key=lambda x: LEVELS[x], default="info")

    def texts(self, min_level: str = "info") -> list[str]:
        return [t for lvl, t in self.problems if LEVELS[lvl] >= LEVELS[min_level]]


def check_startup(
    db_path: str,
    data_dir: str,
    *,
    expect_data: bool = False,
    allow_ephemeral: bool = False,
    check_mount: bool = True,
    now: datetime.datetime | None = None,
) -> StartupReport:
    """Оценивает состояние хранилища и обновляет маркер. Базу не создаёт и не меняет."""
    abs_db = os.path.abspath(db_path)
    stats = db_stats(abs_db)
    rep = StartupReport(db_path=abs_db, data_dir=data_dir, stats=stats)

    # 1) путь к базе
    inside = is_inside(abs_db, data_dir)
    if not inside:
        rep.path_bad = True
        msg = (
            f"База лежит НЕ на постоянном томе: DB_PATH = {abs_db}, а том смонтирован в {data_dir}. "
            "При пересборке контейнера эти данные пропадут. Проверьте переменную DB_PATH у хостинга: "
            f"она должна быть {os.path.join(data_dir, 'budget.db')}."
        )
        rep.problems.append(("info" if allow_ephemeral else "critical", msg))
    elif check_mount and not os.path.ismount(data_dir):
        rep.problems.append((
            "warn",
            f"{data_dir} не похож на отдельный смонтированный том (это обычная папка контейнера). "
            "Если том подключён, а предупреждение ложное, задайте SKIP_MOUNT_CHECK=1; "
            "если не подключён, данные пропадут при пересборке."
            + (
                " Подключённые к контейнеру тома (возможно, один из них и есть ваш диск, "
                "тогда укажите его путь в DATA_DIR, DB_PATH и STATE_PATH): " + ", ".join(list_extra_mounts())
                if list_extra_mounts() else " Других подключённых томов в контейнере не видно."
            ),
        ))

    # 2) маркер в томе против текущей базы
    marker = read_marker(data_dir)
    prev = (marker or {}).get("last_stats") or {}
    prev_has_data = bool(prev) and not prev.get("empty", True)

    if stats["empty"]:
        if prev_has_data:
            rep.lost = True
            rep.problems.append((
                "critical",
                "БАЗА ПУСТА ИЛИ ПРОПАЛА, а раньше в ней были данные "
                f"({stats_line(prev)}; запись от {marker.get('last_stats_at', '?')}). "
                "Бот запущен с пустой базой. Если нужна копия, используйте /restore и последний "
                "присланный файл базы.",
            ))
        elif expect_data:
            rep.lost = True
            rep.problems.append((
                "critical",
                "База пуста, хотя задано EXPECT_EXISTING_DATA=1 (данные должны быть). "
                "Вероятно, том не подключён или подключён пустой. Восстановите базу через /restore.",
            ))
        elif marker is None:
            rep.new_install = True
            rep.problems.append((
                "warn",
                "Новая установка: в томе нет ни базы, ни маркера. Если это НЕ первый запуск бота, "
                "значит данные потеряны: том не подключён или создан заново.",
            ))
        else:
            rep.problems.append(("info", "База пока пустая (в прошлый запуск она тоже была пустой)."))
    elif marker is None:
        rep.problems.append(("info", "База с данными найдена, маркер в томе создан впервые."))

    # 3) обновляем маркер. Если данные потеряны, «последнее хорошее состояние» не затираем,
    #    иначе при следующем запуске предупреждение исчезло бы.
    m = dict(marker or {})
    m.setdefault("install_id", uuid.uuid4().hex[:8])
    m.setdefault("created_at", _now_iso(now))
    m["starts"] = int(m.get("starts", 0)) + 1
    m["last_start"] = _now_iso(now)
    m["db_path"] = abs_db
    if rep.lost:
        m.setdefault("lost_since", _now_iso(now))
    elif not stats["empty"] or not prev_has_data:
        m["last_stats"] = stats
        m["last_stats_at"] = _now_iso(now)
        m.pop("lost_since", None)
    rep.install_id = m["install_id"]
    rep.starts = m["starts"]
    rep.marker_written = write_marker(data_dir, m)
    if not rep.marker_written:
        rep.problems.append((
            "critical" if inside else "warn",
            f"Не удалось записать маркер в {data_dir}: каталог недоступен для записи.",
        ))
    return rep


def refresh_marker(data_dir: str, db_path: str, now: datetime.datetime | None = None, **extra) -> dict | None:
    """Обновляет last_stats в маркере (вызывается периодически и после /restore).
    Пока потеря не устранена (база пуста, а раньше были данные), «хорошее состояние» не затирается."""
    marker = read_marker(data_dir)
    if marker is None:
        return None
    stats = db_stats(os.path.abspath(db_path))
    prev = marker.get("last_stats") or {}
    prev_has_data = bool(prev) and not prev.get("empty", True)
    if not stats["empty"] or not prev_has_data:
        marker["last_stats"] = stats
        marker["last_stats_at"] = _now_iso(now)
        marker.pop("lost_since", None)
    marker.update(extra)
    write_marker(data_dir, marker)
    return marker


def format_marker_summary(data_dir: str) -> str:
    m = read_marker(data_dir)
    if not m:
        return "маркер в томе: нет"
    return f"том: id {m.get('install_id', '?')}, запусков: {m.get('starts', '?')}, создан {m.get('created_at', '?')}"


# ---------------------------------------------------------------- перенос старой базы
LEGACY_DB_NAME = "budget.db"
LEGACY_STATE_NAME = "bot_state.pkl"


@dataclass
class AdoptResult:
    db_from: str | None = None       # откуда скопирована база (None = перенос не делали)
    state_from: str | None = None    # откуда скопировано состояние диалогов
    stats: dict | None = None        # что оказалось в перенесённой базе
    notes: list = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.db_from or self.state_from)


def _unique_dirs(dirs) -> list[str]:
    seen, out = set(), []
    for d in dirs:
        if not d:
            continue
        real = os.path.realpath(os.path.abspath(d))
        if real not in seen:
            seen.add(real)
            out.append(real)
    return out


def adopt_legacy_files(
    db_path: str,
    state_path: str,
    data_dir: str,
    legacy_dirs,
    *,
    allow_ephemeral: bool = False,
) -> AdoptResult:
    """Переносит базу и состояние диалогов, лежавшие рядом с bot.py (старая раскладка), на том.

    Правила безопасности:
    - исходные файлы НЕ меняются и НЕ удаляются: база копируется через sqlite3 backup API (учитывает WAL);
    - существующую непустую базу на томе не трогаем никогда;
    - если маркер в томе помнит, что данные там уже были (том потерян/опустел), старый файл молча
      не подставляем: он может быть старее, и потеря осталась бы незамеченной. Тогда только сообщаем;
    - если база вне DATA_DIR (и это не разрешено), ничего не копируем: путь и так помечен как проблемный.
    """
    res = AdoptResult()
    abs_db = os.path.abspath(db_path)
    abs_state = os.path.abspath(state_path)
    if not is_inside(abs_db, data_dir) and not allow_ephemeral:
        return res

    dirs = _unique_dirs(legacy_dirs)
    candidates = []
    for d in dirs:
        cand = os.path.join(d, LEGACY_DB_NAME)
        if os.path.realpath(cand) == os.path.realpath(abs_db):
            continue
        st = db_stats(cand)
        if st["exists"] and not st["empty"]:
            candidates.append((cand, st))

    marker = read_marker(data_dir) or {}
    prev = marker.get("last_stats") or {}
    had_data_on_volume = bool(prev) and not prev.get("empty", True)
    already_adopted = bool(marker.get("adopted_from"))
    target = db_stats(abs_db)

    if candidates:
        if not target["empty"]:
            if not already_adopted:  # после переноса старый файл так и лежит, повторять про него не нужно
                res.notes.append(
                    f"Рядом с ботом лежит старая база {candidates[0][0]} ({stats_line(candidates[0][1])}), "
                    "но на томе уже есть своя база с данными. Она не тронута, старый файл не использован "
                    "и не удалён."
                )
        elif had_data_on_volume or already_adopted:
            res.notes.append(
                f"Найден старый файл базы {candidates[0][0]} ({stats_line(candidates[0][1])}), но автоматически "
                "он НЕ подставлен: на томе раньше уже были данные, и старый файл может быть устаревшим. "
                "Если нужна именно эта база, скачайте её и отправьте боту через /restore."
            )
        else:
            # лучший кандидат: где больше всего записей (трат + копилка + обязательные)
            def weight(item):
                st = item[1]
                return (st["expenses"] + st["topups"] + st["mandatory"], st["size"])
            cand, st = max(candidates, key=weight)
            try:
                validate_db_file(cand)
            except RestoreError as e:
                res.notes.append(f"Старая база {cand} найдена, но не перенесена: {e}")
            else:
                # пустая заготовка на томе (если есть) заменяется; её хвосты WAL удаляем, иначе они
                # могли бы примениться к чужому файлу
                for suffix in ("-wal", "-shm"):
                    try:
                        os.remove(abs_db + suffix)
                    except FileNotFoundError:
                        pass
                backup_to_file(cand, abs_db)
                copied = db_stats(abs_db)
                if copied["empty"] or (copied["expenses"], copied["topups"]) != (st["expenses"], st["topups"]):
                    # копия не совпала с оригиналом: откатываем пустую копию и сообщаем
                    os.remove(abs_db)
                    res.notes.append(f"Перенос базы из {cand} не удался: копия не совпала с оригиналом.")
                else:
                    res.db_from, res.stats = cand, copied
                    res.notes.append(
                        f"Старая база перенесена на постоянный том: {cand} -> {abs_db} "
                        f"({stats_line(copied)}). Исходный файл не изменён и не удалён."
                    )
                    if len(candidates) > 1:
                        others = ", ".join(c for c, _ in candidates if c != cand)
                        res.notes.append(f"Были и другие старые базы (не использованы): {others}.")

    # состояние диалогов: копируем, только если на томе его нет
    if not os.path.exists(abs_state):
        sources = []
        if res.db_from:
            sources.append(os.path.dirname(res.db_from))
        sources.extend(dirs)
        for d in _unique_dirs(sources):
            cand = os.path.join(d, LEGACY_STATE_NAME)
            if os.path.realpath(cand) == os.path.realpath(abs_state):
                continue
            if os.path.isfile(cand) and os.path.getsize(cand) > 0:
                os.makedirs(os.path.dirname(abs_state), exist_ok=True)
                tmp = abs_state + ".tmp"
                shutil.copyfile(cand, tmp)
                os.replace(tmp, abs_state)
                res.state_from = cand
                res.notes.append(f"Состояние диалогов перенесено: {cand} -> {abs_state}.")
                break
    return res


# ---------------------------------------------------------------- резервные копии
def backup_to_file(db_path: str, dest: str) -> str:
    """Согласованная копия через sqlite3 backup API (учитывает WAL: данные из -wal попадают в копию).
    Копия самодостаточна: один файл без -wal/-shm. Проверяется integrity_check."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    tmp = dest + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
        res = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if res != "ok":
            raise sqlite3.DatabaseError(f"копия не прошла integrity_check: {res}")
    except Exception:
        dst.close()
        src.close()
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    dst.close()
    src.close()
    os.replace(tmp, dest)
    return dest


def backup_bytes(db_path: str) -> bytes:
    with tempfile.TemporaryDirectory() as td:
        out = backup_to_file(db_path, os.path.join(td, "copy.db"))
        with open(out, "rb") as f:
            return f.read()


def rotate_auto_backups(backup_dir: str, keep: int) -> list[str]:
    """Оставляет `keep` самых свежих автокопий (auto_*.db). Остальные файлы
    (before_restore_*, pre_migration_*, чужие) не трогает никогда. Возвращает удалённые имена."""
    keep = max(1, int(keep))
    try:
        names = sorted((n for n in os.listdir(backup_dir) if AUTO_BACKUP_RE.match(n)), reverse=True)
    except FileNotFoundError:
        return []
    removed = []
    for name in names[keep:]:
        try:
            os.remove(os.path.join(backup_dir, name))
            removed.append(name)
        except OSError as e:
            logger.warning("Не удалось удалить старую копию %s: %s", name, e)
    return removed


WEEKDAY_NAMES = {  # по первым двум буквам: mon/пн/понедельник и т.д.
    "mo": 0, "tu": 1, "we": 2, "th": 3, "fr": 4, "sa": 5, "su": 6,
    "пн": 0, "по": 0, "вт": 1, "ср": 2, "чт": 3, "че": 3, "пт": 4, "пя": 4,
    "сб": 5, "су": 5, "вс": 6, "во": 6,
}


def parse_weekday(value, default: int = 6) -> int:
    """0..6 (0 = понедельник) или mon..sun / пн..вс / название дня. Непонятное значение -> default."""
    s = str(value or "").strip().lower()
    if s.isdigit():
        return int(s) if 0 <= int(s) <= 6 else default
    return WEEKDAY_NAMES.get(s[:2], default)


def parse_hhmm(value, default=(3, 0)) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        return int(m.group(1)), int(m.group(2))
    return default


def last_backup_slot(now: datetime.datetime, weekday: int, hh: int, mm: int) -> datetime.datetime:
    """Самый свежий момент расписания, который уже наступил (now должен быть aware)."""
    days_back = (now.weekday() - weekday) % 7
    slot = (now - datetime.timedelta(days=days_back)).replace(hour=hh, minute=mm, second=0, microsecond=0)
    if slot > now:
        slot -= datetime.timedelta(days=7)
    return slot


def backup_due(now: datetime.datetime, last_backup_iso: str | None, weekday: int, hh: int, mm: int) -> bool:
    """Пора ли делать копию: после последнего слота расписания копии ещё не было."""
    slot = last_backup_slot(now, weekday, hh, mm)
    if not last_backup_iso:
        return True
    try:
        last = datetime.datetime.strptime(last_backup_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc
        )
    except ValueError:
        return True
    return last < slot


# ---------------------------------------------------------------- проверка файла и восстановление
class RestoreError(Exception):
    """Сообщение предназначено для показа администратору."""


def validate_db_file(path: str, max_version: int | None = None) -> dict:
    """Проверяет присланный файл. Бросает RestoreError с понятным текстом. Возвращает статистику.
    Работает с копией, оригинал не меняется."""
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise RestoreError("Файл пустой.")
    with open(path, "rb") as f:
        if f.read(16) != SQLITE_HEADER:
            raise RestoreError("Это не файл SQLite (нет сигнатуры «SQLite format 3»).")
    try:
        conn = sqlite3.connect(path, timeout=10)
    except sqlite3.Error as e:
        raise RestoreError(f"Не удалось открыть файл как базу: {e}")
    try:
        try:
            res = conn.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.DatabaseError as e:
            raise RestoreError(f"База повреждена: {e}")
        if res != "ok":
            raise RestoreError(f"Проверка целостности не пройдена: {res}")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in tables]
        if missing:
            raise RestoreError("В файле нет нужных таблиц: " + ", ".join(missing) + ". Это не база «копилки».")
        for table, need in REQUIRED_TABLES.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            if not need <= have:
                raise RestoreError(f"В таблице {table} не хватает колонок: " + ", ".join(sorted(need - have)))
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if max_version is not None and version > max_version:
            raise RestoreError(
                f"База создана более новой версией бота (схема {version}, а этот бот знает до {max_version})."
            )
    finally:
        conn.close()
    info = db_stats(path)
    info["version"] = version
    return info


def _maybe_ungzip(src: str, dst: str) -> None:
    with open(src, "rb") as f:
        head = f.read(2)
    if head == b"\x1f\x8b":
        with gzip.open(src, "rb") as zin, open(dst, "wb") as out:
            shutil.copyfileobj(zin, out)
    else:
        shutil.copyfile(src, dst)


def prepare_restore(uploaded_path: str, today: datetime.date) -> tuple[str, dict]:
    """Копирует присланный файл во временную папку, проверяет, применяет к копии миграции
    и проверяет снова. Живую базу не трогает. Возвращает (путь к готовому кандидату, статистика).
    Временную папку удаляет вызывающий (os.path.dirname(candidate))."""
    import migrations  # здесь, чтобы не было циклического импорта

    work = tempfile.mkdtemp(prefix="restore_")
    candidate = os.path.join(work, "candidate.db")
    try:
        _maybe_ungzip(uploaded_path, candidate)
        info = validate_db_file(candidate, max_version=migrations.SCHEMA_VERSION)
        try:
            migrations.migrate(candidate, today, backup_dir=None)
        except Exception as e:
            raise RestoreError(f"Не удалось привести базу к текущей версии: {e}")
        info = validate_db_file(candidate, max_version=migrations.SCHEMA_VERSION)
        # из присланного файла убираем побочные -wal/-shm, чтобы кандидат был одним файлом
        c = sqlite3.connect(candidate)
        try:
            c.execute("PRAGMA journal_mode=DELETE")
        finally:
            c.close()
        return candidate, info
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


def apply_restore(candidate: str, db_path: str, backup_dir: str, now: datetime.datetime | None = None) -> dict:
    """Заменяет содержимое живой базы кандидатом. Перед этим сохраняет копию текущей базы
    (before_restore_*.db). Замена идёт через backup API внутри одной транзакции записи, поэтому
    файл не подменяется «из-под» работающих соединений и переход атомарный."""
    now = now or datetime.datetime.now()
    result = {"before_restore": None}
    if os.path.isfile(db_path) and os.path.getsize(db_path) > 0:
        stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
        dest = os.path.join(backup_dir, f"before_restore_{stamp}.db")
        backup_to_file(db_path, dest)
        result["before_restore"] = dest
    src = sqlite3.connect(candidate, timeout=30)
    dst = sqlite3.connect(db_path, timeout=30)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=WAL")
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if ok != "ok":
            raise sqlite3.DatabaseError(f"после восстановления integrity_check = {ok}")
    finally:
        dst.close()
        src.close()
    result["stats"] = db_stats(db_path)
    return result
