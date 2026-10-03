import pytest
import numpy as np
import pandas as pd
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


@pytest.fixture
def synthetic_parquet(tmp_path, monkeypatch):
    """Synthetic XAUUSD 1H OHLC for 2025-12-01..2026-02-01 in a temp dir.

    The real parquet archive only exists on the server, so these tests used
    to fail locally with "No historical data found". A seeded sine-wave +
    drift path guarantees SMA/RSI crossovers (so the engine records trades),
    and backtest.PARQUET_DIR is redirected to the temp file.
    """
    rng = np.random.default_rng(42)
    idx = pd.date_range("2025-12-01", "2026-02-01", freq="1h", inclusive="left")
    t = np.arange(len(idx))
    close = (
        2600.0
        + 80.0 * np.sin(2 * np.pi * t / 168)   # 7-day cycle → MA crossovers
        + 0.04 * t
        + np.cumsum(rng.normal(0, 1.2, len(idx)))
    )
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + rng.uniform(0.5, 2.0, len(idx))
    low = np.minimum(open_, close) - rng.uniform(0.5, 2.0, len(idx))
    df = pd.DataFrame({
        # unit-agnostic epoch seconds (astype('int64') follows the index
        # resolution — us vs ns — and broke the date filter once already)
        "timestamp": (idx - pd.Timestamp("1970-01-01")) // pd.Timedelta("1s"),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": rng.integers(50, 500, len(idx)).astype("float64"),
    })
    df.to_parquet(tmp_path / "XAUUSD_2026.parquet", index=False)
    monkeypatch.setattr("app.api.backtest.PARQUET_DIR", tmp_path)
    return tmp_path


@pytest.mark.asyncio
class TestBacktest:
    """Integration tests for POST /api/backtest/run.

    Offline & deterministic: synthetic parquet fixture for market data,
    strategy_code injected inline (no AI generation), freeform AI attempt
    fails fast without keys and falls back to the real vectorized engine.
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

    async def test_backtest_metrics_valid(self, client: AsyncClient, auth_headers: dict, synthetic_parquet):
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

    async def test_backtest_equity_curve(self, client: AsyncClient, auth_headers: dict, synthetic_parquet):
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

    async def test_backtest_trade_log(self, client: AsyncClient, auth_headers: dict, synthetic_parquet):
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
