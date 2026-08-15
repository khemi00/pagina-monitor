import hashlib
import json
import re
import urllib.request
from urllib.error import HTTPError
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError
)


STATE_FILE = Path("state.json")
SITES_FILE = Path("sites.json")

MAX_HISTORY = 500

SIMPLE_TIMEOUT = 30
BROWSER_TIMEOUT = 45000


# ============================================================
# ESTRAZIONE TESTO HTML
# ============================================================

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

        return " ".join(
            self.text_parts
        )


# ============================================================
# FUNZIONI GENERALI
# ============================================================

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


def normalize_text(text):

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def extract_visible_text(html):

    parser = VisibleTextExtractor()

    parser.feed(html)

    return normalize_text(
        parser.get_text()
    )


def calculate_hash(text):

    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()


# ============================================================
# RILEVAMENTO PAGINE CHE RICHIEDONO BROWSER
# ============================================================

def looks_like_browser_required(text):

    if not text:
        return True

    # Una pagina quasi vuota potrebbe essere soltanto
    # uno "shell" che viene riempito tramite JavaScript.
    if len(text) < 80:
        return True

    lower = text.lower()

    suspicious_messages = [
        "please enable javascript",
        "enable javascript to continue",
        "javascript is required",
        "you need to enable javascript",
        "checking your browser",
        "verify you are human",
        "just a moment",
        "attention required",
        "access denied"
    ]

    # Evitiamo falsi positivi su pagine grandi che
    # potrebbero contenere casualmente queste parole.
    if len(text) < 2000:

        for message in suspicious_messages:

            if message in lower:
                return True

    return False


# ============================================================
# METODO 1: RICHIESTA HTTP NORMALE
# ============================================================

def download_simple(url):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/130.0 Safari/537.36"
            ),

            "Accept": (
                "text/html,"
                "application/xhtml+xml,"
                "application/xml;q=0.9,"
                "*/*;q=0.8"
            ),

            "Accept-Language":
                "en-GB,en;q=0.9,it;q=0.8"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=SIMPLE_TIMEOUT
    ) as response:

        charset = (
            response.headers
            .get_content_charset()
            or "utf-8"
        )

        html = response.read().decode(
            charset,
            errors="replace"
        )

    return html


# ============================================================
# METODO 2: BROWSER CHROMIUM
# ============================================================

class BrowserFetcher:

    def __init__(self):

        self.playwright = None
        self.browser = None


    def start(self):

        if self.browser is not None:
            return

        print(
            "Avvio Chromium..."
        )

        self.playwright = (
            sync_playwright().start()
        )

        self.browser = (
            self.playwright
            .chromium
            .launch(
                headless=True
            )
        )


    def fetch_text(self, url):

        self.start()

        context = (
            self.browser.new_context(
                locale="en-GB",
                viewport={
                    "width": 1365,
                    "height": 900
                }
            )
        )

        page = context.new_page()

        try:

            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=BROWSER_TIMEOUT
            )

            try:

                page.locator(
                    "body"
                ).wait_for(
                    state="attached",
                    timeout=10000
                )

            except PlaywrightTimeoutError:

                pass


            # Lasciamo un breve tempo alle pagine
            # dinamiche per caricare i contenuti.
            page.wait_for_timeout(
                2500
            )


            text = page.locator(
                "body"
            ).inner_text(
                timeout=10000
            )

            text = normalize_text(
                text
            )


            status_code = None

            if response is not None:
                status_code = response.status


            if (
                status_code is not None
                and status_code >= 400
            ):

                raise RuntimeError(
                    f"Browser HTTP {status_code}"
                )


            if not text:

                raise RuntimeError(
                    "Il browser non ha trovato "
                    "testo visibile."
                )


            if looks_like_browser_required(
                text
            ):

                raise RuntimeError(
                    "Il sito continua a mostrare "
                    "una pagina di blocco o verifica."
                )


            return text


        finally:

            context.close()


    def close(self):

        if self.browser is not None:

            self.browser.close()

            self.browser = None


        if self.playwright is not None:

            self.playwright.stop()

            self.playwright = None


# ============================================================
# SELEZIONE AUTOMATICA DEL METODO
# ============================================================

def fetch_visible_text_auto(
    url,
    site_state,
    browser_fetcher
):

    preferred_method = (
        site_state.get(
            "preferred_method",
            "simple"
        )
    )


    # --------------------------------------------------------
    # Il sito in passato ha già richiesto Chromium.
    #
    # Continuiamo a usare Chromium per evitare che il metodo
    # cambi continuamente e produca falsi cambiamenti.
    # --------------------------------------------------------

    if preferred_method == "browser":

        print(
            "Metodo: browser "
            "(selezionato automaticamente "
            "in un controllo precedente)"
        )

        text = browser_fetcher.fetch_text(
            url
        )

        return (
            text,
            "browser",
            False
        )


    # --------------------------------------------------------
    # Prima proviamo il metodo leggero.
    # --------------------------------------------------------

    try:

        html = download_simple(
            url
        )

        text = extract_visible_text(
            html
        )


        if looks_like_browser_required(
            text
        ):

            print(
                "La pagina sembra richiedere "
                "JavaScript."
            )

            print(
                "Passaggio automatico a Chromium."
            )

            browser_text = (
                browser_fetcher.fetch_text(
                    url
                )
            )

            return (
                browser_text,
                "browser",
                True
            )


        print(
            "Metodo: richiesta HTTP semplice"
        )

        return (
            text,
            "simple",
            False
        )


    except HTTPError as error:

        # 403 = Forbidden
        # 429 = Too Many Requests

        if error.code not in (
            403,
            429
        ):

            raise


        print(
            f"HTTP {error.code} "
            "con metodo semplice."
        )

        print(
            "Provo automaticamente Chromium..."
        )


        try:

            browser_text = (
                browser_fetcher.fetch_text(
                    url
                )
            )

        except Exception as browser_error:

            raise RuntimeError(
                f"HTTP {error.code} con metodo "
                "semplice; fallback Chromium "
                "fallito: "
                f"{type(browser_error).__name__}: "
                f"{browser_error}"
            ) from browser_error


        return (
            browser_text,
            "browser",
            True
        )


