import hashlib
import json
import re
import urllib.request
import urllib.error
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo


STATE_FILE = Path("state.json")
SITES_FILE = Path("sites.json")
MAX_HISTORY = 500


class VisibleTextExtractor(HTMLParser):
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
        if (
            tag.lower() in self.ignored_tags
            and self.ignore_depth > 0
        ):
            self.ignore_depth -= 1

    def handle_data(self, data):
        if self.ignore_depth == 0:
            text = data.strip()

            if text:
                self.text_parts.append(text)

    def get_text(self):
        return " ".join(self.text_parts)


def load_json(path):
    if not path.exists():
        raise FileNotFoundError(
            f"File non trovato: {path}"
        )

    with path.open(
        "r",
        encoding="utf-8"
    ) as file:
        return json.load(file)


def save_state(state):
    with STATE_FILE.open(
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2
        )

        file.write("\n")


def download_page(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(compatible; GitHubPageMonitor/2.0)"
            ),
            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "*/*;q=0.8"
            ),
            "Accept-Language": "it-IT,it;q=0.9,en;q=0.8"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=30
    ) as response:

        charset = (
            response.headers.get_content_charset()
            or "utf-8"
        )

        return response.read().decode(
            charset,
            errors="replace"
        )


def extract_visible_text(html):
    parser = VisibleTextExtractor()
    parser.feed(html)

    text = parser.get_text()

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def calculate_hash(text):
    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


def create_empty_site_state(site):
    return {
        "name": site["name"],
        "url": site["url"],
        "current_hash": None,
        "last_checked": None,
        "last_changed": None,
        "last_changed_check": None,
        "last_error": None,
        "total_checks": 0,
        "history": []
    }


def check_site(site, old_site_state):
    site_id = site["id"]
    name = site["name"]
    url = site["url"]

    print()
    print("=" * 60)
    print(f"Sito: {name}")
    print(f"URL: {url}")
    print("=" * 60)

    state = old_site_state or create_empty_site_state(
        site
    )

    # Aggiorniamo nome e URL nel caso siano stati
    # modificati in sites.json
    state["name"] = name
    state["url"] = url

    now = datetime.now(
        ZoneInfo("Europe/Rome")
    ).isoformat(timespec="seconds")

    check_number = (
        state.get("total_checks", 0) + 1
    )

    try:
        html = download_page(url)

        visible_text = extract_visible_text(
            html
        )

        if not visible_text:
            raise ValueError(
                "La pagina non contiene testo visibile."
            )

        new_hash = calculate_hash(
            visible_text
        )

        old_hash = state.get(
            "current_hash"
        )

        if old_hash is None:

            status = "baseline"
            changed = False

            print(
                f"Controllo #{check_number}: "
                "baseline creata."
            )

        elif new_hash != old_hash:

            status = "changed"
            changed = True

            print(
                f"Controllo #{check_number}: "
                "CAMBIAMENTO RILEVATO!"
            )

        else:

            status = "unchanged"
            changed = False

            print(
                f"Controllo #{check_number}: "
                "nessun cambiamento."
            )

        history_entry = {
            "check": check_number,
            "timestamp": now,
            "changed": changed,
            "status": status
        }

        state["current_hash"] = new_hash
        state["last_checked"] = now
        state["last_error"] = None
        state["total_checks"] = check_number

        if changed:
            state["last_changed"] = now
            state[
                "last_changed_check"
            ] = check_number

    except Exception as error:

        error_message = (
            f"{type(error).__name__}: {error}"
        )

        print(
            f"Controllo #{check_number}: "
            f"ERRORE — {error_message}"
        )

        history_entry = {
            "check": check_number,
            "timestamp": now,
            "changed": False,
            "status": "error",
            "error": error_message
        }

        state["last_checked"] = now
        state["last_error"] = error_message
        state["total_checks"] = check_number

        # Importante:
        # in caso di errore NON modifichiamo
        # current_hash.

    history = state.get(
        "history",
        []
    )

    history.append(
        history_entry
    )

    state["history"] = history[
        -MAX_HISTORY:
    ]

    return state


def validate_sites(config):
    sites = config.get(
        "sites"
    )

    if not isinstance(
        sites,
        list
    ):
        raise ValueError(
            "sites.json deve contenere "
            'una lista chiamata "sites".'
        )

    ids = set()

    for site in sites:

        for field in (
            "id",
            "name",
            "url"
        ):

            if not site.get(field):
                raise ValueError(
                    f"Campo '{field}' mancante "
                    "in sites.json."
                )

        site_id = site["id"]

        if site_id in ids:
            raise ValueError(
                f"ID duplicato: {site_id}"
            )

        ids.add(site_id)

        if not (
            site["url"].startswith(
                "http://"
            )
            or site["url"].startswith(
                "https://"
            )
        ):
            raise ValueError(
                f"URL non valido per {site_id}"
            )

    return sites


def main():
    config = load_json(
        SITES_FILE
    )

    sites = validate_sites(
        config
    )

    try:
        old_state = load_json(
            STATE_FILE
        )
    except FileNotFoundError:
        old_state = {
            "sites": {}
        }

    old_sites = old_state.get(
        "sites",
        {}
    )

    new_state = {
        "last_run": datetime.now(
            ZoneInfo("Europe/Rome")
        ).isoformat(
            timespec="seconds"
        ),
        "sites": {}
    }

    print(
        f"Siti da controllare: {len(sites)}"
    )

    for site in sites:

        site_id = site["id"]

        old_site_state = old_sites.get(
            site_id
        )

        new_state["sites"][
            site_id
        ] = check_site(
            site,
            old_site_state
        )

    save_state(
        new_state
    )

    print()
    print("=" * 60)
    print("Controllo completato.")
    print("=" * 60)

    for site_id, site_state in (
        new_state["sites"].items()
    ):

        latest = (
            site_state["history"][-1]
        )

        print(
            f"{site_state['name']}: "
            f"{latest['status']}"
        )


if __name__ == "__main__":
    main()
