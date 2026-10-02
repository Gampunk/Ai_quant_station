"""
Start the backend: one process, one worker.

Autopilot state and login throttling live in this process's memory, so a second
worker would run a second autopilot and keep its own login counts. Never add workers.

    PORT     port to listen on, default 8002
    HOST     address to listen on, default 0.0.0.0
"""
import os
import sys

# Add backend folder to path
backend_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, backend_dir)

import uvicorn  # noqa: E402

from app.main import app  # noqa: E402

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8002")),
        workers=1,
        log_level="info",
    )
