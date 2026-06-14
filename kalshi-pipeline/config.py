import os
from dotenv import load_dotenv

load_dotenv()

# --- Anthropic ---
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# --- Kalshi ---
# Create an API key in the Kalshi dashboard. It gives you a key ID (UUID) and a
# downloadable RSA private key (PEM). Point KALSHI_PRIVATE_KEY_PATH at the PEM.
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_key.pem")

# Production vs demo (paper trading). Start on demo.
KALSHI_ENV = os.getenv("KALSHI_ENV", "demo").lower()
if KALSHI_ENV == "prod":
    KALSHI_HOST = "https://api.elections.kalshi.com/trade-api/v2"
    KALSHI_WS_HOST = "wss://api.elections.kalshi.com/trade-api/ws/v2"
else:
    KALSHI_HOST = "https://demo-api.kalshi.co/trade-api/v2"
    KALSHI_WS_HOST = "wss://demo-api.kalshi.co/trade-api/ws/v2"

# The path prefix that must be included when signing requests.
KALSHI_API_PREFIX = "/trade-api/v2"
KALSHI_WS_PATH = "/trade-api/ws/v2"

# --- Twitter API v2 ---
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")

# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_IDS = [
    c.strip() for c in os.getenv("TELEGRAM_CHANNEL_IDS", "").split(",") if c.strip()
]

# --- NewsAPI (optional, RSS fallback) ---
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")

# --- RSS Feeds (fallback) ---
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=AI+artificial+intelligence&hl=en-US&gl=US&ceid=US:en",
    "https://feeds.feedburner.com/TechCrunch",
    "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "https://www.theverge.com/rss/index.xml",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
]

# --- Pipeline Settings ---
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
MAX_BET_USD = float(os.getenv("MAX_BET_USD", "25"))
DAILY_LOSS_LIMIT_USD = float(os.getenv("DAILY_LOSS_LIMIT_USD", "100"))
EDGE_THRESHOLD = float(os.getenv("EDGE_THRESHOLD", "0.10"))
NEWS_LOOKBACK_HOURS = 6

# --- V2 Settings ---
# NOTE: on Kalshi, volume/open-interest is measured in CONTRACTS, not USD.
# These thresholds bound the "niche market" band by contract count.
MAX_VOLUME_USD = float(os.getenv("MAX_VOLUME", "50000"))   # max contracts
MIN_VOLUME_USD = float(os.getenv("MIN_VOLUME", "50"))      # min contracts
MATERIALITY_THRESHOLD = float(os.getenv("MATERIALITY_THRESHOLD", "0.6"))
SPEED_TARGET_SECONDS = float(os.getenv("SPEED_TARGET_SECONDS", "5"))
CLASSIFICATION_MODEL = "claude-haiku-4-5-20251001"
SCORING_MODEL = "claude-sonnet-4-6-20250514"
# scorer.py (V1) references CLAUDE_MODEL — keep an alias for compatibility.
CLAUDE_MODEL = SCORING_MODEL

# --- Categories to track ---
MARKET_CATEGORIES = [
    "ai",
    "technology",
    "crypto",
    "politics",
    "science",
]

# --- Twitter filter keywords (for filtered stream rules) ---
TWITTER_KEYWORDS = [
    "OpenAI", "GPT-5", "Anthropic", "Claude", "Google AI", "Gemini",
    "Bitcoin", "Ethereum", "Solana", "crypto",
    "Fed rate", "tariff", "Congress", "White House",
    "SpaceX", "Starship", "NASA",
    "Apple", "NVIDIA", "Microsoft", "Google",
]
