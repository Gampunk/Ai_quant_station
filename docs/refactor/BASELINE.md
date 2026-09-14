# Refactor baseline

Recorded 2026-09-14 on branch `refactor/hardening`, before any application code changed.
Every later step must keep these results or improve them.

## Environment

| Part | Version |
|---|---|
| Backend Python | 3.11.16, matches production Docker and Render |
| Node | 24 locally, production builds with 20 |
| PyTorch | CPU-only build |

## Setup, once

```bash
cd backend
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python torch --index-url https://download.pytorch.org/whl/cpu
uv pip install --python .venv/bin/python -r requirements-dev.txt
cd ../mt5_connector
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-test.txt
cd ../frontend && npm ci
```

## Verify, every step

```bash
./scripts/verify.sh
```

## Results

| Check | Result |
|---|---|
| Backend tests | 9 passed, 9 skipped |
| Frontend unit tests | 106 passed |
| Frontend type check | clean |
| Frontend build | ok, single 1.1 MB bundle |
| Frontend lint | **broken**, no ESLint config exists |

Skipped tests need `NVIDIA_API_KEY`, and three of them also need the local parquet archive.
Set the key in your environment to run them.

## Problems found while setting up

| Problem | Impact | Fixed in |
|---|---|---|
| With no `SECRET_KEY`, 17 of 18 backend tests fail with 401 | Auth is unusable without a key. Reproduces the signing key bug | Step 4 |
| `package-lock.json` out of sync with `package.json` | `npm ci` fails, so the production Docker build was broken | This step |
| `openpyxl` missing from requirements | Excel reports crash on a clean install | This step |
| `pytest`, `pytest-asyncio`, `pytest-timeout` missing | Test suite could not run | This step, in `requirements-dev.txt` |
| Lint script has no config | Lint has never run | Step 12 |
| Parquet archive absent and HuggingFace fallback fails | Backtest pages have no data | Data phase |
| Connector pins `MetaTrader5==5.0.45`, which supports Python up to 3.11 | Cannot install on Windows Python 3.14 | Step 2 |

## Lock file repair

Regenerated with npm 10, the version the Docker image uses.
No direct dependency changed version.
27 sub-dependencies inside the test tooling moved by a patch version.
The esbuild used for the app bundle is unchanged.

## Negative control

Breaking one frontend test makes `verify.sh` exit 1 and print `FAIL  frontend unit tests`.
