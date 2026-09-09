import os
import sys
import logging
import threading
import traceback
import re
import hashlib
from flask import Flask, render_template, abort, request, jsonify
from werkzeug.serving import WSGIRequestHandler

# Import our custom modules
from config import Config
import processor
import grammar_engine

# Initialize Flask with absolute pathing for templates
app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))


def _normalize_issue_text(val):
    clean = re.sub(r"<.*?>", "", val or "")
    return re.sub(r"\s+", " ", clean.strip()).lower()


def _rebuild_corrected_from_issues(issues):
    if not issues:
        return "✅ No correction needed."
    lines = []
    for it in issues:
        txt = (it.get("text") or "").strip()
        if not txt:
            continue
        suggestion = (it.get("suggestion") or "").strip()
        if suggestion and suggestion.lower() != txt.lower():
            lines.append(f"{txt} -> {suggestion}")
        else:
            lines.append(f"{txt} -> (review)")
    return "<br>".join(lines) if lines else "✅ No correction needed."


def _clear_safe_term_from_live_rows(term):
    term_norm = _normalize_issue_text(term)
    if not term_norm:
        return 0

    cleared = 0

    def _strip_matching_span(match):
        inner = match.group(1)
        if _normalize_issue_text(inner) == term_norm:
            return inner
        return match.group(0)

    for row in processor.live_rows:
        for section_name in ["Headline", "Reaction", "Analysis", "Body"]:
            section_data = row.get(section_name)
            if not isinstance(section_data, dict):
                continue

            highlighted = section_data.get("highlighted", "")
            if "<span style='color:red'>" not in highlighted:
                continue

            new_highlighted = re.sub(
                r"<span style='color:red'>(.*?)</span>",
                _strip_matching_span,
                highlighted,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if new_highlighted == highlighted:
                continue

            cleared += 1
            section_data["highlighted"] = new_highlighted
            issues = section_data.get("issues") if isinstance(section_data.get("issues"), list) else []
            remaining = [i for i in issues if _normalize_issue_text(i.get("text", "")) != term_norm]
            if "<span style='color:red'>" in new_highlighted and remaining:
                section_data["issues"] = remaining
                section_data["corrected"] = _rebuild_corrected_from_issues(remaining)
            else:
                section_data["issues"] = []
                section_data["corrected"] = "✅ No correction needed."

    return cleared


@app.route("/")
def index():
    """Renders the main dashboard with live news rows."""
    try:
        rows_sorted = sorted(
            processor.live_rows,
            key=lambda r: r.get("timestamp") or processor.datetime.min.replace(tzinfo=processor.timezone.utc),
            reverse=True,
        )
        # Localize timestamps for the UI (Safe check for concurrent updates)
        for idx, r in enumerate(rows_sorted):
            if r.get("timestamp"):
                # Only convert if it hasn't been localized to London TZ yet
                if r["timestamp"].tzinfo != Config.LONDON_TZ:
                    r["timestamp"] = r["timestamp"].astimezone(Config.LONDON_TZ)

            # Keep context rendering resilient across code versions/restarts.
            if not r.get("Context") and r.get("HeadlineContext"):
                r["Context"] = r.get("HeadlineContext")
            auto_fetch = idx < Config.CONTEXT_AUTO_FETCH_ROWS
            processor.ensure_row_context(r, auto_fetch=auto_fetch, include_web=False)

        # Identify rows that still have active red highlights (errors) for notifications
        uncorrected_rows = []
        for r in rows_sorted:
            for section in ["Headline", "Reaction", "Analysis", "Body"]:
                section_data = r.get(section)
                if isinstance(section_data, dict) and "<span style='color:red'>" in section_data.get("highlighted", ""):
                    issues = section_data.get("issues") if isinstance(section_data.get("issues"), list) else []
                    issue_key = "|".join(str(i.get("key", "")) for i in issues if isinstance(i, dict) and i.get("key"))
                    if not issue_key:
                        issue_key = hashlib.sha1(section_data.get("highlighted", "").encode("utf-8", errors="ignore")).hexdigest()[:12]
                    uncorrected_rows.append({
                        "id": r.get("id"),
                        "section": section,
                        "error_key": issue_key,
                        "timestamp": r.get("timestamp")
                    })
                    break

        return render_template(
            "index.html",
            rows=rows_sorted,
            uncorrected_rows=uncorrected_rows,
            safe_terms=sorted(grammar_engine.load_safe_terms()),
            context_timeout_seconds=Config.CONTEXT_LOADING_TIMEOUT_SECONDS,
        )

    except Exception as e:
        error_message = traceback.format_exc()
        print(f"[RENDER ERROR] {error_message}")
        return f"<pre>🔥 INTERNAL SERVER ERROR:\n\n{error_message}</pre>", 500


@app.route("/dismiss", methods=["POST"])
def dismiss_specific_error():
    """Handles user 'Dismiss' clicks to whitelist a term and clear red highlights."""
    data = request.get_json(silent=True) or {}
    row_id = data.get("id")
    section = data.get("section")
    issue_key = (data.get("issue_key") or "").strip()
    save_term = bool(data.get("save_term", False))

    def _normalize(val):
        return re.sub(r"\s+", " ", (val or "").strip()).lower()

    def _fallback_issues_from_corrected(corrected_text):
        out = []
        lines = [ln.strip() for ln in re.sub(r"<br\s*/?>", "\n", corrected_text or "", flags=re.IGNORECASE).splitlines() if ln.strip()]
        for line in lines:
            left, _, right = line.partition("->")
            wrong = left.strip()
            suggestion = right.strip() if _ else ""
            if wrong:
                out.append({
                    "key": hashlib.sha1(wrong.lower().encode("utf-8", errors="ignore")).hexdigest()[:12],
                    "text": wrong,
                    "suggestion": suggestion if suggestion and suggestion.lower() != "(review)" else "",
                    "kind": "unknown",
                    "learnable": False,
                })
        return out

    def _get_issues(section_data):
        issues = section_data.get("issues")
        if isinstance(issues, list):
            return issues
        return _fallback_issues_from_corrected(section_data.get("corrected", ""))

    def _rebuild_corrected_from_issues(issues):
        if not issues:
            return "✅ No correction needed."
        lines = []
        for it in issues:
            txt = (it.get("text") or "").strip()
            if not txt:
                continue
            suggestion = (it.get("suggestion") or "").strip()
            if suggestion and suggestion.lower() != txt.lower():
                lines.append(f"{txt} -> {suggestion}")
            else:
                lines.append(f"{txt} -> (review)")
        return "<br>".join(lines) if lines else "✅ No correction needed."

    def _strip_issue_span(highlighted, issue_text):
        issue_norm = _normalize(issue_text)
        if not issue_norm:
            return highlighted

        def _replace(match):
            inner = match.group(1)
            if _normalize(inner) == issue_norm:
                return inner
            return match.group(0)

        return re.sub(r"<span style='color:red'>(.*?)</span>", _replace, highlighted, flags=re.IGNORECASE | re.DOTALL)

    def clear_section_errors(row, section_name, target_issue_key="", persist_term=False):
        section_data = row.get(section_name)
        if not isinstance(section_data, dict):
            return False
        highlighted = section_data.get("highlighted", "")
        if "<span style='color:red'>" not in highlighted:
            return False

        issues = _get_issues(section_data)

        if target_issue_key:
            target = next((i for i in issues if str(i.get("key")) == str(target_issue_key)), None)
            if not target:
                return False

            issue_text = target.get("text", "")
            new_highlighted = _strip_issue_span(highlighted, issue_text)
            if new_highlighted == highlighted:
                return False

            clean_term = re.sub(r"<.*?>", "", issue_text).strip().lower()
            if clean_term and persist_term:
                # The explicit Add to Safe Words action overrides automatic typo rejection.
                grammar_engine.save_safe_term(clean_term, reject_typos=False)

            remaining_issues = [i for i in issues if str(i.get("key")) != str(target_issue_key)]
            section_data["highlighted"] = new_highlighted
            if "<span style='color:red'>" in new_highlighted and remaining_issues:
                section_data["issues"] = remaining_issues
                section_data["corrected"] = _rebuild_corrected_from_issues(remaining_issues)
            else:
                section_data["issues"] = []
                section_data["corrected"] = "✅ No correction needed."
            return True

        errors = re.findall(r"<span style='color:red'>(.*?)</span>", highlighted)
        clean = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", highlighted)
        section_data["highlighted"] = clean
        section_data["corrected"] = "✅ No correction needed."
        section_data["issues"] = []

        if persist_term:
            for e in errors:
                clean_term = re.sub(r"<.*?>", "", e).strip().lower()
                if clean_term:
                    grammar_engine.save_safe_term(clean_term, reject_typos=False)

        return True

    if row_id is None and section is None:
        dismissed_count = 0
        for r in processor.live_rows:
            for section_name in ["Headline", "Reaction", "Analysis", "Body"]:
                if clear_section_errors(r, section_name, persist_term=save_term):
                    dismissed_count += 1
        print(f"[DISMISS] Dismissed all active errors across dashboard ({dismissed_count} sections)")
        return "", 204

    for r in processor.live_rows:
        if str(r.get("id")) == str(row_id):
            if clear_section_errors(r, section, target_issue_key=issue_key, persist_term=save_term):
                print(f"[DISMISS] Dismissed error in {section} for ID {row_id}")
            break

    return "", 204


@app.route("/corrected", methods=["POST"])
def mark_corrected():
    """Manually marks a section as corrected by the user."""
    data = request.get_json(silent=True) or {}
    row_id = data.get("id")
    section = data.get("section")
    issue_key = (data.get("issue_key") or "").strip()

    def _normalize(val):
        return re.sub(r"\s+", " ", (val or "").strip()).lower()

    def _strip_issue_span(highlighted, issue_text):
        issue_norm = _normalize(issue_text)
        if not issue_norm:
            return highlighted

        def _replace(match):
            inner = match.group(1)
            if _normalize(inner) == issue_norm:
                return inner
            return match.group(0)

        return re.sub(r"<span style='color:red'>(.*?)</span>", _replace, highlighted, flags=re.IGNORECASE | re.DOTALL)

    def _rebuild_corrected_from_issues(issues):
        if not issues:
            return "✅ No correction needed."
        lines = []
        for it in issues:
            txt = (it.get("text") or "").strip()
            if not txt:
                continue
            suggestion = (it.get("suggestion") or "").strip()
            if suggestion and suggestion.lower() != txt.lower():
                lines.append(f"{txt} -> {suggestion}")
            else:
                lines.append(f"{txt} -> (review)")
        return "<br>".join(lines) if lines else "✅ No correction needed."

    for r in processor.live_rows:
        if str(r.get("id")) == str(row_id):
            section_data = r.get(section)
            if isinstance(section_data, dict):
                highlighted = section_data.get("highlighted", "")
                if issue_key and isinstance(section_data.get("issues"), list):
                    target = next((i for i in section_data.get("issues", []) if str(i.get("key")) == str(issue_key)), None)
                    if target:
                        section_data["highlighted"] = _strip_issue_span(highlighted, target.get("text", ""))
                        remaining = [i for i in section_data.get("issues", []) if str(i.get("key")) != str(issue_key)]
                        if "<span style='color:red'>" in section_data["highlighted"] and remaining:
                            section_data["issues"] = remaining
                            section_data["corrected"] = _rebuild_corrected_from_issues(remaining)
                        else:
                            section_data["issues"] = []
                            section_data["corrected"] = "✅ No correction needed."
                    else:
                        clean_text = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", highlighted)
                        section_data["highlighted"] = clean_text
                        section_data["issues"] = []
                        section_data["corrected"] = "✅ No correction needed."
                else:
                    clean_text = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", highlighted)
                    section_data["highlighted"] = clean_text
                    section_data["issues"] = []
                    section_data["corrected"] = "✅ No correction needed."
                print(f"[CORRECTED] Marked corrected: ID={row_id}, section={section}, issue={issue_key or 'ALL'}")
                break

    return "", 204


@app.route("/safe_terms", methods=["GET", "POST", "DELETE"])
def safe_terms():
    if request.method == "GET":
        return jsonify({"ok": True, "terms": sorted(grammar_engine.load_safe_terms())})

    data = request.get_json(silent=True) or {}
    term = (data.get("term") or "").strip().lower()
    if not term:
        return jsonify({"ok": False, "error": "Enter a safe word first."}), 400

    if request.method == "DELETE":
        grammar_engine.remove_safe_term(term)
        return jsonify({"ok": True, "terms": sorted(grammar_engine.load_safe_terms())})

    before = grammar_engine.load_safe_terms()
    grammar_engine.save_safe_term(term, reject_typos=False)
    after = grammar_engine.load_safe_terms()
    if term not in after:
        return jsonify({"ok": False, "error": "That word was not added. Use one word, 3+ characters, no spaces or punctuation like < > ; =."}), 400

    cleared = _clear_safe_term_from_live_rows(term)
    status = "existing" if term in before else "added"
    return jsonify({"ok": True, "status": status, "term": term, "cleared": cleared, "terms": sorted(after)})


@app.route("/report_missed", methods=["POST"])
def report_missed_error():
    """Records a user-reported missed error for future checks."""
    data = request.get_json(silent=True) or {}
    row_id = data.get("id")
    section = (data.get("section") or "").strip()
    wrong = (data.get("wrong") or "").strip()
    correction = (data.get("correction") or "").strip()
    text = (data.get("text") or "").strip()

    if not wrong:
        return jsonify({"ok": False, "error": "Missing 'wrong' text."}), 400

    if not text and row_id is not None and section:
        for r in processor.live_rows:
            if str(r.get("id")) != str(row_id):
                continue
            section_data = r.get(section)
            if isinstance(section_data, dict):
                text = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", section_data.get("highlighted", ""))
            break

    saved = grammar_engine.report_missed_error(
        wrong=wrong,
        correction=correction,
        label=section,
        text=text,
        row_id=row_id,
    )
    if not saved:
        return jsonify({"ok": False, "error": "Could not save missed error."}), 500

    print(f"[LEARNED] Missed error: '{wrong}' -> '{correction or '(review)'}' | section={section} | id={row_id}")
    return jsonify({"ok": True})


@app.route("/context/fetch", methods=["POST"])
def fetch_context_for_row():
    data = request.get_json(silent=True) or {}
    row_id = data.get("id")
    include_web = bool(data.get("include_web", False))
    if row_id is None:
        return jsonify({"ok": False, "error": "Missing row id"}), 400

    queued = processor.force_fetch_context_for_row(row_id=row_id, include_web=include_web)
    if not queued:
        return jsonify({"ok": False, "error": "Row not found"}), 404
    return jsonify({"ok": True})


if __name__ == "__main__":
    # Start the Newsquawk polling background thread from processor.py
    # ThreadPoolExecutor in processor.py handles the high-speed concurrency
    threading.Thread(target=processor.poll_newsquawk, daemon=True).start()


    # Configure a quieter logger for terminal output
    class QuietHandler(WSGIRequestHandler):
        def log_request(self, code='-', size='-'):
            pass


    log = logging.getLogger('werkzeug')
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    log.addHandler(handler)

    print("[START] Dashboard on http://0.0.0.0:5001")
    print(f"[START] Safe terms file: {Config.SAFE_TERMS_FILE}")

    # use_reloader=False is CRITICAL for concurrent background threads
    app.run(host="0.0.0.0", port=5001, debug=True, use_reloader=False, request_handler=QuietHandler)
