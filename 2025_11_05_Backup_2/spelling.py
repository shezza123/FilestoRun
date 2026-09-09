import os
import threading
import time
import requests
from flask import Flask, render_template, abort, request
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone
import pytz
london_tz = pytz.timezone("Europe/London")
import hashlib
import logging
import sys
from werkzeug.serving import WSGIRequestHandler
import difflib
import re
import traceback
import os; # print(f"📁 Running from: {os.getcwd()}")



# Load API key
load_dotenv()
openai_api_key = os.getenv("OPENAI_API_KEY")
if not openai_api_key:
    raise Exception("❌ OPENAI_API_KEY missing from .env")

client = OpenAI(api_key=openai_api_key)
app = Flask(__name__, template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates'))

# Whitelist of valid proper nouns (names, positions, tickers, etc.)
APPROVED_TERMS = {
    'Bessent', 'Powell', 'Trump', 'Lagarde', 'Xi', 'iPhone', 'RFK Jr.'
}


NEWSQUAWK_API = "https://newsquawk.com/headlines.json?code=X9Vnzz4uUKgG72s6QAxE"
# Clear stale memory from Flask reloader in debug mode
seen_keys = set()
live_rows = []
seen_ids = {}
headline_queue = []
last_poll_time = datetime.now(timezone.utc) - timedelta(seconds=5)

# Clear stale memory from Flask reloader in debug mode
try:
    live_rows.clear()
    headline_queue.clear()
    seen_ids.clear()
    seen_keys.clear()
except:
    pass


def make_key(headline, reaction, analysis, content):
    data = f"{headline}|{reaction}|{analysis}|{content}"
    return hashlib.md5(data.encode()).hexdigest()


def apply_green_highlight(original_text, highlighted_text, corrected_text):
    """Apply green highlighting to corrected words based on red-highlighted mistakes in the original"""
    import re
    import difflib

    result = corrected_text
    red_spans = list(re.finditer(r"<span style='color:red'>(.*?)</span>", highlighted_text))

    if not red_spans:
        return corrected_text

    plain_highlighted = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", highlighted_text)
    original_words = set(re.sub(r"<.*?>", "", original_text).split())
    corrected_words = set(re.sub(r"<.*?>", "", corrected_text).split())

    for red_match in red_spans:
        mistake = red_match.group(1).strip()

        # 🧠 Check full word match only in corrected text
        if re.search(r'\b' + re.escape(mistake) + r'\b', corrected_text):
            print(f"🛑 UNCORRECTED ERROR: GPT failed to fix '{mistake}'")

            # 🔍 Try to guess the correction using difflib
            suggestions = difflib.get_close_matches(mistake, [
                'tariffs', 'imposing tariffs', 'taxing', 'applying duties', 'levying', 'duty', 'charges',
                'correct', 'accurate', 'forecast', 'agreement', 'policy', 'inflation', 'stability',
                'exporting', 'importing', 'trade', 'penalising', 'adjusting'
            ], n=1, cutoff=0.6)

            if suggestions:
                replacement = suggestions[0]
                # Remove red span if present before applying green
                result = re.sub(r"<span style='color:red'>" + re.escape(mistake) + r"</span>", mistake, result)
                green_version = f"<span style='color:green'>{mistake}</span>"
                print(f"✅ Suggested fix for '{mistake}' → '{replacement}'")
                result = re.sub(r'\b' + re.escape(mistake) + r'\b', green_version, result, count=1)

            else:
                print(f"⚠️ No suggestion found. Highlighting original word as green.")
                green_version = f"<span style='color:green'>{mistake}</span>"
                result = re.sub(r'\b' + re.escape(mistake) + r'\b', green_version, result, count=1)

            continue

        # 🔍 Find candidate replacements (words in corrected not in original)
        candidates = list(corrected_words - original_words)
        if candidates:
            replacement = " ".join(sorted(candidates, key=lambda x: corrected_text.find(x)))
            green_version = f"<span style='color:green'>{replacement}</span>"
            pattern = re.escape(replacement)
            result = re.sub(pattern, green_version, result, count=1)
            print(f"✅ Correction made: replaced '{mistake}' with '{replacement}'")
        else:
            print(f"⚠️ Could not find correction for '{mistake}'")

    return result



def check_previous_errors_fixed(previous_highlighted, current_text):
    """Check if errors previously highlighted have been fixed in the current text"""
    import re

    # Extract the previously highlighted errors
    previous_errors = re.findall(r"<span style='color:red'>(.*?)</span>", previous_highlighted)

    if not previous_errors:
        return True  # No errors to check

    for error in previous_errors:
        # Handle apostrophes specially
        if "'" in error:
            # For possessive forms (ending with 's)
            if error.endswith("'s"):
                # Check if error exists as-is
                if re.search(r'\b' + re.escape(error) + r'\b', current_text):
                    print(f"❌ Error still exists: '{error}'")
                    return False

                # Check if error exists without apostrophe (common correction)
                error_alt = error.replace("'s", "s")
                # If the fixed version (without apostrophe) is in the text, consider it fixed
                if re.search(r'\b' + re.escape(error_alt) + r'\b', current_text):
                    # This is a valid correction (e.g., "President's" to "Presidents")
                    continue

                # If neither original error nor fixed version exists, consider it fixed
                continue

            # Other apostrophe cases
            else:
                # Original error still exists
                if re.search(r'\b' + re.escape(error) + r'\b', current_text):
                    print(f"❌ Error still exists: '{error}'")
                    return False

                # Check alternate versions (with/without apostrophe)
                error_alt = error.replace("'", "")
                if re.search(r'\b' + re.escape(error_alt) + r'\b', current_text):
                    # This is a valid correction (e.g., "isn't" to "isnt")
                    continue

                # If neither original error nor fixed version exists, consider it fixed
                continue

        # Standard case - check if error still exists with word boundaries
        elif re.search(r'\b' + re.escape(error) + r'\b', current_text):
            print(f"❌ Error still exists: '{error}'")
            return False  # Error still exists

    print(f"✅ All errors have been fixed!")
    return True  # All previous errors have been fixed


def poll_newsquawk():
    global live_rows
    # Define field_map at the beginning of the function
    field_map = {
        "subject": "Headline",
        "reaction_details": "Reaction",
        "analysis_details": "Analysis",
        "content": "Body"
    }

    while True:
        try:
            res = requests.get(NEWSQUAWK_API, timeout=10)
            headlines = res.json()
            now = datetime.now(timezone.utc)

            headlines.sort(key=lambda x: (x.get("published_at") or {}).get("iso_8601", ""))

            for item in headlines:
                hid = item.get("id")
                published_at = (item.get("published_at") or {}).get("iso_8601", "")
                if not hid or not published_at:
                    continue

                pub_time = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                if pub_time < now - timedelta(seconds=1000):
                    continue

                headline = item.get("subject", "").strip()
                reaction = item.get("reaction_details", "").strip()
                analysis = item.get("analysis_details", "").strip()
                content = item.get("content", "").strip()
                section = (item.get("section") or {}).get("name", "").strip().lower()
                if section in {"newsquawk update"}:
                    continue

                current_key = make_key(headline, reaction, analysis, content)

                # Find existing row if we've seen this item before
                existing_row = None
                existing_row_index = -1
                for i, r in enumerate(live_rows):
                    if r.get("id") == hid:
                        existing_row = r
                        existing_row_index = i
                        break

                # If this is an update to an item we already know
                if existing_row and seen_ids.get(hid) != current_key:
                    # Check for each field if previously flagged errors have been fixed
                    for field, label in field_map.items():
                        current_text = item.get(field, "").strip()
                        if not current_text or label not in existing_row:
                            continue

                        previous_highlighted = existing_row[label].get("highlighted", "")

                        # If we previously found errors (has red highlighting)
                        if previous_highlighted and "<span style='color:red'>" in previous_highlighted:
                            # Check if the errors have been fixed
                            if check_previous_errors_fixed(previous_highlighted, current_text):
                                print(f"🎉 User corrected {label} for ID {hid}")

                                # Remove red spans to get plain base text
                                plain_text = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1",
                                                    previous_highlighted)

                                # Apply green highlight over what was corrected
                                corrected = apply_green_highlight(plain_text, previous_highlighted, current_text)

                                existing_row[label]["highlighted"] = plain_text
                                existing_row[label]["corrected"] = corrected
                                live_rows[existing_row_index] = existing_row
                                item[field] = current_text  # ✅ Prevent GPT from reprocessing this fixed field

                        # ADD THIS NEW BLOCK: Update content even when no errors existed but content changed
                        else:
                            # Remove span tags to get plain text for comparison
                            plain_previous = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1",
                                                    previous_highlighted)
                            # If content changed but there were no errors before
                            if plain_previous != current_text and current_text:
                                print(f"📝 Content updated for {label} (ID: {hid})")
                                existing_row[label]["highlighted"] = current_text
                                existing_row[label]["corrected"] = "✅ No correction needed."
                                live_rows[existing_row_index] = existing_row
                                item[field] = current_text  # Prevent GPT from reprocessing this updated field
                # Process the item normally if it's new or has other changes
                if seen_ids.get(hid) != current_key:
                    is_update = hid in seen_ids
                    seen_ids[hid] = current_key

                    old_item = next((h.copy() for h in headline_queue if h["id"] == hid), None)
                    if old_item:
                        preserved_time = old_item.get("published_time")
                    else:
                        preserved_time = pub_time

                    if old_item:
                        if old_item["headline"] != headline:
                            print(f"\n--- 📝 New Headline:\n{headline}")
                        if old_item["reaction"] != reaction:
                            print(f"\n--- 📊 New Reaction:\n{reaction}")
                        if old_item["analysis"] != analysis:
                            print(f"\n--- 🧠 New Analysis:\n{analysis}")
                        if old_item["content"] != content:
                            print(f"\n--- 📄 New Body Content:\n{content[:400]}..." if len(
                                content) > 400 else f"\n--- 📄 New Body Content:\n{content}")
                        print(f"🔁 [{datetime.now().strftime('%H:%M:%S')}] | Updated headline: {headline}\n")
                    else:
                        print(f"\n🆕 [{datetime.now().strftime('%H:%M:%S')}] | New headline: {headline}")
                        if reaction:
                            print(f"📊 Reaction: {reaction}")
                        if analysis:
                            print(f"🧠 Analysis: {analysis}")
                        if content:
                            print(f"📄 Body: {content[:300]}..." if len(content) > 300 else f"📄 Body: {content}")

                    # First check if any content has changed
                    content_changed = False
                    for field, label in field_map.items():
                        current_text = item.get(field, "").strip()

                        # Handle the case where a field is newly added (wasn't in the existing row)
                        if current_text and (
                                existing_row is None or label not in existing_row or not existing_row.get(label,
                                                                                                          {}).get(
                            "highlighted", "")):
                            print(f"🆕 New content in {label} field detected")
                            content_changed = True
                            break

                        # Handle case where content changed in existing field
                        if label in existing_row and current_text:
                            previous_highlighted = existing_row[label].get("highlighted", "")

                            # ✅ Ensure fallback initialization in case the field wasn't set properly
                            if not previous_highlighted:
                                print(f"⚠️ Missing highlighted value in existing_row for {label}. Initializing.")
                                existing_row[label] = {
                                    "highlighted": current_text,
                                    "corrected": "✅ No correction needed."
                                }
                                plain_previous = current_text
                            else:
                                plain_previous = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1",
                                                        previous_highlighted).strip()

                            if plain_previous != current_text:
                                print(f"🔄 Detected content change in {label}")
                                content_changed = True
                                break

                    # Only skip if nothing changed AND no errors to fix
                    if existing_row and not content_changed:
                        skip = True
                        for field, label in field_map.items():
                            if label not in existing_row:
                                continue
                            highlighted = existing_row[label].get("highlighted")
                            if highlighted is None:
                                continue
                            if "<span style='color:red'>" not in highlighted:
                                continue
                            # Safely fetch the corresponding field key
                            field_key = next((k for k, v in field_map.items() if v == label), None)
                            current_text = item.get(field_key, "") if field_key else ""
                            if not check_previous_errors_fixed(highlighted, current_text):
                                skip = False
                                break
                        if skip:
                            continue

                    # Otherwise create a new row with grammar checks
                    row = {
                        "Headline": {},
                        "Reaction": {},
                        "Analysis": {},
                        "Body": {},
                        "timestamp": preserved_time,
                        "id": hid
                    }

                    for field, label in field_map.items():
                        original_text = item.get(field, "").strip()
                        if not original_text:
                            continue

                        try:
                            checked = check_grammar(original_text, label)
                        except Exception as e:
                            print(f"❌ GPT error on {label}: {e}")
                            checked = {"highlighted": original_text, "corrected": original_text}

                        # Always include updated field
                        row[label] = {
                            "highlighted": checked["highlighted"],
                            "corrected": checked["corrected"]
                        }

                        # If this was an update to an existing row, apply updated field to the live row too
                        if existing_row:
                            existing_row[label] = row[label]

                    row["timestamp"] = preserved_time
                    row["id"] = hid

                    for i, r in enumerate(live_rows):
                        if r.get("id") == hid:
                            live_rows[i] = row
                            break
                    else:
                        print(f"✅ Sending to Flask: {hid} | {headline} | {row}")
                        live_rows.append(row)

                    headline_queue[:] = [h for h in headline_queue if h["id"] != hid]
                    headline_queue.append({
                        "id": hid,
                        "headline": headline,
                        "reaction": reaction,
                        "analysis": analysis,
                        "content": content,
                        "published_time": preserved_time
                    })

            live_rows.sort(key=lambda r: r.get("timestamp", datetime.min), reverse=True)
            live_rows = live_rows[:50]

        except Exception as e:
            print(f"⚠️ Polling error: {e}")
            traceback.print_exc()

        time.sleep(0.5)

