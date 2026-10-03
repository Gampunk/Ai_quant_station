"""
Master Report Generator
=======================
Fetches ALL [AUTOPILOT] trades directly from MT5 connector API and generates
a master Excel report covering the full period with 4 sheets:
  1. Master Summary — overall stats
  2. Day-by-Day — per-date breakdown with bar chart
  3. All Trades — every trade in a flat table
  4. Prompt Performance — per-prompt aggregated stats

Usage:
    cd backend/
    python -m scripts.generate_master_report          # generate + email
    python -m scripts.generate_master_report --dry-run # skip email
"""
import argparse
import asyncio
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.styles import Font, Alignment, Border, Side, PatternFill

REPORTS_DIR = Path(__file__).resolve().parent.parent / "daily_reports"
# The connector's address comes from the environment (backend/.env), never the code.
CONNECTOR_URL = os.environ.get("MT5_CONNECTOR_URL", "").rstrip("/")
if not CONNECTOR_URL:
    sys.exit("Set MT5_CONNECTOR_URL (and MT5_API_TOKEN) in the environment or backend/.env")
API_TOKEN = os.environ.get("MT5_API_TOKEN", "")


# ── Fetch from MT5 Connector ──────────────────────────────────────────────

async def _fetch_all_autopilot_trades() -> list[dict]:
    """Fetch ALL [AUTOPILOT] trades from MT5 connector history endpoint.
    Uses hours=5000 to capture everything available."""
    headers = {}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"

    print(f"  Fetching from {CONNECTOR_URL}/history?hours=5000 ...")
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(
            f"{CONNECTOR_URL}/history",
            params={"hours": 5000},
            headers=headers,
        )
    if resp.status_code != 200:
        print(f"  [ERR] MT5 connector returned {resp.status_code}: {resp.text[:200]}")
        return []

    deals = resp.json().get("deals", [])
    print(f"  Raw deals received: {len(deals)}")

    # Identify [AUTOPILOT] position_ids
    autopilot_pids = {
        d.get("position_id")
        for d in deals
        if d.get("position_id") and (d.get("comment") or "").strip().startswith("[AUTOPILOT]")
    }
    auto_deals = [d for d in deals if d.get("position_id") in autopilot_pids]
    print(f"  [AUTOPILOT] deals: {len(auto_deals)}")

    # Group by position_id into open/close pairs
    pos_map: dict = {}
    for deal in auto_deals:
        pid = deal.get("position_id")
        if not pid:
            continue
        if pid not in pos_map:
            pos_map[pid] = {"open": None, "close": None}
        if deal.get("entry") == "OPEN":
            pos_map[pid]["open"] = deal
        else:
            pos_map[pid]["close"] = deal

    print(f"  Unique positions: {len(pos_map)}")
    print()

    # Build trade records
    trades = []
    for pid, pair in pos_map.items():
        open_deal = pair["open"]
        close_deal = pair["close"]
        if not open_deal:
            continue  # no open entry — skip

        profit = close_deal.get("profit", 0) if close_deal else 0
        comment = open_deal.get("comment", "") or ""
        prompt_match = re.search(r"P(\d+)", comment)
        prompt_number = int(prompt_match.group(1)) if prompt_match else None

        close_comment = (close_deal.get("comment", "") or "").lower() if close_deal else ""
        if close_deal:
            if "sl" in close_comment:
                res_type = "Loss"
            elif "tp" in close_comment:
                res_type = "Win"
            elif profit > 0:
                res_type = "Win"
            else:
                res_type = "Loss"
        else:
            res_type = "Open"

        deal_time = open_deal.get("time", "")
        try:
            dt = datetime.strptime(deal_time, "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            dt = datetime.min

        trades.append({
            "ticket": str(pid),
            "symbol": open_deal.get("symbol", ""),
            "direction": open_deal.get("direction", ""),
            "entry_price": open_deal.get("price"),
            "exit_price": close_deal.get("price") if close_deal else None,
            "lot_size": open_deal.get("volume", 0),
            "profit": round(float(profit) if profit else 0.0, 2),
            "prompt_number": prompt_number,
            "prompt": f"#{prompt_number}" if prompt_number else "",
            "result": res_type,
            "datetime": dt,
            "date": dt.strftime("%Y-%m-%d") if dt else "",
            "executed_at": deal_time,
        })

    trades.sort(key=lambda t: t["datetime"])
    return trades


# ── Aggregation ────────────────────────────────────────────────────────────

def _compute_overall(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["result"] != "Open"]
    wins = sum(1 for t in closed if t["result"] == "Win")
    losses = sum(1 for t in closed if t["result"] == "Loss")
    pnl = sum(t["profit"] for t in closed)
    total = len(closed)
    win_rate = round(wins / total * 100, 1) if total > 0 else 0.0
    avg_profit = round(pnl / total, 2) if total > 0 else 0.0
    return {
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "pnl": round(pnl, 2),
        "avg_profit": avg_profit,
    }


def _compute_daily_breakdown(trades: list[dict]) -> list[dict]:
    closed = [t for t in trades if t["result"] != "Open"]
    days: dict[str, dict] = {}
    for t in closed:
        date_key = t["date"]
        if date_key not in days:
            days[date_key] = {"date": date_key, "trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        days[date_key]["trades"] += 1
        if t["result"] == "Win":
            days[date_key]["wins"] += 1
        elif t["result"] == "Loss":
            days[date_key]["losses"] += 1
        days[date_key]["pnl"] += t["profit"]

    result = []
    for d in sorted(days.keys()):
        day_data = days[d]
        total = day_data["wins"] + day_data["losses"]
        day_data["win_rate"] = round(day_data["wins"] / total * 100, 1) if total > 0 else 0.0
        day_data["pnl"] = round(day_data["pnl"], 2)
        try:
            dt = datetime.strptime(d, "%Y-%m-%d")
            day_data["day_name"] = dt.strftime("%A")
        except (ValueError, TypeError):
            day_data["day_name"] = ""
        result.append(day_data)
    return result


def _compute_prompt_stats(trades: list[dict]) -> list[dict]:
    closed = [t for t in trades if t["result"] != "Open"]
    groups: dict[str, dict] = {}
    for t in closed:
        key = t["prompt"] or "?"
        if key not in groups:
            groups[key] = {"prompt": key, "wins": 0, "losses": 0, "pnl": 0.0}
        groups[key]["pnl"] += t["profit"]
        if t["result"] == "Win":
            groups[key]["wins"] += 1
        else:
            groups[key]["losses"] += 1

    stats = []
    for g in groups.values():
        total = g["wins"] + g["losses"]
        win_pct = round(g["wins"] / total * 100, 1) if total > 0 else 0.0
        avg_p = round(g["pnl"] / total, 2) if total > 0 else 0.0
        stats.append({
            "prompt": g["prompt"],
            "wins": g["wins"],
            "losses": g["losses"],
            "wl": f"{g['wins']}W / {g['losses']}L",
            "pnl": round(g["pnl"], 2),
            "win_pct": win_pct,
            "avg_profit": avg_p,
        })
    stats.sort(key=lambda x: x["pnl"], reverse=True)
    return stats


# ── Excel Generation ──────────────────────────────────────────────────────

def _generate_master_excel(overall: dict, daily_breakdown: list[dict],
                           all_trades: list[dict], prompt_stats: list[dict]) -> str:
    wb = Workbook()
    thin = Side(style="thin")
    header_font = Font(bold=True, size=11)
    title_font = Font(bold=True, size=14)
    section_font = Font(bold=True, size=12, color="1F4E79")
    blue_fill = PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid")

    # ── Sheet 1: Master Summary ──
    ws = wb.active
    ws.title = "Master Summary"
    for col, w in zip("ABCDEFGHIJ", [14, 14, 10, 12, 12, 10, 12, 14, 14, 10]):
        ws.column_dimensions[col].width = w

    r = 1
    ws.merge_cells("A1:J1")
    ws["A1"] = "AI Quant Station — Full Period Master Report"
    ws["A1"].font = title_font
    ws["A1"].alignment = Alignment(horizontal="center")

    r = 2
    ws.merge_cells(f"A{r}:J{r}")
    date_range = ""
    if daily_breakdown:
        start = daily_breakdown[0]["date"]
        end = daily_breakdown[-1]["date"]
        date_range = f"{start} — {end}"
    ws[f"A{r}"] = f"Period: {date_range}   |   Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    ws[f"A{r}"].font = Font(size=10, italic=True)
    ws[f"A{r}"].alignment = Alignment(horizontal="center")

    r = 4
    ws[f"A{r}"] = "Overall Performance"
    ws[f"A{r}"].font = section_font

    open_count = sum(1 for t in all_trades if t["result"] == "Open")
    labels = [
        ("Trading Days", len(daily_breakdown)),
        ("Total Trades (Closed)", overall["total_trades"]),
        ("Open Positions", open_count),
        ("Wins", overall["wins"]),
        ("Losses", overall["losses"]),
        ("Win Rate", f"{overall['win_rate']}%"),
        ("Total P&L", f"${overall['pnl']:.2f}"),
        ("Avg Profit / Trade", f"${overall['avg_profit']:.2f}"),
    ]
    for i, (label, value) in enumerate(labels):
        r = 5 + i
        ws[f"B{r}"] = label
        ws[f"B{r}"].font = header_font
        ws[f"D{r}"] = value
        ws[f"B{r}"].border = Border(bottom=thin)
        ws[f"D{r}"].border = Border(bottom=thin)

    # ── Sheet 2: Day-by-Day ──
    ws2 = wb.create_sheet("Day-by-Day")
    for col, w in zip("ABCDEFGHIJ", [14, 14, 10, 10, 10, 10, 14]):
        ws2.column_dimensions[col].width = w

    r2 = 1
    ws2.merge_cells("A1:G1")
    ws2["A1"] = "Daily Breakdown"
    ws2["A1"].font = section_font

    r2 = 3
    headers = ["Date", "Day", "Trades", "Wins", "Losses", "Win %", "P&L"]
    for c, h in enumerate(headers, 1):
        cell = ws2.cell(row=r2, column=c, value=h)
        cell.font = header_font
        cell.fill = blue_fill
        cell.border = Border(bottom=thin)

    prev_row = r2
    for dd in daily_breakdown:
        r2 += 1
        ws2.cell(row=r2, column=1, value=dd["date"])
        ws2.cell(row=r2, column=2, value=dd.get("day_name", ""))
        ws2.cell(row=r2, column=3, value=dd["trades"])
        ws2.cell(row=r2, column=4, value=dd["wins"])
        ws2.cell(row=r2, column=5, value=dd["losses"])
        ws2.cell(row=r2, column=6, value=f"{dd['win_rate']}%")
        pnl_cell = ws2.cell(row=r2, column=7, value=dd["pnl"])
        pnl_cell.number_format = "+$#,##0.00;-$#,##0.00"
        for c in range(1, 8):
            ws2.cell(row=r2, column=c).border = Border(bottom=thin)

    # Bar chart
    if daily_breakdown:
        chart_start = prev_row + 1
        chart_end = chart_start + len(daily_breakdown) - 1
        chart = BarChart()
        chart.type = "col"
        chart.title = "Daily P&L — Full Period"
        chart.y_axis.title = "P&L ($)"
        chart.x_axis.title = "Day"
        chart.style = 10
        data_ref = Reference(ws2, min_col=7, min_row=chart_start, max_row=chart_end)
        cats_ref = Reference(ws2, min_col=2, min_row=chart_start, max_row=chart_end)
        chart.add_data(data_ref, titles_from_data=False)
        chart.set_categories(cats_ref)
        chart.width = 22
        chart.height = 14
        series = chart.series[0]
        series.graphicalProperties.solidFill = "2563EB"
        ws2.add_chart(chart, f"A{r2 + 2}")

    # ── Sheet 3: All Trades ──
    ws3 = wb.create_sheet("All Trades")
    for col, w in zip("ABCDEFGHIJK", [10, 14, 10, 10, 12, 12, 8, 12, 10, 12, 10]):
        ws3.column_dimensions[col].width = w

    r3 = 1
    ws3.merge_cells("A1:K1")
    ws3["A1"] = "All Trades — Full Period"
    ws3["A1"].font = section_font

    r3 = 3
    headers3 = ["Ticket", "Date", "Symbol", "Direction", "Entry", "Exit", "Lot", "Profit", "Prompt#", "Result", "Day"]
    for c, h in enumerate(headers3, 1):
        cell = ws3.cell(row=r3, column=c, value=h)
        cell.font = header_font
        cell.fill = blue_fill
        cell.border = Border(bottom=thin)

    for t in all_trades:
        r3 += 1
        ws3.cell(row=r3, column=1, value=t.get("ticket", ""))
        ws3.cell(row=r3, column=2, value=t.get("date", ""))
        ws3.cell(row=r3, column=3, value=t.get("symbol", ""))
        ws3.cell(row=r3, column=4, value=t.get("direction", ""))
        ws3.cell(row=r3, column=5, value=t.get("entry_price"))
        ws3.cell(row=r3, column=6, value=t.get("exit_price"))
        ws3.cell(row=r3, column=7, value=t.get("lot_size"))
        profit_cell = ws3.cell(row=r3, column=8, value=t.get("profit", 0.0))
        profit_cell.number_format = "+$#,##0.00;-$#,##0.00"
        ws3.cell(row=r3, column=9, value=t.get("prompt", ""))
        ws3.cell(row=r3, column=10, value=t.get("result", ""))
        try:
            dt = datetime.strptime(t.get("date", ""), "%Y-%m-%d")
            ws3.cell(row=r3, column=11, value=dt.strftime("%A"))
        except (ValueError, TypeError):
            ws3.cell(row=r3, column=11, value="")
        for c in range(1, 12):
            ws3.cell(row=r3, column=c).border = Border(bottom=thin)

    # ── Sheet 4: Prompt Performance ──
    ws4 = wb.create_sheet("Prompt Performance")
    for col, w in zip("ABCDEFGH", [14, 14, 10, 10, 14, 12, 14]):
        ws4.column_dimensions[col].width = w

    r4 = 1
    ws4.merge_cells("A1:G1")
    ws4["A1"] = "Prompt Performance — Full Period"
    ws4["A1"].font = section_font

    r4 = 3
    prompt_headers = ["Prompt", "Wins", "Losses", "W/L", "P&L", "Win %", "Avg Profit"]
    for c, h in enumerate(prompt_headers, 1):
        cell = ws4.cell(row=r4, column=c, value=h)
        cell.font = header_font
        cell.fill = blue_fill
        cell.border = Border(bottom=thin)

    for ps in prompt_stats:
        r4 += 1
        ws4.cell(row=r4, column=1, value=ps["prompt"])
        ws4.cell(row=r4, column=2, value=ps["wins"])
        ws4.cell(row=r4, column=3, value=ps["losses"])
        ws4.cell(row=r4, column=4, value=ps["wl"])
        ws4.cell(row=r4, column=5, value=ps["pnl"])
        ws4.cell(row=r4, column=6, value=f"{ps['win_pct']}%")
        ws4.cell(row=r4, column=7, value=ps["avg_profit"])
        for c in range(1, 8):
            ws4.cell(row=r4, column=c).border = Border(bottom=thin)

    filename = f"master_report_{datetime.utcnow().strftime('%Y-%m-%d')}.xlsx"
    filepath = str(REPORTS_DIR / filename)
    wb.save(filepath)
    print(f"  [OK] Master report saved: {filepath}")
    return filepath


# ── Telegram ────────────────────────────────────────────────────────────────

def _send_telegram(filepath: str, overall: dict) -> bool:
    """Send master report to Telegram. Returns True on success."""
    try:
        from app.core.config import settings
    except ImportError:
        print("  [WARN] Cannot import settings — skipping Telegram")
        return False

    token = settings.TELEGRAM_BOT_TOKEN
    chat_id = settings.TELEGRAM_CHAT_ID

    if not token or not chat_id:
        print("  [WARN] Telegram not configured — skipping")
        return False

    now_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M')
    text = (
        f"*AI Quant Station — Full Period Master Report*\n"
        f"_{now_str} UTC_\n\n"
        f"*Overall Performance:*\n"
        f"  Total Trades (Closed): {overall['total_trades']}\n"
        f"  Wins: {overall['wins']}  /  Losses: {overall['losses']}\n"
        f"  Win Rate: {overall['win_rate']}%\n"
        f"  Total P&L: ${overall['pnl']:.2f}\n\n"
        f"Report: `{os.path.basename(filepath)}`"
    )

    api_base = f"https://api.telegram.org/bot{token}"

    try:
        # Send text summary
        payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}).encode()
        req = urllib.request.Request(f"{api_base}/sendMessage", data=payload, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)

        # Send Excel file
        boundary = "----FormBoundary7MA4YWxkTrZu0gW"
        with open(filepath, "rb") as f:
            file_data = f.read()

        body = (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"chat_id\"\r\n\r\n"
            f"{chat_id}\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"caption\"\r\n\r\n"
            f"Master Trade Report — {datetime.utcnow().strftime('%Y-%m-%d')}\r\n"
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"document\"; filename=\"{os.path.basename(filepath)}\"\r\n"
            f"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n"
        ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

        req2 = urllib.request.Request(
            f"{api_base}/sendDocument",
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        urllib.request.urlopen(req2, timeout=30)

        print(f"  [OK] Report sent to Telegram chat {chat_id}")
        return True
    except Exception as e:
        print(f"  [WARN] Telegram send failed: {e}")
        return False


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate master trade report from MT5 connector")
    parser.add_argument("--dry-run", action="store_true", help="Generate report but skip email")
    parser.add_argument("--hours", type=int, default=5000, help="Hours of history to fetch (default: 5000)")
    args = parser.parse_args()

    if not REPORTS_DIR.is_dir():
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Fetch trades from MT5 connector
    trades = asyncio.run(_fetch_all_autopilot_trades())

    if not trades:
        print("[ERR] No trades fetched — aborting")
        sys.exit(1)

    # Compute stats
    overall = _compute_overall(trades)
    daily_breakdown = _compute_daily_breakdown(trades)
    prompt_stats = _compute_prompt_stats(trades)

    print(f"  Total: {len(daily_breakdown)} trading days, {len(trades)} trades ({overall['total_trades']} closed)")
    print(f"  Overall P&L: ${overall['pnl']:.2f} ({overall['wins']}W / {overall['losses']}L, {overall['win_rate']}%)")
    print()

    # Show day-by-day
    print("  Day-by-Day:")
    for dd in daily_breakdown:
        day_str = f"    {dd['date']} ({dd['day_name']}): {dd['trades']} trades, {dd['wins']}W/{dd['losses']}L, ${dd['pnl']:+.2f}"
        print(day_str)
    print()

    # Generate Excel
    filepath = _generate_master_excel(overall, daily_breakdown, trades, prompt_stats)

    # Send
    if args.dry_run:
        print("\n  [SKIP] --dry-run: not sending")
    else:
        print()
        _send_telegram(filepath, overall)

    print("\nDone.")


if __name__ == "__main__":
    main()
