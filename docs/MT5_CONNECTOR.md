# MT5 connector

A small service that runs on Windows next to the MetaTrader 5 terminal and
exposes it over HTTP. It lets the backend run on Linux or in Docker. It is the
only component that talks to the broker.

```
┌──────────────────────┐   HTTP + token   ┌──────────────────────┐
│  Backend             │ ───────────────► │  MT5 connector       │
│  Linux, Docker, WSL  │                  │  Windows, port 5001  │
└──────────────────────┘                  └──────────┬───────────┘
                                                      │
                                                      ▼
                                           ┌──────────────────────┐
                                           │  MetaTrader 5        │
                                           └──────────────────────┘
```

## Install, on Windows

Python 3.11 to 3.14. MetaTrader 5 installed, logged in, with **Algo Trading** switched on.

```powershell
py -m venv mt5-venv
mt5-venv\Scripts\python.exe -m pip install -r mt5_connector\requirements.txt
```

## Settings

All settings are environment variables. There are no command-line options.

| Variable | Default | What it does |
|---|---|---|
| `MT5_CONNECTOR_PORT` | asks at startup | Port to listen on. Set it to skip the prompt, for example `5001` |
| `MT5_CONNECTOR_HOST` | `127.0.0.1` | Address to listen on. The default accepts connections from this machine only |
| `MT5_API_TOKEN` | none | **Required.** Every request must send it as `Authorization: Bearer <token>`. Without one, every request is refused with 503 |
| `MT5_ALLOW_NO_TOKEN` | `false` | Run without a token. Only for an instance nothing else can reach |
| `MT5_REQUIRE_DEMO` | `true` | Refuse order, close and modify unless the account is a demo account. Set `false` only for a deliberate live deployment |
| `MT5_MAX_VOLUME` | `1.0` | The largest order it sends, in lots. Larger orders are refused with 403, whatever the backend asks. The backend's own risk limits sit in front of this |
| `MT5_ENABLE_DOCS` | `false` | Serve the interactive `/docs` page, which can place orders. Keep it off anywhere reachable |
| `MT5_TERMINAL_PATH` | none | Path to `terminal64.exe` when several terminals are installed |
| `CORS_ORIGINS` | local dev ports | Browser origins allowed to call it |

## Start

```powershell
$env:MT5_CONNECTOR_PORT = "5001"
$env:MT5_API_TOKEN = [guid]::NewGuid().ToString("N")
$env:MT5_API_TOKEN          # copy this value for the backend
mt5-venv\Scripts\python.exe mt5_connector\connector.py
```

Expect the account, `Demo-only trading guard: ON`, `API token: required` and `Docs page: disabled`.

## Point the backend at it

In `backend/.env`:

```env
MT5_CONNECTOR_URL=http://127.0.0.1:5001
MT5_API_TOKEN=<the same token>
```

The backend refuses a connector address outside local and private networks,
at startup and on every request. For a connector on another network, set
`ALLOW_REMOTE_CONNECTOR=true` in `backend/.env` deliberately.

## Separate servers

When the backend and connector are on different machines:

1. Put both on a private network or tunnel, such as Tailscale or WireGuard. The connector speaks plain HTTP, so the token is readable on an open network.
2. Set `MT5_CONNECTOR_HOST` to the connector machine's private address, not `0.0.0.0`.
3. Allow the port in the Windows firewall for the backend's address only.
4. Set `ALLOW_REMOTE_CONNECTOR=true` in `backend/.env` if the address is not in a private range.

## Test it

On the connector machine:

```powershell
curl.exe -H "Authorization: Bearer <token>" http://127.0.0.1:5001/health
```

Expect `{"status":"healthy","mt5_connected":true,...}`. Without the header, expect 401.

From the backend side, `scripts/demo_check.py` runs a read-only check, and with
`--trade` places and closes one 0.01 lot position on a demo account.

## Without a broker

The real `connector.py` can run against a fake terminal on any OS, with no MT5:

```bash
mt5_connector/.venv/bin/python mt5_connector/testing/run_fake_connector.py --port 5001
```

It needs no token unless you pass `--token`. The connector's own tests use it:

```bash
mt5_connector/.venv/bin/python -m pytest mt5_connector/tests -q
```

## Troubleshooting

| What you see | Cause and fix |
|---|---|
| 503, no `MT5_API_TOKEN` | The connector started without a token. Set one and restart |
| 401 | The backend's token differs from the connector's, or the header is missing |
| 403, not a demo account | MT5 is logged into a real account. Log into demo. Do not disable the guard for testing |
| Order fails mentioning AutoTrading | Switch on Algo Trading in the MT5 toolbar |
| 404 for a symbol | The broker names it differently. Check Market Watch |
| `MT5 not initialized` | The terminal is closed, or several are installed and `MT5_TERMINAL_PATH` is unset |
