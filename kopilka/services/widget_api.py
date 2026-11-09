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
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import urllib.parse
from typing import Any, Callable

from aiohttp import web
from kopilka.database.connection import db
from kopilka.database.repository import get_setting, set_setting
from kopilka.services.categorizer import classify_expense as default_classify_expense
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


def _extract_token_from_input(raw: str) -> str:
    """Извлекает токен как из чистой строки токена, так и из полной ссылки (/webapp/<token>)."""
    s = (raw or "").strip()
    if not s:
        return ""
    m = re.search(r"/(?:webapp|api/v1)/([A-Za-z0-9_\-]+)", s)
    if m:
        return m.group(1)
    m_q = re.search(r"[?&]token=([A-Za-z0-9_\-]+)", s)
    if m_q:
        return m_q.group(1)
    return s.split()[0].strip("`'\"/ ")


def _verify_telegram_init_data(init_data: str) -> int | None:
    """Проверяет подпись Telegram WebApp initData и возвращает user_id."""
    if not init_data:
        return None
    bot_token = (os.environ.get("BOT_TOKEN") or "").strip()
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        user_json = parsed.get("user")
        if not user_json:
            return None
        user_obj = json.loads(user_json)
        uid = int(user_obj.get("id") or 0)
        if not uid:
            return None
        if not bot_token:
            return None
        recv_hash = parsed.pop("hash", "")
        if not recv_hash:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
        calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calc_hash, recv_hash):
            return uid
    except Exception:
        return None
    return None


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
        cols = [c[1] for c in conn.execute("PRAGMA table_info(expenses)")]
        col_str = "id, amount, category, comment, created_at" if "comment" in cols else "id, amount, category, '' AS comment, created_at"
        rows = conn.execute(
            f"SELECT {col_str} FROM expenses "
            "WHERE user_id = ? AND spent_date = ? ORDER BY id DESC LIMIT 5",
            (user_id, today.isoformat()),
        ).fetchall()
        recent_expenses = [
            {
                "id": r["id"],
                "amount": float(r["amount"]),
                "amount_fmt": fmt_money(float(r["amount"])),
                "category": r["category"],
                "comment": r["comment"] or "",
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
    comment: str | None = None,
) -> dict[str, Any]:
    """Добавляет трату из API (по сумме/категории, из текста вроде «450 кофе» или с комментарием товара)."""
    today = today_date()
    spent_date = today

    item_desc = (comment or "").strip()

    if text and not amount:
        parsed = parse_expense_message(text)
        if not parsed:
            raise ValueError("Не удалось распознать сумму и категорию в тексте")
        amount, cat_raw, spent_d = parsed
        spent_date = spent_d if spent_d <= today else today
        category = cat_raw
        if not item_desc:
            item_desc = cat_raw

    if not amount or amount <= 0:
        raise ValueError("Сумма расхода должна быть больше 0")

    cat_name = (category or "Разное").strip()
    classify_fn = _CLASSIFY_CAT or default_classify_expense
    if classify_fn:
        probe = item_desc or cat_name
        if probe and probe != "Разное":
            try:
                classified, _ = classify_fn(user_id, probe)
                if classified and classified != "Разное":
                    cat_name = classified
            except Exception:
                pass

    if _CANONICAL_CAT:
        cat_name = _CANONICAL_CAT(user_id, cat_name)

    clean_comm = item_desc if item_desc.lower() != cat_name.lower() else ""

    with db() as conn:
        cols = [c[1] for c in conn.execute("PRAGMA table_info(expenses)")]
        if "comment" in cols:
            exp_id = conn.execute(
                "INSERT INTO expenses (user_id, spent_date, amount, category, comment) VALUES (?, ?, ?, ?, ?)",
                (user_id, spent_date.isoformat(), float(amount), cat_name, clean_comm),
            ).lastrowid
        else:
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
        "comment": clean_comm,
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
    comment = None

    if request.content_type == "application/json":
        try:
            body = await request.json()
            amount = body.get("amount")
            category = body.get("category")
            text = body.get("text")
            comment = body.get("comment")
        except Exception:
            return web.json_response({"ok": False, "error": "Некорректный JSON"}, status=400, headers=_cors_headers())
    else:
        post_data = await request.post()
        amount = post_data.get("amount")
        category = post_data.get("category")
        text = post_data.get("text")
        comment = post_data.get("comment")

    try:
        amt_float = float(amount) if amount is not None else None
    except ValueError:
        return web.json_response({"ok": False, "error": "Сумма должна быть числом"}, status=400, headers=_cors_headers())

    try:
        res = add_widget_expense_sync(user_id, amount=amt_float, category=category, text=text, comment=comment)
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


async def handle_classify(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    user_id = get_user_id_by_token(token)
    if not user_id:
        return web.json_response({"ok": False, "error": "Неверный токен доступа"}, status=401, headers=_cors_headers())
    q = (request.query.get("q") or "").strip()
    if not q:
        return web.json_response({"ok": True, "category": "Разное", "keyword": ""}, headers=_cors_headers())
    cat = "Разное"
    kw = ""
    classify_fn = _CLASSIFY_CAT or default_classify_expense
    if classify_fn:
        try:
            c, k = classify_fn(user_id, q)
            if c:
                cat = c
                kw = k or ""
        except Exception:
            pass
    return web.json_response({"ok": True, "category": cat, "keyword": kw, "query": q}, headers=_cors_headers())


async def handle_post_receipt(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    user_id = get_user_id_by_token(token)
    if not user_id:
        return web.json_response({"ok": False, "error": "Неверный токен доступа"}, status=401, headers=_cors_headers())

    from kopilka.services.receipt_scanner import scan_receipt_image

    image_bytes = None
    save_immediately = request.query.get("save", "1") in ("1", "true", "yes")

    if request.content_type.startswith("multipart/"):
        reader = await request.multipart()
        field = await reader.next()
        while field:
            if field.name in ("file", "receipt", "photo", "image"):
                image_bytes = await field.read()
                break
            field = await reader.next()
    elif request.content_type == "application/json":
        import base64
        body = await request.json()
        b64 = body.get("image_base64") or body.get("photo") or ""
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        if b64:
            image_bytes = base64.b64decode(b64)
        if "save" in body:
            save_immediately = bool(body.get("save"))
    else:
        image_bytes = await request.read()

    if not image_bytes:
        return web.json_response({"ok": False, "error": "Файл изображения не передан"}, status=400, headers=_cors_headers())

    try:
        scan_res = await asyncio.to_thread(scan_receipt_image, image_bytes, user_id=user_id)
    except Exception as e:
        logger.exception("Ошибка при распознавании чека:")
        return web.json_response({"ok": False, "error": f"Ошибка распознавания: {e}"}, status=500, headers=_cors_headers())

    if not scan_res or not scan_res.get("success") or not scan_res.get("amount"):
        return web.json_response({
            "ok": False,
            "error": scan_res.get("error") if scan_res else "Не удалось распознать чек",
            "merchant": scan_res.get("merchant") if scan_res else None,
        }, status=422, headers=_cors_headers())

    amount = float(scan_res["amount"])
    category = scan_res.get("category") or "Продукты"
    merchant = scan_res.get("merchant") or ""
    items = scan_res.get("items") or []
    preview = ", ".join(items[:3]) if items else ""
    comment = f"{merchant}: {preview}" if (merchant and preview) else (merchant or preview or "")

    if save_immediately:
        exp_res = add_widget_expense_sync(user_id, amount=amount, category=category, comment=comment)
        return web.json_response({
            "ok": True,
            "saved": True,
            "expense": exp_res,
            "parsed": {
                "amount": amount,
                "category": category,
                "merchant": merchant,
                "comment": comment,
                "method": scan_res.get("method"),
            },
            "summary": exp_res.get("summary"),
        }, headers=_cors_headers())

    return web.json_response({
        "ok": True,
        "saved": False,
        "parsed": {
            "amount": amount,
            "category": category,
            "merchant": merchant,
            "comment": comment,
            "method": scan_res.get("method"),
        },
    }, headers=_cors_headers())


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


async def handle_auth_login(request: web.Request) -> web.Response:
    """Однократная авторизация по токену, персональной ссылке или Telegram initData.
    Устанавливает долгоживущую cookie (на 1 год) и возвращает токен для localStorage.
    """
    raw_token = ""
    init_data = ""
    try:
        if request.content_type == "application/json":
            body = await request.json()
            raw_token = body.get("token") or ""
            init_data = body.get("init_data") or ""
        else:
            post_data = await request.post()
            raw_token = post_data.get("token") or ""
            init_data = post_data.get("init_data") or ""
    except Exception:
        return web.json_response({"ok": False, "error": "Некорректный запрос"}, status=400, headers=_cors_headers())

    token = _extract_token_from_input(raw_token)
    user_id = get_user_id_by_token(token) if token else None

    if not user_id and init_data:
        tg_uid = _verify_telegram_init_data(init_data)
        if tg_uid:
            user_id = tg_uid
            token = get_or_create_widget_token(user_id)

    if not user_id or not token:
        return web.json_response(
            {"ok": False, "error": "Неверный токен доступа. Скопируйте токен или ссылку в боте (📱 Виджеты & Web App)."},
            status=401,
            headers=_cors_headers(),
        )

    resp = web.json_response(
        {"ok": True, "token": token, "user_id": user_id, "summary": get_widget_summary_data(user_id)},
        headers=_cors_headers(),
    )
    resp.set_cookie("kopilka_token", token, max_age=31536000, path="/", samesite="Lax")
    return resp


async def handle_icon_svg(request: web.Request) -> web.Response:
    """Векторная иконка приложения для рабочего стола ПК, iOS и Android (PWA)."""
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#1e293b"/>
    </linearGradient>
    <linearGradient id="ring" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#22c55e"/>
      <stop offset="100%" stop-color="#38bdf8"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="115" fill="url(#bg)"/>
  <circle cx="256" cy="256" r="168" fill="none" stroke="#334155" stroke-width="34"/>
  <circle cx="256" cy="256" r="168" fill="none" stroke="url(#ring)" stroke-width="34" stroke-linecap="round" stroke-dasharray="820 1055" transform="rotate(-90 256 256)"/>
  <text x="256" y="292" text-anchor="middle" fill="#f8fafc" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif" font-size="140" font-weight="800">₽</text>
</svg>"""
    return web.Response(
        text=svg,
        content_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_manifest(request: web.Request) -> web.Response:
    """Web App Manifest для установки приложения на рабочий стол ПК / экран «Домой» с сохранением сессии."""
    token = (
        request.match_info.get("token")
        or request.query.get("token")
        or request.cookies.get("kopilka_token")
        or ""
    ).strip()
    if token and not get_user_id_by_token(token):
        token = ""
    start_url = f"/webapp/{token}" if token else "/app"
    manifest = {
        "id": start_url,
        "name": "Копилка — Бюджет и траты",
        "short_name": "Копилка",
        "description": "Дневной бюджет, банк сдачи, копилка и быстрая запись расходов",
        "start_url": start_url,
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": "#0f172a",
        "theme_color": "#0f172a",
        "icons": [
            {
                "src": "/icon.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    }
    return web.json_response(manifest, content_type="application/manifest+json", headers=_cors_headers())


async def handle_service_worker(request: web.Request) -> web.Response:
    """Service Worker для поддержки установки PWA на рабочий стол и главный экран."""
    sw_js = """self.addEventListener('install', (event) => {
  self.skipWaiting();
});
self.addEventListener('activate', (event) => {
  event.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
"""
    return web.Response(
        text=sw_js,
        content_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


async def handle_webapp(request: web.Request) -> web.Response:
    """Рендерит полноценный интерактивный Telegram Mini App / PWA с однократной авторизацией."""
    path_token = (request.match_info.get("token") or "").strip()
    query_token = (request.query.get("token") or "").strip()
    cookie_token = (request.cookies.get("kopilka_token") or "").strip()

    # Если передан явный токен в URL /webapp/{token}, проверяем его
    if path_token:
        user_id = get_user_id_by_token(path_token)
        if not user_id:
            return web.Response(text="<h1>Неверный токен доступа</h1>", status=401, content_type="text/html")
        active_token = path_token
    elif query_token and get_user_id_by_token(query_token):
        active_token = query_token
    elif cookie_token and get_user_id_by_token(cookie_token):
        active_token = cookie_token
    else:
        active_token = ""

    base_url = _get_base_url(request)
    manifest_href = f"/webapp/{active_token}/manifest.json" if active_token else "/manifest.json"

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
  <meta name="theme-color" content="#0f172a">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Копилка">
  <meta name="mobile-web-app-capable" content="yes">
  <link rel="manifest" id="manifestLink" href="{manifest_href}">
  <link rel="icon" type="image/svg+xml" href="/icon.svg">
  <link rel="apple-touch-icon" href="/icon.svg">
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
    * {{
      box-sizing: border-box;
      -webkit-tap-highlight-color: transparent;
    }}
    html, body {{
      margin: 0;
      padding: 0;
      width: 100%;
      max-width: 100%;
      overflow-x: hidden;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      user-select: none;
      -webkit-font-smoothing: antialiased;
    }}
    ::-webkit-scrollbar {{
      width: 5px;
      height: 5px;
    }}
    ::-webkit-scrollbar-track {{
      background: transparent;
    }}
    ::-webkit-scrollbar-thumb {{
      background: rgba(255, 255, 255, 0.16);
      border-radius: 999px;
    }}
    .app-container {{
      width: 100%;
      max-width: 440px;
      margin: 0 auto;
      padding: 14px 14px 28px;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      margin-bottom: 14px;
    }}
    .title {{
      font-size: 20px;
      font-weight: 800;
      display: flex;
      align-items: center;
      gap: 8px;
      letter-spacing: -0.3px;
      min-width: 0;
    }}
    .header-right {{
      display: flex;
      align-items: center;
      gap: 6px;
      flex-shrink: 0;
    }}
    .btn-install-top {{
      background: rgba(56, 189, 248, 0.14);
      color: var(--accent);
      border: 1px solid rgba(56, 189, 248, 0.3);
      padding: 5px 10px;
      border-radius: 10px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      white-space: nowrap;
      transition: 0.2s;
    }}
    .btn-install-top:active {{
      transform: scale(0.97);
    }}
    .date-badge {{
      font-size: 12px;
      color: var(--hint);
      background: var(--card-bg);
      padding: 5px 10px;
      border-radius: 10px;
      white-space: nowrap;
    }}

    .hero-card {{
      background: var(--card-bg);
      border-radius: 20px;
      padding: 18px 16px;
      text-align: center;
      margin-bottom: 12px;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.15);
      position: relative;
      overflow: hidden;
    }}
    .progress-circle {{
      position: relative;
      width: 152px;
      height: 152px;
      margin: 0 auto 10px;
    }}
    .progress-circle svg {{
      transform: rotate(-90deg);
      width: 152px;
      height: 152px;
      display: block;
    }}
    .progress-circle circle {{
      fill: none;
      stroke-width: 10;
      stroke-linecap: round;
    }}
    .circle-bg {{
      stroke: rgba(255, 255, 255, 0.08);
    }}
    .circle-val {{
      stroke: var(--success);
      stroke-dasharray: 402;
      stroke-dashoffset: 402;
      transition: stroke-dashoffset 0.6s ease, stroke 0.3s;
    }}
    .circle-text {{
      position: absolute;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      text-align: center;
      width: 122px;
    }}
    .circle-sum {{
      font-size: 22px;
      font-weight: 800;
      line-height: 1.15;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .circle-sub {{
      font-size: 11px;
      color: var(--hint);
      margin-top: 3px;
      white-space: nowrap;
    }}

    .grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
      gap: 10px;
      margin-bottom: 12px;
    }}
    .card {{
      background: var(--card-bg);
      border-radius: 16px;
      padding: 13px 14px;
      display: flex;
      flex-direction: column;
      min-width: 0;
    }}
    .card-label {{
      font-size: 11px;
      font-weight: 600;
      color: var(--hint);
      margin-bottom: 4px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .card-value {{
      font-size: 17px;
      font-weight: 800;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .card-sub {{
      font-size: 11px;
      color: var(--hint);
      margin-top: 3px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .add-box {{
      background: var(--card-bg);
      border-radius: 18px;
      padding: 15px;
      margin-bottom: 12px;
    }}
    .box-title {{
      font-size: 14px;
      font-weight: 700;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 6px;
    }}
    .smart-input-wrap {{
      margin-bottom: 10px;
      width: 100%;
      position: relative;
    }}
    .smart-input, .login-input {{
      width: 100%;
      min-width: 0;
      background: rgba(0, 0, 0, 0.28);
      border: 1px solid rgba(255, 255, 255, 0.14);
      border-radius: 14px;
      padding: 13px 14px;
      color: var(--text);
      font-size: 16px;
      outline: none;
      transition: border-color 0.2s, box-shadow 0.2s;
      -webkit-appearance: none;
    }}
    .smart-input:focus, .login-input:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.15);
    }}
    .btn-receipt-upload {{
      background: rgba(56, 189, 248, 0.16);
      color: var(--accent);
      border: 1px solid rgba(56, 189, 248, 0.35);
      padding: 5px 11px;
      border-radius: 10px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      transition: 0.18s;
      user-select: none;
    }}
    .btn-receipt-upload:active {{
      transform: scale(0.96);
    }}
    .smart-preview {{
      margin-top: 6px;
      font-size: 12px;
      display: flex;
      align-items: center;
      gap: 6px;
      min-height: 20px;
    }}
    .preview-badge {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      background: rgba(56, 189, 248, 0.12);
      color: var(--accent);
      padding: 3px 8px;
      border-radius: 8px;
      font-weight: 600;
    }}
    .receipt-loading {{
      background: rgba(56, 189, 248, 0.1);
      border: 1px dashed rgba(56, 189, 248, 0.4);
      color: var(--accent);
      border-radius: 12px;
      padding: 10px 12px;
      font-size: 13px;
      font-weight: 600;
      text-align: center;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }}
    .spinner {{
      width: 14px;
      height: 14px;
      border: 2px solid rgba(56, 189, 248, 0.3);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      display: inline-block;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    .exp-comm {{
      font-weight: 600;
      color: var(--text);
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 12px;
    }}
    .chip {{
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid rgba(255, 255, 255, 0.06);
      border-radius: 10px;
      padding: 6px 10px;
      font-size: 12px;
      cursor: pointer;
      transition: 0.18s;
      white-space: nowrap;
    }}
    .chip.active {{
      background: var(--accent);
      color: var(--btn-text);
      border-color: transparent;
      font-weight: 600;
    }}
    .btn-submit {{
      width: 100%;
      background: var(--accent);
      color: var(--btn-text);
      border: none;
      border-radius: 12px;
      padding: 12px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      transition: opacity 0.2s, transform 0.1s;
    }}
    .btn-submit:active {{
      opacity: 0.85;
      transform: scale(0.99);
    }}
    .btn-secondary {{
      width: 100%;
      background: rgba(255, 255, 255, 0.08);
      color: var(--text);
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 12px;
      padding: 11px 12px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      margin-top: 8px;
      text-align: center;
      display: block;
      text-decoration: none;
    }}

    .expenses-list {{
      background: var(--card-bg);
      border-radius: 18px;
      padding: 15px;
    }}
    .exp-item {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 8px;
      padding: 8px 0;
      border-bottom: 1px solid rgba(255, 255, 255, 0.06);
      font-size: 14px;
    }}
    .exp-item:last-child {{
      border-bottom: none;
    }}
    .exp-info {{
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .exp-del {{
      color: var(--danger);
      cursor: pointer;
      padding: 4px 8px;
      opacity: 0.8;
      font-weight: bold;
      flex-shrink: 0;
    }}

    /* Экран однократного входа (если открыто на новом устройстве без токена) */
    .auth-card {{
      background: var(--card-bg);
      border-radius: 20px;
      padding: 22px 18px;
      margin-top: 16px;
      box-shadow: 0 8px 28px rgba(0, 0, 0, 0.25);
    }}
    .auth-title {{
      font-size: 18px;
      font-weight: 800;
      margin-bottom: 8px;
    }}
    .auth-desc {{
      font-size: 13px;
      color: var(--hint);
      line-height: 1.45;
      margin-bottom: 14px;
    }}
    .auth-err {{
      color: var(--danger);
      font-size: 12px;
      margin-top: 8px;
      display: none;
    }}

    /* Модальное окно установки на рабочий стол / экран Домой */
    .modal-backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.65);
      backdrop-filter: blur(4px);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 16px;
      z-index: 100;
    }}
    .modal-backdrop.open {{
      display: flex;
    }}
    .modal-card {{
      width: 100%;
      max-width: 390px;
      background: var(--card-bg);
      border: 1px solid rgba(255, 255, 255, 0.1);
      border-radius: 20px;
      padding: 18px;
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.45);
    }}
    .modal-title {{
      font-size: 17px;
      font-weight: 800;
      margin-bottom: 8px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .modal-close {{
      cursor: pointer;
      color: var(--hint);
      font-size: 18px;
      padding: 2px 6px;
    }}
    .modal-step {{
      font-size: 13px;
      color: var(--text);
      line-height: 1.45;
      background: rgba(0, 0, 0, 0.22);
      border-radius: 12px;
      padding: 10px 12px;
      margin-bottom: 8px;
    }}
  </style>
</head>
<body>
  <div class="app-container">
    <div class="header">
      <div class="title">Копилка</div>
      <div class="header-right">
        <button class="btn-install-top" id="btnInstallTop" title="Добавить на рабочий стол / главный экран">📲 На экран</button>
        <div class="date-badge" id="dateBadge">...</div>
      </div>
    </div>

    <!-- Блок однократной авторизации (показывается только если токен ещё не сохранён на устройстве) -->
    <div id="authView" style="display: none;">
      <div class="auth-card">
        <div class="auth-title">🔐 Вход в Копилку (1 раз)</div>
        <div class="auth-desc">
          Введите ваш токен доступа или персональную ссылку из бота (раздел <b>«📱 Виджеты &amp; Web App»</b>).<br><br>
          Устройство запомнит вас автоматически — при запуске с рабочего стола вводить ничего повторно не придётся.
        </div>
        <input type="text" id="inpAuthToken" class="login-input" placeholder="Вставьте токен или ссылку..." autocomplete="off">
        <div class="auth-err" id="authError"></div>
        <button class="btn-submit" id="btnLogin" style="margin-top: 12px;">Войти и запомнить устройство</button>
      </div>
    </div>

    <!-- Основной дашборд -->
    <div id="dashboardView" style="display: none;">
      <div class="hero-card">
        <div class="progress-circle">
          <svg viewBox="0 0 152 152">
            <circle class="circle-bg" cx="76" cy="76" r="64"></circle>
            <circle class="circle-val" id="circleProgress" cx="76" cy="76" r="64"></circle>
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
          <div class="card-sub" id="poolSub">накоплено экономией</div>
        </div>
        <div class="card">
          <div class="card-label">🏦 КОПИЛКА</div>
          <div class="card-value" id="piggyVal">...</div>
          <div class="card-sub" id="goalSub">баланс копилки</div>
        </div>
      </div>

      <div class="add-box">
        <div class="box-title">
          <span>⚡️ Быстрая запись расхода</span>
          <label for="inpReceipt" class="btn-receipt-upload" id="lblReceipt" title="Загрузить фото чека">
            📷 Чек
            <input type="file" id="inpReceipt" accept="image/*" capture="environment" style="display: none;">
          </label>
        </div>
        <div class="smart-input-wrap">
          <input type="text" id="inpSmart" class="smart-input" placeholder="Например: 450 кофе или 280 помело" autocomplete="off">
          <div class="smart-preview" id="smartPreview" style="display: none;">
            <span class="preview-badge" id="previewBadge">✨ 450 ₽ • Кофе · Кафе</span>
          </div>
        </div>
        <div class="receipt-loading" id="receiptLoading" style="display: none;">
          <span class="spinner"></span> Распознавание чека и QR-кода...
        </div>
        <div class="chips" id="chipsBox">
          <div class="chip active" data-cat="Продукты">🛒 Продукты</div>
          <div class="chip" data-cat="Кафе и рестораны">☕️ Кафе</div>
          <div class="chip" data-cat="Транспорт">🚕 Транспорт</div>
          <div class="chip" data-cat="Дом">🏠 Дом</div>
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
    </div>
  </div>

  <!-- Модальное окно установки на рабочий стол / главный экран -->
  <div class="modal-backdrop" id="installModal">
    <div class="modal-card">
      <div class="modal-title">
        <span>📲 Добавить на рабочий стол</span>
        <span class="modal-close" id="btnCloseModal">✕</span>
      </div>
      <div style="font-size: 12px; color: var(--success); margin-bottom: 10px; font-weight: 600;">
        ✅ Авторизация уже вшита — входить заново не нужно!
      </div>

      <button class="btn-submit" id="btnNativePwaInstall" style="display: none; margin-bottom: 10px;">
        💻 Установить приложение в 1 клик
      </button>

      <button class="btn-submit" id="btnTgHomeScreen" style="display: none; margin-bottom: 10px;">
        📱 Добавить ярлык на экран (Telegram)
      </button>

      <button class="btn-secondary" id="btnOpenInBrowser" style="display: none; margin-top: 0; margin-bottom: 10px;">
        🌐 Открыть в браузере (для установки на ПК / телефон)
      </button>

      <div class="modal-step">
        <b>💻 На компьютере (Chrome / Edge / Яндекс):</b><br>
        Нажмите значок <b>«Установить Копилка»</b> (⊕ справа в адресной строке браузера) или меню <b>⋮ → «Сохранить и поделиться» → «Установить как приложение»</b>.
      </div>
      <div class="modal-step">
        <b>🍏 На iPhone (Safari):</b><br>
        Нажмите кнопку <b>«Поделиться»</b> (квадрат со стрелкой вверх) → <b>«На экран "Домой"»</b>.
      </div>
      <div class="modal-step">
        <b>🤖 На Android (Chrome):</b><br>
        Нажмите меню <b>⋮ → «Добавить на главный экран»</b> (или «Установить приложение»).
      </div>

      <button class="btn-secondary" id="btnCopyDirectLink">📋 Скопировать личную ссылку со входом</button>
    </div>
  </div>

  <script>
    const BASE_URL = "{base_url}";
    const SERVER_TOKEN = "{active_token}";
    const tg = window.Telegram ? window.Telegram.WebApp : null;
    if (tg) {{
      tg.ready();
      tg.expand();
    }}

    if ("serviceWorker" in navigator) {{
      navigator.serviceWorker.register("/sw.js").catch(() => {{}});
    }}

    let currentToken = SERVER_TOKEN || localStorage.getItem("kopilka_token") || "";
    let deferredInstallPrompt = null;

    window.addEventListener("beforeinstallprompt", (e) => {{
      e.preventDefault();
      deferredInstallPrompt = e;
      const btnNative = document.getElementById("btnNativePwaInstall");
      if (btnNative) btnNative.style.display = "block";
    }});

    function saveTokenPersistently(tok) {{
      if (!tok) return;
      currentToken = tok;
      try {{
        localStorage.setItem("kopilka_token", tok);
      }} catch (e) {{}}
      document.cookie = `kopilka_token=${{encodeURIComponent(tok)}}; path=/; max-age=31536000; SameSite=Lax`;
      const mf = document.getElementById("manifestLink");
      if (mf) mf.href = `/webapp/${{encodeURIComponent(tok)}}/manifest.json`;
    }}

    if (SERVER_TOKEN) {{
      saveTokenPersistently(SERVER_TOKEN);
    }}

    let selectedCat = "Продукты";
    let manualCategoryOverride = false;
    let parsedAmount = null;
    let parsedItem = "";
    let classifyTimeout = null;

    const chips = document.querySelectorAll(".chip");
    function selectCategoryChip(catName) {{
      if (!catName) return;
      chips.forEach(c => {{
        if (c.dataset.cat.toLowerCase() === catName.toLowerCase()) {{
          chips.forEach(x => x.classList.remove("active"));
          c.classList.add("active");
          selectedCat = c.dataset.cat;
        }}
      }});
    }}

    chips.forEach(c => {{
      c.addEventListener("click", () => {{
        manualCategoryOverride = true;
        chips.forEach(x => x.classList.remove("active"));
        c.classList.add("active");
        selectedCat = c.dataset.cat;
        if (parsedAmount || parsedItem) {{
          updateSmartPreview();
        }}
      }});
    }});

    function renderSummary(data) {{
      document.getElementById("authView").style.display = "none";
      document.getElementById("dashboardView").style.display = "block";

      document.getElementById("dateBadge").innerText = data.date_fmt;
      document.getElementById("remSum").innerText = data.daily_remaining_fmt;
      document.getElementById("limitSubtitle").innerText = `Потрачено ${{data.daily_spent_fmt}} из ${{data.daily_limit_fmt}} (${{data.spent_percent}}%)`;

      const poolEl = document.getElementById("poolVal");
      const poolSubEl = document.getElementById("poolSub");
      poolEl.innerText = data.change_pool_fmt;
      if (data.change_pool >= 0) {{
        poolEl.style.color = "var(--success)";
        poolSubEl.innerText = "накоплено экономией";
      }} else {{
        poolEl.style.color = "var(--danger)";
        poolSubEl.innerText = "перерасход за месяц";
      }}

      document.getElementById("piggyVal").innerText = data.piggy_balance_fmt;
      if (data.goal) {{
        document.getElementById("goalSub").innerText = `Цель: ${{data.goal.title}} (${{data.goal.percent}}%)`;
      }} else {{
        document.getElementById("goalSub").innerText = "баланс копилки";
      }}

      // Анимация круга (r=64 -> 2*pi*64 = 402.12)
      const circle = document.getElementById("circleProgress");
      const totalLen = 2 * Math.PI * 64;
      const pct = Math.min(100, Math.max(0, data.spent_percent));
      const offset = totalLen - (pct / 100) * totalLen;
      circle.style.strokeDashoffset = offset;
      if (pct >= 100 || data.daily_spent > data.daily_limit) {{
        circle.style.stroke = "var(--danger)";
      }} else if (pct >= 80) {{
        circle.style.stroke = "var(--warning)";
      }} else {{
        circle.style.stroke = "var(--success)";
      }}

      const box = document.getElementById("expensesBox");
      if (!data.recent_expenses || data.recent_expenses.length === 0) {{
        box.innerHTML = `<div style="color: var(--hint); font-size: 13px; text-align: center; padding: 8px;">Сегодня пока нет трат</div>`;
      }} else {{
        box.innerHTML = data.recent_expenses.map(e => {{
          const comm = (e.comment || "").trim();
          const itemDesc = (comm && comm.toLowerCase() !== (e.category || "").toLowerCase())
            ? `<span class="exp-comm">${{comm}}</span> <span style="color: var(--hint);">· ${{e.category}}</span>`
            : `<span style="color: var(--hint);">${{e.category}}</span>`;
          return `
            <div class="exp-item">
              <div class="exp-info"><b>${{e.amount_fmt}}</b> • ${{itemDesc}}</div>
              <div class="exp-del" onclick="deleteExpense(${{e.id}})">✕</div>
            </div>
          `;
        }}).join("");
      }}
    }}

    async function loadData() {{
      if (!currentToken) {{
        // Попробуем бесшовный вход через Telegram initData, если открыто внутри Telegram
        if (tg && tg.initData) {{
          try {{
            const authRes = await fetch(`${{BASE_URL}}/api/v1/auth/login`, {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ init_data: tg.initData }})
            }});
            const authData = await authRes.json();
            if (authData.ok && authData.token) {{
              saveTokenPersistently(authData.token);
              if (authData.summary) {{
                renderSummary(authData.summary);
                return;
              }}
            }}
          }} catch (e) {{}}
        }}
        document.getElementById("dashboardView").style.display = "none";
        document.getElementById("authView").style.display = "block";
        document.getElementById("dateBadge").innerText = "Вход";
        return;
      }}

      try {{
        const res = await fetch(`${{BASE_URL}}/api/v1/${{encodeURIComponent(currentToken)}}/summary`);
        if (res.status === 401) {{
          localStorage.removeItem("kopilka_token");
          currentToken = "";
          document.getElementById("dashboardView").style.display = "none";
          document.getElementById("authView").style.display = "block";
          return;
        }}
        const data = await res.json();
        if (!data.ok) return;
        saveTokenPersistently(currentToken);
        renderSummary(data);
      }} catch (err) {{
        console.error(err);
      }}
    }}

    document.getElementById("btnLogin").addEventListener("click", async () => {{
      const val = document.getElementById("inpAuthToken").value.trim();
      const errEl = document.getElementById("authError");
      errEl.style.display = "none";
      if (!val) {{
        errEl.innerText = "Вставьте токен или ссылку из бота";
        errEl.style.display = "block";
        return;
      }}
      try {{
        const res = await fetch(`${{BASE_URL}}/api/v1/auth/login`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ token: val }})
        }});
        const data = await res.json();
        if (!data.ok) {{
          errEl.innerText = data.error || "Неверный токен";
          errEl.style.display = "block";
          return;
        }}
        saveTokenPersistently(data.token);
        if (window.history && window.history.replaceState) {{
          window.history.replaceState({{}}, "", `/webapp/${{encodeURIComponent(data.token)}}`);
        }}
        if (data.summary) {{
          renderSummary(data.summary);
        }} else {{
          await loadData();
        }}
      }} catch (e) {{
        errEl.innerText = "Ошибка соединения с сервером";
        errEl.style.display = "block";
      }}
    }});

    async function deleteExpense(id) {{
      if (!currentToken) return;
      if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("medium");
      await fetch(`${{BASE_URL}}/api/v1/${{encodeURIComponent(currentToken)}}/expense/` + id, {{ method: "POST" }});
      loadData();
    }}

    function parseSmartInput(val) {{
      const raw = (val || "").trim();
      if (!raw) return {{ amount: null, item: "" }};
      let amt = null;
      let item = "";
      const mStart = raw.match(/^(\\d+(?:[\\s_]\\d+)*(?:[.,]\\d+)?)\\s*(.*)$/);
      if (mStart) {{
        amt = parseFloat(mStart[1].replace(/[\\s_]/g, "").replace(",", "."));
        item = mStart[2].trim();
      }} else {{
        const mEnd = raw.match(/^(.*?)\\s*(\\d+(?:[\\s_]\\d+)*(?:[.,]\\d+)?)$/);
        if (mEnd) {{
          amt = parseFloat(mEnd[2].replace(/[\\s_]/g, "").replace(",", "."));
          item = mEnd[1].trim();
        }}
      }}
      return {{ amount: amt, item: item }};
    }}

    function updateSmartPreview() {{
      const smartPreview = document.getElementById("smartPreview");
      const previewBadge = document.getElementById("previewBadge");
      if (!parsedAmount && !parsedItem) {{
        smartPreview.style.display = "none";
        return;
      }}
      smartPreview.style.display = "flex";
      const amtStr = parsedAmount ? `${{Math.round(parsedAmount * 100) / 100}} ₽` : "… ₽";
      const itemStr = parsedItem ? ` • ${{parsedItem}}` : "";
      previewBadge.innerHTML = `✨ <b>${{amtStr}}</b>${{itemStr}} <span style="color: var(--hint);">· ${{selectedCat}}</span>`;
    }}

    const inpSmart = document.getElementById("inpSmart");
    inpSmart.addEventListener("input", () => {{
      const parsed = parseSmartInput(inpSmart.value);
      parsedAmount = parsed.amount;
      parsedItem = parsed.item;
      updateSmartPreview();

      if (parsedItem && !manualCategoryOverride) {{
        clearTimeout(classifyTimeout);
        classifyTimeout = setTimeout(async () => {{
          try {{
            const res = await fetch(`${{BASE_URL}}/api/v1/${{encodeURIComponent(currentToken)}}/classify?q=` + encodeURIComponent(parsedItem));
            const data = await res.json();
            if (data.ok && data.category && data.category !== "Разное" && !manualCategoryOverride) {{
              selectCategoryChip(data.category);
              updateSmartPreview();
            }}
          }} catch (e) {{}}
        }}, 220);
      }}
    }});

    async function submitExpense() {{
      if (!currentToken) return;
      const parsed = parseSmartInput(inpSmart.value);
      let amount = parsed.amount;
      let comment = parsed.item;
      let category = selectedCat;

      if (!amount || amount <= 0) {{
        alert("Введите сумму расхода, например: 450 кофе или 280 помело");
        inpSmart.focus();
        return;
      }}

      if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");

      const btn = document.getElementById("btnSubmit");
      btn.disabled = true;
      btn.innerText = "Записываю...";

      try {{
        await fetch(`${{BASE_URL}}/api/v1/${{encodeURIComponent(currentToken)}}/expense`, {{
          method: "POST",
          headers: {{ "Content-Type": "application/json" }},
          body: JSON.stringify({{ amount: amount, category: category, comment: comment }})
        }});
        inpSmart.value = "";
        parsedAmount = null;
        parsedItem = "";
        manualCategoryOverride = false;
        document.getElementById("smartPreview").style.display = "none";
        await loadData();
      }} catch (e) {{
        alert("Ошибка записи");
      }} finally {{
        btn.disabled = false;
        btn.innerText = "Записать расход";
      }}
    }}

    document.getElementById("btnSubmit").addEventListener("click", submitExpense);
    inpSmart.addEventListener("keydown", (e) => {{
      if (e.key === "Enter") submitExpense();
    }});

    // Загрузка фото чека с распознаванием
    const inpReceipt = document.getElementById("inpReceipt");
    const receiptLoading = document.getElementById("receiptLoading");

    inpReceipt.addEventListener("change", async (e) => {{
      const file = e.target.files && e.target.files[0];
      if (!file) return;
      if (!currentToken) return;

      receiptLoading.style.display = "flex";
      if (tg && tg.HapticFeedback) tg.HapticFeedback.impactOccurred("medium");

      const formData = new FormData();
      formData.append("file", file);

      try {{
        const res = await fetch(`${{BASE_URL}}/api/v1/${{encodeURIComponent(currentToken)}}/receipt?save=1`, {{
          method: "POST",
          body: formData
        }});
        const data = await res.json();
        if (!data.ok) {{
          alert(data.error || "Не удалось распознать чек");
          return;
        }}
        if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
        const p = data.parsed || {{}};
        const smartPreview = document.getElementById("smartPreview");
        const previewBadge = document.getElementById("previewBadge");
        smartPreview.style.display = "flex";
        previewBadge.innerHTML = `✅ Чек: <b>${{p.amount}} ₽</b> · ${{p.comment || p.category}}`;
        setTimeout(() => {{ smartPreview.style.display = "none"; }}, 4000);
        if (data.summary) {{
          renderSummary(data.summary);
        }} else {{
          await loadData();
        }}
      }} catch (err) {{
        alert("Ошибка отправки фото чека");
      }} finally {{
        receiptLoading.style.display = "none";
        inpReceipt.value = "";
      }}
    }});

    // Модальное окно установки на рабочий стол / главный экран
    const installModal = document.getElementById("installModal");
    const btnInstallTop = document.getElementById("btnInstallTop");
    const btnCloseModal = document.getElementById("btnCloseModal");
    const btnNativePwaInstall = document.getElementById("btnNativePwaInstall");
    const btnTgHomeScreen = document.getElementById("btnTgHomeScreen");
    const btnOpenInBrowser = document.getElementById("btnOpenInBrowser");
    const btnCopyDirectLink = document.getElementById("btnCopyDirectLink");

    if (tg && typeof tg.addToHomeScreen === "function") {{
      btnTgHomeScreen.style.display = "block";
      btnTgHomeScreen.addEventListener("click", () => {{
        try {{ tg.addToHomeScreen(); }} catch (e) {{}}
      }});
    }}

    if (tg && tg.initData) {{
      btnOpenInBrowser.style.display = "block";
      btnOpenInBrowser.addEventListener("click", () => {{
        const targetUrl = currentToken
          ? `${{BASE_URL}}/webapp/${{encodeURIComponent(currentToken)}}?install=1`
          : `${{BASE_URL}}/app?install=1`;
        if (typeof tg.openLink === "function") {{
          tg.openLink(targetUrl);
        }} else {{
          window.open(targetUrl, "_blank");
        }}
      }});
    }}

    btnNativePwaInstall.addEventListener("click", async () => {{
      if (!deferredInstallPrompt) return;
      deferredInstallPrompt.prompt();
      await deferredInstallPrompt.userChoice;
      deferredInstallPrompt = null;
      btnNativePwaInstall.style.display = "none";
      installModal.classList.remove("open");
    }});

    btnInstallTop.addEventListener("click", async () => {{
      if (deferredInstallPrompt) {{
        deferredInstallPrompt.prompt();
        await deferredInstallPrompt.userChoice;
        deferredInstallPrompt = null;
        btnNativePwaInstall.style.display = "none";
        return;
      }}
      installModal.classList.add("open");
    }});

    btnCloseModal.addEventListener("click", () => {{
      installModal.classList.remove("open");
    }});
    installModal.addEventListener("click", (e) => {{
      if (e.target === installModal) installModal.classList.remove("open");
    }});

    btnCopyDirectLink.addEventListener("click", async () => {{
      const link = currentToken
        ? `${{BASE_URL}}/webapp/${{encodeURIComponent(currentToken)}}`
        : `${{BASE_URL}}/app`;
      try {{
        await navigator.clipboard.writeText(link);
        btnCopyDirectLink.innerText = "✅ Ссылка скопирована!";
        setTimeout(() => {{ btnCopyDirectLink.innerText = "📋 Скопировать личную ссылку со входом"; }}, 2000);
      }} catch (e) {{
        prompt("Скопируйте вашу персональную ссылку для быстрого входа:", link);
      }}
    }});

    if (new URLSearchParams(window.location.search).get("install") === "1") {{
      installModal.classList.add("open");
    }}

    loadData();
  </script>
</body>
</html>
"""
    resp = web.Response(text=html, content_type="text/html")
    if active_token:
        resp.set_cookie("kopilka_token", active_token, max_age=31536000, path="/", samesite="Lax")
    return resp


# --- Серверное приложение aiohttp ---


def create_widget_app() -> web.Application:
    """Создает и настраивает экземпляр aiohttp приложения."""
    app = web.Application()
    app.router.add_options("/{tail:.*}", handle_options)
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/icon.svg", handle_icon_svg)
    app.router.add_get("/sw.js", handle_service_worker)
    app.router.add_get("/manifest.json", handle_manifest)
    app.router.add_post("/api/v1/auth/login", handle_auth_login)
    app.router.add_get("/api/v1/{token}/summary", handle_get_summary)
    app.router.add_get("/api/v1/{token}/classify", handle_classify)
    app.router.add_post("/api/v1/{token}/expense", handle_post_expense)
    app.router.add_post("/api/v1/{token}/expense/{exp_id}", handle_delete_expense)
    app.router.add_post("/api/v1/{token}/receipt", handle_post_receipt)
    app.router.add_get("/api/v1/{token}/scriptable", handle_get_scriptable)
    app.router.add_get("/api/v1/{token}/shortcuts", handle_get_shortcuts)
    app.router.add_get("/api/v1/{token}/kwgt", handle_get_kwgt)
    app.router.add_get("/app", handle_webapp)
    app.router.add_get("/webapp", handle_webapp)
    app.router.add_get("/webapp/{token}", handle_webapp)
    app.router.add_get("/webapp/{token}/manifest.json", handle_manifest)
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