def check_grammar(text, label):
    """Grammar checker with full double-check logic for both errors and clean text"""
    if not text.strip():
        return {"highlighted": "", "corrected": ""}

    max_chars = 100000
    text = text[:max_chars]

    for attempt in range(3):
        result = check_grammar_pass(text, label, is_safety_pass=False)

        # ✅ If red spans found, let check_grammar_pass handle verification
        if "<span" in result.get("highlighted", ""):
            return result

        # ✅ If corrected output is different, return it
        if result.get("corrected") != "✅ No correction needed.":
            return result

        print(f"⚠️ GPT 5-mini pass #{attempt + 1} returned no correction. Retrying...")

        if attempt == 1:
            print("⚠️ Skipping 3rd pass to save tokens (2 clean passes).")
            break

    # 🔁 Final safety audit: ask GPT to reverify if it's truly clean
    try:
        verification_prompt = f"""
Please read the following financial news text and tell me:
Is there **any** obvious grammar or spelling error in this?

Text:
{text}

Respond only with one word: Yes or No.
"""
        audit_response = client.responses.create(
            model="gpt-5-mini",
            input=f"System: You are an expert grammar verifier for UK financial news.\nUser: {verification_prompt}",
            reasoning={"effort": "low"},
            text={"verbosity": "low"},
            max_output_tokens=32
        )
        verdict = audit_response.output_text.strip().lower()

        if verdict == "yes":
            print("🚨 Audit said text contains an error — force recheck")
            return check_grammar_pass(text, label, is_safety_pass=False)

    except Exception as e:
        print(f"⚠️ Final audit failed: {e}")

    clean_text = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", text)
    return {"highlighted": clean_text, "corrected": "✅ No correction needed."}



