# Impulse Analyst v2 — Project Guide

## Overview

Professional quantitative trading platform with AI-powered analysis, automated trading, and strategy backtesting. Stack: FastAPI (Python) + React (TypeScript + Vite) + SQLite/PostgreSQL.

## Architecture

```
frontend/  (React + Vite + Tailwind)
  └── src/
      ├── pages/          # 10 pages (Login, Dashboard, Terminal, AI Analyst, etc.)
      ├── components/     # UI components (shadcn/ui)
      ├── store/          # Zustand auth store
      ├── lib/            # API client (axios)
      └── hooks/          # useToast hook

backend/  (FastAPI)
  └── app/
      ├── api/            # 11 route modules (auth, ai, trade, mt5, yahoo, etc.)
      ├── core/           # Config, database, security, services
      ├── models/         # SQLAlchemy models (15 tables)
      └── main.py         # FastAPI app entry point
```

## Quick Start

### Backend

```bash
cd backend
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env
# Set SECRET_KEY (32+ characters) and DEFAULT_ADMIN_PASSWORD (12+) in .env.
# The server refuses to start without a strong SECRET_KEY.
.venv/bin/python run.py
# Starts on http://localhost:8002
```

Full instructions: README.md and docs/HOW_TO_RUN.md.

### Frontend

```bash
cd frontend
npm install
npm run dev
# Starts on http://localhost:5173
```

### First Run

On first startup with a fresh database, the backend auto-creates:
- All 25 database tables
- One `admin` account, only if `DEFAULT_ADMIN_PASSWORD` is set and strong

**Delete old `finance_engine.db` if upgrading from an older version** (schema changed).

## Environment Variables

Required in `backend/.env`:

| Variable | Description |
|---|---|
| `SECRET_KEY` | JWT signing key. Generate: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DEFAULT_ADMIN_PASSWORD` | Admin user password (set at first startup) |
| `DATABASE_URL` | Default: `sqlite+aiosqlite:///./finance_engine.db` |

Optional:

| Variable | Default | Description |
|---|---|---|
| `NVIDIA_API_KEY` | — | NVIDIA NIM API key (nvapi-...) |
| `GROQ_API_KEY` | — | Groq API key |
| `OPEN_ROUTER_API_KEY` | — | OpenRouter API key |
| `GEMINI_API_KEY` | — | Google Gemini API key |
| `CEREBRAS_API_KEY` | — | Cerebras API key |
| `ANTHROPIC_API_KEY` | — | Anthropic Claude API key |
| `MT5_API_TOKEN` | — | MT5 Connector auth token |
| `MT5_CONNECTOR_URL` | — | External MT5 connector URL |
| `MT5_BROKER_UTC_OFFSET` | 0 | Broker timezone offset (e.g., 2 for UTC+2) |
| `ALLOW_REMOTE_CONNECTOR` | False | Allow a connector address outside local and private networks |
| `HUGGINGFACE_API_KEY` | — | For data archiving |
| `CORS_ORIGINS` | `http://localhost:5173,http://localhost:3000` | Allowed CORS origins |
| `APP_ENV` | development | `production` switches on JSON logs and hides `/docs`, `/redoc`, `/openapi.json` |
| `FORWARDED_ALLOW_IPS` | — | Proxies whose `X-Forwarded-For` is believed, comma separated, addresses or networks. Empty: no proxy |
| `HOST`, `PORT` | 0.0.0.0, 8002 | Where `run.py` listens |

## Accounts and roles

`main.py:create_default_users()` creates only `admin`, from `DEFAULT_ADMIN_PASSWORD`,
and only on a database with no admin. Nothing else is created automatically.

Add people or reset a password with `python create_admin.py`. It prompts for the password.

| Role | Can do |
|---|---|
| admin | Everything, including user management |
| trader | Trade, run the autopilot, run anything that executes AI-written code |
| viewer | Read dashboards, history, reports and settings |

## Database Tables (25)

