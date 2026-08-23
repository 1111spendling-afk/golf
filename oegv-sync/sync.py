"""Täglicher, containerfähiger ÖGV-Friendslist-Abgleich.

Alle Zugangsdaten werden ausschließlich über Umgebungsvariablen geliefert.
Dieses Script schreibt keine Zugangsdaten und keine Friendslist auf die Platte.
"""

import json
import os
import re
import sys
from pathlib import Path
import unicodedata
import urllib.error
import urllib.request
from playwright.sync_api import sync_playwright

FRIENDS_URL = "https://www.golf.at/mygolf/flightpartner"
DIAGNOSTIC = os.environ.get("OEGV_DIAGNOSTIC", "").strip().lower() in {"1", "true", "yes", "diagnose"}
DIAGNOSTIC_DIR = Path("diagnostic-screenshots")


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Fehlende Server-Einstellung: {name}")
    return value


def click_if_present(page, selectors: list[str]) -> bool:
    for selector in selectors:
        try:
            page.locator(selector).first.click(timeout=2_500)
            return True
        except Exception:
            continue
    return False

def wait_for_search_results(page) -> None:
    selectors = [
        "button:has-text('Hinzufügen')",
        "a:has-text('Hinzufügen')",
        "input[value='Hinzufügen']",
    ]
    for selector in selectors:
        try:
            page.locator(selector).first.wait_for(state="visible", timeout=12_000)
            return
        except Exception:
            continue
    return


def fill_if_present(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            field = page.locator(selector).first
            field.wait_for(state="visible", timeout=2_500)
            field.fill(value)
            return True
        except Exception:
            continue
    return False


def normalized(value: str) -> str:
    value = value.casefold().translate(str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}))
    plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def club_key(value: str) -> tuple[str, ...]:
    generic = {"gc", "golf", "club", "golfclub", "golfanlage", "golfplatz", "schloss", "country", "resort", "e", "v"}
    return tuple(token for token in normalized(value).split() if token not in generic)


CLUB_ALIAS_GROUPS = [
    ("atzenbrugg", {"atzenbrugg", "atzenbruck"}),
    ("ottenstein", {"ottenstein"}),
    ("himberg", {"himberg", "gutenhof"}),
    ("ebreichsdorf", {"ebreichsdorf"}),
]

def club_match_tokens(value: str) -> set[str]:
    value_norm = normalized(value)
    tokens = set(club_key(value))
    tokens.discard("diamond")
    for canonical, aliases in CLUB_ALIAS_GROUPS:
        if any(alias in value_norm for alias in aliases):
            tokens.add(canonical)
    return tokens


def clubs_equivalent(left: str, right: str) -> bool:
    left_norm, right_norm = normalized(left), normalized(right)
    if left_norm == right_norm:
        return True
    left_tokens, right_tokens = club_match_tokens(left), club_match_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens & right_tokens:
        return True
    for left_token in left_tokens:
        for right_token in right_tokens:
            shorter, longer = sorted((left_token, right_token), key=len)
            if len(shorter) >= 5 and edit_distance(shorter, longer) <= 2:
                return True
    return False