def check_grammar_pass(text, label, is_safety_pass=False):
    """Improved grammar checker optimized for GPT-5-mini with higher reliability for UK financial news"""
    import re

    # Skip safety pass entirely - just use a single reliable pass
    if is_safety_pass:
        return {"highlighted": text, "corrected": "✅ No correction needed."}

    # Use GPT-5-mini with a much more constrained prompt
    prompt = f"""You are the most conservative and reliable British English grammar checker possible for financial news.

TEXT CATEGORY: {label}

TEXT TO CHECK:
{text}

ONLY flag the following SEVERE errors:
- Obvious misspelled words (definitely wrong spelling, not variations)
- Severe grammar errors (e.g., subject-verb disagreement)
- Doubled words (e.g., "the the")
- Malformed punctuation (e.g., "Russia;s" instead of "Russia's")
- **Truncated or malformed numbers (e.g., "204-" instead of "2040")**



DO NOT flag:
- Stylistic preferences
- British English spellings (e.g., "labour", "favour", "centre", "defence", "realise", "authorise", etc.)
- Do not suggest US spellings (e.g., "color", "analyze", "defense", "center") in place of correct UK spellings
- - Acceptable singular vs plural forms unless it causes subject-verb disagreement or breaks sentence logic.
- Industry terminology or financial jargon
- Company names, stock tickers, or product names
- Anything that could be correct in context
- Abbreviations like mln, bln, tln, q1, q2, Y/Y, etc.
- Any currency codes or numbers
- Capitalizations that might be correct in financial contexts

HIGHLIGHT FORMAT: Use <span style='color:red'>error</span> to mark errors.

RESPONSE FORMAT:
---
Highlighted: [text with errors highlighted]
Corrected: [corrected text]
---

CRITICAL: Only flag something if you are confident it is an error. If unsure, leave it unchanged — but always complete the check and return the result.
Flag any clear spelling mistakes (e.g., 'Europ' instead of 'Europe'), doubled words, or grammar issues like wrong tense or subject-verb agreement. Be cautious — but never ignore obvious typos or malformed proper nouns.
Be conservative - only flag the most obvious errors where you are 100% certain, but always flag **clear spelling mistakes**, like when a proper noun or country name is misspelled.

"""

    # Use the cheaper 3.5 model with system message for stronger instruction
    response = client.responses.create(
        model="gpt-5-mini",
        input=f"System: You are a specialized, extremely conservative financial news grammar checker that only flags severe and obvious errors. You know financial terminology and prioritize precision over catching every error.\nUser: {prompt}",
        reasoning={"effort": "low"},
        text={"verbosity": "medium"},
        max_output_tokens=4096
    )

    try:
        content = response.output_text.strip()
        print(f"\n📤 GPT RAW RESPONSE for '{label}':\n{content}\n")
        highlighted_line = ""
        corrected_line = ""

        # Parse the response more robustly
        highlight_section = re.search(r'Highlighted:(.*?)(?:Corrected:|$)', content, re.DOTALL)
        if highlight_section:
            highlighted_line = highlight_section.group(1).strip()

        correct_section = re.search(r'Corrected:(.*?)(?:$)', content, re.DOTALL)
        if correct_section:
            corrected_line = correct_section.group(1).strip()
            # Strip trailing "---" if GPT adds it
            corrected_line = re.sub(r"\s*---$", "", corrected_line.strip())

        # Fallback if parsing fails
        if not highlighted_line or not corrected_line:
            print(f"⚠️ Failed to parse GPT-5-mini response for {label} - defaulting to no errors")
            clean_text = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", text)
            return {"highlighted": clean_text, "corrected": "✅ No correction needed."}

        # These additional safety checks make the checker more reliable

        # 1. Remove any false positives for common financial abbreviations and terms
        # Strip highlights from number+mln/bln/tln patterns (e.g. <span>761mln</span>)
        highlighted_line = re.sub(
            r"<span style='color:red'>(\d{1,4}(\.\d{1,3})?(mln|bln|tln))</span>",
            r"\1",
            highlighted_line,
            flags=re.IGNORECASE
        )

        for abbrev in ['mln', 'bln', 'tln', 'prev', 'prev.', 'apr', 'apr.', 'q1', 'q2', 'q3', 'q4', 'pmi', 'ytd', 'yoy',
                       's&p', 'usd', 'eur', 'gbp', 'jpy', 'cny', 'corp', 'ecb', 'boj', 'boe', 'fed', 'cpi', 'gdp',
                       'nfp']:
            highlighted_line = re.sub(
                rf"<span style='color:red'>({abbrev})</span>",  # Capture inside group
                r"\1",  # Keep the word, remove span
                highlighted_line,
                flags=re.IGNORECASE
            )

        # 2. If no actual span tags found, assume no errors detected
        # 2. If no actual span tags found, assume no errors detected
        if "<span" not in highlighted_line:
            return {"highlighted": text, "corrected": "✅ No correction needed."}
        else:
            # ✅ Double-check the flagged errors to confirm they're valid
            flagged_errors = re.findall(r"<span style='color:red'>(.*?)</span>", highlighted_line)
            verified_errors = []

            for err in flagged_errors:
                verification_prompt = f"""
        Is the following word or phrase clearly incorrect in the context of financial news headlines?

        Text: {text}
        Flagged: "{err}"

        Respond with one word only: Yes or No.
        """
                try:
                    verify_response = client.responses.create(
                        model="gpt-5-mini",
                        input=f"System: You are a strict grammar verifier for financial news.\nUser: {verification_prompt}",
                        reasoning={"effort": "low"},
                        text={"verbosity": "low"},
                        max_output_tokens=32
                    )
                    verdict = verify_response.output_text.strip().lower()

                    if verdict == "yes":
                        verified_errors.append(err)
                except Exception as e:
                    print(f"⚠️ Verification failed for '{err}': {e}")
                    verified_errors.append(err)  # Assume valid if verification fails

            # 🚫 If none of the errors are confirmed as real mistakes, discard the correction
            if not verified_errors:
                print("❌ Double-check failed: all flagged errors were unconfirmed")
                return {"highlighted": text, "corrected": "✅ No correction needed."}

        # 3. Compare highlighted text with original text (ignoring spans)
        plain_highlighted = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", highlighted_line)
        if plain_highlighted.strip() != text.strip():
            print(f"⚠️ Mismatch between original and highlighted text - defaulting to no errors")
            return {"highlighted": text, "corrected": "✅ No correction needed."}

        # 4. Count the number of red spans - if more than 2, be suspicious and reject
        error_count = len(re.findall(r"<span style='color:red'>(.*?)</span>", highlighted_line))
        if error_count > 2 and len(text.split()) > 10:
            print(f"⚠️ Suspiciously high number of errors ({error_count}) - defaulting to no errors")
            return {"highlighted": text, "corrected": "✅ No correction needed."}

        # 5. Check each flagged error to make sure it's not a false positive
        flagged_errors = re.findall(r"<span style='color:red'>(.*?)</span>", highlighted_line)
        safe_terms = ['mln', 'bln', 'tln', 'prev', 'prev.', 'apr', 'apr.', 'q1', 'q2', 'q3', 'q4', 'pmi', 'ytd', 'yoy',
                      's&p', 'usd', 'eur', 'gbp', 'jpy', 'cny', 'policy', 'policies', 'tariff', 'tariffs', 'fed',
                      'ecb', 'boe', 'boj', 'mfg', 'pce', 'cpi', 'gdp', 'nfp', 'fomc', 'rba', 'cba', 'corp', 'ism',
                      'long', 'force majeure', 'short', 'bid', 'ask', 'bull', 'bear', 'hawkish', 'dovish', 'fiscal',
                      'monetary',
                      'repo', 'reverse repo', 'swap', 'spread', 'curve', 'yield', 'open', 'close', 'high', 'low']

        # Remove false positives that are approved proper names
        for error in flagged_errors:
            if error in APPROVED_TERMS:
                highlighted_line = highlighted_line.replace(f"<span style='color:red'>{error}</span>", error)
                print(f"⚠️ Whitelisted term ignored: {error}")

        safe_errors = []
        for error in flagged_errors:
            if error.lower() in safe_terms:
                highlighted_line = highlighted_line.replace(f"<span style='color:red'>{error}</span>", error)
                safe_errors.append(error)

        # 🚫 Discard correction only if **every** flagged word is safe (in safe_terms or APPROVED_TERMS)
        all_safe = True
        for error in flagged_errors:
            if error.lower() not in safe_terms and error not in APPROVED_TERMS:
                all_safe = False
                break

        if all_safe and flagged_errors:
            print(f"⚠️ All flagged errors are safe: {flagged_errors} — skipping correction")
            return {"highlighted": text, "corrected": "✅ No correction needed."}

        # 🚫 Reject completions that change 4-digit years (e.g., 2026 → 2025)
        year_pattern = re.compile(r'\b20\d{2}\b')
        original_years = set(year_pattern.findall(text))
        corrected_years = set(year_pattern.findall(corrected_line))
        if original_years != corrected_years:
            print(f"⚠️ Year change detected ({original_years} → {corrected_years}) — rejecting correction")
            return {"highlighted": text, "corrected": "✅ No correction needed."}

        # ⛔ Reject numeric-only highlights like "2.0" or "2.0 (Prev. 8.0)"
        numeric_only = all(re.match(r"^[\d\s\.\(\)\-%]+$", err) for err in flagged_errors)
        if numeric_only:
            print("⚠️ Ignoring numeric-only highlight — likely a false positive")
            return {"highlighted": text, "corrected": "✅ No correction needed."}

        # 6. If everything looks good, proceed with the correction
        green_corrected = apply_green_highlight(original_text=text, highlighted_text=highlighted_line,
                                                corrected_text=corrected_line)

        # 7. Final check for identical content (no actual corrections)
        plain_corrected = re.sub(r"<span style='color:green'>(.*?)</span>", r"\1", green_corrected)
        if plain_corrected.strip() == text.strip():
            return {"highlighted": text, "corrected": "✅ No correction needed."}

        return {"highlighted": highlighted_line, "corrected": green_corrected}

    except Exception as e:
        print(f"❌ Error processing grammar check: {e}")
        return {"highlighted": text, "corrected": "✅ No correction needed."}