| Table | Purpose |
|---|---|
| `users` | User accounts with bcrypt password hashes |
| `market_data` | Cached OHLC market data |
| `chat_memories` | AI conversation history + detected trade setups |
| `global_insights` | Aggregated BUY/SELL signal counts per symbol |
| `model_usage` | AI provider/model usage tracking per user |
| `trade_records` | ** Every trade** placed via Terminal or AI Analyst, linked to chat |
| `user_feedback` | Thumbs up/down feedback on AI responses |
| `calculation_history` | Technical indicator calculation records |
| `indicator_requests` | Indicator request frequency tracking |
| `user_preferences` | User settings (favorite symbols, defaults) |
| `autopilot_trades` | Autopilot trade history with AI reasoning |
| `user_prompts` | Custom strategy prompts created by users |
| `default_prompt_strategies` | Cached AI-generated strategy code |
| `autopilot_settings` | Autopilot configuration per user |
| `historical_backtests` | Historical lab backtest/analysis results |
| `user_api_keys` | Per-user AI provider keys, encrypted with `SECRET_KEY` |
| `revoked_tokens` | Tokens revoked by logout or refresh, until they expire |
| `position_audits` | Record of every close and stop/target change |
| `autopilot_logs` | Autopilot log lines per cycle |
| `ai_call_logs` | Every AI call: provider, model, tokens, outcome |
| `strategy_scores` | Win rate, profit factor and cost per prompt, updated hourly |
| `chat_embeddings` | Vectors of past AI Analyst answers, used for retrieval |
| `risk_settings` | Every version of the risk limits, with who changed them and why. The newest is in force |
| `risk_days` | Equity at the first order check of each UTC day, the baseline for the daily loss limit |
| `risk_decisions` | Every order the risk gate checked: refused, sent or failed, with the numbers and the settings version |

## Authentication Flow

```
LoginPage.tsx
  → useAuthStore.login(username, password)
  → POST /api/auth/login
  → Backend verifies bcrypt password
  → Returns JWT access_token (15min) + refresh_token (7 days)
  → Tokens stored in sessionStorage via zustand persist
  → Every API request: axios interceptor adds Authorization: Bearer {token}

On 401 response (attachAuth in authStore.ts, the one handler for every client):
  → One refresh at a time, shared by all requests that failed together
  → POST /api/auth/refresh with refresh_token
  → Gets new token pair → retries the original request once
  → If refresh fails → logout(), and the request fails without a retry
  → A retried request that gets 401 again is not refreshed again, so it cannot loop
  → 401 from login, refresh or logout never triggers a refresh

On page refresh:
  → App.tsx useEffect calls checkAuth()
  → Reads tokens from sessionStorage
  → Decodes JWT, checks exp
  → If expired → refreshAccessToken()
  → If valid → set isAuthenticated = true

ProtectedRoute:
  → Wraps all dashboard routes
  → If !isAuthenticated → redirect to /login

On logout:
  → authStore.logout() calls POST /api/auth/logout with both tokens
  → Backend revokes the access and the refresh token
  → Local state is cleared either way

On every request, the backend:
  → Verifies the token signature, then checks it is not revoked
  → Reads the user's role and active status from the database
  → So demoting or disabling someone applies on their next request

On a password change (own, by an admin, or create_admin.py --reset):
  → users.password_version goes up by one; every token carries the version it was issued with
  → Every older access and refresh token is refused, on every device
  → The person changing their own password gets a new pair back and stays logged in

Login throttling (in memory, single worker):
  → 10 attempts per address per minute
  → The address comes from X-Forwarded-For only when the request is from a proxy
    listed in FORWARDED_ALLOW_IPS (core/client_ip.py). Otherwise the header is ignored
  → 5 failures lock an account for 15 minutes
```

## All Pages & Their Flows

### 1. Login Page (`/login`)

```
Frontend: LoginPage.tsx
Backend:  auth.py → POST /login, POST /refresh
Model:    User (users table)

Flow:
  User enters username + password
  → POST /api/auth/login
  → verify_password() uses bcrypt.checkpw() directly
  → Returns { access_token, refresh_token }
  → JWT payload: { sub: username, user_id, role, name, exp, type }
  → Frontend decodes JWT with decodeBase64Url() (handles base64url encoding)
  → Stores in sessionStorage, redirects to /
```

### 2. Dashboard Page (`/`)

