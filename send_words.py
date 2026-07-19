import json
import os
import random
import re
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SENT_FILE = "sent_words.json"

TIMEZONE = "Europe/Amsterdam"
TARGET_DAYS = {0, 2}  # Monday, Wednesday (0=Monday, 6=Sunday)
TARGET_HOUR = 12

MIN_WORDS = 5
MAX_WORDS = 10

# Public Dutch frequency list
FREQUENCY_LIST_URL = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/nl/nl_50k.txt"
# Adjusted range to pick words more likely to be common lemmas
# Start at 300 to skip very common articles/pronouns/prepositions
# End at 2500 to avoid too many obscure or highly inflected words
WORD_SELECTION_START_INDEX = 300
WORD_SELECTION_END_INDEX = 2500


def load_json_file(filename, default):
    if not os.path.exists(filename):
        return default
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_correct_local_time():
    return TRUE
    #now = datetime.now(ZoneInfo(TIMEZONE))
    # Check if it's the target day and the current hour is the target hour
    # We allow a window of the first 10 minutes of the hour to account for
    # minor cron schedule variations, ensuring it sends only once.
   # return now.weekday() in TARGET_DAYS and now.hour == TARGET_HOUR and now.minute < 10


def fetch_frequency_words():
    print(f"Fetching frequency list from: {FREQUENCY_LIST_URL}")
    r = requests.get(FREQUENCY_LIST_URL, timeout=30)
    r.raise_for_status()
    words = []

    for line in r.text.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        word = parts[0].strip().lower()

        # Basic filtering to get clean words
        if not re.match(r"^[a-záéíóúàèìòùâêîôûäëïöüç' -]+$", word):
            continue
        if len(word) < 3 or len(word) > 20: # Exclude very short and very long words
            continue
        if word.isdigit(): # Exclude numbers
            continue
        # Heuristic to filter out common articles/prepositions that are too simple
        if word in ["de", "het", "een", "in", "op", "aan", "voor", "naar", "met", "uit", "om", "te", "bij"]:
            continue

        words.append(word)

    # Deduplicate while preserving order
    seen = set()
    unique_words = []
    for w in words:
        if w not in seen:
            seen.add(w)
            unique_words.append(w)

    print(f"Fetched {len(unique_words)} unique words after initial filtering.")
    return unique_words


def pick_candidate_words(all_words, sent_words_history, count):
    sent_set = set(sent_words_history)

    # Apply the refined range from the frequency list
    filtered_for_level = all_words[WORD_SELECTION_START_INDEX:WORD_SELECTION_END_INDEX]

    # Filter out words that were already sent
    unsent_candidates = [w for w in filtered_for_level if w not in sent_set]

    # If we run out of unsent words in the current selection range,
    # reset the history for this range to start over.
    if len(unsent_candidates) < count:
        print("Ran out of unique words in the selected range. Resetting sent history.")
        # Only clear words that are within the current selection range
        # This prevents clearing history for words outside this range if we change WORD_SELECTION_START_INDEX/END_INDEX
        sent_words_history.clear() # Clear all for simplicity
        unsent_candidates = filtered_for_level[:] # Re-populate from the filtered range

    # Randomly select words from the unsent candidates
    selected = random.sample(unsent_candidates, min(count, len(unsent_candidates)))
    return selected


def get_wiktionary_info(word):
    """
    Uses en.wiktionary REST API to fetch page definition summary.
    This is free but quality can vary.
    """
    url = f"https://en.wiktionary.org/api/rest_v1/page/definition/{word}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            print(f"Wiktionary API for '{word}' returned status {r.status_code}")
            return None
        data = r.json()

        if "nl" not in data:
            print(f"Wiktionary API for '{word}' did not contain 'nl' (Dutch) section.")
            # If "nl" is not found, try to find a meaning in other languages (e.g., English)
            # This is a fallback to at least provide *some* meaning if Dutch is missing.
            if "en" in data and data["en"]:
                for entry in data["en"]:
                    definitions = entry.get("definitions", [])
                    if definitions:
                        return {
                            "word": word,
                            "pos": entry.get("partOfSpeech", "unknown"),
                            "meaning": definitions[0].get("definition", "meaning not found (English fallback)"),
                            "example": definitions[0].get("examples", [None])[0]
                        }
            return None # No Dutch definition found, and no English fallback
        
        entries = data["nl"]
        meanings = []
        pos = None
        example = None

        # Iterate through all Dutch entries to find the best info
        for entry in entries:
            # Prefer common parts of speech for our purposes
            current_pos = entry.get("partOfSpeech")
            if current_pos in ["noun", "verb", "adjective"]:
                pos = current_pos
            elif not pos and current_pos: # Take first POS if preferred not found
                pos = current_pos

            definitions = entry.get("definitions", [])
            for d in definitions:
                definition_text = d.get("definition")
                if definition_text and not definition_text.startswith("Alternative form of"): # Exclude alternative forms
                    meanings.append(definition_text)

                examples = d.get("examples", [])
                if not example and examples: # Take first example
                    example = examples[0]

        meaning = meanings[0] if meanings else None

        return {
            "word": word,
            "pos": pos,
            "meaning": meaning,
            "example": example
        }
    except requests.exceptions.Timeout:
        print(f"Wiktionary API request for '{word}' timed out.")
        return None
    except requests.exceptions.RequestException as e:
        print(f"Wiktionary API request for '{word}' failed: {e}")
        return None
    except Exception as e:
        print(f"Error parsing Wiktionary API response for '{word}': {e}")
        # print(f"Raw data for '{word}': {r.text if r else 'N/A'}") # Uncomment for deeper debugging
        return None


