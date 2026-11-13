import asyncio
import pytest
import bot
from aiohttp import web
from kopilka.database.connection import db
from kopilka.services.widget_api import (
    create_widget_app,
    get_or_create_widget_token,
    rotate_widget_token,
    get_user_id_by_token,
    get_widget_summary_data,
    add_widget_expense_sync,
    configure_api,
)

@pytest.fixture
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(bot, "DATA_DIR", str(data))
    monkeypatch.setattr(bot, "DB_PATH", str(data / "budget.db"))
    import kopilka.config as kconfig
    monkeypatch.setattr(kconfig, "DB_PATH", str(data / "budget.db"))
    bot.init_db()
    return data

def test_widget_token_management(env):
    uid = 42
    tok1 = get_or_create_widget_token(uid)
    assert tok1 and len(tok1) >= 16
    assert get_or_create_widget_token(uid) == tok1
    assert get_user_id_by_token(tok1) == uid

    tok2 = rotate_widget_token(uid)
    assert tok2 != tok1
    assert get_user_id_by_token(tok1) is None
    assert get_user_id_by_token(tok2) == uid

def test_add_widget_expense_and_summary(env):
    uid = 42
    tok = get_or_create_widget_token(uid)

    configure_api(
        budget_info_fn=lambda u, d: {"daily_limit": 2000.0, "piggy_total": 5000.0},
        change_pool_fn=lambda u, d: 300.0,
        day_spent_fn=None, # use db
        bot_username="kopilka_test_bot",
    )

    s1 = get_widget_summary_data(uid)
    assert s1["daily_limit"] == 2000.0
    assert s1["daily_spent"] == 0.0
    assert s1["daily_remaining"] == 2000.0

    res = add_widget_expense_sync(uid, amount=450, category="Продукты")
    assert res["ok"] is True
    assert res["amount"] == 450.0

    s2 = get_widget_summary_data(uid)
    assert s2["daily_spent"] == 450.0
    assert s2["daily_remaining"] == 1550.0

    # Add via text
    res_text = add_widget_expense_sync(uid, text="350 кофе")
    assert res_text["ok"] is True
    assert res_text["amount"] == 350.0
    assert res_text["category"] == "Кафе и рестораны"
    assert res_text["comment"] == "Кофе"

    s3 = get_widget_summary_data(uid)
    assert s3["daily_spent"] == 800.0
    assert s3["daily_remaining"] == 1200.0
    assert s3["recent_expenses"][0]["comment"] == "Кофе"

