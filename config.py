import os
import json
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "english_bot.db"

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN is not set in .env")

# Parse ADMIN_IDS flexibly (json array like [123, 456] or comma separated '123, 456' or single int)
raw_admins = os.getenv("ADMIN_IDS", "[]").strip()
try:
    if raw_admins.startswith("[") and raw_admins.endswith("]"):
        ADMIN_IDS = [int(x) for x in json.loads(raw_admins)]
    else:
        ADMIN_IDS = [int(x.strip()) for x in raw_admins.split(",") if x.strip()]
except Exception:
    ADMIN_IDS = []

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

# Mini App Web Server settings
WEBAPP_URL = os.getenv("WEBAPP_URL", "").rstrip("/")
WEB_PORT = int(os.getenv("WEB_PORT", "8080"))
WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
WEB_DIR = BASE_DIR / "web"