def edit_distance(left: str, right: str) -> int:
    previous = list(range(len(right) + 1))
    for row_index, left_char in enumerate(left, start=1):
        current = [row_index]
        for column_index, right_char in enumerate(right, start=1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[column_index] + 1,
                    previous[column_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


FIRST_NAME_ALIAS_GROUPS = [
    {"max", "maximilian"},
    {"karli", "karl"},
    {"fritz", "friedrich"},
    {"ferdi", "ferdinand"},
    {"sepp", "josef"},
    {"pepi", "josef"},
    {"hansi", "hans", "johann"},
    {"andi", "andreas"},
    {"franzi", "franz", "franziska"},
    {"alex", "alexander", "alexandra"},
    {"gabi", "gabriele", "gabriela"},
    {"gerti", "gertrud"},
    {"kathi", "katharina", "kathrin", "katrin"},
    {"michi", "michael", "michaela"},
    {"mike", "michael"},
    {"tom", "thomas"},
    {"thomi", "thomas"},
    {"susi", "susanne", "susanna"},
    {"uli", "ulrich", "ulrike"},
    {"ulli", "ulrich", "ulrike"},
    {"willi", "wilhelm"},
    {"lisi", "elisabeth"},
    {"liesi", "elisabeth"},
    {"resi", "theresia", "therese"},
    {"rudi", "rudolf"},
    {"ruedi", "rudolf"},
    {"wolfi", "wolfgang"},
]

def first_name_forms(value: str) -> set[str]:
    forms = set(normalized(value).split())
    for group in FIRST_NAME_ALIAS_GROUPS:
        if forms & group:
            forms.update(group)
    return forms


def first_names_equivalent(left: str, right: str) -> bool:
    left_tokens, right_tokens = first_name_forms(left), first_name_forms(right)
    if not left_tokens or not right_tokens:
        return False
    for left_token in left_tokens:
        for right_token in right_tokens:
            shorter, longer = sorted((left_token, right_token), key=len)
            if left_token == right_token or (len(shorter) >= 3 and longer.startswith(shorter)) or edit_distance(left_token, right_token) <= (2 if len(shorter) >= 6 else 1):
                return True
    return False


def name_match_flags(text: str, first_name: str, last_name: str) -> tuple[bool, bool]:
    tokens = normalized(text).split()[:8]
    first_tokens = normalized(first_name).split()
    last_tokens = normalized(last_name).split()
    if not tokens or not first_tokens or not last_tokens:
        return False, False
    first_ok = any(first_names_equivalent(token, wanted) for token in tokens for wanted in first_tokens)
    last_ok = any(
        token == wanted or (len(wanted) >= 4 and edit_distance(token, wanted) <= 2)
        for token in tokens for wanted in last_tokens
    )
    return first_ok, last_ok


def name_matches(text: str, first_name: str, last_name: str) -> bool:
    first_ok, last_ok = name_match_flags(text, first_name, last_name)
    return first_ok and last_ok



def parse_candidate_whi(text: str) -> float | None:
    values = re.findall(r"(?<!\d)(\d{1,2}(?:[.,]\d)?)(?!\d)", text)
    for value in values:
        try:
            number = float(value.replace(",", "."))
        except ValueError:
            continue
        if 0 <= number <= 54:
            return number
    return None


def find_result_candidates(page, first_name: str, last_name: str, club: str, whi: str = "") -> list[dict]:
    """Collect visible ÖGV rows and score first name, surname, club and WHI."""
    buttons = page.locator("button, input[type='submit'], input[type='button'], a")
    candidates = []
    seen_rows = set()
    try:
        master_whi = float(str(whi).replace(",", ".")) if str(whi).strip() else None
    except ValueError:
        master_whi = None
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            label = normalized(button.inner_text() or button.get_attribute("value") or "")
        except Exception:
            continue
        if label != normalized("Hinzufügen"):
            continue
        container = button
        for _ in range(10):
            container = container.locator("..")
            try:
                row_text = " ".join(container.inner_text(timeout=500).split())
            except Exception:
                continue
            if len(row_text) < 20:
                continue
            row_buttons = container.locator("button, input[type='submit'], input[type='button'], a")
            add_buttons = []
            for row_index in range(row_buttons.count()):
                row_button = row_buttons.nth(row_index)
                try:
                    row_label = normalized(row_button.inner_text() or row_button.get_attribute("value") or "")
                except Exception:
                    continue
                if row_label == normalized("Hinzufügen"):
                    add_buttons.append(row_button)
            if len(add_buttons) != 1 or row_text in seen_rows:
                continue
            seen_rows.add(row_text)
            first_ok, last_ok = name_match_flags(row_text, first_name, last_name)
            club_ok = clubs_equivalent(row_text, club)
            candidate_whi = parse_candidate_whi(row_text)
            whi_diff = abs(candidate_whi - master_whi) if candidate_whi is not None and master_whi is not None else None
            candidates.append({
                "button": add_buttons[0],
                "text": row_text[:500],
                "first": first_ok,
                "last": last_ok,
                "club": club_ok,
                "whi": candidate_whi,
                "whiDiff": whi_diff,
                "score": sum((first_ok, last_ok, club_ok)),
            })
            break
    return candidates


def find_result_button(page, first_name: str, last_name: str, club: str, whi: str = ""):
    candidates = find_result_candidates(page, first_name, last_name, club, whi)
    return [candidate["button"] for candidate in candidates if candidate["score"] >= 2]




def verify_friend(page, first_name: str, last_name: str, club: str) -> bool:
    page.goto(FRIENDS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1_500)
    for _ in range(40):
        page.mouse.wheel(0, 2_500)
        page.wait_for_timeout(150)
    friends = parse_friends(page.evaluate("document.body.innerText"))
    wanted_club = normalized(club)
    return any(name_matches(item["name"], first_name, last_name) and clubs_equivalent(item["club"], club) for item in friends)


def login_page(playwright):
    user, password = env("OEGV_USER"), env("OEGV_PASSWORD")
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(FRIENDS_URL, wait_until="domcontentloaded")
    click_if_present(page, ["button:has-text('Zustimmen & fortfahren')", "button:has-text('Alle akzeptieren')", "button:has-text('Akzeptieren')"])
    user_ok = fill_if_present(page, ["input[name='username']", "input[name='user']", "input[name='email']", "input[type='email']", "input[type='text']"], user)
    password_ok = fill_if_present(page, ["input[name='password']", "input[name='pass']", "input[type='password']"], password)
    if not (user_ok and password_ok):
        browser.close()
        raise RuntimeError("ÖGV-Loginfelder wurden nicht gefunden.")
    if not click_if_present(page, ["button[type='submit']", "input[type='submit']", "button:has-text('Login')", "button:has-text('Einloggen')", "button:has-text('Anmelden')"]):
        page.keyboard.press("Enter")
    page.wait_for_timeout(4_000)
    page.goto(FRIENDS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2_000)
    return browser, page


def parse_friends(text: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    start = next((index + 1 for index, line in enumerate(lines)
                  if line == "Name" and "Meine Flightpartner/Freunde" in "\n".join(lines[max(0, index - 15):index])), None)
    if start is None:
        raise RuntimeError("ÖGV-Friendslist wurde nach dem Login nicht erkannt.")
    rows: list[dict[str, str]] = []
    index = start
    while index + 2 < len(lines):
        current = lines[index]
        if current in {"News", "Alle", "Finden Sie uns", "Österreichischer Golf-Verband", "Menü", "Über uns", "Impressum", "Datenschutz"}:
            break
        if current in {"WHI", "Entfernen"}:
            index += 1
            continue
        name, club, whi = current, lines[index + 1], lines[index + 2]
        if re.match(r"^\d+[\.,]\d+$", whi) or whi == "RV":
            rows.append({"name": name, "club": club, "whi": whi.replace(",", ".")})
            index += 4 if index + 3 < len(lines) and lines[index + 3] == "Entfernen" else 3
        else:
            index += 1
    if not rows:
        raise RuntimeError("Die ÖGV-Friendslist enthält keine lesbaren Spieler.")
    return rows


def get_friends() -> list[dict[str, str]]:
    with sync_playwright() as playwright:
        browser, page = login_page(playwright)
        for _ in range(40):
            page.mouse.wheel(0, 2_500)
            page.wait_for_timeout(250)
        result = parse_friends(page.evaluate("document.body.innerText"))
        browser.close()
        return result



def diagnostic_snapshot(page, first_name: str, last_name: str, club: str, reason: str) -> None:
    if not DIAGNOSTIC:
        return
    DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)
    safe = normalized(f"{last_name}_{first_name}")[:80].replace(" ", "_") or "spieler"
    path = DIAGNOSTIC_DIR / f"{safe}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"DIAGNOSE SCREENSHOT: {path} | {reason}")


def diagnostic_results(page) -> list[str]:
    details: list[str] = []
    buttons = page.locator("button, input[type='submit'], input[type='button'], a")
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            label = normalized(button.inner_text() or button.get_attribute("value") or "")
        except Exception:
            continue
        if label != "hinzufügen":
            continue
        container = button
        best = ""
        for _ in range(12):
            container = container.locator("..")
            try:
                text = " ".join(container.inner_text(timeout=500).split())
            except Exception:
                continue
            if len(text) >= 20 and (not best or len(text) < len(best)):
                best = text
        if best and best not in details:
            details.append(best[:500])
    return details


def process_missing_players(players: list[dict[str, str]], perform_add: bool) -> list[dict[str, str]]:
    added: list[dict[str, str]] = []
    if not players:
        print("Keine fehlenden Spieler für diesen Vorgang.")
        return added
    with sync_playwright() as playwright:
        browser, page = login_page(playwright)
        try:
            for player in players:
                first_name = str(player.get("firstName", "")).strip()
                last_name = str(player.get("lastName", "")).strip()
                club = str(player.get("homeClub", "")).strip()
                whi = str(player.get("whi", player.get("WHI", ""))).strip()
                if not first_name or not last_name or not club:
                    continue
                if normalized(first_name) == "thomas" and normalized(last_name) == "popp":
                    print(f"DAUERHAFT AUSGESCHLOSSEN: {last_name} {first_name} – {club}")
                    continue
                print(f"DIAGNOSE SUCHE: {last_name}, {first_name} | Masterlisten-Club: {club}")
                if not click_if_present(page, ["a:has-text('Freunde hinzufügen')", "button:has-text('Freunde hinzufügen')"]):
                    raise RuntimeError("ÖGV-Schaltfläche 'Freunde hinzufügen' wurde nicht gefunden.")
                if not fill_if_present(page, ["input[name*='nach' i]", "input[placeholder*='Nachname' i]", "input[aria-label*='Nachname' i]"], last_name[:20]):
                    raise RuntimeError(f"ÖGV-Nachnamefeld für {first_name} {last_name} wurde nicht gefunden.")
                if not fill_if_present(page, ["input[name*='vor' i]", "input[placeholder*='Vorname' i]", "input[aria-label*='Vorname' i]"], first_name[:20]):
                    raise RuntimeError(f"ÖGV-Vornamefeld für {first_name} wurde nicht gefunden.")
                if not click_if_present(page, ["button:has-text('Suchen')", "input[value='Suchen']"]):
                    raise RuntimeError(f"ÖGV-Suche für {first_name} {last_name} konnte nicht gestartet werden.")
                page.wait_for_timeout(1_500)
                details = diagnostic_results(page) if DIAGNOSTIC else []
                for detail in details:
                    print(f"DIAGNOSE TREFFER: {detail}")
                candidates = find_result_candidates(page, first_name, last_name, club, whi)
                print(f"DIAGNOSE ERGEBNIS: {len(candidates)} sichtbare Treffer")
                eligible = [candidate for candidate in candidates if candidate["score"] >= 2]
                choice = None
                review_reason = ""
                if len(eligible) == 1:
                    choice = eligible[0]
                    if choice["score"] < 3:
                        review_reason = "nur zwei Merkmale sicher passend"
                elif len(eligible) > 1:
                    with_whi = [candidate for candidate in eligible if candidate["whiDiff"] is not None and candidate["whiDiff"] <= 5]
                    if len(with_whi) == 1:
                        choice = with_whi[0]
                        review_reason = "mehrere Treffer; Auswahl über WHI-Abstand"
                    else:
                        review_reason = "mehrere passende Treffer"
                else:
                    named = [candidate for candidate in candidates if candidate["first"] and candidate["last"] and candidate["whiDiff"] is not None and candidate["whiDiff"] <= 5]
                    if len(named) == 1:
                        choice = named[0]
                        review_reason = "Clubbezeichnung abweichend; Auswahl über WHI-Abstand"
                    else:
                        review_reason = "zu wenige sichere Merkmale"
                if choice is None:
                    diagnostic_snapshot(page, first_name, last_name, club, "MANUELL PRÜFEN: " + review_reason)
                    print(f"MANUELL PRÜFEN: {first_name} {last_name} – {club} ({review_reason})")
                    continue
                if review_reason:
                    print(f"MANUELL PRÜFEN NACH AUFNAHME: {last_name} {first_name} – {club} ({review_reason})")
                if not perform_add:
                    print(f"DIAGNOSE VORSCHLAG: {last_name} {first_name} – {club} | kein Klick im Diagnosemodus")
                    continue
                choice["button"].click()
                page.wait_for_timeout(1_500)
                if verify_friend(page, first_name, last_name, club):
                    added.append({"name": f"{last_name} {first_name}", "club": club, "review": bool(review_reason), "reviewReason": review_reason})
                    print(f"Aufgenommen und bestätigt: {last_name} {first_name} – {club}")
                else:
                    diagnostic_snapshot(page, first_name, last_name, club, "Klick erfolgt, aber Aufnahme nicht bestätigt")
                    print(f"Nicht bestätigt: {last_name} {first_name} – {club}")
        finally:
            browser.close()
    return added


def add_missing_players(players: list[dict[str, str]]) -> list[dict[str, str]]:
    return process_missing_players(players, True)


def diagnose_missing_players(players: list[dict[str, str]]) -> None:
    process_missing_players(players, False)


def post_to_mga(players: list[dict[str, str]], mode: str, added_players: list[dict[str, str]] | None = None) -> dict:
    target = env("MGA_SYNC_URL")
    token = env("MGA_SYNC_TOKEN")
    sites_bypass_token = env("MGA_SITE_BYPASS_TOKEN")
    request = urllib.request.Request(
        target,
        data=json.dumps({"players": players, "mode": mode, "addedPlayers": added_players or []}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "OAI-Sites-Authorization": f"Bearer {sites_bypass_token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"MGA-Site hat den Abgleich abgelehnt ({error.code}).") from error


if __name__ == "__main__":
    try:
        mode = os.environ.get("OEGV_MODE", "whi").strip().lower()
        if mode not in {"compare", "add_one", "add_missing", "whi", "diagnose"}:
            raise RuntimeError(f"Unbekannter ÖGV-Vorgang: {mode}")
        friends = get_friends()
        result = post_to_mga(friends, mode)
        if mode == "diagnose":
            diagnose_missing_players(result.get("missingPlayers", []))
        elif mode in {"add_one", "add_missing"}:
            candidates = result.get("missingPlayers", [])
            if mode == "add_one":
                candidates = candidates[:1]
            added = add_missing_players(candidates)
            refreshed = get_friends()
            result = post_to_mga(refreshed, "compare", added)
            result["addedCount"] = len(added)
            result["addedPlayers"] = added
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