def clean_text(text):
    if not text:
        return None
    text = re.sub(r"<[^>]+>", "", text) # Remove HTML tags
    text = re.sub(r"\[\[[^\]]+\]\]", "", text) # Remove MediaWiki internal links
    text = re.sub(r"\(.*?\)|\{.*?\}", "", text) # Remove text in parentheses or curly braces
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_entry(word):
    info = get_wiktionary_info(word)

    if info:
        meaning = clean_text(info.get("meaning"))
        pos = clean_text(info.get("pos"))
        example = clean_text(info.get("example"))

        return {
            "word": word,
            "meaning": meaning or "meaning not found",
            "pos": pos or "unknown",
            "example": example or f"Voorbeeld: Ik gebruik het woord '{word}' in een zin."
        }

    # Fallback if Wiktionary API fails or returns nothing useful
    return {
        "word": word,
        "meaning": "meaning not found",
        "pos": "unknown",
        "example": f"Voorbeeld: Ik gebruik het woord '{word}' in een zin."
    }


def format_message(entries):
    now = datetime.now(ZoneInfo(TIMEZONE))
    today = now.strftime("%A, %d %B %Y")
    lines = [f"🇳🇱 Dutch Vocabulary — {today}\n"]

    for i, e in enumerate(entries, start=1):
        pos_part = f" ({e['pos']})" if e.get("pos") and e["pos"] != "unknown" else ""
        lines.append(
            f"{i}. *{e['word']}*{pos_part}\n"
            f"   Meaning: {e['meaning']}\n"
            f"   _{e['example']}_\n"
        )

    lines.append("Veel succes met leren! 📚")
    return "\n".join(lines)


def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    r = requests.post(url, data=payload, timeout=30)
    r.raise_for_status()


def git_commit_and_push():
    try:
        subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
        subprocess.run(["git", "add", SENT_FILE], check=True)

        diff_result = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)

        if diff_result.returncode != 0:
            subprocess.run(["git", "commit", "-m", "Update sent words"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("sent_words.json committed and pushed.")
        else:
            print("No changes to commit.")
    except Exception as e:
        print(f"Git commit/push skipped or failed: {e}")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")

    # Temporarily override for testing: uncomment the line below if you need to force a send
    # if os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("RUN_TEST_OVERRIDE") == "true":
    #     print("Test override enabled: skipping time check.")
    # else:
    if not is_correct_local_time():
        print("Not the correct Amsterdam local time (Mon/Wed at 12:00). Exiting.")
        return

    print("Starting vocabulary generation and sending.")
    sent_words = load_json_file(SENT_FILE, [])
    print(f"Loaded {len(sent_words)} previously sent words.")

    all_words = fetch_frequency_words()
    if not all_words:
        raise ValueError("Could not fetch or process frequency list.")

    count = random.randint(MIN_WORDS, MAX_WORDS)
    selected_words = pick_candidate_words(all_words, sent_words, count)
    print(f"Selected {len(selected_words)} words: {', '.join(selected_words)}")

    entries = []
    for word in selected_words:
        entry = build_entry(word)
        entries.append(entry)
        # print(f"Built entry for '{word}': {entry['meaning']}") # Debug line

    message = format_message(entries)
    send_telegram_message(BOT_TOKEN, CHAT_ID, message)
    print("Telegram message sent successfully.")

    # Update sent words history
    sent_words.extend(selected_words)
    save_json_file(SENT_FILE, sent_words)
    print(f"Updated sent_words.json with {len(selected_words)} new words. Total sent: {len(sent_words)}")


    if os.getenv("GITHUB_ACTIONS") == "true":
        git_commit_and_push()


if __name__ == "__main__":
    main()