```
Frontend: DashboardPage.tsx
Backend:  mt5.py → GET /positions
Model:    — (reads live from MT5)

Flow:
  On mount → GET /api/mt5/positions
  → Returns account info (balance, equity, margin) + open positions
  → Displays 4 metric cards + positions table
  → If MT5 offline → shows $0.00 with error toast
```

### 3. Terminal Page (`/terminal`)

```
Frontend: TerminalPage.tsx
Backend:  trade.py → POST /order, POST /close, POST /modify
          mt5.py → GET /symbols/all, GET /positions, GET /history
Model:    TradeRecord (trade_records) — saved after every order

Flow:
  On mount → fetches symbols + positions
  User selects symbol, direction (BUY/SELL), volume, SL/TP
  → POST /api/trade/order with { symbol, action, volume, sl, tp }
  → The risk gate (core/risk.py) checks it: stop loss present and placed right,
    risk within the per-trade limit, daily loss, margin level, open trades.
    A refusal returns 422 naming the rule, and is recorded in risk_decisions
  → Backend sends it through the MT5 connector (core/mt5_connector.py)
  → Saves to trade_records: the fill as entry_price, the quote as requested_price
    (and the same pair on close), so slippage can be measured
  → Returns ticket number
  → Positions list refreshes
  Close button has loading state (prevents double-submit)
```

### 4. AI Analyst Page (`/ai-analyst`)

```
Frontend: AIAnalystPage.tsx
Backend:  ai.py → POST /chat, POST /feedback, GET /providers
          yahoo.py → GET /yahoo/{symbol}, GET /yahoo/symbols
          execute.py → run_python_code() (sandbox, called internally)
Model:    ChatMemory, GlobalInsights, ModelUsage, UserFeedback, TradeRecord

Flow:
  1. User selects data source: Yahoo or MT5
  2. Loads symbol list, picks symbol, clicks "Load"
  3. Candle chart renders using lightweight-charts
  4. User types question → POST /api/ai/chat
  5. Backend:
     a. Builds market context from candle_data or yahoo fallback
     b. Retrieves past conversations about same symbol (basic RAG)
     c. Calls AI provider with system prompt + market data
     d. If AI responds with ```python code:
        - Executes in sandbox (execute.py)
        - Returns output + charts + tables
        - If code fails: self-correction (asks AI to fix, re-runs)
     e. Saves to ChatMemory, updates GlobalInsights, ModelUsage
     f. Returns ChatResponse with chat_memory_id
  6. Frontend renders:
     - AI text (code blocks filtered out)
     - "Execute Trade" button if trade setup detected
     - Charts from show_chart()
     - Tables from show_table()
     - Execution output
  7. User can 👍/👎 each response (saved to user_feedback)

  "Execute Trade" button → POST /api/trade/order with chat_memory_id
  → Links trade back to the AI analysis that suggested it
  → Future: enables win-rate tracking per strategy prompt

  Live mode: refreshes data every 60s and re-analyses
```

### 5. Historical Lab Page (`/historical-lab`)

```
Frontend: HistoricalLabPage.tsx
Backend:  historical_lab.py → POST /run, GET /status/{id}, POST /chat
          historical_loader.py → load_data(), add_indicators()
          backtest_engine.py → BacktestEngine, DeepAnalysisEngine
Model:    HistoricalBacktest (historical_backtests)

Modes:
  A) BACKTEST MODE
     User writes strategy in plain English
     → Backend calls AI to convert to Python code (calculate_signals function)
     → Code runs in sandbox → produces signal column (1/-1/0)
     → BacktestEngine runs signals against historical parquet data
     → Returns: Sharpe Ratio, Win Rate, Profit Factor, Max Drawdown, Equity Curve

  B) DEEP ANALYSIS MODE
     No strategy needed
     → DeepAnalysisEngine analyzes price behavior
     → Returns: hourly volatility, day-of-week patterns, return distribution

  Background processing:
     → POST returns immediately with pending status + ID
     → Frontend polls GET /status/{id} every 2s
     → Backend runs background task
     → On complete → shows metrics + chart + chat vault

  Chat Vault (both modes):
     → User can ask follow-up questions about the results
     → Backend builds context with current results + market data
     → AI responds with text + optional code execution
     → Same sandbox as AI Analyst

  Technical indicators (add_indicators):
     → 22 columns: EMA 9/21/50/200, SMA 20/50, MACD, RSI 14,
       Stochastic, Bollinger Bands, ATR 14, OBV
     → Uses `ta` library (technical-analysis-library-python)
