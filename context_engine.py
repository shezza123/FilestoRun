import json
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from openai import OpenAI

from config import Config


client = OpenAI(api_key=Config.OPENAI_API_KEY)

NEWSQUAWK_WEB_URL = "https://newsquawk.com/headlines"
SEARCH_CACHE_TTL_SECONDS = 900
SEARCH_CACHE = {}
SEARCH_CACHE_LOCK = threading.Lock()

NOISE_WORDS = {
    "reports", "report", "says", "said", "news", "update", "headline", "body",
    "this", "that", "with", "from", "into", "over", "under", "after", "before",
    "while", "about", "their", "there", "where", "which", "would", "could",
    "should", "likely", "possibly", "expects", "expect", "according", "sources",
    "daily", "european", "equity", "opening", "market", "wrap", "asia", "pac",
    "us", "newsquawk",
}

EVENT_TERMS = {
    "talk", "talks", "meeting", "meetings", "dialogue", "summit", "visit",
    "minister", "ministry", "leaders", "leader", "ceasefire", "agreement",
    "sanctions", "sanction", "tariffs", "tariff", "war", "peace", "aid",
    "exports", "imports", "energy", "inflation", "rates", "treasury", "bond",
    "guidance", "earnings", "forecast", "acquisition", "merger", "cut", "raised",
    "holding", "holdings", "stake", "position", "buy", "sell", "exit",
}

TITLE_STOPWORDS = {
    "prime", "premier", "president", "chancellor", "governor", "minister", "leader",
    "vice", "deputy", "state", "foreign", "central", "bank", "chair", "chief", "pm", "fm",
}

MONTH_ORDER = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

COUNTRY_VARIANTS = {
    "china": ["china", "chinese"],
    "germany": ["germany", "german"],
    "iran": ["iran", "iranian"],
    "hungary": ["hungary", "hungarian"],
    "ukraine": ["ukraine", "ukrainian"],
    "russia": ["russia", "russian"],
    "poland": ["poland", "polish"],
    "japan": ["japan", "japanese"],
    "france": ["france", "french"],
    "italy": ["italy", "italian"],
    "spain": ["spain", "spanish"],
    "uk": ["uk", "britain", "british", "united kingdom"],
    "united states": ["us", "u.s.", "usa", "united states", "american"],
}

WEAK_ANCHOR_WORDS = {
    "jan", "january", "feb", "february", "mar", "march", "apr", "april", "may", "jun", "june",
    "jul", "july", "aug", "august", "sep", "sept", "september", "oct", "october", "nov", "november", "dec", "december",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "today", "tomorrow", "yesterday",
}

CURRENCY_CODES = {"usd", "eur", "gbp", "jpy", "cny", "chf", "aud", "cad", "nzd", "twd", "hkd", "sgd"}

UPPER_TOKEN_EXCLUDE = {
    "US", "EU", "UK",
    "PM", "FM", "DM", "CM", "MP", "MS",
    "LN", "TT", "HK", "SW", "NA", "DE", "FP", "SM", "MI", "LS", "PA", "ST",
    "FY", "YTD", "YOY", "MOM", "QOQ", "QTD", "NET", "EXP", "PREV",
    "EPS", "EBIT", "EBITDA", "EARNINGS", "REVENUE", "GUIDANCE", "SSS", "ADJ",
}


def _tag_names_from_headline_tags(headline_tags):
    out = []
    seen = set()
    for entry in headline_tags or []:
        name = ""
        if isinstance(entry, str):
            name = entry
        elif isinstance(entry, dict):
            name = (entry.get("name") or "").strip()
            if not name and isinstance(entry.get("tag"), dict):
                name = (entry.get("tag", {}).get("name") or "").strip()
        if not name:
            continue
        key = re.sub(r"\s+", " ", name).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def has_excluded_context_tag(headline_tags):
    for tag_name in _tag_names_from_headline_tags(headline_tags):
        if "research" in tag_name:
            return True
        if "market update" in tag_name:
            return True
        if "market analysis" in tag_name:
            return True
        if "us broker moves" in tag_name:
            return True
        if "european movers" in tag_name:
            return True
    return False


def _is_digest_or_opening_headline(headline):
    s = (headline or "").strip()
    if not s:
        return False
    patterns = [
        r"(?i)^newsquawk\s+daily\s+.*opening\s+news",
        r"(?i)^daily\s+.*opening\s+news",
        r"(?i)^newsquawk\s+.*market\s+wrap",
        r"(?i)^newsquawk\s+daily\s+asia-pac\s+opening\s+news",
    ]
    return any(re.search(p, s) for p in patterns)


def _has_excluded_context_marker(headline):
    text = (headline or "").strip().lower()
    if not text:
        return False
    if any(
        marker in text
        for marker in (
            "[market analysis]",
            "[market update]",
            "[research",
            "[european movers]",
        )
    ):
        return True
    if re.search(r"\bus\s+broker\s+moves\b", text):
        return True
    if re.search(r"\beuropean\s+movers\b", text):
        return True
    if _is_digest_or_opening_headline(headline):
        return True
    return False


def is_context_excluded(headline, headline_tags):
    return has_excluded_context_tag(headline_tags or []) or _has_excluded_context_marker(headline)


def context_exclusion_reason(headline, headline_tags):
    if has_excluded_context_tag(headline_tags or []) or _has_excluded_context_marker(headline):
        return "Historical context disabled for Research/Market Update/Market Analysis/US Broker Moves/European Movers/Opening-Wrap digest headlines."
    return ""


