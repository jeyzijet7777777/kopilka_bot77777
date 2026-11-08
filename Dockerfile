FROM python:3.11-slim

WORKDIR /app

# Всё, что должно пережить пересборку (база, состояние диалогов, резервные копии), лежит в /app/data (на Bothost это постоянная папка проекта: сохраняется при «Обновить из Git»).
# На других хостингах к этому пути нужно подключить постоянный том (Volume / Storage / Disk).
# Переменные хостинга имеют приоритет над ENV ниже: если там задан DB_PATH вне /app/data,
# бот при старте предупредит администратора (а при STRICT_DATA_PATH=1 откажется запускаться).
# Инструкцию VOLUME намеренно не используем: часть хостингов её не поддерживает, а в локальном
# docker run она создаёт анонимный том, из-за которого проверка «том не подключён» ничего не увидит.
ENV PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    DATA_DIR=/app/data \
    DB_PATH=/app/data/budget.db \
    STATE_PATH=/app/data/bot_state.pkl \
    WIDGET_API_PORT=8080 \
    WIDGET_API_BASE_URL=https://bot-1793275701-1092-jeyzi008.bothost.tech

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /app/data
COPY kopilka ./kopilka
COPY *.py template.xlsx ./

EXPOSE 8080

CMD ["python", "bot.py"]
