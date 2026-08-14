import hashlib
import json
import re
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo


STATE_FILE = Path("state.json")


class VisibleTextExtractor(HTMLParser):
    """
    Estrae il testo visibile dalla pagina ignorando script,
    style e altri elementi non utili al confronto.
    """

    def __init__(self):
        super().__init__()
        self.text_parts = []
        self.ignore_depth = 0
        self.ignored_tags = {
            "script",
            "style",
            "noscript",
            "svg",
            "canvas",
            "template"
        }

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.ignored_tags:
            self.ignore_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self.ignored_tags and self.ignore_depth > 0:
            self.ignore_depth -= 1

    def handle_data(self, data):
        if self.ignore_depth == 0:
            text = data.strip()

            if text:
                self.text_parts.append(text)

    def get_text(self):
        return " ".join(self.text_parts)


def load_state():
    """Carica lo stato precedente da state.json."""
    if not STATE_FILE.exists():
        raise FileNotFoundError("state.json non trovato")

    with STATE_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state(state):
    """Salva il nuovo stato."""
    with STATE_FILE.open("w", encoding="utf-8") as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2
        )

        file.write("\n")


def download_page(url):
    """Scarica la pagina da controllare."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; GitHubPageMonitor/1.0)"
            )
        }
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        charset = response.headers.get_content_charset() or "utf-8"

        return response.read().decode(
            charset,
            errors="replace"
        )


def extract_visible_text(html):
    """Estrae e normalizza il testo visibile."""
    parser = VisibleTextExtractor()
    parser.feed(html)

    text = parser.get_text()

    # Trasforma sequenze di spazi, tab e newline in un singolo spazio
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def calculate_hash(text):
    """Calcola l'hash SHA-256 del contenuto."""
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def main():
    state = load_state()

    url = state["url"]

    print(f"Controllo pagina: {url}")

    html = download_page(url)
    visible_text = extract_visible_text(html)
    new_hash = calculate_hash(visible_text)

    old_hash = state.get("current_hash")

    now = datetime.now(
        ZoneInfo("Europe/Rome")
    ).isoformat(timespec="seconds")

    total_checks = state.get("total_checks", 0) + 1

    # Primo controllo: crea semplicemente la baseline
    if old_hash is None:
        changed = False
        status = "baseline"

        print("Primo controllo: baseline creata.")

    else:
        changed = new_hash != old_hash

        if changed:
            status = "changed"
            print("CAMBIAMENTO RILEVATO!")

        else:
            status = "unchanged"
            print("Nessun cambiamento rilevato.")

    history_entry = {
        "check": total_checks,
        "timestamp": now,
        "changed": changed,
        "status": status
    }

    history = state.get("history", [])
    history.append(history_entry)

    state["current_hash"] = new_hash
    state["last_checked"] = now
    state["total_checks"] = total_checks
    state["history"] = history

    if changed:
        state["last_changed"] = now
        state["last_changed_check"] = total_checks

    # Manteniamo al massimo gli ultimi 500 controlli
    state["history"] = state["history"][-500:]

    save_state(state)

    print(f"Controllo #{total_checks} completato.")
    print(f"Hash: {new_hash}")


if __name__ == "__main__":
    main()