def _clean_text(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _is_weak_anchor_token(token):
    tok = (token or "").strip().lower().strip(".")
    if not tok:
        return True
    if tok in WEAK_ANCHOR_WORDS:
        return True
    if re.fullmatch(r"q[1-4]", tok):
        return True
    if re.fullmatch(r"fy\d{2,4}", tok):
        return True
    if re.fullmatch(r"\d{1,2}(st|nd|rd|th)", tok):
        return True
    return False


def _is_weak_anchor_phrase(phrase):
    parts = [p.strip().lower() for p in re.findall(r"[A-Za-z0-9']+", phrase or "")]
    if not parts:
        return True
    if all(_is_weak_anchor_token(p) for p in parts):
        return True
    # Avoid date-like phrases such as "March Meeting".
    if len(parts) <= 3 and _is_weak_anchor_token(parts[0]) and any(p in {"meeting", "summit", "session"} for p in parts[1:]):
        return True
    return False


def _normalize_headline(text):
    text = _clean_text(text)
    text = re.sub(r"(?i)\s*-\s*(bloomberg|reuters|ap|wsj|ft|axios)\s+news\s*$", "", text)
    text = re.sub(r"(?i)^\[[^\]]+\]\s*", "", text)
    text = re.sub(r"(?i)newsquawk daily .*? opening news\s*-?\s*", "", text)
    return text[:280]


def _normalize_body(text):
    return _clean_text(text)[:800]


def build_context_key(headline, body):
    return f"{_normalize_headline(headline)} || {_normalize_body(body)}"


def _extract_anchors(headline, body):
    text = f"{_normalize_headline(headline)} {_normalize_body(body)}"
    raw = []

    # Multi-word names first
    for m in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b", text):
        token = m.group(1).strip()
        if token.lower() in NOISE_WORDS:
            continue
        if _is_weak_anchor_phrase(token):
            continue
        raw.append(token)

    for m in re.finditer(r"\b([A-Z][a-z]{2,}|[A-Z]{2,8})\b", text):
        token = m.group(1).strip()
        if token.lower() in NOISE_WORDS:
            continue
        if _is_weak_anchor_token(token):
            continue
        if token.upper() in UPPER_TOKEN_EXCLUDE:
            continue
        if token.lower() in CURRENCY_CODES:
            continue
        raw.append(token)

    out = []
    seen = set()
    for r in raw:
        k = r.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out[:10]


def _extract_event_terms(headline):
    words = re.findall(r"[a-z]{4,}", _normalize_headline(headline).lower())
    out = []
    seen = set()
    for w in words:
        if w in EVENT_TERMS and w not in seen:
            seen.add(w)
            out.append(w)
    return out[:8]


def _extract_counterparty_terms(headline):
    text = _normalize_headline(headline)
    if not text:
        return []

    m = re.search(
        r"(?i)\b(?:meeting|meet(?:s|ing)?|talks?|dialogue|summit|visit(?:s|ing)?)\b[^\n]{0,80}?\bwith\b\s+([^,;:\-\.]{3,120})",
        text,
    )
    if not m:
        return []

    segment = m.group(1).strip()
    if not segment:
        return []

    tokens = []
    seen = set()
    for tok in re.findall(r"[A-Za-z]{3,}", segment):
        low = tok.lower()
        if low in NOISE_WORDS or low in TITLE_STOPWORDS:
            continue
        if low in seen:
            continue
        seen.add(low)
        tokens.append(low)
    return tokens[:4]


def _dedupe_terms(terms, limit=None):
    out = []
    seen = set()
    for term in terms or []:
        t = re.sub(r"\s+", " ", str(term or "")).strip()
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
        if isinstance(limit, int) and len(out) >= limit:
            break
    return out


def _month_prev_labels(month, count=2):
    if not month:
        return []
    m = month.strip().lower()[:3]
    if m not in MONTH_ORDER:
        return []
    idx = MONTH_ORDER.index(m)
    out = []
    for i in range(1, count + 1):
        out.append(MONTH_ORDER[(idx - i) % 12].title())
    return out


def _extract_month_metric_terms(headline):
    text = _normalize_headline(headline)
    m = re.search(r"\((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\)", text, flags=re.IGNORECASE)
    if not m:
        return []
    month = m.group(1)
    metric = text[: m.start()].strip(" -:;,.")
    if not metric or len(metric) < 8:
        return []
    prev_months = _month_prev_labels(month, count=2)
    terms = [f"{metric} ({pm})" for pm in prev_months]
    terms.append(metric)
    return _dedupe_terms(terms, limit=4)


def _extract_report_month(headline):
    text = _normalize_headline(headline)
    m = re.search(r"\((Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\)", text, flags=re.IGNORECASE)
    if not m:
        return ""
    return m.group(1).title()


def _extract_company_anchor(headline):
    text = _normalize_headline(headline)
    m = re.match(r"^([A-Z][A-Za-z0-9&\.\- '\u2019]{2,}?)\s*\([^)]+\)", text)
    if not m:
        return ""
    company = re.sub(r"\s+", " ", m.group(1)).strip()
    if len(company) < 3:
        return ""
    return company


def _extract_primary_ticker(headline):
    text = _normalize_headline(headline)
    m = re.search(r"\(([A-Z]{1,6})(?:\s+[A-Z]{1,3})?\)", text)
    if not m:
        return ""
    tok = (m.group(1) or "").strip().upper()
    if tok in UPPER_TOKEN_EXCLUDE or tok in {"USD", "EUR", "GBP", "JPY", "TWD"}:
        return ""
    return tok


def _extract_prior_quarter_terms(headline, company, ticker):
    text = _normalize_headline(headline)
    m = re.search(r"\bQ([1-4])\b", text, flags=re.IGNORECASE)
    if not m:
        return []
    q = int(m.group(1))
    priors = [((q - 2 - i) % 4) + 1 for i in range(2)]
    base = company or ticker
    if not base:
        return []
    return [f"{base} Q{priors[0]}", f"{base} Q{priors[1]}"]


def _company_must_group(company, ticker):
    group = []
    if company:
        group.append(company.lower())
        simplified = re.sub(r"['\u2019]", "", company.lower())
        if simplified != company.lower():
            group.append(simplified)
        parts = [p.lower() for p in re.findall(r"[A-Za-z]{3,}", company)[:2]]
        group.extend(parts)
    if ticker:
        group.append(ticker.lower())
    return _dedupe_terms(group, limit=6)


def _is_earnings_style_headline(headline):
    low = _normalize_headline(headline).lower()
    if re.search(r"\bq[1-4]\b", low) and any(t in low for t in ("eps", "revenue", "guides", "guidance", "adj.")):
        return True
    if "fy" in low and any(t in low for t in ("eps", "revenue")):
        return True
    return False


def _extract_upper_tokens(headline, body):
    text = f"{_normalize_headline(headline)} {_normalize_body(body)}"
    out = []
    seen = set()
    for tok in re.findall(r"\b[A-Z]{2,6}\b", text):
        low = tok.lower()
        if low in seen or low in CURRENCY_CODES:
            continue
        if tok in UPPER_TOKEN_EXCLUDE:
            continue
        if re.fullmatch(r"Q[1-4]", tok):
            continue
        seen.add(low)
        out.append(tok)
    return out[:8]


def _extract_country_hits(headline, body):
    text = f"{_normalize_headline(headline)} {_normalize_body(body)}".lower()
    hits = []
    for canon, variants in COUNTRY_VARIANTS.items():
        if any(v in text for v in variants):
            hits.append(canon)
    return hits[:4]


def _extract_bilateral_terms(headline, body):
    text = _normalize_headline(headline).lower()
    if not re.search(r"\b(meeting|meet|talks?|summit|dialogue|visit)\b", text):
        return [], []
    country_hits = _extract_country_hits(headline, body)
    if len(country_hits) < 2:
        return [], []
    a = country_hits[0]
    b = country_hits[1]
    a_terms = COUNTRY_VARIANTS.get(a, [a])
    b_terms = COUNTRY_VARIANTS.get(b, [b])
    primary = [f"{a_terms[0]} {b_terms[0]}", f"{a_terms[0]} {b_terms[0]} meeting"]
    must_groups = [a_terms, b_terms]
    return _dedupe_terms(primary, limit=3), must_groups


def _extract_geopolitical_terms(headline, body, anchors):
    low = _normalize_headline(headline).lower()
    if not any(
        k in low
        for k in ("says", "security", "troop", "military", "attack", "war", "energy", "sanction", "prepared")
    ):
        return [], [], []

    country_hits = _extract_country_hits(headline, body)
    if not country_hits:
        return [], [], []

    primary = []
    must_groups = []
    focus = []

    if len(country_hits) >= 2:
        a_terms = COUNTRY_VARIANTS.get(country_hits[0], [country_hits[0]])
        b_terms = COUNTRY_VARIANTS.get(country_hits[1], [country_hits[1]])
        primary.append(f"{a_terms[0]} {b_terms[0]}")
        must_groups = [a_terms, b_terms]
        focus.extend(a_terms[:2])
        focus.extend(b_terms[:2])
    else:
        c_terms = COUNTRY_VARIANTS.get(country_hits[0], [country_hits[0]])
        primary.append(c_terms[0])
        focus.extend(c_terms[:2])

    people = []
    for a in anchors[:6]:
        if re.fullmatch(r"[A-Z][a-z]{2,}", a.strip()):
            people.append(a)
    if people:
        primary.append(people[0])
        focus.append(people[0].lower())

    return _dedupe_terms(primary, limit=4), must_groups, _dedupe_terms(focus, limit=6)


def _extract_bond_auction_terms(headline):
    text = _normalize_headline(headline)
    m = re.search(r"\b([A-Z][a-z]+)\s+sells\b", text)
    if not m:
        return []
    issuer = m.group(1).strip()
    return [f"{issuer} sells", issuer]


def _extract_central_bank_speaker_terms(headline):
    text = _normalize_headline(headline)
    m = re.search(r"\b([A-Z]{2,8})\s+Governor\s+([A-Z][a-z]+)\b", text)
    if not m:
        return []
    inst = m.group(1).strip()
    person = m.group(2).strip()
    return [f"{inst} Governor {person}", person, inst]


def _extract_policy_actor_topic_terms(headline, body):
    text = _normalize_headline(headline)
    low = text.lower()
    terms = []
    if "commerce ministry" in low and ("chip" in low or "semiconductor" in low):
        terms.extend([
            "China Commerce Ministry chip research",
            "China Commerce Ministry chip design",
            "MOFCOM chip research",
            "China semiconductors",
        ])
    return _dedupe_terms(terms, limit=6)


def _is_pboc_usdcny_fixing_headline(headline):
    low = _normalize_headline(headline).lower()
    if "pboc" not in low:
        return False
    if "usd/cny" not in low and "usdcny" not in low:
        return False
    if not any(term in low for term in ("mid-point", "midpoint", "fixing", "fix")):
        return False
    return True


def _pboc_fixing_query_plan():
    terms = [
        "PBoC USD/CNY mid-point",
        "PBoC sets USD/CNY mid-point",
        "PBoC is expected to set USD/CNY mid-point",
        "USD/CNY mid-point",
    ]
    return {
        "mode": "pboc_usdcny_fixing",
        "primary_terms": terms,
        "fallback_terms": [],
        "focus_terms": ["pboc", "usd/cny", "cny", "mid-point", "midpoint", "fixing"],
        "must_groups": [
            ["pboc"],
            ["usd/cny", "usdcny", "cny"],
            ["mid-point", "midpoint", "fixing", "fix"],
        ],
        "query_preview": terms,
    }


def _is_pboc_fixing_item(item):
    blob = _item_text_blob(item)
    if "pboc" not in blob:
        return False
    if not any(term in blob for term in ("usd/cny", "usdcny", "cny")):
        return False
    if not any(term in blob for term in ("mid-point", "midpoint", "fixing", "fix")):
        return False
    return True


def _build_query_plan(headline, body):
    if _is_pboc_usdcny_fixing_headline(headline):
        return _pboc_fixing_query_plan()

    generic_terms = _keyword_candidates(headline, body)
    anchors = _extract_anchors(headline, body)
    keywords = _extract_keywords(headline, body)
    upper_tokens = _extract_upper_tokens(headline, body)

    primary_terms = []
    fallback_terms = []
    focus_terms = []
    must_groups = []

    month_metric_terms = _extract_month_metric_terms(headline)
    if month_metric_terms:
        primary_terms.extend(month_metric_terms)
        focus_terms.extend([t for t in keywords if t in {"inflation", "rate", "core", "final", "cpi", "ppi"}])

    company = _extract_company_anchor(headline)
    ticker = _extract_primary_ticker(headline)
    if company:
        primary_terms.append(company)
        focus_terms.append(company.lower())
    if ticker:
        primary_terms.append(ticker)
        focus_terms.append(ticker.lower())

    prior_quarter_terms = _extract_prior_quarter_terms(headline, company, ticker)
    if prior_quarter_terms:
        primary_terms.extend(prior_quarter_terms)

    if _is_earnings_style_headline(headline):
        company_group = _company_must_group(company, ticker)
        if company_group:
            must_groups.append(company_group)

    geop_terms, geop_groups, geop_focus = _extract_geopolitical_terms(headline, body, anchors)
    if geop_terms:
        primary_terms.extend(geop_terms)
    if geop_groups:
        must_groups.extend(geop_groups)
    if geop_focus:
        focus_terms.extend(geop_focus)

    if upper_tokens:
        # Keep high-signal abbreviations like IRGC, RBA, ASML.
        primary_terms.extend(upper_tokens[:2])
        fallback_terms.extend(upper_tokens[2:5])
        focus_terms.extend([u.lower() for u in upper_tokens[:4]])

    bilateral_terms, bilateral_groups = _extract_bilateral_terms(headline, body)
    if bilateral_terms:
        primary_terms.extend(bilateral_terms)
        must_groups.extend(bilateral_groups)
        for group in bilateral_groups:
            focus_terms.extend(group[:2])

    primary_terms.extend(_extract_bond_auction_terms(headline))
    primary_terms.extend(_extract_central_bank_speaker_terms(headline))
    primary_terms.extend(_extract_policy_actor_topic_terms(headline, body))

    # Specific first, broad fallback second.
    fallback_terms.extend(generic_terms)
    if not primary_terms:
        primary_terms.extend(generic_terms[:3])

    return {
        "primary_terms": _dedupe_terms(primary_terms, limit=8),
        "fallback_terms": _dedupe_terms(fallback_terms, limit=10),
        "focus_terms": _dedupe_terms(focus_terms, limit=10),
        "must_groups": [g for g in must_groups if g],
        "query_preview": _dedupe_terms(primary_terms + fallback_terms, limit=8),
    }


def _extract_keywords(headline, body):
    text = f"{_normalize_headline(headline)} {_normalize_body(body)}".lower()
    words = re.findall(r"[a-z]{4,}", text)
    out = []
    seen = set()
    for w in words:
        if w in NOISE_WORDS:
            continue
        if w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out[:12]


def _keyword_candidates(headline, body):
    anchors = _extract_anchors(headline, body)
    events = _extract_event_terms(headline)
    keywords = _extract_keywords(headline, body)

    # Prefer high-signal proper nouns first, then events.
    out = []
    if len(anchors) >= 2:
        pair = []
        for a in anchors:
            if not pair:
                pair.append(a)
                continue
            if a.lower() in pair[0].lower() or pair[0].lower() in a.lower():
                continue
            pair.append(a)
            break
        if len(pair) < 2:
            pair = anchors[:2]
        phrase = f"{pair[0]} {pair[1]}"
        if events:
            phrase = f"{phrase} {events[0]}"
        out.append(phrase)

    for a in anchors:
        if len(out) >= 5:
            break
        out.append(a)
    for e in events:
        if len(out) >= 7:
            break
        if e not in out:
            out.append(e)
    for k in keywords:
        if len(out) >= 8:
            break
        if k not in out:
            out.append(k)
    return out


def _parse_published(item):
    iso = ((item.get("published_at") or {}).get("iso_8601") or "").strip()
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_published_label(item, include_time=True):
    published = _parse_published(item)
    if not published:
        return ""
    fmt = "%Y-%m-%d %H:%M" if include_time else "%Y-%m-%d"
    return published.astimezone(timezone.utc).strftime(fmt)


def _cache_get(keyword, page):
    now = datetime.now(timezone.utc)
    key = (keyword.lower(), int(page))
    with SEARCH_CACHE_LOCK:
        entry = SEARCH_CACHE.get(key)
        if not entry:
            return None
        ts, payload = entry
        if (now - ts).total_seconds() > SEARCH_CACHE_TTL_SECONDS:
            SEARCH_CACHE.pop(key, None)
            return None
        return payload


def _cache_put(keyword, page, payload):
    key = (keyword.lower(), int(page))
    with SEARCH_CACHE_LOCK:
        SEARCH_CACHE[key] = (datetime.now(timezone.utc), payload)


def _search_newsquawk_keyword(keyword, page):
    cached = _cache_get(keyword, page)
    if cached is not None:
        return cached, ""

    params = {
        "search[keyword_query][]": keyword,
        "page": page,
    }
    last_error = ""
    for attempt, timeout_s in enumerate((4, 7)):
        try:
            response = requests.get(Config.NEWSQUAWK_API, params=params, timeout=timeout_s)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                return [], "Newsquawk search returned unexpected payload"
            _cache_put(keyword, page, data)
            return data, ""
        except requests.exceptions.Timeout:
            last_error = "Newsquawk search timed out."
            if attempt == 0:
                time.sleep(0.15)
        except Exception:
            last_error = "Newsquawk search temporarily unavailable."
            if attempt == 0:
                time.sleep(0.15)
    return [], last_error


def _dedupe_by_id(items):
    out = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        hid = item.get("id")
        if not hid:
            continue
        if hid in seen:
            continue
        seen.add(hid)
        out.append(item)
    return out


def _item_text_blob(item):
    parts = [
        item.get("subject", ""),
        item.get("reaction_details", ""),
        item.get("analysis_details", ""),
        item.get("content", ""),
    ]
    return _clean_text(" ".join(parts)).lower()


def _is_digest_subject(subject):
    s = (subject or "").strip()
    if not s:
        return True
    return _is_digest_or_opening_headline(s)


def _score_item(
    item,
    anchors,
    event_terms,
    keywords,
    current_headline,
    counterparty_terms=None,
    focus_terms=None,
    must_groups=None,
    strict_groups=False,
):
    if is_context_excluded(item.get("subject", ""), item.get("headline_tags") or []):
        return -1

    subject = _clean_text(item.get("subject", ""))
    if not subject:
        return -1
    if _is_digest_subject(subject):
        return -1
    if _normalize_headline(subject).lower() == _normalize_headline(current_headline).lower():
        return -1

    blob = _item_text_blob(item)
    counterparty_terms = counterparty_terms or []
    focus_terms = focus_terms or []
    must_groups = must_groups or []

    primary_anchor_hits = sum(1 for a in anchors[:2] if a.lower() in blob)
    anchor_hits = sum(1 for a in anchors[:8] if a.lower() in blob)
    secondary_anchor_hits = sum(1 for a in anchors[2:6] if a.lower() in blob)
    event_hits = sum(1 for e in event_terms[:8] if e in blob)
    keyword_hits = sum(1 for k in keywords[:10] if k in blob)
    focus_hits = sum(1 for f in focus_terms[:10] if f in blob)
    counterparty_hits = sum(1 for t in counterparty_terms[:4] if t in blob)

    group_hits = []
    for group in must_groups:
        match = any(term.lower() in blob for term in group if term)
        group_hits.append(match)
    if must_groups and not any(group_hits):
        return -1
    if strict_groups and group_hits and not all(group_hits):
        return -1

    if anchors and primary_anchor_hits < 1 and focus_hits < 1:
        return -1

    if len(anchors) >= 2 and anchor_hits < 1 and event_hits < 1:
        return -1
    if len(anchors) >= 3 and secondary_anchor_hits < 1 and event_hits < 1:
        return -1
    if event_terms and event_hits < 1 and anchor_hits < 2 and keyword_hits < 2 and focus_hits < 1:
        return -1

    score = anchor_hits * 3 + event_hits * 2 + keyword_hits + focus_hits * 3 + counterparty_hits * 2
    if group_hits:
        score += sum(2 for hit in group_hits if hit)
        score -= sum(2 for hit in group_hits if not hit)

    published = _parse_published(item)
    if published is not None:
        age_hours = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600.0)
        if age_hours <= 12:
            score += 5
        elif age_hours <= 48:
            score += 4
        elif age_hours <= 24 * 7:
            score += 3
        elif age_hours <= 24 * 30:
            score += 2
        elif age_hours <= 24 * 120:
            score += 1

    return score


