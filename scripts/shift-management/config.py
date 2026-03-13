"""Shift management system configuration."""
import os
from pathlib import Path

# Load .env file if present
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

# --- kintone ---
KINTONE_DOMAIN = os.environ.get("KINTONE_DOMAIN", "ny76p.cybozu.com")
KINTONE_USERNAME = os.environ.get("KINTONE_USERNAME", "master420")
KINTONE_PASSWORD = os.environ.get("KINTONE_PASSWORD", "")

# App IDs (set after creation)
KINTONE_SHIFT_WISH_APP_ID = int(os.environ.get("KINTONE_SHIFT_WISH_APP_ID", "0"))
KINTONE_SHIFT_CONFIRMED_APP_ID = int(os.environ.get("KINTONE_SHIFT_CONFIRMED_APP_ID", "0"))
KINTONE_STAFF_MASTER_APP_ID = int(os.environ.get("KINTONE_STAFF_MASTER_APP_ID", "0"))

# --- Google Spreadsheet ---
SHIFT_SPREADSHEET_ID = os.environ.get("SHIFT_SPREADSHEET_ID", "")

# --- Slack ---
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")
SLACK_APP_TOKEN = os.environ.get("SLACK_APP_TOKEN", "")
BOLT_PORT = int(os.environ.get("BOLT_PORT", 3000))
SLACK_SHIFT_CHANNEL = os.environ.get("SLACK_SHIFT_CHANNEL", "#shift-management")
SLACK_ADMIN_ID = os.environ.get("SLACK_ADMIN_ID", "U07UBN61QFN")

# --- LINE Messaging API ---
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.environ.get("LINE_CHANNEL_SECRET", "")

# --- Anthropic (Claude API) ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# --- MF Cloud Attendance ---
MF_ACCESS_TOKEN = os.environ.get("MF_ACCESS_TOKEN", "")
MF_CLIENT_ID = os.environ.get("MF_CLIENT_ID", "")
MF_CLIENT_SECRET = os.environ.get("MF_CLIENT_SECRET", "")

# --- Staff ID ---
STAFF_ID_COLUMN = "staff_id"  # Column A in スタッフマスタ (S001 format)
