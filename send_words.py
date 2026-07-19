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
TARGET_DAYS = {0, 2}  # Monday, Wednesday
TARGET_HOUR = 12

MIN_WORDS = 5
MAX_WORDS = 10

# Public Dutch frequency list
FREQUENCY_LIST_URL = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/nl/nl_50k.txt"


def load_json_file(filename, default):
    if not os.path.exists(filename):
        return default
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json_file(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_correct_local_time():
    return True
   # now = datetime.now(ZoneInfo(TIMEZONE))
   # return now.weekday() in TARGET_DAYS and now.hour == TARGET_HOUR


def fetch_frequency_words():
    r = requests.get(FREQUENCY_LIST_URL, timeout=30)
    r.raise_for_status()
    words = []

    for line in r.text.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        word = parts[0].strip().lower()

        # Basic filtering
        if not re.match(r"^[a-záéíóúàèìòùâêîôûäëïöüç' -]+$", word):
            continue
        if len(word) < 3 or len(word) > 20:
            continue
        if word.isdigit():
            continue

        words.append(word)

    # Deduplicate while preserving order
    seen = set()
    unique_words = []
    for w in words:
        if w not in seen:
            seen.add(w)
            unique_words.append(w)

    return unique_words


def pick_candidate_words(all_words, sent_words, count):
    sent_set = set(sent_words)

    # Use common-word slice for beginner/intermediate:
    # skip the very top function words, then use a broad common range
    filtered = all_words[150:5000]

    unsent = [w for w in filtered if w not in sent_set]

    if len(unsent) < count:
        sent_words.clear()
        unsent = filtered[:]

    return random.sample(unsent, min(count, len(unsent)))


def get_wiktionary_info(word):
    """
    Uses en.wiktionary REST API to fetch page HTML summary-ish content.
    Not perfect, but free and often enough.
    """
    url = f"https://en.wiktionary.org/api/rest_v1/page/definition/{word}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()

        if "nl" not in data:
            return None

        entries = data["nl"]
        meanings = []
        pos = None
        example = None

        for entry in entries:
            if not pos and "partOfSpeech" in entry:
                pos = entry["partOfSpeech"]

            definitions = entry.get("definitions", [])
            for d in definitions:
                definition_text = d.get("definition")
                if definition_text:
                    meanings.append(definition_text)

                examples = d.get("examples", [])
                if not example and examples:
                    example = examples[0]

        meaning = meanings[0] if meanings else None

        return {
            "word": word,
            "pos": pos,
            "meaning": meaning,
            "example": example
        }
    except Exception:
        return None


def clean_text(text):
    if not text:
        return None
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
        raise ValueError("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")

    if not is_correct_local_time():
        print("Not the correct Amsterdam local time. Exiting.")
        return

    sent_words = load_json_file(SENT_FILE, [])

    all_words = fetch_frequency_words()
    count = random.randint(MIN_WORDS, MAX_WORDS)
    selected_words = pick_candidate_words(all_words, sent_words, count)

    entries = [build_entry(w) for w in selected_words]

    message = format_message(entries)
    send_telegram_message(BOT_TOKEN, CHAT_ID, message)

    sent_words.extend(selected_words)
    save_json_file(SENT_FILE, sent_words)

    if os.getenv("GITHUB_ACTIONS") == "true":
        git_commit_and_push()


if __name__ == "__main__":
    main()
