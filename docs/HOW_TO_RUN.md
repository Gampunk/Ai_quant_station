# How to run

Three parts: the backend and frontend, which run anywhere, and the MT5 connector,
which runs on Windows next to the MetaTrader 5 terminal.

## 1. Backend

Needs Python 3.11, the version production uses.

```bash
cd backend
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env
```

Without `uv`, `python3.11 -m venv .venv` then `.venv/bin/pip install -r requirements.txt` works too.

Set these in `backend/.env`:

| Setting | Required | Notes |
|---|---|---|
| `SECRET_KEY` | yes | At least 32 characters. Generate with `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `DEFAULT_ADMIN_PASSWORD` | yes, first start | At least 12 characters. Creates `admin` on an empty database |
| `DATABASE_URL` | no | Defaults to a SQLite file, `backend/finance_engine.db` |
| `MT5_CONNECTOR_URL` | for trading | For example `http://127.0.0.1:5001` |
| `MT5_API_TOKEN` | for trading | Must match the connector's token |
| `MT5_USE_EXTERNAL_CONNECTOR` | for trading | `True` routes the MT5 pages through the connector |
| AI provider keys | no | `NVIDIA_API_KEY`, `GROQ_API_KEY` and others. Users can also save their own on the Settings page |

Start it:

```bash
.venv/bin/python run.py
```

Expect `Application startup complete` and, on a new database,
`Admin account created from DEFAULT_ADMIN_PASSWORD`. It listens on port 8002.

```bash
curl localhost:8002/health
```

Expect `{"status":"healthy"}`.

## 2. Frontend

```bash
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173. The dev server forwards `/api` to the backend on port 8002.

## 3. Accounts

Only `admin` is created automatically. Add or reset accounts from `backend/`. The tool asks for the password:

```bash
.venv/bin/python create_admin.py --username someone --name "Some One" --role trader
.venv/bin/python create_admin.py --username someone --reset
```

| Role | Can |
|---|---|
| admin | everything, including managing users |
| trader | trade, run the autopilot, and use anything that runs AI-written code |
| viewer | read dashboards, history and reports |

Five wrong passwords lock an account for 15 minutes. Restarting the backend clears it.

## 4. MT5 connector

See [MT5_CONNECTOR.md](MT5_CONNECTOR.md). For a step-by-step setup on your own
Windows machine against a demo account, see [refactor/LOCAL_DEMO_SETUP.md](refactor/LOCAL_DEMO_SETUP.md).

## Checks

From the repository root, after installing `backend/requirements-dev.txt`:

```bash
./scripts/verify.sh
```

## Troubleshooting

| What you see | Fix |
|---|---|
| `SECRET_KEY is not set` or `is only N characters` | Set a strong `SECRET_KEY` in `backend/.env` |
| `No admin account created` in the log | `DEFAULT_ADMIN_PASSWORD` is missing, short, or a published one. Fix it, delete the database file if it has no data you need, and restart. Or run `create_admin.py` |
| Login answers 429 | Too many attempts. Wait a minute, or 15 minutes for a locked account, or restart the backend |
| Trading answers 403 | The account is a viewer. Trading needs admin or trader |
| `ConnectorAddressBlocked` | `MT5_CONNECTOR_URL` is not a local or private address. Set `ALLOW_REMOTE_CONNECTOR=true` only if that is intended |
| Connector answers 401 or 503 | The tokens do not match, or the connector has none. See MT5_CONNECTOR.md |