def test_widget_http_endpoints(env):
    async def _run():
        uid = 42
        tok = get_or_create_widget_token(uid)

        app = create_widget_app()
        from aiohttp.test_utils import TestServer, TestClient
        client = TestClient(TestServer(app))
        await client.start_server()

        try:
            # 1. Health
            resp = await client.get("/health")
            assert resp.status == 200
            j = await resp.json()
            assert j["ok"] is True

            # 2. Summary
            resp = await client.get(f"/api/v1/{tok}/summary")
            assert resp.status == 200
            summary = await resp.json()
            assert summary["ok"] is True
            assert "daily_remaining" in summary

            # 3. Add expense via POST JSON
            resp = await client.post(
                f"/api/v1/{tok}/expense",
                json={"amount": 500, "category": "Кафе"},
            )
            assert resp.status == 200
            add_res = await resp.json()
            assert add_res["ok"] is True
            assert add_res["amount"] == 500.0
            exp_id = add_res["expense_id"]

            # 4. Delete expense
            del_resp = await client.post(f"/api/v1/{tok}/expense/{exp_id}")
            assert del_resp.status == 200
            del_j = await del_resp.json()
            assert del_j["ok"] is True

            # 5. Scriptable js
            js_resp = await client.get(f"/api/v1/{tok}/scriptable")
            assert js_resp.status == 200
            js_text = await js_resp.text()
            assert "Scriptable" in js_text
            assert tok in js_text

            # 6. Shortcuts HTML
            sc_resp = await client.get(f"/api/v1/{tok}/shortcuts")
            assert sc_resp.status == 200
            sc_html = await sc_resp.text()
            assert "Быстрые команды" in sc_html

            # 7. KWGT HTML
            kwgt_resp = await client.get(f"/api/v1/{tok}/kwgt")
            assert kwgt_resp.status == 200

            # 8. Web App HTML
            wa_resp = await client.get(f"/webapp/{tok}")
            assert wa_resp.status == 200
            wa_html = await wa_resp.text()
            assert "Mini App" in wa_html
            assert "smart-input" in wa_html
            assert "inpReceipt" in wa_html
            assert "overflow-x: hidden" in wa_html
            assert "kopilka_token" in wa_resp.cookies

            # 8b. Classify API endpoint
            cl_resp = await client.get(f"/api/v1/{tok}/classify?q=помело")
            assert cl_resp.status == 200
            cl_data = await cl_resp.json()
            assert cl_data["ok"] is True
            assert cl_data["category"] == "Продукты"

            # 8c. Receipt upload endpoint
            import io
            from PIL import Image
            img = Image.new("RGB", (50, 50), color="white")
            buf = io.BytesIO()
            img.save(buf, format="JPEG")
            img_bytes = buf.getvalue()

            from aiohttp import FormData
            fd = FormData()
            fd.add_field("file", img_bytes, filename="receipt.jpg", content_type="image/jpeg")
            rc_resp = await client.post(f"/api/v1/{tok}/receipt", data=fd)
            # пустая белая картинка не содержит суммы, поэтому 422
            assert rc_resp.status == 422
            rc_data = await rc_resp.json()
            assert rc_data["ok"] is False

            # 8d. Successful transfer/receipt upload with mock
            import datetime
            from unittest.mock import patch
            fake_scan = {
                "success": True,
                "amount": 1500.0,
                "spent_date": datetime.date.today(),
                "merchant": "Перевод: Иван И.",
                "category": "Разное",
                "method": "transfer",
                "items": ["Кому: Иван И."],
            }
            with patch("kopilka.services.receipt_scanner.scan_receipt_image", return_value=fake_scan):
                fd_ok = FormData()
                fd_ok.add_field("file", img_bytes, filename="transfer.jpg", content_type="image/jpeg")
                rc_ok_resp = await client.post(f"/api/v1/{tok}/receipt", data=fd_ok)
                assert rc_ok_resp.status == 200
                rc_ok_data = await rc_ok_resp.json()
                assert rc_ok_data["ok"] is True
                assert rc_ok_data["saved"] is True
                assert rc_ok_data["parsed"]["amount"] == 1500.0
                assert "Иван И." in rc_ok_data["parsed"]["comment"]

            # 9. Opening /app after 1-time auth uses persistent cookie automatically
            app_resp = await client.get("/app")
            assert app_resp.status == 200
            app_html = await app_resp.text()
            assert f'const SERVER_TOKEN = "{tok}";' in app_html

            # 10. One-time login endpoint (/api/v1/auth/login) with token or full URL
            login_resp = await client.post("/api/v1/auth/login", json={"token": f"https://example.com/webapp/{tok}"})
            assert login_resp.status == 200
            login_j = await login_resp.json()
            assert login_j["ok"] is True
            assert login_j["token"] == tok

            # 11. PWA Manifest, Service Worker, Icon
            mf_resp = await client.get(f"/webapp/{tok}/manifest.json")
            assert mf_resp.status == 200
            mf_j = await mf_resp.json()
            assert mf_j["start_url"] == f"/webapp/{tok}"
            assert mf_j["display"] == "standalone"

            sw_resp = await client.get("/sw.js")
            assert sw_resp.status == 200

            icon_resp = await client.get("/icon.svg")
            assert icon_resp.status == 200
        finally:
            await client.close()

    asyncio.run(_run())
