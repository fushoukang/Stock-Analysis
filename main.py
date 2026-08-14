"""
Entry point. Starts the FastAPI web server (which itself starts the Alpaca
live data stream on startup — see web/app.py).

Usage:
    python main.py
"""
from __future__ import annotations

import uvicorn

from config import settings

if __name__ == "__main__":
    uvicorn.run("web.app:app", host=settings.host, port=settings.port, reload=False)
