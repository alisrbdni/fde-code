"""Local entry point: load ../.env, then expose the ASGI app.

    cd api && python3 -m uvicorn app:app --port 8080 --interface asgi3
"""
import os

from function.config import REPO_DIR, load_dotenv

load_dotenv(os.path.join(REPO_DIR, ".env"))

from function import new  # noqa: E402  (settings are read after .env loads)

app = new().handle
