"""Täglicher, containerfähiger ÖGV-Friendslist-Abgleich.

Alle Zugangsdaten werden ausschließlich über Umgebungsvariablen geliefert.
Dieses Script schreibt keine Zugangsdaten und keine Friendslist auf die Platte.
"""

import json
import os
import re
import sys
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
    user, password = env("OEGV_USER"), env("OEGV_PASSWORD")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        page.goto(FRIENDS_URL, wait_until="domcontentloaded")
        click_if_present(page, ["button:has-text('Zustimmen & fortfahren')", "button:has-text('Alle akzeptieren')", "button:has-text('Akzeptieren')"])
        user_ok = fill_if_present(page, ["input[name='username']", "input[name='user']", "input[name='email']", "input[type='email']", "input[type='text']"], user)
        password_ok = fill_if_present(page, ["input[name='password']", "input[name='pass']", "input[type='password']"], password)
        if not (user_ok and password_ok):
            raise RuntimeError("ÖGV-Loginfelder wurden nicht gefunden.")
        if not click_if_present(page, ["button[type='submit']", "input[type='submit']", "button:has-text('Login')", "button:has-text('Einloggen')", "button:has-text('Anmelden')"]):
            page.keyboard.press("Enter")
        page.wait_for_timeout(4_000)
        page.goto(FRIENDS_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2_000)
        for _ in range(40):
            page.mouse.wheel(0, 2_500)
            page.wait_for_timeout(250)
        result = parse_friends(page.evaluate("document.body.innerText"))
        browser.close()
        return result


def post_to_mga(players: list[dict[str, str]]) -> dict:
    target = env("MGA_SYNC_URL")
    token = env("MGA_SYNC_TOKEN")
    print(f"MGA-Zieladresse: {target}")
    request = urllib.request.Request(
        target,
        data=json.dumps({"players": players}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"MGA-Site hat den Abgleich abgelehnt ({error.code}): {detail[:500]}"
        ) from error


if __name__ == "__main__":
    try:
        result = post_to_mga(get_friends())
        print(json.dumps(result, ensure_ascii=False))
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
