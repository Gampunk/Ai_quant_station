# Impulse Analyst v2

A quantitative trading platform: a React frontend, a FastAPI backend, and a
separate connector that talks to MetaTrader 5 on Windows.

```
frontend/       React + TypeScript + Vite
backend/        FastAPI, SQLAlchemy, AI providers, autopilot, backtesting
mt5_connector/  Small Windows service wrapping the MetaTrader5 Python package
docs/           Guides. Refactor progress and findings live in docs/refactor/
scripts/        verify.sh runs every check; demo_check.py tests a demo connector
```

## Run it locally

Needs Python 3.11 and Node 20 or later. [`uv`](https://docs.astral.sh/uv/) is the easiest way to get Python 3.11.

**Backend**

```bash
cd backend
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env
```

Edit `backend/.env` and set two values. The server refuses to start without them.

```bash
# a random signing key, at least 32 characters
python3 -c "import secrets; print(secrets.token_hex(32))"
```

- `SECRET_KEY`: paste the value printed above.
- `DEFAULT_ADMIN_PASSWORD`: at least 12 characters. Creates the `admin` account on first start.

Then start it:

```bash
.venv/bin/python run.py
```

It listens on http://localhost:8002. Check it with `curl localhost:8002/health`.

**Frontend**, in a second terminal:

```bash
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173 and log in as `admin` with the password you set.

**More people.** Only `admin` is created automatically. Add others from `backend/`:

```bash
.venv/bin/python create_admin.py --username someone --name "Some One" --role trader
```

Roles are `admin`, `trader` and `viewer`. Only admin and trader can trade or run the autopilot.

## Trading

Trading goes through the MT5 connector. See [docs/MT5_CONNECTOR.md](docs/MT5_CONNECTOR.md).
To try everything without a broker, run the connector against a fake terminal:

```bash
mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001
```

Its test environment is set up as described in [docs/refactor/BASELINE.md](docs/refactor/BASELINE.md).

## Checks

```bash
./scripts/verify.sh
```

Runs the backend, connector and frontend tests, the type check and a production build.
Install the test tools first with `requirements-dev.txt` instead of `requirements.txt`.
Both files are generated, with exact versions: edit `requirements.in` and regenerate (see the top of that file).

## More

- [docs/HOW_TO_RUN.md](docs/HOW_TO_RUN.md): detailed run guide and troubleshooting
- [docs/MT5_CONNECTOR.md](docs/MT5_CONNECTOR.md): connector setup and security settings
- [docs/refactor/](docs/refactor/): what has changed, what was found, and what is next
- [AGENTS.md](AGENTS.md): architecture, pages and API routes in detail
