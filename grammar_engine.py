import json
import os
import re
import html
import difflib
import hashlib
from collections import Counter
from datetime import datetime, timezone

import enchant
import requests

from config import Config


dictionary = enchant.Dict("en_GB")

STYLE_PROFILES = {
    "Headline": {
        "description": "Fragment-style market headline.",
        "allow_fragment_grammar": True,
        "allow_missing_full_stop": True,
    },
    "Reaction": {
        "description": "Short market reaction text.",
        "allow_fragment_grammar": True,
        "allow_missing_full_stop": True,
    },
    "Analysis": {
        "description": "Short analysis text.",
        "allow_fragment_grammar": False,
        "allow_missing_full_stop": True,
    },
    "Body": {
        "description": "Long-form body text.",
        "allow_fragment_grammar": False,
        "allow_missing_full_stop": True,
    },
}

TOKEN_OBSERVE_MIN = 4
NAME_LOOKUP_CACHE = {}
NAME_LOOKUP_TIMEOUT_SECONDS = 3

PHARMA_SUFFIXES = (
    "mab", "limab", "zumab", "ximab", "umab", "nib", "tinib", "ciclib",
    "tide", "glutide", "patide", "stat", "vir", "previr", "asvir", "buvir",
    "cel", "tagene", "vovec", "parvovec", "plasmid", "cycline", "mycin",
    "caine", "pril", "sartan", "azole", "lukast", "oxetine", "apine",
)

OFFICIAL_TITLE_RE = re.compile(
    r"(?i)\b(?:"
    r"foreign\s+minister|finance\s+minister|prime\s+minister|defence\s+minister|defense\s+minister|"
    r"secretary\s+of\s+state|minister|president|premier|chancellor|governor|secretary|ambassador|envoy|"
    r"spokes(?:man|woman|person)|leader|chief|chair|deputy|official"
    r")\b"
)


