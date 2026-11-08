"""REST API и Web App для виджетов iOS и Android.

Предоставляет:
1. REST API эндпоинты для получения сводки бюджета и добавления трат.
2. Готовый скрипт для виджетов iOS Scriptable (Small, Medium, Lock Screen).
3. Инструкции и конфигурацию для Apple Shortcuts (iOS Команды / Siri).
4. Шаблоны для Android KWGT, Tasker и MacroDroid.
5. Интерактивное веб-приложение Telegram Mini App (Web App).
"""

import asyncio
import datetime
import json
import logging
import os
import secrets
from typing import Any, Callable

from aiohttp import web
from kopilka.database.connection import db
from kopilka.database.repository import get_setting, set_setting
from kopilka.services.goals_service import get_piggy_goal
from kopilka.utils.formatting import fmt_money, today_date
from kopilka.utils.parsers import parse_expense_message

logger = logging.getLogger("budget-bot.widget_api")

# Реестр внешних функций логики из bot.py
_GET_BUDGET_INFO: Callable[[int, datetime.date], dict] | None = None
_CALC_CHANGE_POOL: Callable[[int, datetime.date], float] | None = None
_DAY_SPENT: Callable[[int, datetime.date], float] | None = None
_CANONICAL_CAT: Callable[[int, str], str] | None = None
_CLASSIFY_CAT: Callable[[int, str, list], tuple] | None = None
_USED_CATS: Callable[[int, int], list] | None = None
_BOT_USERNAME: str = "kopilka_bot"

_RUNNER: web.AppRunner | None = None
_SITE: web.TCPSite | None = None


def configure_api(
    budget_info_fn: Callable[[int, datetime.date], dict] | None = None,
    change_pool_fn: Callable[[int, datetime.date], float] | None = None,
    day_spent_fn: Callable[[int, datetime.date], float] | None = None,
    canonical_cat_fn: Callable[[int, str], str] | None = None,
    classify_cat_fn: Callable[[int, str, list], tuple] | None = None,
    used_cats_fn: Callable[[int, int], list] | None = None,
    bot_username: str = "kopilka_bot",
):
    """Инициализация ссылок на функции подсчета бюджета из бота."""
    global _GET_BUDGET_INFO, _CALC_CHANGE_POOL, _DAY_SPENT, _CANONICAL_CAT, _CLASSIFY_CAT, _USED_CATS, _BOT_USERNAME
    _GET_BUDGET_INFO = budget_info_fn
    _CALC_CHANGE_POOL = change_pool_fn
    _DAY_SPENT = day_spent_fn
    _CANONICAL_CAT = canonical_cat_fn
    _CLASSIFY_CAT = classify_cat_fn
    _USED_CATS = used_cats_fn
    if bot_username:
        _BOT_USERNAME = bot_username


# --- Управление токенами доступа к виджетам ---


def get_or_create_widget_token(user_id: int) -> str:
    """Возвращает существующий токен пользователя или генерирует новый."""
    token = get_setting(user_id, "widget_token")
    if not token:
        token = secrets.token_urlsafe(16)
        set_setting(user_id, "widget_token", token)
    return token


def rotate_widget_token(user_id: int) -> str:
    """Генерирует новый токен для пользователя (старый инвалидируется)."""
    token = secrets.token_urlsafe(16)
    set_setting(user_id, "widget_token", token)
    return token


def get_user_id_by_token(token: str) -> int | None:
    """Находит user_id по секретному токену виджета."""
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM settings WHERE key = 'widget_token' AND value = ?",
            (token,),
        ).fetchone()
        return int(row["user_id"]) if row else None


# --- Сбор данных для API ---