# ============================================================
# STATO SITO
# ============================================================

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

        "preferred_method": "simple",
        "last_fetch_method": None,

        "history": []
    }


# ============================================================
# CONTROLLO SINGOLO SITO
# ============================================================

def check_site(
    site,
    old_site_state,
    browser_fetcher
):

    name = site["name"]
    url = site["url"]

    print()
    print("=" * 65)
    print(f"Sito: {name}")
    print(f"URL: {url}")
    print("=" * 65)


    state = (
        old_site_state
        or create_empty_site_state(site)
    )


    state["name"] = name
    state["url"] = url


    previous_method = (
        state.get(
            "preferred_method",
            "simple"
        )
    )


    now = datetime.now(
        ZoneInfo("Europe/Rome")
    ).isoformat(
        timespec="seconds"
    )


    check_number = (
        state.get(
            "total_checks",
            0
        )
        + 1
    )


    try:

        (
            visible_text,
            fetch_method,
            method_switched
        ) = fetch_visible_text_auto(
            url,
            state,
            browser_fetcher
        )


        new_hash = calculate_hash(
            visible_text
        )


        old_hash = state.get(
            "current_hash"
        )


        note = None


        # ----------------------------------------------------
        # PRIMO CONTROLLO ASSOLUTO
        # ----------------------------------------------------

        if old_hash is None:

            status = "baseline"
            changed = False

            print(
                f"Controllo #{check_number}: "
                "baseline creata."
            )


        # ----------------------------------------------------
        # PASSAGGIO AUTOMATICO SIMPLE → BROWSER
        #
        # Il testo ottenuto dal browser può essere diverso da
        # quello del semplice HTML anche se il sito non è
        # cambiato.
        #
        # Per questo creiamo una nuova baseline e NON
        # segnaliamo un falso cambiamento.
        # ----------------------------------------------------

        elif (
            method_switched
            and previous_method != "browser"
            and fetch_method == "browser"
        ):

            status = "baseline"
            changed = False

            note = (
                "Nuova baseline dopo il "
                "passaggio automatico a Chromium."
            )

            print(
                f"Controllo #{check_number}: "
                "passaggio a Chromium."
            )

            print(
                "Nuova baseline creata "
                "per evitare falsi cambiamenti."
            )


        # ----------------------------------------------------
        # CONFRONTO NORMALE
        # ----------------------------------------------------

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
            "status": status,
            "method": fetch_method
        }


        if note is not None:

            history_entry[
                "note"
            ] = note


        state["current_hash"] = (
            new_hash
        )

        state["last_checked"] = now

        state["last_error"] = None

        state["total_checks"] = (
            check_number
        )

        state["preferred_method"] = (
            fetch_method
        )

        state["last_fetch_method"] = (
            fetch_method
        )


        if changed:

            state["last_changed"] = now

            state[
                "last_changed_check"
            ] = check_number


    except Exception as error:

        error_message = (
            f"{type(error).__name__}: "
            f"{error}"
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

        state["last_error"] = (
            error_message
        )

        state["total_checks"] = (
            check_number
        )


        # NON modifichiamo current_hash.
        #
        # Quindi un errore temporaneo non distrugge
        # la baseline precedente.


    history = state.get(
        "history",
        []
    )

    history.append(
        history_entry
    )

    state["history"] = (
        history[-MAX_HISTORY:]
    )


    return state


# ============================================================
# VALIDAZIONE sites.json
# ============================================================

def validate_sites(config):

    sites = config.get(
        "sites"
    )


    if not isinstance(
        sites,
        list
    ):

        raise ValueError(
            'sites.json deve contenere '
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
                    f"Campo '{field}' "
                    "mancante in sites.json."
                )


        site_id = site["id"]


        if site_id in ids:

            raise ValueError(
                f"ID duplicato: {site_id}"
            )


        ids.add(
            site_id
        )


        if not (
            site["url"].startswith(
                "http://"
            )
            or
            site["url"].startswith(
                "https://"
            )
        ):

            raise ValueError(
                f"URL non valido "
                f"per {site_id}"
            )


    return sites


# ============================================================
# MAIN
# ============================================================

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


    browser_fetcher = (
        BrowserFetcher()
    )


    print(
        f"Siti da controllare: "
        f"{len(sites)}"
    )


    try:

        for site in sites:

            site_id = site["id"]

            old_site_state = (
                old_sites.get(
                    site_id
                )
            )


            new_state["sites"][
                site_id
            ] = check_site(
                site,
                old_site_state,
                browser_fetcher
            )


    finally:

        browser_fetcher.close()


    save_state(
        new_state
    )


    print()
    print("=" * 65)
    print("CONTROLLO COMPLETATO")
    print("=" * 65)


    for (
        site_id,
        site_state
    ) in new_state[
        "sites"
    ].items():

        latest = (
            site_state[
                "history"
            ][-1]
        )

        method = (
            site_state.get(
                "last_fetch_method"
            )
            or "-"
        )

        print(
            f"{site_state['name']}: "
            f"{latest['status']} "
            f"[{method}]"
        )


if __name__ == "__main__":
    main()
