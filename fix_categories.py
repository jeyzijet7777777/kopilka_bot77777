"""Скрипт для исправления и нормализации категорий в существующей базе данных.

Использование:
    python fix_categories.py [путь_к_базе_данных]

Если путь не указан, база определяется автоматически из .env или стандартных путей.
Перед внесением изменений автоматически создаётся резервная копия базы.
"""
import datetime
import os
import shutil
import sqlite3
import sys

# Добавляем корень проекта в sys.path
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from kopilka.config import DB_PATH
from kopilka.services.categorizer import recategorize_user_expenses


def find_db_path() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip() in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)

    if len(sys.argv) > 1 and sys.argv[1].strip():
        candidate = sys.argv[1].strip()
        if os.path.exists(candidate):
            return os.path.abspath(candidate)
        else:
            print(f"❌ Указанный файл не найден: {candidate}")
            print("Использование: python fix_categories.py [путь_к_базе_данных]")
            sys.exit(1)

    candidates = [
        DB_PATH,
        os.path.join(ROOT, "budget.db"),
        os.path.join(ROOT, "data", "budget.db"),
        "/app/data/budget.db",
        r"C:\data\budget.db",
    ]

    for c in candidates:
        if c and os.path.exists(c) and os.path.getsize(c) > 0:
            return os.path.abspath(c)

    # Если не найден непустой, вернём DB_PATH
    return os.path.abspath(DB_PATH)


def main():
    db_file = find_db_path()
    print("=" * 60)
    print("🪄 Исправление категорий в базе данных")
    print("=" * 60)
    print(f"📁 База данных: {db_file}")

    if not os.path.exists(db_file) or os.path.getsize(db_file) == 0:
        print("⚠️ База данных не существует или пуста. Нечего исправлять.")
        return

    # Создаём резервную копию перед изменениями
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    backup_file = f"{db_file}.backup_before_fix_{stamp}"
    try:
        shutil.copy2(db_file, backup_file)
        print(f"💾 Создана резервная копия: {backup_file}")
    except Exception as e:
        print(f"⚠️ Не удалось создать резервную копию: {e}")

    # Запускаем перекатегоризацию
    print("\n🔍 Анализ и обновление категорий...")
    result = recategorize_user_expenses(db_path=db_file)

    scanned = result["total_scanned"]
    updated = result["updated_count"]
    changes = result["changes"]

    print(f"\n📊 Результаты:")
    print(f"• Проверено трат: {scanned}")
    print(f"• Исправлено категорий: {updated}")

    if updated > 0:
        print("\n📝 Произведённые изменения:")
        # Сортируем по количеству изменений
        sorted_changes = sorted(changes.items(), key=lambda kv: kv[1], reverse=True)
        for (old_c, new_c), count in sorted_changes:
            print(f"  • «{old_c}» -> «{new_c}» ({count} трат)")
        print("\n✅ База данных успешно обновлена!")
    else:
        print("\n✨ Все категории в базе уже в порядке. Изменений не требуется.")


if __name__ == "__main__":
    main()
