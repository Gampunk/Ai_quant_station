import pytest
import asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.ai_memory import AutopilotCycle, AiCallLog
from app.models.user import User


@pytest.mark.asyncio
class TestAutopilot:
    """Integration tests for Autopilot endpoints.

    Tests settings persistence, prompt management, start/stop lifecycle.
    MT5-dependent tests are conditionally skipped if connector is down.
    """

    async def test_cycle_history_includes_cycle_reason_and_linked_ai_call(
        self, client: AsyncClient, auth_headers: dict, db_session: AsyncSession
    ):
        user = (await db_session.execute(select(User).where(User.username == "admin"))).scalar_one()
        cycle_id = "11111111-2222-4333-8444-555555555555"
        db_session.add(AutopilotCycle(
            cycle_id=cycle_id, user_id=user.id, cycle_number=7, symbol="XAUUSD",
            status="completed", outcome="no_setup", outcome_reason="AI found no valid setup",
        ))
        db_session.add(AiCallLog(
            user_id=user.id, cycle_id=cycle_id, cycle_number=7, provider="test-provider",
            model="test-model", stage="analysis", outcome="success", latency_ms=125,
        ))
        await db_session.commit()

        response = await client.get("/api/autopilot/cycles", headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        matching = next(c for c in data["cycles"] if c["cycle_id"] == cycle_id)
        assert matching["outcome_reason"] == "AI found no valid setup"
        assert any(event["stage"] == "ai:analysis" and event["outcome"] == "success"
                   for event in matching["timeline"])

    async def test_autopilot_status_endpoint(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/api/autopilot/status", headers=auth_headers)
        assert resp.status_code == 200, f"Status failed: {resp.text[:200]}"
        data = resp.json()
        assert "enabled" in data, "Missing enabled field"
        assert "running" in data, "Missing running field"
        assert "stats" in data, "Missing stats field"
        assert data["enabled"] == False, "Expected disabled by default"
        assert data["running"] == False, "Expected not running by default"
        print(f"\n[test_autopilot_status] enabled={data['enabled']} running={data['running']} "
              f"stats: {data['stats']}")

    async def test_autopilot_settings_persist(self, client: AsyncClient, auth_headers: dict):
        settings = {
            "symbol": "XAUUSD",
            "default_lot": 0.05,
            "interval_seconds": 600,
            "max_trades_per_day": 5,
            "cooldown_minutes": 10,
            "max_daily_loss": -25.0,
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
        }
        resp = await client.post("/api/autopilot/settings", json=settings, headers=auth_headers)
        assert resp.status_code == 200, f"Settings save failed: {resp.text[:200]}"
        print(f"\n[test_autopilot_settings_persist] Settings saved")

        status = await client.get("/api/autopilot/status", headers=auth_headers)
        assert status.status_code == 200
        s = status.json().get("settings", {})
        assert s.get("symbol") == "XAUUSD", f"Expected XAUUSD, got {s.get('symbol')}"
        assert s.get("default_lot") == 0.05, f"Expected 0.05, got {s.get('default_lot')}"
        assert s.get("interval_seconds") == 600, f"Expected 600, got {s.get('interval_seconds')}"
        print(f"[test_autopilot_settings_persist] Verified: symbol={s.get('symbol')} "
              f"lot={s.get('default_lot')} interval={s.get('interval_seconds')}s")

    async def test_autopilot_prompt_list(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/api/autopilot/prompts", headers=auth_headers)
        assert resp.status_code == 200, f"Prompts failed: {resp.text[:200]}"
        data = resp.json()
        assert "default_prompts" in data, "Missing default_prompts"
        assert "personal_prompts" in data, "Missing personal_prompts"
        assert len(data["default_prompts"]) > 0, "No default prompts loaded"
        print(f"\n[test_autopilot_prompt_list] {len(data['default_prompts'])} default prompts, "
              f"{len(data['personal_prompts'])} personal prompts")

    async def test_autopilot_create_personal_prompt(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/api/autopilot/prompts", json={
            "content": "Test: Buy XAUUSD when RSI(14) < 30 on 1H timeframe, SL at 20 ATR, TP at 3x risk."
        }, headers=auth_headers)
        assert resp.status_code == 200, f"Create prompt failed: {resp.text[:200]}"
        prompt = resp.json()
        prompt_id = prompt.get("id")
        assert prompt_id is not None, f"Missing id: {prompt}"
        print(f"\n[test_autopilot_create_prompt] Created prompt ID={prompt_id}")

        list_resp = await client.get("/api/autopilot/prompts", headers=auth_headers)
        assert list_resp.status_code == 200
        personal = list_resp.json().get("personal_prompts", [])
        matching = [p for p in personal if p["id"] == str(prompt_id) or p["id"] == f"custom_{prompt_id}"]
        assert len(matching) > 0, "Created prompt not found in list"
        print(f"[test_autopilot_create_prompt] Verified in list")

    async def test_autopilot_start_stop(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/api/autopilot/start", headers=auth_headers)
        assert resp.status_code == 200, f"Start failed: {resp.text[:200]}"
        data = resp.json()
        assert data.get("success"), f"Start not successful: {data}"
        print(f"\n[test_autopilot_start_stop] Start initiated")

        await asyncio.sleep(2)

        status = await client.get("/api/autopilot/status", headers=auth_headers)
        assert status.status_code == 200
        s = status.json()
        print(f"[test_autopilot_start_stop] Status: enabled={s['enabled']} running={s['running']}")

        resp = await client.post("/api/autopilot/stop", headers=auth_headers)
        assert resp.status_code == 200, f"Stop failed: {resp.text[:200]}"
        data = resp.json()
        assert data.get("success"), f"Stop not successful: {data}"
        print(f"[test_autopilot_start_stop] Stop completed")

        await asyncio.sleep(1)

        status2 = await client.get("/api/autopilot/status", headers=auth_headers)
        assert status2.status_code == 200
        s2 = status2.json()
        assert s2["enabled"] == False, "enabled should be False after stop"
        assert s2["running"] == False, "running should be False after stop"
        print(f"[test_autopilot_start_stop] Verified stopped")

    async def test_autopilot_results(self, client: AsyncClient, auth_headers: dict):
        resp = await client.get("/api/autopilot/results", headers=auth_headers)
        assert resp.status_code == 200, f"Results failed: {resp.text[:200]}"
        data = resp.json()
        assert isinstance(data, list), f"Expected list, got {type(data)}"
        print(f"\n[test_autopilot_results] {len(data)} trades in history")
        if data:
            t = data[0]
            assert "symbol" in t, f"Missing symbol in trade: {t}"
            assert "direction" in t, f"Missing direction in trade: {t}"
            print(f"  Most recent: {t.get('symbol')} {t.get('direction')} "
                  f"Lot={t.get('lot_size')} PnL=${t.get('profit', 0)}")