def get_widget_summary_data(user_id: int) -> dict[str, Any]:
    """Формирует полную сводку текущего дня для виджетов и Web App."""
    today = today_date()

    if _GET_BUDGET_INFO:
        info = _GET_BUDGET_INFO(user_id, today)
        daily_limit = float(info.get("daily_limit", 0.0))
        piggy_balance = float(info.get("piggy_total", 0.0))
    else:
        daily_limit = 1000.0
        piggy_balance = 0.0

    if _DAY_SPENT:
        daily_spent = float(_DAY_SPENT(user_id, today))
    else:
        with db() as conn:
            r = conn.execute(
                "SELECT SUM(amount) AS total FROM expenses WHERE user_id = ? AND spent_date = ?",
                (user_id, today.isoformat()),
            ).fetchone()
            daily_spent = float(r["total"]) if r and r["total"] else 0.0

    if _CALC_CHANGE_POOL:
        change_pool = float(_CALC_CHANGE_POOL(user_id, today))
    else:
        change_pool = 0.0

    daily_remaining = max(0.0, daily_limit - daily_spent)
    overrun = max(0.0, daily_spent - daily_limit)
    spent_pct = round((daily_spent / daily_limit) * 100, 1) if daily_limit > 0 else 0.0

    goal = get_piggy_goal(user_id)
    goal_data = None
    if goal:
        target = goal["target"]
        goal_pct = round((piggy_balance / target) * 100, 1) if target > 0 else 0.0
        goal_data = {
            "title": goal["title"],
            "target": target,
            "target_fmt": fmt_money(target),
            "percent": goal_pct,
            "remaining": max(0.0, target - piggy_balance),
            "remaining_fmt": fmt_money(max(0.0, target - piggy_balance)),
            "achieved": piggy_balance >= target,
        }

    # Последние 5 трат за сегодня
    with db() as conn:
        rows = conn.execute(
            "SELECT id, amount, category, created_at FROM expenses "
            "WHERE user_id = ? AND spent_date = ? ORDER BY id DESC LIMIT 5",
            (user_id, today.isoformat()),
        ).fetchall()
        recent_expenses = [
            {
                "id": r["id"],
                "amount": float(r["amount"]),
                "amount_fmt": fmt_money(float(r["amount"])),
                "category": r["category"],
            }
            for r in rows
        ]

    status_text = (
        f"Остаток: {fmt_money(daily_remaining)}"
        if daily_spent <= daily_limit
        else f"Превышение: +{fmt_money(overrun)}"
    )

    return {
        "ok": True,
        "date": today.isoformat(),
        "date_fmt": today.strftime("%d.%m.%Y"),
        "user_id": user_id,
        "daily_limit": daily_limit,
        "daily_limit_fmt": fmt_money(daily_limit),
        "daily_spent": daily_spent,
        "daily_spent_fmt": fmt_money(daily_spent),
        "daily_remaining": daily_remaining,
        "daily_remaining_fmt": fmt_money(daily_remaining),
        "overrun": overrun,
        "overrun_fmt": fmt_money(overrun),
        "spent_percent": spent_pct,
        "change_pool": change_pool,
        "change_pool_fmt": fmt_money(change_pool),
        "piggy_balance": piggy_balance,
        "piggy_balance_fmt": fmt_money(piggy_balance),
        "status_text": status_text,
        "currency": "₽",
        "goal": goal_data,
        "recent_expenses": recent_expenses,
    }


def add_widget_expense_sync(
    user_id: int,
    amount: float | None = None,
    category: str | None = None,
    text: str | None = None,
) -> dict[str, Any]:
    """Добавляет трату из API (по сумме/категории или из текста вроде «450 кофе»)."""
    today = today_date()
    spent_date = today

    if text and not amount:
        parsed = parse_expense_message(text)
        if not parsed:
            raise ValueError("Не удалось распознать сумму и категорию в тексте")
        amount, cat_raw, spent_d = parsed
        spent_date = spent_d if spent_d <= today else today
        category = cat_raw

    if not amount or amount <= 0:
        raise ValueError("Сумма расхода должна быть больше 0")

    cat_name = (category or "Разное").strip()
    if _CLASSIFY_CAT and _USED_CATS and cat_name != "Разное":
        used = _USED_CATS(user_id, 15)
        classified, _ = _CLASSIFY_CAT(user_id, cat_name, used)
        if classified and classified != "Разное":
            cat_name = classified

    if _CANONICAL_CAT:
        cat_name = _CANONICAL_CAT(user_id, cat_name)

    with db() as conn:
        exp_id = conn.execute(
            "INSERT INTO expenses (user_id, spent_date, amount, category) VALUES (?, ?, ?, ?)",
            (user_id, spent_date.isoformat(), float(amount), cat_name),
        ).lastrowid

    summary = get_widget_summary_data(user_id)
    return {
        "ok": True,
        "expense_id": exp_id,
        "amount": float(amount),
        "amount_fmt": fmt_money(float(amount)),
        "category": cat_name,
        "spent_date": spent_date.isoformat(),
        "summary": summary,
    }


# --- HTTP Обработчики (aiohttp) ---


def _get_base_url(request: web.Request) -> str:
    """Определяет базовый внешний URL для клиента."""
    env_base = os.environ.get("WIDGET_API_BASE_URL")
    if env_base:
        return env_base.rstrip("/")
    if "bothost.tech" in request.host:
        return f"https://{request.host}"
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    if "bothost.tech" in host:
        return f"https://{host}"
    return f"{scheme}://{host}"


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }


async def handle_options(request: web.Request) -> web.Response:
    return web.Response(headers=_cors_headers())


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "kopilka_widget_api", "time": datetime.datetime.now().isoformat()})


