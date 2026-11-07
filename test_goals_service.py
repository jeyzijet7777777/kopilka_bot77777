import pytest
import bot
from kopilka.database.connection import db
from kopilka.services.goals_service import (
    get_piggy_goal,
    set_piggy_goal,
    clear_piggy_goal,
    format_goal_progress,
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

def test_goals_crud(env):
    uid = 100
    assert get_piggy_goal(uid) is None

    set_piggy_goal(uid, "Отпуск на море", 150000)
    g = get_piggy_goal(uid)
    assert g is not None
    assert g["title"] == "Отпуск на море"
    assert g["target"] == 150000.0

    text = format_goal_progress(75000, g)
    assert "50.0%" in text
    assert "Осталось накопить" in text
    assert "Отпуск на море" in text

    text_completed = format_goal_progress(160000, g)
    assert "Цель достигнута" in text_completed

    clear_piggy_goal(uid)
    assert get_piggy_goal(uid) is None
