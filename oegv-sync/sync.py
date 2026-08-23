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
    plain = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", plain.casefold()).strip()


def club_key(value: str) -> tuple[str, ...]:
    generic = {"gc", "golf", "club", "golfclub", "golfanlage", "golfplatz", "schloss", "country", "resort", "e", "v"}
    return tuple(token for token in normalized(value).split() if token not in generic)


def clubs_equivalent(left: str, right: str) -> bool:
    left_tokens, right_tokens = set(club_key(left)), set(club_key(right))
    if not left_tokens or not right_tokens:
        return normalized(left) == normalized(right)
    return left_tokens == right_tokens or left_tokens <= right_tokens or right_tokens <= left_tokens


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


def first_names_equivalent(left: str, right: str) -> bool:
    left_tokens, right_tokens = normalized(left).split(), normalized(right).split()
    if not left_tokens or not right_tokens:
        return False
    for left_token in left_tokens:
        for right_token in right_tokens:
            shorter, longer = sorted((left_token, right_token), key=len)
            if left_token == right_token or (len(shorter) >= 3 and longer.startswith(shorter)) or edit_distance(left_token, right_token) <= (2 if len(shorter) >= 6 else 1):
                return True
    return False


def name_matches(text: str, first_name: str, last_name: str) -> bool:
    haystack = normalized(text)
    first = normalized(first_name)
    last = normalized(last_name)
    if first in haystack and last in haystack:
        return True
    if last not in haystack:
        return False
    remaining = haystack.replace(last, " ", 1).strip()
    return first_names_equivalent(remaining, first)



def find_result_button(page, first_name: str, last_name: str, club: str):
    """Match the actual add button inside the matching visible result row."""
    buttons = page.locator("button, input[type='submit'], input[type='button'], a")
    matches = []
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            label = normalized(button.inner_text() or button.get_attribute("value") or "")
        except Exception:
            continue
        if label != "hinzufügen":
            continue
        container = button
        for _ in range(8):
            container = container.locator("..")
            try:
                row_text = " ".join(container.inner_text(timeout=500).split())
            except Exception:
                continue
            if not row_text or len(row_text) < 20:
                continue
            row_buttons = container.locator("button, input[type='submit'], input[type='button'], a")
            add_buttons = []
            for row_index in range(row_buttons.count()):
                row_button = row_buttons.nth(row_index)
                try:
                    row_label = normalized(row_button.inner_text() or row_button.get_attribute("value") or "")
                except Exception:
                    continue
                if row_label == "hinzufügen":
                    add_buttons.append(row_button)
            if len(add_buttons) != 1:
                continue
            if name_matches(row_text, first_name, last_name) and clubs_equivalent(row_text, club):
                matches.append(add_buttons[0])
                break
    unique = []
    seen = set()
    for button in matches:
        try:
            identity = button.evaluate("(node) => node")
        except Exception:
            identity = str(len(unique))
        if identity not in seen:
            seen.add(identity)
            unique.append(button)
    return unique


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
                if not first_name or not last_name or not club:
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
                matches = find_result_button(page, first_name, last_name, club)
                print(f"DIAGNOSE ERGEBNIS: {len(matches)} passende Club-Zeilen")
                if len(matches) != 1:
                    diagnostic_snapshot(page, first_name, last_name, club, f"{len(matches)} passende Club-Zeilen")
                    print(f"Nicht eindeutig: {first_name} {last_name} – {club} ({len(matches)} Treffer)")
                    continue
                if not perform_add:
                    print(f"DIAGNOSE VORSCHLAG: {last_name} {first_name} – {club} | kein Klick im Diagnosemodus")
                    continue
                matches[0].click()
                page.wait_for_timeout(1_500)
                if verify_friend(page, first_name, last_name, club):
                    added.append({"name": f"{last_name} {first_name}", "club": club})
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
