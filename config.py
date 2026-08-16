
import os
from dotenv import load_dotenv
 
load_dotenv()
 
# --- Database ---
MONITOR_DB = os.getenv("MONITOR_DB", "monitor.db")
 
# --- Watcher ---
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
USER_AGENT = os.getenv(
    "USER_AGENT", "StealthMonitor/1.0 (+https://github.com/your-repo)"
)
# Where watcher.py POSTs detected changes. Set to your app.py's address.
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://127.0.0.1:9000/webhook/change-detected")
 
# --- Featherless AI (OpenAI-compatible) ---
FEATHERLESS_API_KEY = os.getenv("FEATHERLESS_API_KEY", "")
FEATHERLESS_BASE_URL = os.getenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1")
FEATHERLESS_MODEL=os.getenv("FEATHERLESS_MODEL", "moonshotai/Kimi-K2-Instruct")
# --- Caspian SDK ---
# CASPIAN_API_KEY / CASPIAN_BASE_URL are read directly from env by CommClient()
# itself (per the SDK's own convention), but we keep the recipient + the
# connection id used for proactive sends here.
CASPIAN_API_KEY=os.getenv("CASPIAN_API_KEY", "")
CASPIAN_BASE_URL=os.getenv("CASPIAN_BASE_URL","")

CASPIAN_TELEGRAM_BOT_TOKEN = os.getenv("CASPIAN_TELEGRAM_BOT_TOKEN", "")
ALERT_TELEGRAM_CHAT_ID = os.getenv("ALERT_TELEGRAM_CHAT_ID", "")

#--- Exa API ---
EXA_API_KEY = os.getenv("EXA_API_KEY", "")

#WEBHOOK_URL 
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://127.0.0.1:5000/webhook/change-detected")

# Email: address alerts get sent to.
ALERT_RECIPIENT = os.getenv("ALERT_RECIPIENT", "")
 

