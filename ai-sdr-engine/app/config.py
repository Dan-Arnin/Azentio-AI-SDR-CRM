# app/config.py - Environment-driven settings for the SDR engine
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
DATA_DIR = ROOT / "data"


# Load KEY=VALUE pairs from the repo .env without overriding real env vars
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")


# Read an env var with a default
def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


OPENAI_API_KEY = env("OPENAI_API_KEY")
LLM_BACKEND = env("LLM_BACKEND", "api_key" if OPENAI_API_KEY else "chatgpt")
CODEX_AUTH_PATH = Path(env("CODEX_AUTH_PATH", str(Path.home() / ".codex" / "auth.json")))
CHATGPT_BASE_URL = env("CHATGPT_BASE_URL", "https://chatgpt.com/backend-api/codex")

GENERATION_MODEL = env("GENERATION_MODEL", "gpt-6.1-sol")
JUDGE_MODEL = env("JUDGE_MODEL", "gpt-6-luna")
DECISION_MODEL = env("DECISION_MODEL", "gpt-6-luna")
DECISION_PROVIDER = env("DECISION_PROVIDER", "openai_decisions" if OPENAI_API_KEY else "agents")
INTENT_CONFIDENCE_FLOOR = float(env("INTENT_CONFIDENCE_FLOOR", "0.85"))
EVIDENCE_CONFIDENCE_FLOOR = float(env("EVIDENCE_CONFIDENCE_FLOOR", "0.6"))

DB_PATH = Path(env("DB_PATH", str(DATA_DIR / "sdr.sqlite3")))
CRM_WEBHOOK_URL = env("CRM_WEBHOOK_URL", "")
CRM_WEBHOOK_SECRET = env("CRM_WEBHOOK_SECRET", "")
EMAIL_TRANSPORT = env("EMAIL_TRANSPORT", "simulated")
RESEND_API_KEY = env("RESEND_API_KEY")
EMAIL_FROM = env("EMAIL_FROM", "Azentio Sales Development <onboarding@resend.dev>")
DEMO_INBOX = env("DEMO_INBOX", "delivered@resend.dev")
SENDER_NAME = env("SENDER_NAME", "Nadia Karim")
SENDER_TITLE = env("SENDER_TITLE", "Sales Development")
REPLY_TO = env("REPLY_TO", "")
DEMO_MODE = env("DEMO_MODE", "true").lower() == "true"
DEMO_TIMEZONE = env("DEMO_TIMEZONE", "Asia/Kolkata")
POLICY_VERSION = env("POLICY_VERSION", "demo-icp-v2")

DEFAULT_AE_OWNER = env("DEFAULT_AE_OWNER", "ae.sara")
DEFAULT_SDR_OWNER = env("DEFAULT_SDR_OWNER", "sdr.omar")
