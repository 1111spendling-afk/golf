"""Täglicher, containerfähiger ÖGV-Friendslist-Abgleich.

Alle Zugangsdaten werden ausschließlich über Umgebungsvariablen geliefert.
Dieses Script schreibt keine Zugangsdaten und keine Friendslist auf die Platte.
"""

import json
import os
import re
import sys
import unicodedata
import urllib.error
import urllib.request
from playwright.sync_api import sync_playwright

FRIENDS_URL = "https://www.golf.at/mygolf/flightpartner"


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


def name_matches(text: str, first_name: str, last_name: str) -> bool:
    haystack = normalized(text)
    return normalized(first_name) in haystack and normalized(last_name) in haystack


def find_result_button(page, first_name: str, last_name: str, club: str):
    """Find the add button in the one result row matching name and club.

    The ÖGV result page contains several rows. A broad parent can contain all
    rows and would make every button look like a match, so only a compact
    ancestor with exactly one occurrence of the requested surname and club is
    accepted.
    """
    wanted_club = normalized(club)
    wanted_last = normalized(last_name)
    candidates: list[tuple[int, object]] = []
    buttons = page.locator("button, input[type='submit'], a")
    for index in range(buttons.count()):
        button = buttons.nth(index)
        try:
            label = normalized(button.inner_text() or button.get_attribute("value") or "")
        except Exception:
            continue
        if label != "hinzufügen":
            continue
        container = button
        best_text = ""
        best_length = 10**9
        for _ in range(12):
            container = container.locator("..")
            try:
                text = normalized(container.inner_text(timeout=500))
            except Exception:
                continue
            if not name_matches(text, first_name, last_name) or wanted_club not in text:
                continue
            if text.count(wanted_last) != 1 or text.count(wanted_club) != 1:
                continue
            if len(text) < best_length:
                best_text = text
                best_length = len(text)
        if best_text:
            candidates.append((best_length, button))
    if not candidates:
        return []
    smallest = min(length for length, _ in candidates)
    return [button for length, button in candidates if length == smallest]


def verify_friend(page, first_name: str, last_name: str, club: str) -> bool:
    page.goto(FRIENDS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(1_500)
    for _ in range(40):
        page.mouse.wheel(0, 2_500)
        page.wait_for_timeout(150)
    friends = parse_friends(page.evaluate("document.body.innerText"))
    wanted_club = normalized(club)
    return any(name_matches(item["name"], first_name, last_name) and normalized(item["club"]) == wanted_club for item in friends)


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


def add_missing_players(players: list[dict[str, str]]) -> list[dict[str, str]]:
    added: list[dict[str, str]] = []
    if not players:
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
                if not click_if_present(page, ["a:has-text('Freunde hinzufügen')", "button:has-text('Freunde hinzufügen')"]):
                    raise RuntimeError("ÖGV-Schaltfläche 'Freunde hinzufügen' wurde nicht gefunden.")
                if not fill_if_present(page, ["input[name*='nach' i]", "input[placeholder*='Nachname' i]", "input[aria-label*='Nachname' i]"], last_name[:20]):
                    raise RuntimeError(f"ÖGV-Nachnamefeld für {first_name} {last_name} wurde nicht gefunden.")
                if not fill_if_present(page, ["input[name*='vor' i]", "input[placeholder*='Vorname' i]", "input[aria-label*='Vorname' i]"], first_name[:20]):
                    raise RuntimeError(f"ÖGV-Vornamefeld für {first_name} {last_name} wurde nicht gefunden.")
                if not click_if_present(page, ["button:has-text('Suchen')", "input[value='Suchen']"]):
                    raise RuntimeError(f"ÖGV-Suche für {first_name} {last_name} konnte nicht gestartet werden.")
                page.wait_for_timeout(1_500)
                matches = find_result_button(page, first_name, last_name, club)
                if len(matches) != 1:
                    print(f"Nicht eindeutig: {first_name} {last_name} – {club} ({len(matches)} Treffer)")
                    continue
                matches[0].click()
                page.wait_for_timeout(1_500)
                if verify_friend(page, first_name, last_name, club):
                    added.append({"name": f"{last_name} {first_name}", "club": club})
                    print(f"Aufgenommen und bestätigt: {last_name} {first_name} – {club}")
                else:
                    print(f"Nicht bestätigt: {last_name} {first_name} – {club}")
        finally:
            browser.close()
    return added


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
        if mode not in {"compare", "add_one", "add_missing", "whi"}:
            raise RuntimeError(f"Unbekannter ÖGV-Vorgang: {mode}")
        friends = get_friends()
        result = post_to_mga(friends, mode)
        if mode in {"add_one", "add_missing"}:
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
