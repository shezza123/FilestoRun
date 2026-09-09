import os
import pytz
from dotenv import load_dotenv


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load API key from .env file
load_dotenv(os.path.join(BASE_DIR, ".env"))


class Config:
    # API Keys & Secrets
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
    GOOGLE_CSE_API_KEY = (
        os.getenv("GOOGLE_CSE_API_KEY")
        or os.getenv("API_KEY_1")
        or os.getenv("API_KEY_2")
    )
    GOOGLE_CSE_ID = (
        os.getenv("GOOGLE_CSE_ID")
        or os.getenv("CSE_ID_1")
        or os.getenv("CSE_ID_2")
    )

    # External API Endpoints
    NEWSQUAWK_API = "https://newsquawk.com/headlines.json?code=X9Vnzz4uUKgG72s6QAxE"

    # Localization & Time
    LONDON_TZ = pytz.timezone("Europe/London")

    # Persistence
    SAFE_TERMS_FILE = os.path.join(BASE_DIR, "safe_terms.txt")
    UNKNOWN_TOKEN_STATS_FILE = os.path.join(BASE_DIR, "unknown_token_stats.json")
    KNOWN_MISSPELLINGS_FILE = os.path.join(BASE_DIR, "known_misspellings.json")
    MISSED_ERRORS_FILE = os.path.join(BASE_DIR, "missed_errors.jsonl")

    # Context Enrichment
    CONTEXT_ENABLED = True
    CONTEXT_TTL_SECONDS = 86400
    CONTEXT_MAX_RESULTS = 8
    CONTEXT_LOADING_TIMEOUT_SECONDS = 120
    CONTEXT_AUTO_FETCH_ROWS = 10
    CONTEXT_WORKERS = 4

    # Whitelist of valid proper nouns (names, positions, tickers, etc.)
    APPROVED_TERMS = {
        'Bessent', 'Powell', 'Trump', 'Lagarde', 'Xi', 'iPhone', 'RFK Jr.'
    }

    # Finance-safe vocabulary for pre-spellcheck
    FINANCIAL_TERMS = {
        'pmi', 'gdp', 'cpi', 'ppi', 'ecb', 'boj', 'boe', 'fed', 'fomc', 'nfp', 'ism', 'yoy', 'y/y',
        'mom', 'm/m', 'qoq', 'q/q', 'ytd', 'repo', 'reverse', 'swap', 'spread', 'yield', 'bond',
        'equity', 'fx', 'eur', 'usd', 'gbp', 'jpy', 'cny', 'chf', 'aud', 'cad', 'nzd', 'hkd', 'sgd',
        'zar', 'brl', 'inr', 'rba', 'pboc', 'bid', 'ask', 'bull', 'bear', 'hawkish', 'dovish', 'mln',
        'bln', 'tln', 'prev', 'corp', 'ipo', 'etf', 'nav', 'bps', 'mfg', 'capex', 'opex', 'fedfunds',
        'dow', 'nasdaq', 's&p', 'spx', 'vix', 'nikkei', 'ftse', 'dax', 'long', 'short', 'curve'
    }


# Startup Verification
if not Config.OPENAI_API_KEY:
    raise Exception("❌ OPENAI_API_KEY missing from .env")
