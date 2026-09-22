# Quick start

The short version lives in the [README](../README.md). In brief:

1. Create `backend/.env` from `backend/.env.example` and set `SECRET_KEY` and `DEFAULT_ADMIN_PASSWORD`.
2. Start the backend from `backend/` with `.venv/bin/python run.py`. It listens on port 8002.
3. Start the frontend from `frontend/` with `npm run dev`, then open http://localhost:5173.
4. Log in as `admin` with the password from step 1.

There is no default password. For trading, see [MT5_CONNECTOR.md](MT5_CONNECTOR.md).
