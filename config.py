"""Application settings, read from environment variables (or a local .env file)."""
import os

from dotenv import load_dotenv

load_dotenv()


class Config:
    # MongoDB Atlas (recommended) or local MongoDB, e.g. mongodb://localhost:27017
    MONGODB_URI = os.getenv("MONGODB_URI", "")
    MONGODB_DB = os.getenv("MONGODB_DB", "medikiosk")

    # Used by Flask to sign the session cookie (flash messages + CSRF token).
    SECRET_KEY = os.getenv("SECRET_KEY") or os.urandom(32).hex()

    # Default number of days that counts as "expiring soon" (can be changed per page in the UI).
    EXPIRY_SOON_DAYS = int(os.getenv("EXPIRY_SOON_DAYS", "30"))

    # Timezone used to display transaction timestamps (stored in UTC).
    DISPLAY_TZ = os.getenv("DISPLAY_TZ", "Asia/Kolkata")

    CSRF_ENABLED = True