def _load_unknown_token_stats():
    if not os.path.exists(Config.UNKNOWN_TOKEN_STATS_FILE):
        return Counter()
    try:
        with open(Config.UNKNOWN_TOKEN_STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return Counter()
        return Counter({k: int(v) for k, v in data.items() if isinstance(k, str)})
    except Exception:
        return Counter()


def _save_unknown_token_stats(counter):
    try:
        with open(Config.UNKNOWN_TOKEN_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(dict(counter), f, ensure_ascii=True, indent=2, sort_keys=True)
    except Exception:
        pass


UNKNOWN_TOKEN_STATS = _load_unknown_token_stats()


def _load_known_misspellings():
    if not os.path.exists(Config.KNOWN_MISSPELLINGS_FILE):
        return {}
    try:
        with open(Config.KNOWN_MISSPELLINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        out = {}
        for wrong, correction in data.items():
            if not isinstance(wrong, str):
                continue
            if not isinstance(correction, str):
                continue
            key = wrong.strip().lower()
            value = correction.strip()
            if key:
                out[key] = value
        return out
    except Exception:
        return {}


def _save_known_misspellings(mapping):
    try:
        with open(Config.KNOWN_MISSPELLINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=True, indent=2, sort_keys=True)
    except Exception:
        pass


KNOWN_MISSPELLINGS = _load_known_misspellings()


def _normalize_text(text):
    if not text:
        return ""
    cleaned = html.unescape(text)
    cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
    cleaned = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = (
        cleaned.replace("\u2019", "'")
        .replace("\u2018", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2013", "-")
        .replace("\u2014", "-")
    )
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r" *\n *", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip()
    return cleaned


def _style_rules_text(label):
    profile = STYLE_PROFILES.get(label, STYLE_PROFILES["Body"])
    lines = [
        f"Section: {label}",
        f"Style: {profile['description']}",
        "Newsquawk style note: missing trailing full stop is valid and should NOT be flagged.",
        "Newsquawk style note: colon-separated list formatting with spaces (e.g. 'Raised : Appaloosa') is valid and should NOT be flagged.",
    ]
    if profile["allow_fragment_grammar"]:
        lines.append("Fragment-style grammar can be valid in this section.")
    else:
        lines.append("Normal sentence grammar should be checked in this section.")
    return "\n".join(lines)


# === Persistent Safe Term Management ===


def load_safe_terms():
    if not os.path.exists(Config.SAFE_TERMS_FILE):
        return set()
    with open(Config.SAFE_TERMS_FILE, "r", encoding="utf-8") as f:
        return {line.strip().lower() for line in f if line.strip()}


def _is_sane_safe_term(term):
    token = (term or "").strip().lower()
    if not token:
        return False
    if token in {"http", "https", "href", "src", "img", "jpg", "png", "www"}:
        return False
    if " " in token:
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9'\-/]{2,}", token):
        return False
    if re.search(r"[;,<>=" + '"' + r"]", token):
        return False
    return True


def _looks_like_obvious_typo_for_safe_list(term):
    token = (term or "").strip().lower()
    if not token:
        return False
    if not re.fullmatch(r"[a-z][a-z'\-]{4,}", token):
        return False
    if token in _load_known_misspellings():
        return True
    if _is_token_dictionary_valid(token):
        return False
    if _looks_like_market_token(token) or _looks_like_pharma_token(token):
        return False

    suggestion = _suggest_spelling_fix(token)
    if not suggestion:
        return False

    suggestion_low = suggestion.lower()
    if suggestion_low == token:
        return False

    # Preserve US/UK spelling variants that appear constantly in market wires.
    uk_us_variants = {
        "center", "centers", "centered", "defense", "defenses", "defensive",
        "labor", "favorable", "unfavorable", "behavior", "rumored", "honored",
        "traveled", "traveling", "fueling", "canceled", "canceling", "skeptical",
        "aluminum", "sulfur", "theater", "theaters", "modeling", "signaling",
    }
    if token in uk_us_variants:
        return False

    ratio = difflib.SequenceMatcher(None, token, suggestion_low).ratio()
    if ratio >= 0.78:
        return True

    # Common missing-space typos that dictionary similarity alone can miss.
    if suggestion_low.replace(" ", "") == token and " " in suggestion_low:
        return True
    return False


def save_safe_term(term, reject_typos=True):
    term = term.lower().strip()
    if not term:
        return
    if not _is_sane_safe_term(term):
        return
    if reject_typos and _looks_like_obvious_typo_for_safe_list(term):
        print(f"[SAFE TERM] Rejected likely typo: {term}")
        return
    current_terms = load_safe_terms()
    if term in current_terms:
        return
    with open(Config.SAFE_TERMS_FILE, "a", encoding="utf-8") as f:
        f.write(term + "\n")
    print(f"[SAFE TERM] Added: {term}")


def remove_safe_term(term):
    term = term.lower().strip()
    if not term:
        return
    if not os.path.exists(Config.SAFE_TERMS_FILE):
        return
    try:
        with open(Config.SAFE_TERMS_FILE, "r", encoding="utf-8") as f:
            terms = [line.strip() for line in f if line.strip()]
        filtered = [t for t in terms if t.lower() != term]
        if len(filtered) == len(terms):
            return
        with open(Config.SAFE_TERMS_FILE, "w", encoding="utf-8") as f:
            for t in filtered:
                f.write(t + "\n")
        print(f"[SAFE TERM] Removed: {term}")
    except Exception:
        pass


def report_missed_error(wrong, correction="", label="", text="", row_id=None):
    normalized_wrong = _normalize_text(wrong).strip()
    normalized_correction = _normalize_text(correction).strip()
    if not normalized_wrong:
        return False

    # Guard against accidentally learning valid single words as "must flag" with no correction.
    if _is_single_word_candidate(normalized_wrong):
        if _is_token_dictionary_valid(normalized_wrong) and not normalized_correction:
            return False

    key = normalized_wrong.lower()
    KNOWN_MISSPELLINGS[key] = normalized_correction
    _save_known_misspellings(KNOWN_MISSPELLINGS)
    remove_safe_term(key)

    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "row_id": row_id,
        "section": label,
        "wrong": normalized_wrong,
        "correction": normalized_correction,
        "text": _normalize_text(text),
    }
    try:
        with open(Config.MISSED_ERRORS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=True) + "\n")
    except Exception:
        pass

    return True


def _looks_like_market_token(token):
    if not token:
        return False
    if re.fullmatch(r"[A-Z]{2,6}", token):
        return True
    if re.fullmatch(r"[A-Z]{2,6}/[A-Z]{2,6}", token):
        return True
    if re.fullmatch(r"[A-Za-z]{1,6}\d{1,3}", token):
        return True
    if re.fullmatch(r"Q[1-4]", token.upper()):
        return True
    if re.fullmatch(r"FY\d{2,4}", token.upper()):
        return True
    if re.fullmatch(r"\d+(st|nd|rd|th)", token.lower()):
        return True
    return False


def _looks_like_pharma_token(token):
    raw = (token or "").strip("-'.,;:()[]{}")
    if len(raw) < 6:
        return False
    low = raw.lower()
    if not re.fullmatch(r"[a-z][a-z0-9\-']+", low):
        return False
    if any(low.endswith(suffix) for suffix in PHARMA_SUFFIXES):
        return True
    if any(part in low for part in ("imab", "zumab", "tinib", "glutide", "statin", "cycline", "biologic")):
        return True
    # Many trial/drug codes are mixed alphanumeric and absent from normal dictionaries.
    if re.search(r"[a-z]{2,}\d{1,4}|\d{1,4}[a-z]{2,}", low):
        return True
    return False


def _is_name_like_token(token):
    return bool(re.fullmatch(r"[A-Z][a-z]{3,}(?:['-][A-Z]?[a-z]+)?", (token or "").strip()))


def _dedupe_values(values, limit=None):
    out = []
    seen = set()
    for value in values or []:
        item = re.sub(r"\s+", " ", str(value or "")).strip()
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if isinstance(limit, int) and len(out) >= limit:
            break
    return out


def _extract_official_name_candidates(text):
    candidates = []
    seen = set()
    for title_match in OFFICIAL_TITLE_RE.finditer(text or ""):
        window = text[title_match.end(): title_match.end() + 90]
        # Skip linking and department words so titles such as "Secretary of State"
        # yield "Rubio", not "State Rubio", as the name candidate.
        cleaned_window = re.sub(
            r"^\s*(?:(?:of|for|at|from|to|the|state|defence|defense|treasury|commerce|labor|labour|energy|department|iranian|chinese|russian|ukrainian|german|french|japanese|us|u\.s\.|uk|eu|ecb|fed|boe|boj|pboc|rba|snb|boc|nbp)\s+)+",
            "",
            window,
            flags=re.IGNORECASE,
        )
        match = re.search(r"^\s*([A-Z][a-z]{3,}(?:\s+[A-Z][a-z]{2,}){0,2})\b", cleaned_window)
        if not match:
            continue
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        candidates.append(name)
    return candidates[:3]


def _best_name_spelling_from_wikipedia(query, candidate):
    key = (query.lower(), candidate.lower())
    if key in NAME_LOOKUP_CACHE:
        return NAME_LOOKUP_CACHE[key]

    suggestion = ""
    try:
        headers = {"User-Agent": "NewsquawkSpellChecker/1.0"}
        search_queries = [query]
        if candidate in query:
            before_candidate = query.split(candidate, 1)[0].strip(" -,:;")
            without_candidate = re.sub(rf"\b{re.escape(candidate)}\b", "", query).strip(" -,:;")
            search_queries.extend([before_candidate, without_candidate])

        cand_parts = re.findall(r"[A-Za-z]{4,}", candidate)
        for search_query in _dedupe_values(search_queries, limit=3):
            if len(search_query) < 4:
                continue
            response = requests.get(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "opensearch",
                    "search": search_query,
                    "limit": 5,
                    "namespace": 0,
                    "format": "json",
                },
                headers=headers,
                timeout=NAME_LOOKUP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
            titles = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
            for title in titles:
                title = str(title or "").strip()
                if not title or "disambiguation" in title.lower():
                    continue

                evidence = title
                try:
                    summary_response = requests.get(
                        "https://en.wikipedia.org/api/rest_v1/page/summary/" + requests.utils.quote(title),
                        headers=headers,
                        timeout=NAME_LOOKUP_TIMEOUT_SECONDS,
                    )
                    if summary_response.ok:
                        summary = summary_response.json()
                        evidence += " " + (summary.get("extract") or "")[:1200]
                except Exception:
                    pass

                title_parts = re.findall(r"[A-Z][a-z]{3,}", evidence)
                for cand_part in cand_parts:
                    matches = difflib.get_close_matches(cand_part, title_parts, n=1, cutoff=0.86)
                    if matches and matches[0].lower() != cand_part.lower():
                        suggestion = candidate.replace(cand_part, matches[0])
                        NAME_LOOKUP_CACHE[key] = suggestion
                        return suggestion
    except Exception:
        suggestion = ""

    NAME_LOOKUP_CACHE[key] = suggestion
    return suggestion


def _find_official_name_candidates(text, label):
    if label not in {"Headline", "Reaction", "Analysis", "Body"}:
        return [], {}
    if not OFFICIAL_TITLE_RE.search(text or ""):
        return [], {}

    accepted = []
    correction_map = {}
    for name in _extract_official_name_candidates(text):
        if not any(_is_name_like_token(part) for part in name.split()):
            continue
        query = re.sub(r"\s+", " ", text[:180]).strip()
        suggestion = _best_name_spelling_from_wikipedia(query, name)
        if suggestion and suggestion.lower() != name.lower():
            accepted.append(name)
            correction_map[name] = suggestion
    return accepted, correction_map


def _learn_style_tokens(text, safe_terms):
    changed = False
    for token in re.findall(r"\b[A-Za-z0-9][A-Za-z0-9/\-']*\b", text):
        lw = token.lower()
        if token[0].isdigit():
            continue
        if lw in safe_terms:
            continue
        if not _looks_like_market_token(token):
            continue
        UNKNOWN_TOKEN_STATS[lw] += 1
        if UNKNOWN_TOKEN_STATS[lw] >= TOKEN_OBSERVE_MIN:
            save_safe_term(lw)
            safe_terms.add(lw)
            changed = True
    if changed:
        _save_unknown_token_stats(UNKNOWN_TOKEN_STATS)


# === Text Manipulation & Highlighting ===


def apply_green_highlight(original_text, highlighted_text, corrected_text):
    corrections = []
    seen = set()

    red_words = re.findall(r"<span style='color:red'>(.*?)</span>", highlighted_text)
    if not red_words:
        return "✅ No correction needed."

    corrected_clean = re.sub(r"<.*?>", "", corrected_text)
    corr_tokens = re.findall(r"\b\w+\b", corrected_clean)

    for wrong in red_words:
        match = difflib.get_close_matches(wrong.lower(), [c.lower() for c in corr_tokens], n=1, cutoff=0.65)
        if not match:
            pair = f"{wrong} -> (review)"
        else:
            corrected = next((c for c in corr_tokens if c.lower() == match[0]), match[0])
            if wrong.lower() == corrected.lower():
                continue
            pair = f"{wrong} -> {corrected}"
        if pair not in seen:
            seen.add(pair)
            corrections.append(pair)

    return "<br>".join(corrections) if corrections else "✅ No correction needed."


def _candidate_pattern(candidate):
    if re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", candidate):
        return re.compile(rf"\b{re.escape(candidate)}\b", re.IGNORECASE)
    return re.compile(re.escape(candidate), re.IGNORECASE)


def _highlight_candidates(text, candidates):
    spans = []
    for cand in candidates:
        pattern = _candidate_pattern(cand)
        for m in pattern.finditer(text):
            spans.append((m.start(), m.end()))

    if not spans:
        return text

    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    merged = []
    current_end = -1
    for start, end in spans:
        if start < current_end:
            continue
        merged.append((start, end))
        current_end = end

    out = text
    for start, end in reversed(merged):
        out = out[:start] + "<span style='color:red'>" + out[start:end] + "</span>" + out[end:]
    return out


def _build_correction_output(candidates, correction_map):
    if not candidates:
        return "✅ No correction needed."
    lines = []
    for cand in candidates:
        correction = correction_map.get(cand) or "(review)"
        if correction.strip() and correction.strip().lower() != cand.strip().lower():
            lines.append(f"{cand} -> {correction}")
        else:
            lines.append(f"{cand} -> (review)")
    return "<br>".join(lines)


def _issue_key(text):
    token = (text or "").strip().lower()
    return hashlib.sha1(token.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _build_issues(accepted, correction_map, rule_set, pre_set, must_flag_set):
    issues = []
    seen_keys = set()
    for cand in accepted:
        text = (cand or "").strip()
        if not text:
            continue
        key = _issue_key(text)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        lower = text.lower()
        if lower in must_flag_set:
            kind = "must_flag"
        elif lower in rule_set:
            kind = "rule"
        elif lower in pre_set:
            kind = "spelling"
        else:
            kind = "unknown"

        suggestion = (correction_map.get(cand) or "").strip()
        learnable = _is_sane_safe_term(lower)

        issues.append(
            {
                "key": key,
                "text": text,
                "suggestion": suggestion,
                "kind": kind,
                "learnable": learnable,
            }
        )
    return issues


def _suggest_spelling_fix(candidate):
    token = (candidate or "").strip()
    if not _is_single_word_candidate(token):
        return ""
    if _looks_like_pharma_token(token) or _is_name_like_token(token):
        return ""
    stripped = token.strip("-'")
    if not stripped:
        return ""
    try:
        suggestions = dictionary.suggest(stripped)
    except Exception:
        return ""
    if not suggestions:
        return ""
    best = suggestions[0].strip()
    if not best:
        return ""
    ratio = difflib.SequenceMatcher(None, stripped.lower(), best.lower()).ratio()
    # Avoid confident-looking but wrong dictionary guesses for specialist terms.
    if ratio < 0.74 and len(stripped) > 5:
        return ""
    if token.istitle():
        best = best.title()
    elif token.isupper():
        best = best.upper()
    return best


def _rule_based_correction(candidate, source_text=""):
    text = (candidate or "")
    if source_text and _is_single_word_candidate(text):
        doubled = re.compile(rf"\b({re.escape(text)})[ \t]+\1\b", re.IGNORECASE)
        if doubled.search(source_text):
            return ""
    if ";" in text and re.search(r"\b[A-Za-z]+;[A-Za-z]+\b", text):
        return text.replace(";", "'")
    if re.search(r"\b[A-Za-z]+(?:-[A-Za-z]+)?'s\s+(?:says|said|adds|added|notes|noted|tells|told|states|stated|warns|warned|announces|announced)\b", text, flags=re.IGNORECASE):
        return re.sub(r"(?i)'s\s+(says|said|adds|added|notes|noted|tells|told|states|stated|warns|warned|announces|announced)", r" \1", text)
    if re.search(r"\b\d+\.\.\d+\b", text):
        return text.replace("..", ".")
    if _is_single_word_candidate(text) and len(text.strip()) >= 4:
        return _suggest_spelling_fix(text)
    return ""


def _is_single_word_candidate(candidate):
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z'\-]*", (candidate or "").strip()))


def _is_known_valid_single_word(candidate, safe_terms, approved_terms):
    if not _is_single_word_candidate(candidate):
        return False
    lw = candidate.lower().strip("-'")
    if not lw:
        return False
    if lw in safe_terms or lw in approved_terms:
        return True
    base = re.sub(r"('s|s')$", "", lw)
    if base in safe_terms or base in approved_terms:
        return True
    return _is_dictionary_valid(base)


def _is_non_fix_expansion(candidate, correction):
    cand = (candidate or "").strip().lower()
    corr = (correction or "").strip().lower()
    if not cand or not corr:
        return False
    if cand == corr:
        return False
    corr_words = re.findall(r"[a-z]+", corr)
    if len(corr_words) <= 1:
        return False
    return cand in corr_words


# === AI & Spellcheck Logic ===


def _is_dictionary_valid(word):
    if dictionary.check(word):
        return True
    lw = word.lower()
    return dictionary.check(lw)


def _is_token_dictionary_valid(token):
    token = (token or "").strip()
    if not token:
        return False
    variants = [token, token.title(), token.upper(), token.lower()]
    seen = set()
    for v in variants:
        if v in seen:
            continue
        seen.add(v)
        if dictionary.check(v):
            return True
    return False


def _find_must_flag_candidates(text, known_misspellings):
    candidates = []
    correction_map = {}
    for wrong, correction in known_misspellings.items():
        if not wrong:
            continue
        if _candidate_pattern(wrong).search(text):
            candidates.append(wrong)
            correction_map[wrong] = correction
    return candidates, correction_map


def _known_watchlist_text(known_misspellings, limit=40):
    keys = sorted(known_misspellings.keys(), key=len)[:limit]
    if not keys:
        return ""
    return "Known previously-missed forms to watch for: " + ", ".join(keys)


def pre_spellcheck(text, label, known_misspellings=None):
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", text)
    flagged = []

    approved = {t.lower() for t in Config.APPROVED_TERMS}
    safe_terms = Config.FINANCIAL_TERMS | load_safe_terms()
    known_misspellings = known_misspellings or {}

    _learn_style_tokens(text, safe_terms)

    for w in words:
        raw = w.strip("-'")
        lw = raw.lower()
        if not lw or len(lw) <= 2:
            continue
        if lw in known_misspellings:
            flagged.append(w)
            continue
        if _looks_like_pharma_token(raw):
            continue
        # Preserve case-aware dictionary checks so valid all-caps/proper nouns are not flagged.
        if _is_token_dictionary_valid(raw):
            continue
        # Avoid false positives on proper names/titlecase tokens in headlines.
        if w[:1].isupper() and not w.isupper():
            continue
        if lw in approved or lw in safe_terms:
            continue
        if _looks_like_market_token(w):
            continue

        base_raw = re.sub(r"(?i)('s|s')$", "", raw)
        base = base_raw.lower()
        if base in safe_terms or base in approved:
            continue

        if _is_token_dictionary_valid(base_raw):
            continue

        flagged.append(w)

    unique = []
    seen = set()
    for w in flagged:
        k = w.lower()
        if k in seen:
            continue
        seen.add(k)
        unique.append(w)
    return unique


def _find_rule_based_candidates(text, label):
    candidates = []

    # Doubled words: "the the"
    for m in re.finditer(r"\b([A-Za-z']+)[ \t]+\1\b", text, flags=re.IGNORECASE):
        doubled = m.group(1)
        if doubled:
            trailing = text[m.end():m.end() + 2]
            if doubled.isupper() and trailing.strip().startswith(":"):
                continue
            candidates.append(doubled)

    # Malformed apostrophe with semicolon: "Russia;s"
    for m in re.finditer(r"\b[A-Za-z]+;[A-Za-z]+\b", text):
        candidates.append(m.group(0))

    # Double possessive before speech verbs: "Lagarde's says"
    speech_verbs = r"says|said|adds|added|notes|noted|tells|told|states|stated|warns|warned|announces|announced"
    for m in re.finditer(rf"\b[A-Za-z]+(?:-[A-Za-z]+)?'s\s+(?:{speech_verbs})\b", text, flags=re.IGNORECASE):
        candidates.append(m.group(0))

    # Comma/semicolon checks are intentionally narrow. Market/news text often uses
    # unusual comma spacing in lists, tickers and copied wire fragments.
    for m in re.finditer(r"\b([a-z]{4,})\s+([,;])([a-z]{4,})\b", text):
        candidates.append(m.group(0))

    for m in re.finditer(r"\b([a-z]{4,})([,;]){2,}\s*([a-z]{4,})\b", text):
        candidates.append(m.group(0))

    # Obvious malformed decimal punctuation like "2..3"
    for m in re.finditer(r"\b\d+\.\.\d+\b", text):
        candidates.append(m.group(0))

    # Keep deterministic list small and unique
    out = []
    seen = set()
    for c in candidates:
        k = c.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    return out


def _detect_ai_candidates(text, label, known_misspellings=None):
    # AI candidate detection intentionally disabled for reliability.
    return []


def _review_candidates_with_ai(text, label, candidates):
    # AI verification intentionally disabled for reliability.
    return {}


def check_grammar(text, label):
    text = _normalize_text(text)
    if not text.strip():
        return {"highlighted": text, "corrected": "✅ No correction needed.", "issues": []}

    known_misspellings = _load_known_misspellings()
    approved_terms = {t.lower() for t in Config.APPROVED_TERMS}
    safe_terms = Config.FINANCIAL_TERMS | load_safe_terms()

    must_flag_candidates, must_flag_map = _find_must_flag_candidates(text, known_misspellings)

    official_name_candidates, official_name_map = _find_official_name_candidates(text, label)
    rule_candidates = _find_rule_based_candidates(text, label)
    pre_candidates = pre_spellcheck(text, label, known_misspellings)

    ordered = []
    seen = set()
    for bucket in (must_flag_candidates, official_name_candidates, rule_candidates, pre_candidates):
        for item in bucket:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(item)

    if not ordered:
        return {"highlighted": text, "corrected": "✅ No correction needed.", "issues": []}

    rule_set = {c.lower() for c in rule_candidates}
    official_name_set = {c.lower() for c in official_name_candidates}
    pre_set = {c.lower() for c in pre_candidates}
    must_flag_set = {c.lower() for c in must_flag_candidates}

    accepted = []
    correction_map = {}
    for cand in ordered:
        key = cand.lower()
        # An explicit Ignore is authoritative, including over must-flag and rule candidates.
        if key in safe_terms:
            continue
        if key in must_flag_set:
            accepted.append(cand)
            correction_map[cand] = must_flag_map.get(key, "")
            continue
        if key in official_name_set:
            accepted.append(cand)
            correction_map[cand] = official_name_map.get(cand, "")
            continue
        if key in rule_set:
            accepted.append(cand)
            correction_map[cand] = _rule_based_correction(cand, source_text=text)
            continue
        if key in pre_set:
            if key not in known_misspellings and _is_known_valid_single_word(cand, safe_terms, approved_terms):
                continue
            accepted.append(cand)
            correction_map[cand] = _suggest_spelling_fix(cand)

    if not accepted:
        return {"highlighted": text, "corrected": "✅ No correction needed.", "issues": []}

    highlighted = _highlight_candidates(text, accepted)
    if "<span" not in highlighted:
        return {"highlighted": text, "corrected": "✅ No correction needed.", "issues": []}

    corrected = _build_correction_output(accepted, correction_map)
    issues = _build_issues(accepted, correction_map, rule_set | official_name_set, pre_set, must_flag_set)
    return {"highlighted": highlighted, "corrected": corrected, "issues": issues}
