"""
Configuration module for Ultimate Telegram File Store & Share Bot.
Loads environment variables and defines application constants.
"""

import os
from typing import Dict, List
from dotenv import load_dotenv

# Load environment variables from .env file if present
load_dotenv()

# Bot Information Constants
CREATOR: str = "@YorichiiPrime"
FEDERATION: str = "@YoriFederation"
OWNER_ID: int = 7728424218

# Telegram & Webhook Configuration
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "https://your-app.vercel.app/api/webhook")

# MongoDB Atlas Configuration
MONGODB_URI: str = os.getenv(
    "MONGODB_URI",
    "mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority"
)

# Admin Configuration
_admin_raw: str = os.getenv("ADMIN_IDS", str(OWNER_ID))
ADMIN_IDS: List[int] = [
    int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit()
]
if OWNER_ID not in ADMIN_IDS:
    ADMIN_IDS.append(OWNER_ID)

# Share Link Expiry Options (Duration in seconds)
EXPIRY_OPTIONS: Dict[str, int] = {
    "1h": 3600,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
    "7d": 604800,
    "30d": 2592000,
}

# UI & Pagination Settings
FILES_PER_PAGE: int = 5
MAX_SEARCH_RESULTS: int = 10
RATE_LIMIT_SECONDS: float = 0.5
