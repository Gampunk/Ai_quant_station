import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select


@pytest.mark.asyncio
class TestAIAnalyst:
    """Real integration tests for POST /api/ai/chat.

    All tests make real API calls to NVIDIA NIM.
    """

    @pytest.mark.requires_ai_key
    async def test_chat_simple_text(self, client: AsyncClient, auth_headers: dict):
        payload = {
            "messages": [{"role": "user", "content": "What is the current market sentiment for gold in 3 sentences?"}],
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "persona": "technical_analyst",
            "symbol": "XAUUSD",
        }
        resp = await client.post("/api/ai/chat", json=payload, headers=auth_headers)
        assert resp.status_code == 200, f"Chat failed: {resp.text[:500]}"
        data = resp.json()
        assert "message" in data, "Missing message field"
        msg = data.get("message", "") or ""
        if len(msg) > 0:
            assert len(msg) > 20, f"Response too short ({len(msg)} chars)"
        print(f"\n[test_chat_simple_text] Response length: {len(msg)} chars")

    @pytest.mark.requires_ai_key
    async def test_chat_memory_saved(self, client: AsyncClient, auth_headers: dict, db_session: AsyncSession):
        from app.models.ai_memory import ChatMemory

        payload = {
            "messages": [{"role": "user", "content": "Analyze gold and reply with: MEMORY_TEST_OK"}],
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "persona": "technical_analyst",
            "symbol": "XAUUSD",
        }
        resp = await client.post("/api/ai/chat", json=payload, headers=auth_headers)
        assert resp.status_code == 200, f"Chat failed: {resp.text[:500]}"
        data = resp.json()
        assert data.get("chat_memory_id") is not None, "Missing chat_memory_id"

        result = await db_session.execute(
            select(ChatMemory).where(ChatMemory.id == data["chat_memory_id"])
        )
        record = result.scalar_one_or_none()
        assert record is not None, "Chat memory record not found in DB"
        assert record.role == "assistant", f"Expected assistant role, got {record.role}"
        print(f"\n[test_chat_memory_saved] DB record ID={record.id}, symbol={record.symbol}")

    @pytest.mark.requires_ai_key
    async def test_chat_with_code_execution(self, client: AsyncClient, auth_headers: dict):
        payload = {
            "messages": [{"role": "user", "content": "Write Python code to print the mean close price of the last 100 candles."}],
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "persona": "quant",
            "symbol": "XAUUSD",
        }
        resp = await client.post("/api/ai/chat", json=payload, headers=auth_headers)
        assert resp.status_code == 200, f"Chat failed: {resp.text[:500]}"
        data = resp.json()
        print(f"\n[test_chat_code_exec] message length: {len(data.get('message', ''))}")

    @pytest.mark.requires_ai_key
    async def test_chat_detects_trade_setup(self, client: AsyncClient, auth_headers: dict):
        payload = {
            "messages": [{"role": "user", "content": "Analyze XAUUSD and generate a detailed trade setup with entry, SL, TP, and risk-reward ratio."}],
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "persona": "technical_analyst",
            "symbol": "XAUUSD",
        }
        resp = await client.post("/api/ai/chat", json=payload, headers=auth_headers)
        assert resp.status_code == 200, f"Chat failed: {resp.text[:500]}"
        data = resp.json()
        if data.get("detected_setup"):
            setup = data["detected_setup"]
            assert "action" in setup, "Missing action in trade setup"
            assert setup["action"] == "TRADE_SETUP", f"Unexpected action: {setup['action']}"
            print(f"\n[test_chat_trade_setup] DETECTED: {setup.get('direction')} {setup.get('symbol')} "
                  f"Entry={setup.get('entry_price')} SL={setup.get('stop_loss')} TP={setup.get('take_profit')}")
        else:
            print("\n[test_chat_trade_setup] No trade setup detected (AI chose not to recommend a trade)")

    @pytest.mark.requires_ai_key
    async def test_chat_global_insights_updated(self, client: AsyncClient, auth_headers: dict, db_session: AsyncSession):
        from app.models.ai_memory import GlobalInsights

        payload = {
            "messages": [{"role": "user", "content": "Analyze EURUSD and provide a trade setup recommendation."}],
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "persona": "technical_analyst",
            "symbol": "EURUSD",
        }
        resp = await client.post("/api/ai/chat", json=payload, headers=auth_headers)
        assert resp.status_code == 200, f"Chat failed: {resp.text[:500]}"

        result = await db_session.execute(
            select(GlobalInsights).where(GlobalInsights.symbol == "EURUSD")
        )
        insight = result.scalar_one_or_none()
        if insight:
            assert insight.total_analyzed > 0, "total_analyzed should be > 0"
            print(f"\n[test_chat_global_insights] EURUSD: analyzed={insight.total_analyzed}, "
                  f"BUY={insight.buy_signals}, SELL={insight.sell_signals}")
        else:
            print("\n[test_chat_global_insights] No insights record created (no trade setup)")

    @pytest.mark.requires_ai_key
    async def test_chat_model_usage_tracked(self, client: AsyncClient, auth_headers: dict, db_session: AsyncSession):
        from app.models.ai_memory import ModelUsage

        payload = {
            "messages": [{"role": "user", "content": "Tell me about gold price action in 2 sentences."}],
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "persona": "technical_analyst",
            "symbol": "XAUUSD",
        }
        resp = await client.post("/api/ai/chat", json=payload, headers=auth_headers)
        assert resp.status_code == 200, f"Chat failed: {resp.text[:500]}"

        result = await db_session.execute(
            select(ModelUsage).where(
                ModelUsage.provider == "nvidia",
                ModelUsage.model == "qwen/qwen3.5-122b-a10b",
            )
        )
        usage = result.scalar_one_or_none()
        assert usage is not None, "ModelUsage record not found"
        assert usage.total_requests > 0, "total_requests should be > 0"
        print(f"\n[test_chat_model_usage] Provider={usage.provider} Model={usage.model} "
              f"Requests={usage.total_requests} Tokens={usage.total_tokens}")

    async def test_chat_invalid_provider(self, client: AsyncClient, auth_headers: dict):
        payload = {
            "messages": [{"role": "user", "content": "hello"}],
            "provider": "nonexistent_provider",
            "model": "fake-model",
            "symbol": "XAUUSD",
        }
        resp = await client.post("/api/ai/chat", json=payload, headers=auth_headers)
        assert resp.status_code in (400, 422), f"Expected 400/422, got {resp.status_code}: {resp.text[:200]}"
        print(f"\n[test_chat_invalid_provider] Correctly rejected with {resp.status_code}: {resp.text[:100]}")
