---
name: database-migration
description: Как безопасно изменить схему базы данных SQLite в kopilka_bot77777 через migrations.py.
---

# Регламент миграций базы данных для Kopilka Bot

При добавлении новых колонок, таблиц или индексов:

1. Откройте `migrations.py`.
2. Найдите текущую максимальную версию схемы (`SCHEMA_VERSION`).
3. Добавьте функцию новой миграции `migrate_vX_to_vY(conn)`.
4. Зарегистрируйте миграцию в списке шагов обновления.
5. Убедитесь, что миграция идемпотентна (использует `IF NOT EXISTS` или проверяет `PRAGMA table_info`).
6. Запустите тесты:
   `pytest test_migrations.py -v`