def _rank_candidates(
    items,
    anchors,
    event_terms,
    keywords,
    current_headline,
    counterparty_terms=None,
    focus_terms=None,
    must_groups=None,
    strict_groups=False,
):
    scored = []
    for item in items:
        score = _score_item(
            item,
            anchors,
            event_terms,
            keywords,
            current_headline,
            counterparty_terms=counterparty_terms,
            focus_terms=focus_terms,
            must_groups=must_groups,
            strict_groups=strict_groups,
        )
        row = dict(item)
        row["_score"] = score
        row["_published"] = _parse_published(item)
        scored.append(row)

    ranked = [r for r in scored if r.get("_score", -1) >= 5]
    if len(ranked) < 10:
        ranked = [r for r in scored if r.get("_score", -1) >= 4]
    if len(ranked) < 8:
        ranked = [r for r in scored if r.get("_score", -1) >= 3]
    if len(ranked) < 5:
        ranked = [r for r in scored if r.get("_score", -1) >= 2]

    ranked.sort(
        key=lambda x: (
            x.get("_published") or datetime.min.replace(tzinfo=timezone.utc),
            x.get("_score", 0),
        ),
        reverse=True,
    )
    return ranked


def _safe_json_parse(text):
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    candidates = [raw]
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        candidates.append(match.group(0))

    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _build_sections_from_ranked(items):
    if not items:
        return None

    sorted_items = sorted(
        items,
        key=lambda x: x.get("_published") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    immediate = []
    seen_subjects = set()
    for item in sorted_items:
        if len(immediate) >= 10:
            break
        subject = _clean_text(item.get("subject", ""))
        if not subject:
            continue
        s_key = subject.lower()
        if s_key in seen_subjects:
            continue
        seen_subjects.add(s_key)
        date_txt = _format_published_label(item, include_time=True)
        immediate.append(f"{date_txt} UTC - {subject}".strip(" -"))

    if not immediate:
        return None

    oldest = ((sorted_items[-1].get("published_at") or {}).get("iso_8601") or "")[:10]
    newest = ((sorted_items[0].get("published_at") or {}).get("iso_8601") or "")[:10]

    return {
        "immediate_context": immediate,
        "historical_arc": [
            f"Related Newsquawk coverage spans from {oldest} to {newest}." if oldest and newest else "Related Newsquawk coverage identified.",
        ],
        "why_now": [
            "Latest related headlines are prioritised first, with older context shown below.",
        ],
    }


def _preview_from_sections(sections):
    preview = []
    for key in ("immediate_context", "historical_arc", "why_now"):
        for line in sections.get(key, []):
            if len(preview) >= 4:
                break
            preview.append(line)
    return preview


def _fallback_sections_from_items(items):
    recent = sorted(
        items,
        key=lambda x: x.get("_published") or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    immediate = []
    historical = []

    for item in recent[:3]:
        ts = _format_published_label(item, include_time=True)
        subject = _clean_text(item.get("subject", ""))
        if subject:
            immediate.append(f"{ts} UTC - {subject}".strip(" -"))

    for item in recent[3:7]:
        ts = _format_published_label(item, include_time=True)
        subject = _clean_text(item.get("subject", ""))
        if subject:
            historical.append(f"{ts} UTC - {subject}".strip(" -"))

    why_now = []
    if immediate:
        why_now.append("Related Newsquawk headlines suggest this theme is recurring and still active.")

    return {
        "immediate_context": immediate,
        "historical_arc": historical,
        "why_now": why_now,
    }


def _build_sources(items):
    out = []
    for item in items[:6]:
        ts = ((item.get("published_at") or {}).get("iso_8601") or "").replace("T", " ").replace("Z", "")
        title = _clean_text(item.get("subject", ""))
        out.append(
            {
                "title": f"{ts} UTC | {title}"[:220],
                "url": NEWSQUAWK_WEB_URL,
            }
        )
    return out


def _web_query_from_anchors(headline, anchors, event_terms):
    parts = []
    for a in anchors[:4]:
        parts.append(a)
    for e in event_terms[:3]:
        if e not in parts:
            parts.append(e)
    if not parts:
        return _normalize_headline(headline)
    return " ".join(parts)[:200]


def _web_query_from_plan(headline, query_plan, anchors, event_terms):
    parts = []
    for term in (query_plan.get("primary_terms") or [])[:3]:
        parts.append(term)
    for term in (query_plan.get("focus_terms") or [])[:3]:
        if term not in parts:
            parts.append(term)
    parts = _dedupe_terms(parts, limit=6)
    if not parts:
        return _web_query_from_anchors(headline, anchors, event_terms)
    return " ".join(parts)[:220]


def _source_relevance_score(text, tokens):
    blob = _clean_text(text).lower()
    if not blob:
        return 0
    score = 0
    for t in tokens[:12]:
        tok = (t or "").strip().lower()
        if tok and tok in blob:
            score += 1
    return score


def _headline_tokens_for_web(headline, body):
    tokens = []
    tokens.extend([a.lower() for a in _extract_anchors(headline, body)[:6]])
    tokens.extend(_extract_keywords(headline, body)[:8])
    return _dedupe_terms(tokens, limit=12)


def _fetch_wikipedia_summary(query, headline_tokens=None):
    if not query:
        return None
    headline_tokens = headline_tokens or []
    try:
        search_url = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "opensearch",
            "search": query,
            "limit": 6,
            "namespace": 0,
            "format": "json",
        }
        res = requests.get(search_url, params=params, timeout=10)
        res.raise_for_status()
        payload = res.json()
        titles = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        if not titles:
            return None

        ranked_titles = []
        for raw_title in titles:
            title = str(raw_title).strip()
            if not title:
                continue
            if "disambiguation" in title.lower():
                continue
            score = _source_relevance_score(title, headline_tokens)
            ranked_titles.append((score, title))
        if not ranked_titles:
            return None
        ranked_titles.sort(key=lambda x: x[0], reverse=True)
        title = ranked_titles[0][1]
        if not title:
            return None

        summary_res = requests.get(
            f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}",
            timeout=10,
        )
        summary_res.raise_for_status()
        s = summary_res.json()
        extract = (s.get("extract") or "").strip()
        page_url = ((s.get("content_urls") or {}).get("desktop", {}) or {}).get("page", "")
        if not extract:
            return None
        return {"title": title, "extract": extract, "url": page_url}
    except Exception:
        return None