@app.route("/")
def index():
    try:
        for r in live_rows:
            try:
                # Localize timestamp to BST if present
                if r.get("timestamp"):
                    r["timestamp"] = r["timestamp"].astimezone(london_tz)

                headline = r.get("Headline", {})
                corrected = headline.get("corrected", "")
                # print(f"🧾 Flask rendering - {r.get('id')} @ {r.get('timestamp')} | Headline: {corrected[:60]}")
            except Exception as e:
                traceback.print_exc()

        template_path = os.path.join(app.template_folder or "templates", "index.html")
        if not os.path.exists(template_path):
            print(f"❌ Template not found at: {template_path}")
            abort(500)

        # ✅ Check for uncorrected rows (containing red-highlighted errors)
        uncorrected_rows = []
        for r in live_rows:
            for section in ["Headline", "Reaction", "Analysis", "Body"]:
                if "<span style='color:red'>" in r.get(section, {}).get("highlighted", ""):
                    uncorrected_rows.append({
                        "section": section,
                        "timestamp": r.get("timestamp")
                    })
                    break  # Only flag once per row

        return render_template("index.html", rows=live_rows, uncorrected_rows=uncorrected_rows)

    except Exception as e:
        error_message = traceback.format_exc()
        print("❌ Top-level render error:")
        print(error_message)
        sys.stdout.flush()
        return f"<pre>🔥 INTERNAL SERVER ERROR:\n\n{error_message}</pre>", 500

@app.route("/dismiss", methods=["POST"])
def dismiss_specific_error():
    data = request.json
    row_id = data.get("id")
    section = data.get("section")

    for r in live_rows:
        if str(r.get("id")) == str(row_id):
            if section in r and "<span style='color:red'>" in r[section].get("highlighted", ""):
                clean = re.sub(r"<span style='color:red'>(.*?)</span>", r"\1", r[section]["highlighted"])
                r[section]["highlighted"] = clean
                r[section]["corrected"] = "✅ No correction needed."
                print(f"✅ Dismissed error in {section} for ID {row_id}")
                break

    return "", 204



if __name__ == "__main__":
    threading.Thread(target=poll_newsquawk, daemon=True).start()

    class QuietHandler(WSGIRequestHandler):
        def log_request(self, code='-', size='-'):
            pass  # suppress only request logs

    log = logging.getLogger('werkzeug')
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.INFO)
    log.addHandler(handler)

    # ✅ Run on all interfaces, port 5001 so others can access it
    app.run(host="0.0.0.0", port=5001, debug=True, request_handler=QuietHandler)




