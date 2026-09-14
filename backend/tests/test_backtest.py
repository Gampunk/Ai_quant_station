import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pathlib import Path

_STRATEGY_CODE_INLINE = """import pandas as pd
import ta
import numpy as np

def calculate_signals(df):
    close = df['close']
    rsi = ta.momentum.rsi(close, window=14)
    sma50 = ta.trend.sma_indicator(close, window=50)
    signal = pd.Series(0, index=df.index)
    signal[(close > sma50) & (rsi > 50) & (rsi < 70)] = 1
    signal[(close < sma50) | (rsi > 80)] = -1
    return signal
"""


@pytest.mark.asyncio
class TestBacktest:
    """Real integration tests for POST /api/backtest/run.

    Uses real AI provider (NVIDIA) + real XAUUSD parquet data.
    """

    async def test_backtest_invalid_symbol(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/api/backtest/run", json={
            "prompt_id": "1",
            "symbol": "INVALID_SYMBOL_XYZ",
            "timeframe": "1T",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
        }, headers=auth_headers)
        data = resp.json()
        if resp.status_code == 200:
            assert data.get("success") is False, f"Expected success=False for invalid symbol, got {data}"
            err_msg = data.get("error", "")
        else:
            err_msg = str(data.get("detail", data.get("error", "")))
        assert len(err_msg) > 0, "Expected error message"
        print(f"\n[test_backtest_invalid_symbol] Correctly rejected: {err_msg[:100]}")

    @pytest.mark.requires_parquet
    @pytest.mark.requires_ai_key
    async def test_backtest_metrics_valid(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/api/backtest/run", json={
            "prompt_id": "1",
            "symbol": "XAUUSD",
            "timeframe": "1H",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "lot_size": 0.01,
            "strategy_code": _STRATEGY_CODE_INLINE,
        }, headers=auth_headers, timeout=120)
        assert resp.status_code == 200, f"Backtest failed: {resp.text[:500]}"
        data = resp.json()
        assert data.get("success"), f"Backtest not successful: {data.get('error', '')}"
        metrics = data.get("metrics", {})
        assert "total_return" in metrics, f"Missing total_return in {metrics}"
        assert "win_rate" in metrics, f"Missing win_rate in {metrics}"
        assert "max_drawdown" in metrics, f"Missing max_drawdown in {metrics}"
        assert isinstance(metrics["total_return"], (int, float)), f"total_return not numeric: {metrics['total_return']}"
        assert isinstance(metrics["win_rate"], (int, float)), f"win_rate not numeric: {metrics['win_rate']}"
        assert 0 <= metrics["win_rate"] <= 100, f"win_rate out of range: {metrics['win_rate']}"
        assert metrics["max_drawdown"] <= 0, f"max_drawdown should be <= 0: {metrics['max_drawdown']}"
        print(f"\n[test_backtest_metrics] Return={metrics['total_return']}% "
              f"WinRate={metrics['win_rate']}% DD={metrics['max_drawdown']}% "
              f"Trades={metrics.get('trades', 'N/A')}")

    @pytest.mark.requires_parquet
    @pytest.mark.requires_ai_key
    async def test_backtest_equity_curve(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/api/backtest/run", json={
            "prompt_id": "2",
            "symbol": "XAUUSD",
            "timeframe": "1H",
            "start_date": "2026-01-01",
            "end_date": "2026-01-31",
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "lot_size": 0.01,
            "strategy_code": _STRATEGY_CODE_INLINE,
        }, headers=auth_headers, timeout=120)
        assert resp.status_code == 200, f"Backtest failed: {resp.text[:500]}"
        data = resp.json()
        assert data.get("success"), f"Backtest not successful"
        curve = data.get("equity_curve", [])
        assert len(curve) > 0, "Equity curve is empty"
        assert all(isinstance(v, (int, float)) for v in curve), "Equity curve values not numeric"
        print(f"\n[test_backtest_equity_curve] {len(curve)} points, "
              f"start={curve[0]:.4f}, end={curve[-1]:.4f}")

    @pytest.mark.requires_parquet
    @pytest.mark.requires_ai_key
    async def test_backtest_trade_log(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/api/backtest/run", json={
            "prompt_id": "3",
            "symbol": "XAUUSD",
            "timeframe": "1H",
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "lot_size": 0.01,
            "strategy_code": _STRATEGY_CODE_INLINE,
        }, headers=auth_headers, timeout=120)
        assert resp.status_code == 200, f"Backtest failed: {resp.text[:500]}"
        data = resp.json()
        assert data.get("success"), f"Backtest not successful"
        trades = data.get("trades", [])
        assert len(trades) > 0, "Expected at least 1 trade"
        t = trades[0]
        assert "entry_time" in t, f"Missing entry_time: {t}"
        assert "exit_time" in t, f"Missing exit_time: {t}"
        assert "direction" in t, f"Missing direction: {t}"
        assert "pnl_dollars" in t, f"Missing pnl_dollars: {t}"
        print(f"\n[test_backtest_trade_log] {len(trades)} trades recorded")
        print(f"  First trade: {t['direction']} Entry={t['entry_price']} "
              f"Exit={t['exit_price']} PnL=${t['pnl_dollars']}")

    async def test_backtest_generated_code(self, client: AsyncClient, auth_headers: dict):
        resp = await client.post("/api/backtest/run", json={
            "prompt_id": "5",
            "symbol": "XAUUSD",
            "timeframe": "1H",
            "start_date": "2026-01-15",
            "end_date": "2026-02-15",
            "provider": "nvidia",
            "model": "qwen/qwen3.5-122b-a10b",
            "lot_size": 0.01,
        }, headers=auth_headers, timeout=120)
        assert resp.status_code == 200, f"Backtest failed: {resp.text[:500]}"
        data = resp.json()
        if data.get("success"):
            assert data.get("generated_code") is not None, "Missing generated_code"
            assert "calculate_signals" in data["generated_code"], "generated_code missing calculate_signals function"
            print(f"\n[test_backtest_generated_code] Code generated ({len(data['generated_code'])} chars)")
        else:
            err = data.get("error", "")
            assert len(err) > 0, "Expected error on failed code generation"
            print(f"\n[test_backtest_generated_code] AI generation failed: {err[:100]}")