def _fetch_gdelt_articles(query, max_records=8, headline_tokens=None):
    if not query:
        return []
    headline_tokens = headline_tokens or []
    params = {
        "query": query,
        "mode": "ArtList",
        "format": "json",
        "maxrecords": max(12, max_records),
        "sort": "DateDesc",
    }
    try:
        res = requests.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, timeout=12)
        res.raise_for_status()
        data = res.json()
    except Exception:
        return []
    out = []
    for a in (data.get("articles") or []):
        title = (a.get("title") or "").strip()
        url = (a.get("url") or "").strip()
        if not title or not url:
            continue
        snippet = (a.get("seendate") or "").strip()
        score = _source_relevance_score(f"{title} {url} {snippet}", headline_tokens)
        if score < 1:
            continue
        out.append(
            {
                "title": title,
                "snippet": snippet,
                "url": url,
                "_score": score,
            }
        )
    out.sort(key=lambda x: x.get("_score", 0), reverse=True)
    trimmed = []
    for item in out[:max_records]:
        row = dict(item)
        row.pop("_score", None)
        trimmed.append(row)
    return trimmed


def _summarise_web_historical(headline, body, wiki, gdelt, related_newsquawk=None):
    source_lines = []
    related_newsquawk = related_newsquawk or []
    if wiki:
        source_lines.append(
            f"WIKI: {wiki.get('title', '')}\nTEXT: {wiki.get('extract', '')}\nURL: {wiki.get('url', '')}"
        )
    for i, g in enumerate(gdelt, start=1):
        source_lines.append(f"G{i}: {g.get('title', '')}\nDATE: {g.get('snippet', '')}\nURL: {g.get('url', '')}")

    if related_newsquawk:
        lines = []
        for item in related_newsquawk[:6]:
            ts = _format_published_label(item, include_time=True)
            subject = _clean_text(item.get("subject", ""))
            if subject:
                lines.append(f"{ts} UTC - {subject}")
        if lines:
            source_lines.append("RELATED NEWSQUAWK:\n" + "\n".join(lines))

    if not source_lines:
        return [], []

    prompt = f"""
Headline: {_normalize_headline(headline)}
Body excerpt: {_normalize_body(body)}

Create concise historical context linked directly to the current headline.
Return JSON only:
{{
  "historical_arc": ["..."],
  "why_now": ["..."]
}}

Rules:
- historical_arc: 2-5 bullets focused on timeline and key prior milestones.
- why_now: 1-3 bullets explaining why this headline matters now.
- Keep tightly relevant to the headline and avoid broad unrelated market chatter.
- Use only information supported by the provided sources.
- If weak evidence, return empty arrays.

Sources:
{"\n\n".join(source_lines)[:10000]}
"""

    try:
        resp = client.with_options(timeout=22.0).responses.create(
            model="gpt-5-nano",
            input=prompt,
            reasoning={"effort": "low"},
            text={"verbosity": "low"},
            max_output_tokens=420,
        )
        parsed = _safe_json_parse(resp.output_text)
        if not parsed:
            return [], []
        historical_arc = [str(x).strip() for x in (parsed.get("historical_arc") or []) if str(x).strip()]
        why_now = [str(x).strip() for x in (parsed.get("why_now") or []) if str(x).strip()]
        return historical_arc[:5], why_now[:3]
    except Exception:
        return [], []


