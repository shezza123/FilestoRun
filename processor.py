import time
import requests
import hashlib
import traceback
import re
import threading
import sys
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from config import Config
from grammar_engine import check_grammar, apply_green_highlight
import context_engine



# === Global State Memory ===
seen_ids = {}
live_rows = []
headline_queue = []
context_cache = {}
context_inflight = set()
context_job_meta = {}
context_lock = threading.Lock()
context_executor = ThreadPoolExecutor(max_workers=Config.CONTEXT_WORKERS)
CONTEXT_CACHE_VERSION = "v14"


def _safe_log(message):
    """Print safely even on non-UTF-8 Windows consoles."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
        fallback = message.encode(encoding, errors="replace").decode(encoding, errors="replace")
        print(fallback)


def make_key(headline, reaction, analysis, content):
    """Generates a unique hash to detect if specific content fields have changed."""
    data = f"{headline}|{reaction}|{analysis}|{content}"
    return hashlib.md5(data.encode()).hexdigest()


def _context_key(headline, body, headline_tags=None):
    norm = context_engine.build_context_key(headline, body)
    if not norm:
        return ""
    blocked = context_engine.is_context_excluded(headline, headline_tags or [])
    policy_suffix = "blocked" if blocked else "allowed"
    return hashlib.md5(f"{norm} || {policy_suffix} || {CONTEXT_CACHE_VERSION}".encode()).hexdigest()


def _strip_data_ref_markers(value):
    if isinstance(value, str):
        return re.sub(r"\s*\[DATA_REF\s+\d+\]", "", value, flags=re.IGNORECASE).strip()
    if isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        return [_strip_data_ref_markers(v) for v in value]
    if isinstance(value, dict):
        return {k: _strip_data_ref_markers(v) for k, v in value.items()}
    return value


def _context_blocked_payload(reason=""):
    return {
        "status": "none",
        "summary": [],
        "sources": [],
        "error": reason or context_engine.context_exclusion_reason("", []),
        "web_enriched": False,
        "context_allowed": False,
    }


def _cache_fresh(entry):
    if not entry:
        return False
    ts = entry.get("updated_at")
    if not isinstance(ts, datetime):
        return False
    return (datetime.now(timezone.utc) - ts).total_seconds() <= Config.CONTEXT_TTL_SECONDS


def _context_placeholder(status="queued", error=""):
    payload = {
        "status": status,
        "summary": [],
        "sources": [],
        "error": error,
    }
    if status in {"queued", "loading"}:
        payload[f"{status}_since"] = datetime.now(timezone.utc).isoformat()
    return payload


def _payload_from_cache_entry(entry, ckey):
    payload = {k: v for k, v in entry.items() if k != "updated_at"}
    payload = _strip_data_ref_markers(payload)
    if not isinstance(payload, dict):
        payload = {}
    payload["key"] = ckey
    return payload


def _is_loading_timed_out(ctx, ckey):
    if not isinstance(ctx, dict):
        return False
    if ctx.get("status") != "loading":
        return False
    with context_lock:
        meta = context_job_meta.get(ckey, {})
    started_at = meta.get("started_at")
    if not started_at:
        return False
    if isinstance(started_at, datetime):
        elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
        return elapsed > Config.CONTEXT_LOADING_TIMEOUT_SECONDS
    raw = ctx.get("loading_since")
    if not raw:
        return False
    try:
        started = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except Exception:
        return False
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    return elapsed > Config.CONTEXT_LOADING_TIMEOUT_SECONDS


def _job_status_for_key(ckey):
    with context_lock:
        meta = context_job_meta.get(ckey, {})
    if not meta:
        return "idle"
    if meta.get("started_at") is None:
        return "queued"
    return "loading"


def _schedule_context_fetch(headline, body, ckey, include_web=False, headline_tags=None):
    if not Config.CONTEXT_ENABLED or not ckey:
        return
    if context_engine.is_context_excluded(headline, headline_tags or []):
        return

    with context_lock:
        cached = context_cache.get(ckey)
        if _cache_fresh(cached) and isinstance(cached, dict) and (not include_web or cached.get("web_enriched")):
            return
        if ckey in context_inflight:
            return
        context_inflight.add(ckey)
        context_job_meta[ckey] = {
            "queued_at": datetime.now(timezone.utc),
            "started_at": None,
            "include_web": bool(include_web),
        }

    def _job():
        try:
            with context_lock:
                if ckey in context_job_meta:
                    context_job_meta[ckey]["started_at"] = datetime.now(timezone.utc)
            result = context_engine.fetch_historical_context(
                headline,
                body,
                include_web=include_web,
                headline_tags=headline_tags,
            )
            if not isinstance(result, dict):
                result = _context_placeholder(status="none")
            result["updated_at"] = datetime.now(timezone.utc)
            with context_lock:
                context_cache[ckey] = result
        except Exception as exc:
            with context_lock:
                context_cache[ckey] = {
                    "status": "error",
                    "summary": [],
                    "sources": [],
                    "error": str(exc),
                    "updated_at": datetime.now(timezone.utc),
                }
        finally:
            with context_lock:
                context_inflight.discard(ckey)
                context_job_meta.pop(ckey, None)

    context_executor.submit(_job)


def _get_context_for_item(headline, body, auto_fetch=True, include_web=False, headline_tags=None):
    if not Config.CONTEXT_ENABLED:
        return _context_placeholder(status="none")

    if context_engine.is_context_excluded(headline, headline_tags or []):
        return _context_blocked_payload(context_engine.context_exclusion_reason(headline, headline_tags or []))

    ckey = _context_key(headline, body, headline_tags=headline_tags)
    if not ckey:
        return _context_placeholder(status="none")

    with context_lock:
        cached = context_cache.get(ckey)

    if _cache_fresh(cached):
        if not isinstance(cached, dict):
            return _context_placeholder(status="none")
        if include_web and not cached.get("web_enriched"):
            if auto_fetch:
                _schedule_context_fetch(headline, body, ckey, include_web=True, headline_tags=headline_tags)
                pending = _context_placeholder(status=_job_status_for_key(ckey))
                pending["key"] = ckey
                pending["web_enriched"] = False
                return pending
        return _payload_from_cache_entry(cached, ckey)

    if not auto_fetch:
        idle = _context_placeholder(status="idle")
        idle["key"] = ckey
        idle["error"] = "Context not loaded yet."
        return idle

    _schedule_context_fetch(headline, body, ckey, include_web=include_web, headline_tags=headline_tags)
    pending = _context_placeholder(status=_job_status_for_key(ckey))
    pending["key"] = ckey
    pending["web_enriched"] = False
    return pending


def _refresh_row_contexts(auto_fetch_ids=None):
    if not Config.CONTEXT_ENABLED:
        return
    with context_lock:
        snapshot = dict(context_cache)
    auto_fetch_ids = set(auto_fetch_ids or [])
    for row in live_rows:
        ctx = row.get("HeadlineContext") or {}
        if not ctx:
            ctx = row.get("Context") or {}
        ckey = ctx.get("key")
        headline = (row.get("Headline") or {}).get("highlighted", "") if isinstance(row.get("Headline"), dict) else ""
        body = (row.get("Body") or {}).get("highlighted", "") if isinstance(row.get("Body"), dict) else ""
        headline_tags = row.get("HeadlineTags") if isinstance(row.get("HeadlineTags"), list) else []
        auto_fetch = row.get("id") in auto_fetch_ids

        if context_engine.is_context_excluded(headline, headline_tags):
            row["Context"] = _context_blocked_payload(context_engine.context_exclusion_reason(headline, headline_tags))
            continue

        current_ckey = _context_key(headline, body, headline_tags=headline_tags)
        if ckey != current_ckey:
            ckey = current_ckey

        if not ckey:
            row["Context"] = _get_context_for_item(
                headline,
                body,
                auto_fetch=auto_fetch,
                include_web=False,
                headline_tags=headline_tags,
            )
            continue
        cached = snapshot.get(ckey)
        if _cache_fresh(cached) and isinstance(cached, dict):
            row["Context"] = _payload_from_cache_entry(cached, ckey)
            continue

        if not auto_fetch:
            idle = _context_placeholder(status="idle")
            idle["key"] = ckey
            idle["error"] = "Context not loaded yet."
            row["Context"] = idle
            continue

        # Cache missing/stale: ensure a fetch is running and avoid endless loading state.
        _schedule_context_fetch(headline, body, ckey, include_web=False, headline_tags=headline_tags)
        status = _job_status_for_key(ckey)
        row["Context"] = {**(ctx if isinstance(ctx, dict) else {}), "key": ckey, "status": status}
        if status in {"queued", "loading"}:
            row["Context"][f"{status}_since"] = datetime.now(timezone.utc).isoformat()

        if _is_loading_timed_out(row["Context"], ckey):
            row["Context"] = {
                "status": "error",
                "summary": [],
                "sources": [],
                "error": "Context lookup timed out. Retrying...",
                "key": ckey,
            }


def ensure_row_context(row, auto_fetch=True, include_web=False):
    """Public helper: guarantees each row has a live context state."""
    if not Config.CONTEXT_ENABLED or not isinstance(row, dict):
        return

    ctx = row.get("Context") or row.get("HeadlineContext") or {}
    headline = (row.get("Headline") or {}).get("highlighted", "") if isinstance(row.get("Headline"), dict) else ""
    body = (row.get("Body") or {}).get("highlighted", "") if isinstance(row.get("Body"), dict) else ""
    headline_tags = row.get("HeadlineTags") if isinstance(row.get("HeadlineTags"), list) else []
    ckey = _context_key(headline, body, headline_tags=headline_tags)

    if context_engine.is_context_excluded(headline, headline_tags):
        row["Context"] = _context_blocked_payload(context_engine.context_exclusion_reason(headline, headline_tags))
        return

    if not ckey:
        row["Context"] = _get_context_for_item(
            headline,
            body,
            auto_fetch=auto_fetch,
            include_web=include_web,
            headline_tags=headline_tags,
        )
        return

    with context_lock:
        cached = context_cache.get(ckey)

    if _cache_fresh(cached) and isinstance(cached, dict):
        if include_web and not cached.get("web_enriched"):
            _schedule_context_fetch(headline, body, ckey, include_web=True, headline_tags=headline_tags)
            row["Context"] = {
                "status": _job_status_for_key(ckey),
                "summary": [],
                "sources": [],
                "error": "Fetching full web historical context...",
                "key": ckey,
                "web_enriched": False,
            }
            return
        row["Context"] = _payload_from_cache_entry(cached, ckey)
        return

    if not auto_fetch:
        row["Context"] = {
            "status": "idle",
            "summary": [],
            "sources": [],
            "error": "Context not loaded yet.",
            "key": ckey,
            "web_enriched": False,
        }
        return

    _schedule_context_fetch(headline, body, ckey, include_web=include_web, headline_tags=headline_tags)
    pending = _context_placeholder(status=_job_status_for_key(ckey))
    pending["key"] = ckey
    pending["web_enriched"] = False
    row["Context"] = pending


def force_fetch_context_for_row(row_id, include_web=True):
    for row in live_rows:
        if str(row.get("id")) != str(row_id):
            continue
        headline = (row.get("Headline") or {}).get("highlighted", "") if isinstance(row.get("Headline"), dict) else ""
        body = (row.get("Body") or {}).get("highlighted", "") if isinstance(row.get("Body"), dict) else ""
        headline_tags = row.get("HeadlineTags") if isinstance(row.get("HeadlineTags"), list) else []

        if context_engine.is_context_excluded(headline, headline_tags):
            row["Context"] = _context_blocked_payload(context_engine.context_exclusion_reason(headline, headline_tags))
            return True

        ckey = _context_key(headline, body, headline_tags=headline_tags)
        if not ckey:
            return False
        _schedule_context_fetch(headline, body, ckey, include_web=include_web, headline_tags=headline_tags)
        row["Context"] = {
            "status": _job_status_for_key(ckey),
            "summary": [],
            "sources": [],
            "error": "Fetching full web historical context..." if include_web else "Fetching context...",
            "key": ckey,
            "web_enriched": False,
        }
        return True
    return False


def check_previous_errors_fixed(previous_highlighted, current_text):
    """Check if errors previously highlighted in red have been fixed in the updated text."""
    previous_errors = re.findall(r"<span style='color:red'>(.*?)</span>", previous_highlighted)

    if not previous_errors:
        return True

    for error in previous_errors:
        if "'" in error:
            if error.endswith("'s"):
                if re.search(r'\b' + re.escape(error) + r'\b', current_text):
                    return False
                error_alt = error.replace("'s", "s")
                if re.search(r'\b' + re.escape(error_alt) + r'\b', current_text):
                    continue
                continue
            else:
                if re.search(r'\b' + re.escape(error) + r'\b', current_text):
                    return False
                error_alt = error.replace("'", "")
                if re.search(r'\b' + re.escape(error_alt) + r'\b', current_text):
                    continue
                continue
        elif re.search(r'\b' + re.escape(error) + r'\b', current_text):
            return False

    return True


def process_single_item(item, field_map, pub_time, current_key):
    """Worker function to process a single headline through GPT concurrently."""
    hid = item.get("id")
    headline = (item.get("subject") or "").strip()
    reaction = (item.get("reaction_details") or "").strip()
    analysis = (item.get("analysis_details") or "").strip()
    content = (item.get("content") or "").strip()
    headline_tags = item.get("headline_tags") if isinstance(item.get("headline_tags"), list) else []

    # Terminal Logging for New Items
    _safe_log(f"\n[NEW] [{datetime.now(timezone.utc).strftime('%H:%M:%S')}] | Processing: {headline}")

    row = {
        "Headline": {"highlighted": "", "corrected": ""},
        "Reaction": {"highlighted": "", "corrected": ""},
        "Analysis": {"highlighted": "", "corrected": ""},
        "Body": {"highlighted": "", "corrected": ""},
        "HeadlineTags": headline_tags,
        "Context": _get_context_for_item(
            headline,
            content,
            auto_fetch=False,
            include_web=False,
            headline_tags=headline_tags,
        ),
        "timestamp": pub_time,
        "id": hid
    }

    for field, label in field_map.items():
        original_text = (item.get(field) or "").strip()
        if not original_text: continue
        try:
            row[label] = check_grammar(original_text, label)
        except Exception as e:
            print(f"❌ GPT error on {label}: {e}")
            row[label] = {"highlighted": original_text, "corrected": original_text}

    return hid, headline, row, current_key



def poll_newsquawk():
    """Main background loop with concurrent processing for maximum speed."""
    global live_rows
    field_map = {
        "subject": "Headline",
        "reaction_details": "Reaction",
        "analysis_details": "Analysis",
        "content": "Body"
    }

    first_run = True

    while True:
        try:
            res = requests.get(Config.NEWSQUAWK_API, timeout=30)
            headlines = res.json()
            now = datetime.now(timezone.utc)

            # 1. Initial Sort by time to identify order
            headlines.sort(key=lambda x: ((x.get("published_at") or {}).get("iso_8601") or ""))

            items_to_process = []

            for item in headlines:
                hid = item.get("id")
                published_at = ((item.get("published_at") or {}).get("iso_8601") or "")
                if not hid or not published_at: continue

                pub_time = datetime.fromisoformat(published_at.replace("Z", "+00:00"))

                # Filter for first run or recent news
                if not first_run and pub_time < now - timedelta(seconds=1000):
                    continue

                headline = (item.get("subject") or "").strip()
                reaction = (item.get("reaction_details") or "").strip()
                analysis = (item.get("analysis_details") or "").strip()
                content = (item.get("content") or "").strip()

                current_key = make_key(headline, reaction, analysis, content)

                # --- Logic for handling UPDATES to existing rows ---
                existing_row = next((r for r in live_rows if r.get("id") == hid), None)
                if existing_row and seen_ids.get(hid) != current_key:
                    # Quick check for fixed errors
                    for field, label in field_map.items():
                        current_text = (item.get(field) or "").strip()
                        if not current_text or label not in existing_row: continue
                        section_data = existing_row.get(label)
                        previous_h = section_data.get("highlighted", "") if isinstance(section_data, dict) else ""

                        if "<span style='color:red'>" in previous_h:
                            if check_previous_errors_fixed(previous_h, current_text):
                                _safe_log(f"[FIXED] Source fixed {label} for ID {hid}")
                                plain = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", previous_h)

                                # Ensure section exists before writing
                                if not isinstance(existing_row.get(label), dict):
                                    existing_row[label] = {}

                                existing_row[label]["highlighted"] = plain
                                existing_row[label]["corrected"] = apply_green_highlight(
                                    plain, previous_h, current_text
                                )

                # --- Collect NEW items for concurrent processing ---
                if seen_ids.get(hid) != current_key:
                    items_to_process.append((item, pub_time, current_key))

            # 2. CONCURRENT PROCESSING (Thread Pool)
            if items_to_process:
                # Slicing the backfill on first run
                if first_run:
                    items_to_process = items_to_process[-20:]
                    _safe_log(f"[BACKFILL] Concurrently processing {len(items_to_process)} headlines...")

                with ThreadPoolExecutor(max_workers=10) as executor:
                    futures = [executor.submit(process_single_item, itm[0], field_map, itm[1], itm[2]) for itm in
                               items_to_process]
                    for future in futures:
                        try:
                            hid, headline, row, c_key = future.result()
                        except Exception as exc:
                            _safe_log(f"[ERROR] Worker failed while processing headline: {exc}")
                            traceback.print_exc()
                            continue
                        seen_ids[hid] = c_key

                        # Replace or Append
                        existing_idx = next((i for i, r in enumerate(live_rows) if r.get("id") == hid), -1)
                        if existing_idx != -1:
                            live_rows[existing_idx] = row
                        else:
                            _safe_log(f"[ROW] Sending to Flask: {hid} | {headline[:60]}...")
                            live_rows.append(row)

            # 3. Final Sort & Limit
            live_rows.sort(key=lambda r: r.get("timestamp", datetime.min), reverse=True)
            live_rows[:] = live_rows[:50]
            auto_ids = [r.get("id") for r in live_rows[: Config.CONTEXT_AUTO_FETCH_ROWS]]
            _refresh_row_contexts(auto_fetch_ids=auto_ids)
            first_run = False

        except Exception as e:
            _safe_log(f"[POLLING ERROR] {e}")
            traceback.print_exc()

        time.sleep(0.1)  # Fast polling cycle
