# Telegram Report Setup — Impulse Analyst

## Step 1: Create Telegram Bot

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Name: `Impulse Analyst Reports` (or any name)
4. Username: `impulse_report_bot` (must end with `bot`)
5. Copy the **BOT_TOKEN** (format: `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

## Step 2: Get Your CHAT_ID

1. Open your new bot in Telegram
2. Send any message (e.g., `/start`)
3. Open browser, visit:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
4. Find `"chat":{"id":` in the response — that number is your **CHAT_ID**

Or use **@userinfobot** — forward your message to it, it shows your chat ID.

## Step 3: Configure .env

```bash
cd /opt/impulse_analyst/backend
nano .env
```

Add these lines:
```bash
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

## Step 4: Test the Bot

```bash
cd /opt/impulse_analyst/backend
source venv/bin/activate
python -m scripts.test_telegram
```

You should see a test message in your Telegram chat.

## Step 5: Deploy

```bash
cd /opt/impulse_analyst
git pull

# Install monitor timer
sudo cp deploy/impulse-monitor.service /etc/systemd/system/
sudo cp deploy/impulse-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now impulse-monitor.timer

# Restart backend (picks up new scheduler config)
sudo systemctl restart impulse-analyst
```

## Step 6: Verify

```bash
# Check scheduler is running
sudo journalctl -u impulse-analyst -n 30 --no-pager | grep -i "report"

# Check monitor timer
sudo systemctl list-timers | grep impulse

# Manual test (send report now)
cd /opt/impulse_analyst/backend
python -c "import asyncio; from app.core.email_reports import run_daily_report; asyncio.run(run_daily_report())"
```

## Report Schedule

| Day | Report | Time (IST) | Time (UTC) |
|-----|--------|------------|------------|
| Mon–Fri | Daily | 9:00 AM | 3:30 AM |
| Saturday | Weekly | 9:00 AM | 3:30 AM |
| Sunday | None | — | — |

## Monitor Alerts

The health monitor runs every 5 minutes and sends Telegram alerts when:
- Backend service is down
- Backend health endpoint unreachable
- MT5 connector unreachable or not initialized

## Troubleshooting

| Issue | Fix |
|-------|-----|
| "TELEGRAM_BOT_TOKEN is empty" | Add token to `.env` |
| "TELEGRAM_CHAT_ID is empty" | Send message to bot first, then get ID |
| Messages not received | Check bot isn't blocked, try `/start` |
| Report not sent | Check logs: `journalctl -u impulse-analyst -n 50` |