def fetch_historical_context(headline, body, include_web=False, headline_tags=None):
    headline_norm = _normalize_headline(headline)
    if not headline_norm:
        return {
            "status": "none",
            "summary": [],
            "sources": [],
            "error": "",
            "query": "",
            "web_enriched": False,
            "context_allowed": True,
        }

    if is_context_excluded(headline, headline_tags or []):
        return {
            "status": "none",
            "summary": [],
            "sources": [],
            "error": context_exclusion_reason(headline, headline_tags),
            "query": "",
            "web_enriched": False,
            "context_allowed": False,
        }

    query_plan = _build_query_plan(headline, body)
    search_terms = query_plan.get("primary_terms") or []
    fallback_terms = query_plan.get("fallback_terms") or []
    anchors = _extract_anchors(headline, body)
    event_terms = _extract_event_terms(headline)
    keywords = _extract_keywords(headline, body)
    counterparty_terms = _extract_counterparty_terms(headline)
    focus_terms = query_plan.get("focus_terms") or []
    must_groups = query_plan.get("must_groups") or []
    context_mode = query_plan.get("mode") or ""

    if not search_terms and not fallback_terms:
        return {
            "status": "none",
            "summary": [],
            "sources": [],
            "error": "No search terms available for Newsquawk context.",
            "query": "",
            "web_enriched": False,
            "context_allowed": True,
        }

    gathered = []
    errors = []

    # Pass 1: specific fast search terms first.
    max_pages = 3
    primary_terms = (search_terms or fallback_terms)[:3]
    for page in range(1, max_pages + 1):
        for term in primary_terms:
            rows, err = _search_newsquawk_keyword(term, page)
            if err:
                errors.append(err)
                continue
            gathered.extend(rows)

        deduped = _dedupe_by_id(gathered)
        ranked_preview = _rank_candidates(
            deduped,
            anchors,
            event_terms,
            keywords,
            headline_norm,
            counterparty_terms=counterparty_terms,
            focus_terms=focus_terms,
            must_groups=must_groups,
            strict_groups=bool(must_groups),
        )
        if context_mode == "pboc_usdcny_fixing":
            ranked_preview = [r for r in ranked_preview if _is_pboc_fixing_item(r)]
        if len(ranked_preview) >= 8:
            break
        if len(deduped) >= 180:
            break

    # Pass 2: broaden search only if strict pass is sparse.
    deduped = _dedupe_by_id(gathered)
    strict_ranked = _rank_candidates(
        deduped,
        anchors,
        event_terms,
        keywords,
        headline_norm,
        counterparty_terms=counterparty_terms,
        focus_terms=focus_terms,
        must_groups=must_groups,
        strict_groups=bool(must_groups),
    )
    if len(strict_ranked) < 4 and fallback_terms:
        for page in range(1, 3):
            for term in fallback_terms[:4]:
                rows, err = _search_newsquawk_keyword(term, page)
                if err:
                    errors.append(err)
                    continue
                gathered.extend(rows)
            if len(_dedupe_by_id(gathered)) >= 260:
                break

    gathered = _dedupe_by_id(gathered)
    if context_mode == "pboc_usdcny_fixing":
        gathered = [item for item in gathered if _is_pboc_fixing_item(item)]

    strict_ranked = _rank_candidates(
        gathered,
        anchors,
        event_terms,
        keywords,
        headline_norm,
        counterparty_terms=counterparty_terms,
        focus_terms=focus_terms,
        must_groups=must_groups,
        strict_groups=bool(must_groups),
    )
    relaxed_ranked = _rank_candidates(
        gathered,
        anchors,
        event_terms,
        keywords,
        headline_norm,
        counterparty_terms=counterparty_terms,
        focus_terms=focus_terms,
        must_groups=must_groups,
        strict_groups=False,
    )
    ranked = strict_ranked if len(strict_ranked) >= 4 else relaxed_ranked

    if context_mode == "pboc_usdcny_fixing":
        ranked = [r for r in ranked if _is_pboc_fixing_item(r)]
        ranked.sort(
            key=lambda x: x.get("_published") or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )

    # Structured monthly data prints should allow a single strong prior-month match.
    report_month = _extract_report_month(headline)
    prev_months = _month_prev_labels(report_month, count=2) if report_month else []
    data_mode = bool(_extract_month_metric_terms(headline)) and bool(prev_months)
    prior_only_items = []

    if data_mode:
        if not ranked:
            err_text = "No reliable related Newsquawk headlines found."
            if errors and not gathered:
                err_text = errors[0]
            return {
                "status": "none",
                "summary": [],
                "sources": [],
                "error": err_text,
                "query": " | ".join((query_plan.get("query_preview") or [])[:6]),
                "web_enriched": False,
                "context_allowed": True,
            }

        def _is_prior_month_item(item):
            subj = _clean_text(item.get("subject", ""))
            return any(f"({m})" in subj for m in prev_months)

        prior_month_items = [r for r in ranked if _is_prior_month_item(r)]
        if not prior_month_items:
            err_text = "No prior-month release found for this data series."
            if errors and not gathered:
                err_text = errors[0]
            return {
                "status": "none",
                "summary": [],
                "sources": [],
                "error": err_text,
                "query": " | ".join((query_plan.get("query_preview") or [])[:6]),
                "web_enriched": False,
                "context_allowed": True,
            }

        ranked = prior_month_items + [r for r in ranked if r not in prior_month_items]
        prior_only_items = prior_month_items

        def _month_priority(item):
            subj = _clean_text(item.get("subject", ""))
            if prev_months and f"({prev_months[0]})" in subj:
                return 2
            if len(prev_months) > 1 and f"({prev_months[1]})" in subj:
                return 1
            return 0

        ranked = sorted(
            ranked,
            key=lambda x: (
                _month_priority(x),
                x.get("_published") or datetime.min.replace(tzinfo=timezone.utc),
                x.get("_score", 0),
            ),
            reverse=True,
        )

    min_required = 1 if data_mode else 2
    if len(ranked) < min_required:
        err_text = "No reliable related Newsquawk headlines found."
        if errors and not gathered:
            err_text = errors[0]
        return {
            "status": "none",
            "summary": [],
            "sources": [],
            "error": err_text,
            "query": " | ".join((query_plan.get("query_preview") or [])[:6]),
            "web_enriched": False,
            "context_allowed": True,
        }

    top_items = prior_only_items[:12] if data_mode and prior_only_items else ranked[:12]
    sections = _build_sections_from_ranked(top_items)
    if not sections:
        sections = _fallback_sections_from_items(top_items)

    wiki = None
    gdelt = []
    if include_web:
        # Historical context from web sources (Wikipedia + GDELT).
        web_query = _web_query_from_plan(headline_norm, query_plan, anchors, event_terms)
        web_tokens = _headline_tokens_for_web(headline, body)
        wiki = _fetch_wikipedia_summary(web_query, headline_tokens=web_tokens)
        gdelt = _fetch_gdelt_articles(web_query, max_records=8, headline_tokens=web_tokens)
        historical_arc, why_now = _summarise_web_historical(headline_norm, body, wiki, gdelt, related_newsquawk=top_items)
        if historical_arc:
            sections["historical_arc"] = historical_arc
        if why_now:
            sections["why_now"] = why_now

    preview = _preview_from_sections(sections)
    if not preview:
        return {
            "status": "none",
            "summary": [],
            "sources": _build_sources(top_items),
            "error": "No reliable context preview available.",
            "query": " | ".join((query_plan.get("query_preview") or [])[:6]),
            "web_enriched": False,
            "context_allowed": True,
        }

    return {
        "status": "ready",
        "summary": preview,
        "sections": sections,
        "sources": _build_sources(top_items)
        + ([{"title": f"Wikipedia: {wiki.get('title', 'Topic')}", "url": wiki.get("url", "")}] if wiki and wiki.get("url") else [])
        + [{"title": g.get("title", "Web source"), "url": g.get("url", "")} for g in gdelt[:3]],
        "error": "",
        "query": " | ".join((query_plan.get("query_preview") or [])[:6]),
        "web_enriched": include_web,
        "context_allowed": True,
    }
