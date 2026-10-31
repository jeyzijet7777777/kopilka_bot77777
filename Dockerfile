FROM python:3.11-slim

WORKDIR /app

# DB_PATH и STATE_PATH по умолчанию лежат в /data — подключите туда постоянный том (Volume),
# иначе база сотрётся при пересоздании контейнера. Переменные хостинга имеют приоритет.
ENV PYTHONUNBUFFERED=1 \
    MPLBACKEND=Agg \
    DB_PATH=/data/budget.db \
    STATE_PATH=/data/bot_state.pkl

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN mkdir -p /data
COPY bot.py template.xlsx ./

VOLUME ["/data"]
CMD ["python", "bot.py"]