async def handle_get_summary(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    user_id = get_user_id_by_token(token)
    if not user_id:
        return web.json_response({"ok": False, "error": "Неверный токен доступа"}, status=401, headers=_cors_headers())
    data = get_widget_summary_data(user_id)
    return web.json_response(data, headers=_cors_headers())


async def handle_post_expense(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    user_id = get_user_id_by_token(token)
    if not user_id:
        return web.json_response({"ok": False, "error": "Неверный токен доступа"}, status=401, headers=_cors_headers())

    amount = None
    category = None
    text = None

    if request.content_type == "application/json":
        try:
            body = await request.json()
            amount = body.get("amount")
            category = body.get("category")
            text = body.get("text")
        except Exception:
            return web.json_response({"ok": False, "error": "Некорректный JSON"}, status=400, headers=_cors_headers())
    else:
        post_data = await request.post()
        amount = post_data.get("amount")
        category = post_data.get("category")
        text = post_data.get("text")

    try:
        amt_float = float(amount) if amount is not None else None
    except ValueError:
        return web.json_response({"ok": False, "error": "Сумма должна быть числом"}, status=400, headers=_cors_headers())

    try:
        res = add_widget_expense_sync(user_id, amount=amt_float, category=category, text=text)
        return web.json_response(res, headers=_cors_headers())
    except Exception as e:
        logger.exception("Ошибка добавления расхода через виджет API")
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=_cors_headers())


async def handle_delete_expense(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    user_id = get_user_id_by_token(token)
    if not user_id:
        return web.json_response({"ok": False, "error": "Неверный токен доступа"}, status=401, headers=_cors_headers())

    exp_id = request.match_info.get("exp_id")
    try:
        eid = int(exp_id)
        with db() as conn:
            conn.execute("DELETE FROM expenses WHERE id = ? AND user_id = ?", (eid, user_id))
        return web.json_response({"ok": True, "deleted_id": eid, "summary": get_widget_summary_data(user_id)}, headers=_cors_headers())
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400, headers=_cors_headers())


async def handle_get_scriptable(request: web.Request) -> web.Response:
    """Возвращает готовый JavaScript файл для приложения Scriptable на iOS."""
    token = request.match_info.get("token", "")
    user_id = get_user_id_by_token(token)
    if not user_id:
        return web.Response(text="// Ошибка: Неверный токен доступа к виджету", status=401)

    base_url = _get_base_url(request)
    summary_url = f"{base_url}/api/v1/{token}/summary"
    web_app_url = f"{base_url}/webapp/{token}"
    tg_url = f"tg://resolve?domain={_BOT_USERNAME}"

    js_code = f"""// Variables used by Scriptable.
// These must be at the very top of the file. Do not edit.
// icon-color: green; icon-glyph: piggy-bank;

/**
 * Виджет «Копилка» для iOS (Scriptable)
 * Поддерживает: Small, Medium и Lock Screen (экран блокировки).
 */

const API_URL = "{summary_url}";
const BOT_URL = "{tg_url}";
const WEB_APP_URL = "{web_app_url}";

async function createWidget() {{
  let data;
  try {{
    const req = new Request(API_URL);
    req.timeoutInterval = 10;
    data = await req.loadJSON();
  }} catch (e) {{
    data = null;
  }}

  const widget = new ListWidget();
  widget.backgroundColor = new Color("#1c1c1e");
  widget.url = BOT_URL;

  if (!data || !data.ok) {{
    const errText = widget.addText("⚠️ Копилка: нет связи");
    errText.textColor = Color.red();
    errText.font = Font.boldSystemFont(13);
    return widget;
  }}

  const rem = data.daily_remaining_fmt || (Math.round(data.daily_remaining) + " ₽");
  const limit = data.daily_limit_fmt || (Math.round(data.daily_limit) + " ₽");
  const spent = data.daily_spent_fmt || (Math.round(data.daily_spent) + " ₽");
  const pool = data.change_pool_fmt || (Math.round(data.change_pool) + " ₽");
  const piggy = data.piggy_balance_fmt || (Math.round(data.piggy_balance) + " ₽");
  const pct = data.spent_percent || 0;

  // Цвет статуса: зеленый (<80%), оранжевый (80-100%), красный (перерасход)
  let statusColor = new Color("#30d158");
  if (pct >= 100 || data.daily_spent > data.daily_limit) {{
    statusColor = new Color("#ff453a");
  }} else if (pct >= 80) {{
    statusColor = new Color("#ff9f0a");
  }}

  const family = config.widgetFamily || "small";

  if (family === "accessoryCircular" || family === "accessoryRectangular") {{
    // Виджет для экрана блокировки iOS
    const lockText = widget.addText(rem);
    lockText.font = Font.boldSystemFont(15);
    const sub = widget.addText("из " + limit);
    sub.font = Font.systemFont(11);
    sub.textColor = Color.gray();
    return widget;
  }}

  if (family === "small") {{
    // Заголовок
    const headerStack = widget.addStack();
    headerStack.centerAlignContent();
    const icon = headerStack.addText("🐷 ");
    icon.font = Font.systemFont(13);
    const title = headerStack.addText("Копилка");
    title.font = Font.boldSystemFont(13);
    title.textColor = new Color("#8e8e93");
    headerStack.addSpacer();

    widget.addSpacer(6);

    // Главная сумма остатка
    const remLabel = widget.addText("Остаток на день");
    remLabel.font = Font.systemFont(10);
    remLabel.textColor = new Color("#8e8e93");

    const sumText = widget.addText(rem);
    sumText.font = Font.boldSystemFont(20);
    sumText.textColor = statusColor;
    sumText.minimumScaleFactor = 0.7;

    widget.addSpacer(3);

    // Прогресс-бар
    const barStack = widget.addStack();
    barStack.size = new Size(130, 4);
    barStack.cornerRadius = 2;
    barStack.backgroundColor = new Color("#2c2c2e");
    const fillWidth = Math.min(130, Math.max(4, Math.round((pct / 100) * 130)));
    const fillStack = barStack.addStack();
    fillStack.size = new Size(fillWidth, 4);
    fillStack.cornerRadius = 2;
    fillStack.backgroundColor = statusColor;

    widget.addSpacer(6);

    // Банк сдачи
    const poolStack = widget.addStack();
    const poolLabel = poolStack.addText("Сдача: ");
    poolLabel.font = Font.systemFont(11);
    poolLabel.textColor = new Color("#8e8e93");
    const poolVal = poolStack.addText(pool);
    poolVal.font = Font.boldSystemFont(11);
    poolVal.textColor = data.change_pool >= 0 ? new Color("#30d158") : new Color("#ff453a");

    return widget;
  }}

  // Medium Widget (горизонтальный)
  const mainStack = widget.addStack();
  mainStack.layoutHorizontally();

  // Левая колонка: Дневной бюджет
  const leftCol = mainStack.addStack();
  leftCol.layoutVertically();
  leftCol.size = new Size(160, 0);

  const head = leftCol.addStack();
  head.addText("🐷 ");
  const t = head.addText("Копилка • Сегодня");
  t.font = Font.boldSystemFont(12);
  t.textColor = new Color("#8e8e93");

  leftCol.addSpacer(8);
  const bigSum = leftCol.addText(rem);
  bigSum.font = Font.boldSystemFont(24);
  bigSum.textColor = statusColor;
  bigSum.minimumScaleFactor = 0.7;

  leftCol.addSpacer(2);
  const sub = leftCol.addText("Потрачено " + spent + " из " + limit);
  sub.font = Font.systemFont(10);
  sub.textColor = new Color("#8e8e93");

  leftCol.addSpacer(6);
  const bar = leftCol.addStack();
  bar.size = new Size(140, 5);
  bar.cornerRadius = 2.5;
  bar.backgroundColor = new Color("#2c2c2e");
  const w = Math.min(140, Math.max(4, Math.round((pct / 100) * 140)));
  const fill = bar.addStack();
  fill.size = new Size(w, 5);
  fill.cornerRadius = 2.5;
  fill.backgroundColor = statusColor;

  mainStack.addSpacer(12);

  // Правая колонка: Сдача, Копилка и Цель
  const rightCol = mainStack.addStack();
  rightCol.layoutVertically();

  const poolTitle = rightCol.addText("Банк сдачи");
  poolTitle.font = Font.systemFont(10);
  poolTitle.textColor = new Color("#8e8e93");
  const poolV = rightCol.addText(pool);
  poolV.font = Font.boldSystemFont(14);
  poolV.textColor = data.change_pool >= 0 ? new Color("#30d158") : new Color("#ff453a");

  rightCol.addSpacer(6);
  const pigTitle = rightCol.addText(data.goal ? "Цель: " + data.goal.title : "Копилка");
  pigTitle.font = Font.systemFont(10);
  pigTitle.textColor = new Color("#8e8e93");
  const pigV = rightCol.addText(piggy);
  pigV.font = Font.boldSystemFont(14);
  pigV.textColor = new Color("#0a84ff");

  if (data.goal) {{
    const gPct = rightCol.addText(data.goal.percent + "% накоплено");
    gPct.font = Font.systemFont(9);
    gPct.textColor = new Color("#8e8e93");
  }}

  return widget;
}}

const widget = await createWidget();
if (config.runsInWidget) {{
  Script.setWidget(widget);
}} else {{
  await widget.presentMedium();
}}
Script.complete();
"""
    return web.Response(text=js_code, content_type="application/javascript", headers={"Content-Disposition": "inline; filename=kopilka_widget.js"})


async def handle_get_shortcuts(request: web.Request) -> web.Response:
    """Возвращает веб-страницу с инструкцией и кнопками для настройки Команд Apple на iOS."""
    token = request.match_info.get("token", "")
    user_id = get_user_id_by_token(token)
    if not user_id:
        return web.Response(text="Неверный токен", status=401)

    base_url = _get_base_url(request)
    post_url = f"{base_url}/api/v1/{token}/expense"
    summary_url = f"{base_url}/api/v1/{token}/summary"

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Быстрые команды iOS • Копилка</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #000; color: #fff; margin: 0; padding: 20px; line-height: 1.5; }}
    .card {{ background: #1c1c1e; border-radius: 16px; padding: 20px; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.5); }}
    h1 {{ font-size: 24px; margin-top: 0; display: flex; align-items: center; gap: 8px; }}
    h2 {{ font-size: 18px; color: #30d158; margin-top: 0; }}
    .btn {{ display: inline-block; background: #0a84ff; color: #fff; padding: 12px 20px; border-radius: 12px; text-decoration: none; font-weight: 600; text-align: center; margin: 8px 0; }}
    code {{ background: #2c2c2e; padding: 3px 6px; border-radius: 6px; font-size: 13px; color: #ff9f0a; word-break: break-all; }}
    ol {{ padding-left: 20px; }}
    li {{ margin-bottom: 10px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🐷 Копилка • Быстрые команды iOS</h1>
    <p>Настройте команду Siri для мгновенной записи расходов прямо с экрана «Домой», кнопки Action или голосом.</p>
  </div>

  <div class="card">
    <h2>⚡️ Команда «Записать расход» (1 клик)</h2>
    <p>Создайте команду в стандартном приложении <b>«Команды» (Shortcuts)</b>:</p>
    <ol>
      <li>Откройте приложение <b>Команды</b> на iPhone и нажмите <b>«+»</b>.</li>
      <li>Добавьте действие <b>«Запросить ввод»</b> (Тип: <i>Текст</i>, Подсказка: <i>«Сумма и категория, например: 450 кофе»</i>).</li>
      <li>Добавьте действие <b>«Получить содержимое URL»</b>:
        <br>URL: <code>{post_url}</code>
        <br>Метод: <b>POST</b>
        <br>Запрос: <b>JSON</b>
        <br>Ключ <code>text</code> = выберите переменную <i>«Предоставленный ввод»</i>.
      </li>
      <li>Добавьте действие <b>«Показать уведомление»</b> или <b>«Произнести»</b> ответ.</li>
      <li>Готово! Назовите команду <b>«Записать расход»</b>.</li>
    </ol>
    <p>💡 <i>Теперь можно сказать: <b>«Привет, Siri, записать расход»</b> или повесить команду на кнопку Action / двойной тап по спинке iPhone!</i></p>
  </div>

  <div class="card">
    <h2>📊 Команда «Остаток на сегодня»</h2>
    <ol>
      <li>Создайте команду с действием <b>«Получить содержимое URL»</b>: <code>{summary_url}</code>.</li>
      <li>Действие <b>«Получить значение словаря»</b> для ключа <code>status_text</code>.</li>
      <li>Действие <b>«Показать предупреждение»</b> или <b>«Произнести»</b>.</li>
    </ol>
  </div>
</body>
</html>
"""
    return web.Response(text=html, content_type="text/html")


async def handle_get_kwgt(request: web.Request) -> web.Response:
    """Возвращает руководство и JSON-конфигурацию для Android KWGT / Tasker / MacroDroid."""
    token = request.match_info.get("token", "")
    user_id = get_user_id_by_token(token)
    if not user_id:
        return web.Response(text="Неверный токен", status=401)

    base_url = _get_base_url(request)
    summary_url = f"{base_url}/api/v1/{token}/summary"
    post_url = f"{base_url}/api/v1/{token}/expense"

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Виджеты Android • Копилка</title>
  <style>
    body {{ font-family: Roboto, -apple-system, sans-serif; background: #121212; color: #fff; margin: 0; padding: 20px; line-height: 1.5; }}
    .card {{ background: #1e1e1e; border-radius: 14px; padding: 20px; margin-bottom: 20px; }}
    h1 {{ font-size: 22px; color: #80cbc4; margin-top: 0; }}
    h2 {{ font-size: 17px; color: #81c784; }}
    code {{ background: #2d2d2d; padding: 4px 8px; border-radius: 6px; font-size: 13px; color: #ffb74d; word-break: break-all; display: inline-block; margin: 4px 0; }}
    ol {{ padding-left: 20px; }}
    li {{ margin-bottom: 8px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>🤖 Копилка • Виджеты и автоматизация Android</h1>
    <p>Способы интеграции с вашим Android-устройством:</p>
  </div>

  <div class="card">
    <h2>1. KWGT (Kustom Widget)</h2>
    <p>В приложении KWGT создайте виджет с формулой запроса по сети:</p>
    <p>Остаток на сегодня:</p>
    <code>$wg("{summary_url}", json, .daily_remaining_fmt)$</code>
    <p>Процент израсходования:</p>
    <code>$wg("{summary_url}", json, .spent_percent)$%</code>
    <p>Банк сдачи:</p>
    <code>$wg("{summary_url}", json, .change_pool_fmt)$</code>
  </div>

  <div class="card">
    <h2>2. Авто-запись из банковских пушей (MacroDroid / Tasker)</h2>
    <p>Когда приходит пуш от банка (Сбер, Т-Банк, Альфа):</p>
    <ol>
      <li><b>Триггер:</b> Уведомление получено (выберите приложение банка).</li>
      <li><b>Действие:</b> HTTP Запрос (POST) на адрес:<br><code>{post_url}</code></li>
      <li><b>Тело запроса (JSON):</b> <code>{{"text": "[notification_text]"}}</code></li>
    </ol>
    <p>🎉 Бот автоматически распознает сумму, магазин и запишет расход без вашего участия!</p>
  </div>

  <div class="card">
    <h2>3. Ярлык чата прямо на рабочем столе</h2>
    <p>В самом Telegram: откройте диалог с ботом → нажмите три точки (⋮) в правом верхнем углу → <b>«Добавить на главный экран»</b>.</p>
  </div>
</body>
</html>
"""
    return web.Response(text=html, content_type="text/html")


async def handle_webapp(request: web.Request) -> web.Response:
    """Рендерит полноценный интерактивный Telegram Mini App (Web App)."""
    token = request.match_info.get("token", "")
    user_id = get_user_id_by_token(token)
    if not user_id:
        return web.Response(text="<h1>Неверный токен доступа</h1>", status=401, content_type="text/html")

    base_url = _get_base_url(request)
    summary_url = f"{base_url}/api/v1/{token}/summary"
    expense_url = f"{base_url}/api/v1/{token}/expense"
    delete_url_prefix = f"{base_url}/api/v1/{token}/expense/"

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
  <title>Копилка • Mini App</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <style>
    :root {{
      --bg: var(--tg-theme-bg-color, #0f172a);
      --text: var(--tg-theme-text-color, #f8fafc);
      --hint: var(--tg-theme-hint-color, #94a3b8);
      --card-bg: var(--tg-theme-secondary-bg-color, #1e293b);
      --accent: var(--tg-theme-button-color, #38bdf8);
      --btn-text: var(--tg-theme-button-text-color, #ffffff);
      --success: #22c55e;
      --danger: #ef4444;
      --warning: #f59e0b;
    }}
    * {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
    body {{
      margin: 0; padding: 16px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg); color: var(--text); user-select: none;
    }}
    .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }}
    .title {{ font-size: 20px; font-weight: 700; display: flex; align-items: center; gap: 8px; }}
    .date-badge {{ font-size: 13px; color: var(--hint); background: var(--card-bg); padding: 4px 10px; border-radius: 12px; }}
    
    .hero-card {{
      background: var(--card-bg); border-radius: 20px; padding: 20px; text-align: center;
      margin-bottom: 16px; box-shadow: 0 4px 20px rgba(0,0,0,0.15); position: relative; overflow: hidden;
    }}
    .progress-circle {{
      position: relative; width: 140px; height: 140px; margin: 0 auto 12px;
    }}
    .progress-circle svg {{ transform: rotate(-90deg); width: 140px; height: 140px; }}
    .progress-circle circle {{
      fill: none; stroke-width: 10; stroke-linecap: round;
    }}
    .circle-bg {{ stroke: rgba(255,255,255,0.08); }}
    .circle-val {{
      stroke: var(--success); stroke-dasharray: 377; stroke-dashoffset: 377;
      transition: stroke-dashoffset 0.6s ease, stroke 0.3s;
    }}
    .circle-text {{
      position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); text-align: center;
    }}
    .circle-sum {{ font-size: 22px; font-weight: 800; line-height: 1.1; }}
    .circle-sub {{ font-size: 11px; color: var(--hint); margin-top: 2px; }}

    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 16px; }}
    .card {{
      background: var(--card-bg); border-radius: 16px; padding: 14px; display: flex; flex-direction: column;
    }}
    .card-label {{ font-size: 11px; color: var(--hint); margin-bottom: 4px; }}
    .card-value {{ font-size: 17px; font-weight: 700; }}
    .card-sub {{ font-size: 10px; color: var(--hint); margin-top: 2px; }}

    .add-box {{
      background: var(--card-bg); border-radius: 18px; padding: 16px; margin-bottom: 16px;
    }}
    .box-title {{ font-size: 14px; font-weight: 600; margin-bottom: 12px; display: flex; align-items: center; gap: 6px; }}
    .input-row {{ display: flex; gap: 8px; margin-bottom: 10px; }}
    input[type="number"], input[type="text"] {{
      flex: 1; background: rgba(0,0,0,0.25); border: 1px solid rgba(255,255,255,0.1); border-radius: 12px;
      padding: 12px; color: var(--text); font-size: 16px; outline: none;
    }}
    input:focus {{ border-color: var(--accent); }}
    .chips {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }}
    .chip {{
      background: rgba(255,255,255,0.06); border: 1px solid transparent; border-radius: 10px;
      padding: 6px 10px; font-size: 12px; cursor: pointer; transition: 0.2s;
    }}
    .chip.active {{ background: var(--accent); color: var(--btn-text); font-weight: 600; }}
    .btn-submit {{
      width: 100%; background: var(--accent); color: var(--btn-text); border: none; border-radius: 12px;
      padding: 13px; font-size: 15px; font-weight: 700; cursor: pointer; transition: opacity 0.2s;
    }}
    .btn-submit:active {{ opacity: 0.8; }}

    .expenses-list {{ background: var(--card-bg); border-radius: 18px; padding: 16px; }}
    .exp-item {{
      display: flex; justify-content: space-between; align-items: center; padding: 8px 0;
      border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 14px;
    }}
    .exp-item:last-child {{ border-bottom: none; }}
    .exp-del {{ color: var(--danger); cursor: pointer; padding: 4px 8px; opacity: 0.7; font-weight: bold; }}
  </style>
</head>
<body>
  <div class="header">
    <div class="title">🐷 Копилка</div>
    <div class="date-badge" id="dateBadge">Загрузка...</div>
  </div>

  <div class="hero-card">
    <div class="progress-circle">
      <svg>
        <circle class="circle-bg" cx="70" cy="70" r="60"></circle>
        <circle class="circle-val" id="circleProgress" cx="70" cy="70" r="60"></circle>
      </svg>
      <div class="circle-text">
        <div class="circle-sum" id="remSum">...</div>
        <div class="circle-sub">остаток на день</div>
      </div>
    </div>
    <div style="font-size: 13px; color: var(--hint);" id="limitSubtitle">Лимит: ...</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-label">🪙 БАНК СДАЧИ</div>
      <div class="card-value" id="poolVal">...</div>
      <div class="card-sub">накоплено экономией</div>
    </div>
    <div class="card">
      <div class="card-label">🏦 КОПИЛКА</div>
      <div class="card-value" id="piggyVal">...</div>
      <div class="card-sub" id="goalSub">баланс копилки</div>
    </div>
  </div>

  <div class="add-box">
    <div class="box-title">⚡️ Быстрая запись расхода</div>
    <div class="input-row">
      <input type="number" id="inpAmount" placeholder="Сумма (₽)" inputmode="decimal">
      <input type="text" id="inpNote" placeholder="Магазин / Товар">
    </div>
    <div class="chips" id="chipsBox">
      <div class="chip active" data-cat="Продукты">🛒 Продукты</div>
      <div class="chip" data-cat="Кафе и рестораны">☕️ Кафе</div>
      <div class="chip" data-cat="Транспорт">🚕 Транспорт</div>
      <div class="chip" data-cat="Покупки">🛍 Покупки</div>
      <div class="chip" data-cat="Здоровье">💊 Аптека</div>
      <div class="chip" data-cat="Разное">⚪️ Разное</div>
    </div>
    <button class="btn-submit" id="btnSubmit">Записать расход</button>
  </div>

  <div class="expenses-list">
    <div class="box-title">🧾 Сегодняшние траты</div>
    <div id="expensesBox">
      <div style="color: var(--hint); font-size: 13px; text-align: center; padding: 12px;">Загрузка...</div>
    </div>
  </div>

  <script>
    const tg = window.Telegram ? window.Telegram.WebApp : null;
    if (tg) {{
      tg.ready();
      tg.expand();
    }}

    let selectedCat = "Продукты";
    const chips = document.querySelectorAll(".chip");
    chips.forEach(c => {{
      c.addEventListener("click", () => {{
        chips.forEach(x => x.classList.remove("active"));
        c.classList.add("active");
        selectedCat = c.dataset.cat;
      }});
    }});

    async function loadData() {{
      try {{
        const res = await fetch("{summary_url}");
        const data = await res.json();
        if (!data.ok) return;

        document.getElementById("dateBadge").innerText = data.date_fmt;
        document.getElementById("remSum").innerText = data.daily_remaining_fmt;
        document.getElementById("limitSubtitle").innerText = `Потрачено ${{data.daily_spent_fmt}} из ${{data.daily_limit_fmt}} (${{data.spent_percent}}%)`;

        document.getElementById("poolVal").innerText = data.change_pool_fmt;
        document.getElementById("poolVal").style.color = data.change_pool >= 0 ? "var(--success)" : "var(--danger)";

        document.getElementById("piggyVal").innerText = data.piggy_balance_fmt;
        if (data.goal) {{
          document.getElementById("goalSub").innerText = `Цель: ${{data.goal.title}} (${{data.goal.percent}}%)`;
        }}

        // Анимация круга
        const circle = document.getElementById("circleProgress");
        const totalLen = 2 * Math.PI * 60; // 377
        const pct = Math.min(100, Math.max(0, data.spent_percent));
        const offset = totalLen - (pct / 100) * totalLen;
        circle.style.strokeDashoffset = offset;
        if (pct >= 100) {{
          circle.style.stroke = "var(--danger)";
        }} else if (pct >= 80) {{
          circle.style.stroke = "var(--warning)";
        }} else {{
          circle.style.stroke = "var(--success)";
        }}

        // Список трат
        const box = document.getElementById("expensesBox");
        if (!data.recent_expenses || data.recent_expenses.length === 0) {{
          box.innerHTML = `<div style="color: var(--hint); font-size: 13px; text-align: center; padding: 8px;">Сегодня пока нет трат</div>`;
        }} else {{
          box.innerHTML = data.recent_expenses.map(e => `
            <div class="exp-item">
              <div><b>${{e.amount_fmt}}</b> <span style="color: var(--hint);">• ${{e.category}}</span></div>
              <div class="exp-del" onclick="deleteExpense(${{e.id}})">✕</div>
            </div>
          `).join("");
        }}
      }} catch (err) {{
        console.error(err);
      }}
    }}

    async function deleteExpense(id) {{
      if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("medium");
      await fetch("{delete_url_prefix}" + id, {{ method: "POST" }});
      loadData();
    }}

    document.getElementById("btnSubmit").addEventListener("click", async () => {{
      const amtInput = document.getElementById("inpAmount");
      const noteInput = document.getElementById("inpNote");
      const amount = parseFloat(amtInput.value);
      if (!amount || amount <= 0) {{
        alert("Введите сумму расхода");
        return;
      }}

      let category = selectedCat;
      const note = noteInput.value.trim();
      if (note) {{
        category = note;
      }}

      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");

      document.getElementById("btnSubmit").disabled = true;
      document.getElementById("btnSubmit").innerText = "Записываю...";

      try {{
        await fetch("{expense_url}", {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ amount: amount, category: category }})
        }});
        amtInput.value = "";
        noteInput.value = "";
        await loadData();
      }} catch (e) {{
        alert("Ошибка записи");
      }} finally {{
        document.getElementById("btnSubmit").disabled = false;
        document.getElementById("btnSubmit").innerText = "Записать расход";
      }}
    }});

    loadData();
  </script>
</body>
</html>
"""
    return web.Response(text=html, content_type="text/html")


# --- Серверное приложение aiohttp ---


def create_widget_app() -> web.Application:
    """Создает и настраивает экземпляр aiohttp приложения."""
    app = web.Application()
    app.router.add_options("/{tail:.*}", handle_options)
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/v1/{token}/summary", handle_get_summary)
    app.router.add_post("/api/v1/{token}/expense", handle_post_expense)
    app.router.add_post("/api/v1/{token}/expense/{exp_id}", handle_delete_expense)
    app.router.add_get("/api/v1/{token}/scriptable", handle_get_scriptable)
    app.router.add_get("/api/v1/{token}/shortcuts", handle_get_shortcuts)
    app.router.add_get("/api/v1/{token}/kwgt", handle_get_kwgt)
    app.router.add_get("/webapp/{token}", handle_webapp)
    return app


async def start_widget_server(host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    """Запускает HTTP-сервер для виджетов в общем цикле событий asyncio."""
    global _RUNNER, _SITE
    if _RUNNER is not None:
        return _RUNNER

    app = create_widget_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=host, port=port)
    await site.start()
    _RUNNER = runner
    _SITE = site
    logger.info("HTTP сервер виджетов запущен на http://%s:%s", host, port)
    return runner


async def stop_widget_server() -> None:
    """Останавливает сервер виджетов при выключении бота."""
    global _RUNNER, _SITE
    if _RUNNER is not None:
        await _RUNNER.cleanup()
        _RUNNER = None
        _SITE = None
        logger.info("HTTP сервер виджетов остановлен")