```

### 6. Prompt Backtesting Page (`/backtest`)

```
Frontend: BacktestPage.tsx
Backend:  backtest.py → POST /run
          (shares prompts with autopilot)
Model:    UserPrompt, DefaultPromptStrategy, HistoricalBacktest

Flow:
  1. User selects a prompt (strategy description)
  2. Backend checks cache for existing strategy_code
  3. If not cached → calls AI to generate calculate_signals(df)
  4. AI response uses `ta` library API (ta.momentum.rsi, ta.trend.sma_indicator)
  5. Code executed in sandbox with restricted builtins
  6. Strategy return = signal * log(close) shift(1) * leverage
  7. If code fails → self-correction (max 2 retries)
  8. Returns: Total Return, Win Rate, Max Drawdown, Trades, Equity Curve
  9. Results saved to historical_backtests
```

### 7. Autopilot Page (`/autopilot`)

```
Frontend: AutopilotPage.tsx (660 lines)
Backend:  autopilot.py → POST /start, POST /stop, GET /status
          (per-user state isolation via _user_states dict)
Model:    AutopilotSettings, AutopilotTrade, UserPrompt

Flow:
  User clicks Start:
  → Background loop (asyncio.create_task, per-user):
    1. Sync results of previous trades
    2. Fetch market data via async httpx
    3. Classify the market regime (trend, volatility) from 15m candles, then pick
       a prompt at random weighted by regime fit and past win rate
    4. Call AI to analyze market + detect TRADE_SETUP JSON
    5. If setup found → the risk gate sizes it from equity and the stop loss
       (the AI's lot is ignored), checks the limits, then sends it via the connector
    6. Sleep (configurable interval, default 300s)
    7. Loop until stopped

  Safety limits enforced:
    → max_trades_per_day (skips cycles after the limit)
    → daily loss brake: max_daily_loss on the autopilot's own trades closed
      today (UTC). Pauses until 00:00 UTC, then resumes by itself. Can be
      switched off; the account-wide limit in the risk gate applies either way
    → Both counts are read from the database every cycle
    → An error in one cycle is logged and the next cycle still runs; if the
      loop ever ends unexpectedly, the status shows it stopped, with the reason
    → Stop and target hits come from the broker's deal reason (core/trade_outcome.py)

  Settings panel:
    → Symbol, lot size, interval, provider, model
    → Prompt selection (multi-select)
    → Personal prompt creation

  Every connector call goes through core/mt5_connector.py, the single client
```

### 8. History Page (`/history`)

```
Frontend: HistoryPage.tsx
Backend:  mt5.py → GET /history
Model:    — (reads from MT5)

Flow:
  → GET /api/mt5/history?hours=X
  → Returns deal list from MT5
  → Filter: All Time, 24h, 1 Week, 1 Month
  → Stats: Total P&L, Total Trades, Wins, Win Rate
```

### 9. Settings Page (`/settings`)

```
Frontend: SettingsPage.tsx
Backend:  auth.py → PUT /password
          ai.py → GET /providers, POST /test
          autopilot.py → POST /settings

Sections:
  → Account: Change password (requires current password)
  → AI Providers: personal API keys, saved on the server encrypted per user
    (GET/POST /api/ai/user-keys), plus a Test Connection button
  → MT5 Connection: a Check Connection button only. The connector's address,
    token and terminal are set on the server
  → Autopilot: Lot size + interval defaults (saved via API)
  → Risk Limits: every limit the risk gate applies, today's standing, recent
    changes and refusals. Admins edit them, with a reason (GET/PUT /api/risk/settings)
  → Data Sync: HuggingFace (UI stub, functional via manual scripts)
```

### 10. User Management Page (`/users`)

```
Frontend: UserManagementPage.tsx
Backend:  auth.py → GET /users, POST /users, PUT /users/{id}, DELETE /users/{id}
Model:    User

Flow:
  → Admin-only access
  → Lists all users in a table
  → Create / Edit / Delete users
  → Modal form for add/edit
  → Cannot delete "admin" user
```

## API Routes (68 total)

```
AUTH:
  POST   /api/auth/login              User login
  POST   /api/auth/refresh            Refresh JWT token
  POST   /api/auth/logout             Logout
  GET    /api/auth/me                 Get current user profile
  PUT    /api/auth/password           Change password
  GET    /api/auth/users              List users (admin)
  POST   /api/auth/users              Create user (admin)
  PUT    /api/auth/users/{id}         Update user (admin)
  DELETE /api/auth/users/{id}         Delete user (admin)

AI:
  POST   /api/ai/chat                 Send message to AI
  GET    /api/ai/providers            List AI providers + models
  POST   /api/ai/test                 Test AI provider connection
  POST   /api/ai/feedback             Save feedback on AI response
  GET    /api/ai/memory               Get user conversation memory
  GET    /api/ai/user-keys            Which providers have a saved personal key
  POST   /api/ai/user-keys            Save personal provider keys (encrypted)

MARKET DATA:
  GET    /api/data/yahoo/{symbol}     Fetch Yahoo Finance data
  GET    /api/data/yahoo/symbols      Available Yahoo symbols
  GET    /api/data/yahoo/quote/{sym}  Current quote
  GET    /api/data/yahoo/search/{q}   Search symbols
  GET    /api/data/yahoo/forex        Forex pairs with prices
  GET    /api/data/yahoo/crypto       Crypto pairs with prices

MT5:
  GET    /api/mt5/health              MT5 connection health
  POST   /api/mt5/initialize          Initialize MT5
  GET    /api/mt5/symbols             Symbols
  GET    /api/mt5/symbols/all         All symbols
  GET    /api/mt5/symbol/{symbol}     Symbol info
  POST   /api/mt5/data/fetch          Fetch historical data
  POST   /api/mt5/data/latest         Fetch latest N candles
  GET    /api/mt5/account             Account info
  GET    /api/mt5/positions           Open positions
  GET    /api/mt5/history             Trade history

TRADING:
  POST   /api/trade/order             Place order (saves to trade_records)
  POST   /api/trade/close             Close position
  POST   /api/trade/modify            Modify SL/TP

CODE EXECUTION:
  POST   /api/execute/calculate-indicator  Calculate technical indicator
  (There is no endpoint that runs client-supplied code. AI-written code runs
   only inside the AI Analyst, Autopilot, Historical Lab and Prompt Backtest.)

ANALYTICS:
  GET    /api/analytics/test          Health check
  POST   /api/analytics/feedback      Submit feedback
  POST   /api/analytics/calculation   Save calculation
  GET    /api/analytics/calculations  Get calculation history
  GET    /api/analytics/indicator-stats  Indicator usage stats
  GET    /api/analytics/feedback-stats   User feedback stats
  GET    /api/analytics/reports          Autopilot performance report
  GET    /api/analytics/reports/export   Report as CSV
  GET    /api/analytics/journal          Trade journal for a date range
  GET    /api/analytics/journal/export   Journal as CSV
  GET    /api/analytics/strategy-scores  Per-prompt scoreboard

AUTOPILOT:
  POST   /api/autopilot/start         Start autopilot
  POST   /api/autopilot/stop          Stop autopilot
  GET    /api/autopilot/status        Get status + settings
  POST   /api/autopilot/settings      Save settings
  POST   /api/autopilot/connect-mt5   Connect MT5
  GET    /api/autopilot/prompts       List prompts
  POST   /api/autopilot/prompts       Create personal prompt
  PUT    /api/autopilot/prompts/{id}  Update prompt
  DELETE /api/autopilot/prompts/{id}  Delete prompt
  GET    /api/autopilot/results       Get trade results
  GET    /api/autopilot/results/export  Trade results as CSV
  GET    /api/autopilot/logs          Autopilot log lines
  GET    /api/autopilot/prompt-stats  Per-prompt statistics

Routes that trade, change autopilot settings, or run AI-written code
(AI chat, prompt backtest, historical lab run and chat) need the admin or
trader role. Everything else needs any logged-in user.

RISK:
  GET    /api/risk/settings           Limits in force, defaults and allowed ranges
  PUT    /api/risk/settings           Change limits (admin, reason required); adds a version
  GET    /api/risk/settings/history   Every version of the limits
  GET    /api/risk/status             Today's equity, loss, margin level and open trades
  GET    /api/risk/decisions          Recent order decisions, filter by outcome or source

BACKTEST:
  POST   /api/backtest/run            Run prompt backtest

HISTORICAL LAB:
  POST   /api/historical-lab/run      Start backtest/analysis
  GET    /api/historical-lab/status/{id}  Poll status
  POST   /api/historical-lab/chat     Chat follow-up
  GET    /api/historical-lab/available-symbols
  GET    /api/historical-lab/available-years/{symbol}
```

## Sandbox Environment (execute.py)

Available inside Python code blocks executed by AI:

### Standard Libraries
```python
import pandas as pd
import numpy as np
import math, json, random, itertools, collections, decimal, warnings
```

### Technical Indicators
```python
import ta  # ta.momentum.rsi(), ta.trend.sma_indicator(), ta.trend.ema_indicator()
```

### Scientific Computing
```python
import scipy
import scipy.stats as stats
import statsmodels.api as sm
from statsmodels.tsa.stattools import adfuller, coint
```

### Machine Learning
```python
import sklearn
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.ensemble import RandomForestRegressor
from sklearn.linear_model import LinearRegression
```

### Visualizations
```python
import matplotlib.pyplot as plt
import seaborn as sns
```

### Market Data
```python
import yfinance as yf
```

### Print Formatting
```python
from tabulate import tabulate
```

### Built-in Functions
```python
show_chart(data, title="Chart", color="#2563eb", chart_type="line")
show_table(data, title="Data")
print()  # Output captured and returned
```

**Security, honestly:** builtins are restricted and code containing dangerous
double-underscore names is rejected, and the code always runs in a separate
process. That is **not** a security boundary. The process has the server's
file and network access, and libraries such as pandas and yfinance can read
files and reach the network without any restricted builtin. Proper containment
is refactor step B1. Separately, the Prompt Backtest runs its generated code
through its own weaker runner in `backtest.py`, not through this sandbox.

## MT5 Broker Timezone Handling

MT5 stamps everything in the broker's server time (often UTC+2 or UTC+3, moving
with summer time). The backend works in real UTC throughout.

- `core/mt5_connector.py` converts at the boundary, in one place: deal times,
  position open times, candle times, and range queries on the way out. No other
  module converts anything
- `core/broker_clock.py` learns the offset from the connector's `/clock`, which
  compares the latest price time with UTC and answers only while prices are live.
  It follows summer time by itself, and keeps the last good reading when the market is closed
- `MT5_BROKER_UTC_OFFSET` in `.env` is only the fallback before the first reading,
  for example after a restart at the weekend. A warning is logged if it disagrees
- `/api/mt5/health` shows the offset in use and where it came from

## Architecture Decisions

| Decision | Why |
|---|---|
| SQLite default, PostgreSQL optional | Zero-config for development, same SQLAlchemy code for production |
| bcrypt directly (not passlib) | passlib incompatible with bcrypt 5.x. Use `bcrypt.hashpw()` and `bcrypt.checkpw()` directly |
| `ta` library (not `pandas_ta`) | `pandas_ta` not available for Python 3.11+. Use `ta` (technical-analysis-library-python) |
| One connector client | Every broker action and MT5 data request goes through `core/mt5_connector.py`. The backend never imports MetaTrader5, and tests fail if a second route appears |
| One risk gate | `core/risk.py` `submit_order` is the only caller of `place_order`, and a test enforces it. The rules live in the pure `evaluate()` function. Limits are versioned rows, not constants, so they can be tuned, later by agents, and every decision names the version it used |
| Connector lot cap | `MT5_MAX_VOLUME` on the connector machine (default 1.0 lot) refuses larger orders whatever the backend sends. Nothing on the website can raise it |
| Per-user autopilot state | `_user_states[user_id]` dict instead of global singleton |
| Restricted `__builtins__` in `exec()` | Prevents AI-generated code from running OS commands |
| `execute.py` sandbox | AI Analyst, Autopilot and Historical Lab code runs through this module, always in a subprocess. Prompt Backtest still has its own runner (to be merged in step B1) |
| `chat_memory_id` in trade link | Enables win-rate tracking per strategy prompt (RAG pipeline) |
| `PRAGMA foreign_keys=ON` for SQLite | Required for CASCADE deletes to work on SQLite |
| No silent errors | A handler that catches `Exception` must log, re-raise, or carry `# swallow-ok: <reason>` on its `except` line. Code in `app/` logs instead of printing. `tests/test_error_handling.py` enforces both. The price sync ends a run with failed symbols in `PriceSyncFailed` |
| Frontend lint | `.eslintrc.cjs`, run by `npm run lint` and `verify.sh`. A disabled hook-dependency warning must say why on the same line |
| Prompt refinement before AI call | `_refine_query()` rewrites vague user queries into structured analysis requests using a fast/cheap model (`mistralai/mistral-7b-instruct-v0.3`), falls back to the user's main model if unavailable. Controlled by `refine_prompt: bool` on `ChatRequest` (default: True). Adds ~300ms latency per query. |

## Known Limitations

1. **No HTTPS** in the nginx config. Needed before anything is exposed. Planned with the domain in step 14.
2. **Single worker only.** Autopilot state and login throttling live in process memory. `run.py` fixes it at one.
3. **The sandbox is not a security boundary.** See the sandbox section above.
4. **Records made before step 10 keep broker time.** Autopilot close times and durations stored earlier are a few hours off. They were left as they are; the live database's history is handled in the step 14 plan.
5. **`pandas_ta` replaced with `ta`**: different API, AI prompts updated accordingly.
6. **Retrieval runs only in the AI Analyst chat.** The autopilot does not use past analyses.

Every known problem, with the step that fixes it, is in `docs/refactor/FINDINGS.md`.

## Data Files

```
data_archive/parquet_storage/     # OHLC parquet files (7 symbols × ~17 years)
backend/finance_engine.db         # SQLite database (auto-created)
backend/prompt_list.txt           # Default strategy prompts
backend/.env                      # Environment variables (not in git)
```

## Deployment

### Docker
```bash
echo "POSTGRES_PASSWORD=<letters and digits>" > .env   # next to docker-compose.yml, git ignores it
docker compose up --build
# Open http://localhost:8080 (HTTP_PORT changes it)
```

- Three containers: Postgres, the backend, nginx. Only nginx has a port on the host.
- The backend runs as a normal user with `APP_ENV=production`: JSON logs, and no `/docs` or `/openapi.json`.
- nginx's address is fixed (172.30.57.10) and is the only one whose `X-Forwarded-For` the backend believes.
- `backend/.env` supplies `SECRET_KEY` and `DEFAULT_ADMIN_PASSWORD`. The connector is reached at
  `host.docker.internal:5001` unless `MT5_CONNECTOR_URL` is set in the root `.env`.
- `data_archive/` is mounted into the backend for the Historical Lab and price sync.

### Linux server (systemd)
`deploy/impulse-analyst.service` runs `run.py` as the `impulse` account on 127.0.0.1:8002,
behind nginx on the same machine. Render was dropped in step 11: it was never used.

### Dependencies
`backend/requirements.in` is the hand-edited list. `requirements.txt` is generated from it with
every version exact and hashed, and is what Docker installs. `requirements-dev.txt` adds the test
tools at the same versions. The commands to regenerate both are at the top of each `.in` file.
`backend/uv.toml` lets uv read both indexes (PyPI and PyTorch's CPU builds); the hashes make that safe.

## RAG & Self-Improvement Architecture

See `docs/RAG_ARCHITECTURE.md` for the full 5-phase plan:

1. **Strategy Scoreboard** — Aggregate win rate per strategy prompt
2. **Vector Embeddings** — sentence-transformers for semantic search
3. **RAG Context Injection** — Inject past winning analyses into AI prompts
4. **Autopilot Smart Selection** — Pick best-performing prompts, not random
5. **Feedback Dashboard** — Visualize strategy performance

Status: phases 1 to 4 exist. The scoreboard updates hourly (`strategy_scorer.py`),
embeddings and retrieval run in the AI Analyst chat (`rag_service.py`), and the
autopilot weights prompts by regime fit and scoreboard results. Phase 5 is partly
covered by the Reports page. The autopilot does not use retrieval.
