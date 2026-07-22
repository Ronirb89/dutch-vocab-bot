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
TARGET_MINUTE_WINDOW = 30 # Allow script to run within the first 30 minutes of the hour

MIN_WORDS = 5
MAX_WORDS = 10

# Public Dutch frequency list
FREQUENCY_LIST_URL = "https://raw.githubusercontent.com/hermitdave/FrequencyWords/master/content/2018/nl/nl_50k.txt"
# Adjusted range to pick words more likely to be common lemmas
# Start at 300 to skip very top function words like articles/pronouns/prepositions
# End at 2500 to avoid too many obscure or highly inflected words
WORD_SELECTION_START_INDEX = 300
WORD_SELECTION_END_INDEX = 2500

# Headers to make the request appear more like a browser
REQUEST_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/100.0.0.0 Safari/537.36',
    'Accept': 'application/json' # Explicitly request JSON
}

# --- Helper Functions ---
def load_json_file(filename, default):
    if not os.path.exists(filename):
        return default
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        print(f"Warning: {filename} is corrupted or empty. Starting with default.")
        return default


def save_json_file(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_correct_local_time():
    #now = datetime.now(ZoneInfo(TIMEZONE))
    # Check if it's the target day and the current hour is the target hour
    # We allow a window of TARGET_MINUTE_WINDOW minutes of the hour to account for
    # minor cron schedule variations, ensuring it sends only once.
   # return now.weekday() in TARGET_DAYS and now.hour == TARGET_HOUR and now.minute < TARGET_MINUTE_WINDOW
    return True
    


def fetch_frequency_words():
    print(f"Fetching frequency list from: {FREQUENCY_LIST_URL}")
    try:
        r = requests.get(FREQUENCY_LIST_URL, timeout=30)
        r.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
    except requests.exceptions.RequestException as e:
        print(f"Failed to fetch frequency list: {e}")
        return []

    words = []
    for line in r.text.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        word = parts[0].strip().lower()

        # Basic filtering to get clean words suitable for dictionary lookup
        if not re.match(r"^[a-záéíóúàèìòùâêîôûäëïöüç' -]+$", word):
            continue
        if len(word) < 3 or len(word) > 25: # Exclude very short or very long words
            continue
        if word.isdigit(): # Exclude numbers
            continue
        # Heuristic to filter out common articles/prepositions that are too simple
        if word in ["de", "het", "een", "in", "op", "aan", "voor", "naar", "met", "uit", "om", "te", "bij", "en", "of"]:
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


def get_wiktionary_info(word):
    """
    Uses en.wiktionary REST API to fetch definition summary.
    This is free but quality can vary.
    """
    url = f"https://en.wiktionary.org/api/rest_v1/page/definition/{word}"
    try:
        # Use the defined headers
        r = requests.get(url, headers=REQUEST_HEADERS, timeout=20)
        
        if r.status_code == 403:
            print(f"Wiktionary API for '{word}' returned status 403 (Forbidden). IP/User-Agent blocking.")
            return None
        if r.status_code == 404:
            # print(f"Wiktionary API for '{word}' returned status 404 (Not Found).")
            return None # Word not found, which is common
        if r.status_code != 200:
            print(f"Wiktionary API for '{word}' returned status {r.status_code} (not 200 or 404).")
            return None
        
        data = r.json()

        # Try to find Dutch definition
        if "nl" in data and data["nl"]:
            entries = data["nl"]
            meanings = []
            pos = None
            example = None

            # Iterate through all Dutch entries to find the best info
            for entry in entries:
                current_pos = entry.get("partOfSpeech")
                # Prioritize common parts of speech for our purposes
                if current_pos in ["noun", "verb", "adjective", "adverb"]: # Added adverb to POS
                    pos = current_pos
                elif not pos and current_pos: # Take first POS if preferred not found
                    pos = current_pos

                definitions = entry.get("definitions", [])
                for d in definitions:
                    definition_text = d.get("definition")
                    # Exclude definitions that are just alternative forms
                    if definition_text and not definition_text.lower().startswith("alternative form of"):
                        meanings.append(definition_text)

                    examples = d.get("examples", [])
                    if not example and examples: # Take first example
                        example = examples[0]
            
            meaning = meanings[0] if meanings else None
            # If a Dutch entry is found, we use it, even if meaning/example are empty,
            # as it helps with POS filtering
            return {
                "word": word,
                "pos": pos,
                "meaning": meaning,
                "example": example
            }
        
        # Fallback to English definition if no Dutch section found
        if "en" in data and data["en"]:
            # print(f"Wiktionary API for '{word}' did not contain 'nl' section. Trying English fallback.")
            for entry in data["en"]:
                definitions = entry.get("definitions", [])
                if definitions:
                    return {
                        "word": word,
                        "pos": entry.get("partOfSpeech", "unknown"),
                        "meaning": definitions[0].get("definition", "meaning not found (English fallback)"),
                        "example": definitions[0].get("examples", [None])[0]
                    }
        
        # print(f"No Dutch or English definition found for '{word}'.")
        return None # No definition found in either Dutch or English


def clean_text(text):
    if not text:
        return None
    # Remove HTML tags (e.g., <i>)
    text = re.sub(r"<[^>]+>", "", text)
    # Remove MediaWiki internal links like [[word]]
    text = re.sub(r"\[\[[^\]]+\]\]", "", text)
    # Remove text in parentheses or curly braces.
    # Be careful not to remove actual grammatical parentheses from definitions.
    # We remove (text) or {text} that might be metadata.
    text = re.sub(r"\s*\(.*?\)\s*|\s*\{.*?\}\s*", " ", text)
    # Remove MediaWiki italics/bold markers (e.g., '''bold''', ''italics'')
    text = re.sub(r"\'\'\'|\'\'|\`\`\`|\`\`", "", text)
    # Remove common Wiktionary internal template calls like {{tag}} or {{l|nl|word}}
    text = re.sub(r"\{\{.*?\}\}", "", text)
    # Remove remnants of w-parser-output or similar HTML/CSS artifacts that might leak
    text = re.sub(r"\b(w-parser-output|object-usage-tag|deprecatedwith)\S*", "", text, flags=re.IGNORECASE)
    # Remove stray punctuation that might be left from filtering
    text = re.sub(r"[.;,:]\s*$", "", text) # Remove trailing .;, at end of line
    text = re.sub(r"\s+\.\s*", ". ", text) # Normalize spaced periods
    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


def build_entry_data(word):
    """Builds a structured dictionary for a word, attempting to get data from Wiktionary."""
    info = get_wiktionary_info(word)

    if info:
        meaning = clean_text(info.get("meaning"))
        pos = clean_text(info.get("pos"))
        example = clean_text(info.get("example"))

        return {
            "word": word,
            "meaning": meaning,
            "pos": pos,
            "example": example
        }
    return None # Return None if Wiktionary info extraction failed or was insufficient


def pick_quality_words(all_words, sent_words_history, desired_count):
    sent_set = set(sent_words_history)
    
    # Filter for the desired difficulty range from the frequency list
    filtered_for_level = all_words[WORD_SELECTION_START_INDEX:WORD_SELECTION_END_INDEX]

    # Filter out words that were already sent
    unsent_potential_candidates = [w for w in filtered_for_level if w not in sent_set]

    # If we don't have enough truly unsent words in the current selection range to fill
    # a reasonably sized pool (e.g., 5 times the desired_count), reset the history.
    # This reset happens BEFORE extensive API calls, for efficiency.
    if len(unsent_potential_candidates) < desired_count * 5 and len(sent_words_history) > 0: 
        print(f"Not enough truly unsent words ({len(unsent_potential_candidates)}) in current range. Resetting sent history.")
        # Only clear words that are within the current selection range if we want to be precise.
        # For simplicity and to ensure fresh words, clearing all history.
        sent_words_history.clear() 
        # After clearing, re-populate unsent_potential_candidates
        unsent_potential_candidates = [w for w in filtered_for_level if w not in sent_set]


    # Shuffle the potential candidates to ensure randomness before fetching detailed info
    random.shuffle(unsent_potential_candidates)

    quality_entries = []
    # Try to build entries for a larger pool to ensure we find enough quality words
    POOL_MAX_ATTEMPTS = max(desired_count * 10, 100) # Try up to 100 words or 10x desired_count
    attempts = 0

    print(f"Attempting to find {desired_count} quality words from {len(unsent_potential_candidates)} candidates.")

    for word in unsent_potential_candidates:
        if len(quality_entries) >= desired_count and attempts >= desired_count * 2: # Stop early if we have enough and tried a decent number
             # print(f"  Enough quality words found ({len(quality_entries)}). Stopping search.")
             break
        if attempts >= POOL_MAX_ATTEMPTS: # Stop if we've tried too many words without success
            print(f"  Reached max attempts ({POOL_MAX_ATTEMPTS}) without enough quality words.")
            break

        attempts += 1
        entry_data = build_entry_data(word) # Call to get Wiktionary info

        if entry_data:
            # Only include words if they have a non-empty meaning AND a preferred POS
            if entry_data["meaning"] and entry_data["meaning"].lower() != "meaning not found":
                # Only accept Noun, Verb, Adjective, Adverb for core vocabulary
                if entry_data["pos"] in ["noun", "verb", "adjective", "adverb"]:
                    quality_entries.append(entry_data)
                # else:
                    # print(f"  Skipping '{word}' due to undesired POS: '{entry_data['pos']}'")
            # else:
                # print(f"  Skipping '{word}' due to no meaning found.")
        # else:
            # print(f"  Skipping '{word}' due to build_entry_data returning None (API error/not found).")


    # Final selection from the quality entries
    if len(quality_entries) < desired_count:
        print(f"WARNING: Could only find {len(quality_entries)} high-quality words after filtering. Picking all available.")
        selected_entries = quality_entries
    else:
        selected_entries = random.sample(quality_entries, desired_count)

    # Extract just the words for history tracking
    selected_words_for_history = [entry["word"] for entry in selected_entries]
    
    return selected_entries, selected_words_for_history


def format_message(entries):
    now = datetime.now(ZoneInfo(TIMEZONE))
    today = now.strftime("%A, %d %B %Y")
    lines = [f"🇳🇱 Dutch Vocabulary — {today}\n"]

    for i, e in enumerate(entries, start=1):
        pos_part = f" ({e['pos'].capitalize()})" if e.get("pos") else "" # Capitalize POS
        meaning_text = e['meaning'] if e.get('meaning') else "Meaning not found."
        example_text = e['example'] if e.get('example') else f"Voorbeeld: Ik gebruik het woord '{e['word']}' in een zin."

        lines.append(
            f"{i}. *{e['word']}*{pos_part}\n"
            f"   Meaning: {meaning_text}\n"
            f"   _{example_text}_\n"
        )

    lines.append("Veel succes met leren! 📚")
    return "\n".join(lines)


def send_telegram_message(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown" # Using Markdown for formatting
    }
    try:
        r = requests.post(url, data=payload, timeout=30)
        r.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
    except requests.exceptions.RequestException as e:
        print(f"Failed to send Telegram message: {e}")
        raise # Re-raise to ensure workflow fails


def git_commit_and_push():
    try:
        # Use github-actions bot user for commit
        subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
        subprocess.run(["git", "add", SENT_FILE], check=True)

        diff_result = subprocess.run(["git", "diff", "--cached", "--quiet"], check=False)

        if diff_result.returncode != 0: # If there are changes
            subprocess.run(["git", "commit", "-m", "Update sent words history"], check=True)
            subprocess.run(["git", "push"], check=True)
            print("sent_words.json committed and pushed.")
        else:
            print("No changes to commit in sent_words.json.")
    except Exception as e:
        print(f"Git commit/push skipped or failed: {e}. Check repository permissions.")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise ValueError("Please set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")

    # Time check (can be temporarily set to return True for testing outside scheduled times)
    # E.g., for testing: `if not is_correct_local_time() and os.getenv("GITHUB_REF") != "refs/heads/test-branch":`
    if not is_correct_local_time():
        print(f"Not the correct Amsterdam local time ({datetime.now(ZoneInfo(TIMEZONE)).strftime('%A, %H:%M')}). Exiting.")
        return

    print("Starting vocabulary generation and sending.")
    sent_words_history = load_json_file(SENT_FILE, [])
    print(f"Loaded {len(sent_words_history)} previously sent words into history.")

    all_words = fetch_frequency_words()
    if not all_words:
        raise ValueError("Could not fetch or process frequency list. Exiting.")

    desired_word_count = random.randint(MIN_WORDS, MAX_WORDS)
    
    # Call the modified pick_quality_words
    final_entries, words_for_history = pick_quality_words(all_words, sent_words_history, desired_word_count)

    if not final_entries:
        print("No high-quality words could be selected after filtering. Exiting.")
        return # Exit if no words could be selected

    print(f"Final selected words: {[entry['word'] for entry in final_entries]}")

    message = format_message(final_entries)
    send_telegram_message(BOT_TOKEN, CHAT_ID, message)
    print("Telegram message sent successfully.")

    # Update sent words history with just the words that were sent
    sent_words_history.extend(words_for_history)
    save_json_file(SENT_FILE, sent_words_history)
    print(f"Updated sent_words.json with {len(words_for_history)} new words. Total sent: {len(sent_words_history)}")

    # Only attempt git commit/push if running in GitHub Actions
    if os.getenv("GITHUB_ACTIONS") == "true":
        git_commit_and_push()


if __name__ == "__main__":
    main()
