"""Orchestriert den ÖGV-Friendslist-Abgleich pro MGA-Webmaster.

Wesentliche Sicherheitsregel:
- Es gibt KEINEN globalen ÖGV-Zugang und KEINEN Credential-Fallback.
- Zugangsdaten werden ausschließlich serverseitig über die MGA-API für genau
  die jeweilige Webmaster-ID geladen.
- Zugangsdaten werden nur als Prozess-Umgebungsvariablen an sync.py übergeben,
  niemals in Dateien oder Logs geschrieben.

Manueller Lauf:
  RUN_ALL_WEBMASTERS=0, WEBMASTER_ID=<id>
Automatischer Lauf:
  RUN_ALL_WEBMASTERS=1; es werden alle Webmaster-IDs mit hinterlegtem
  persönlichem ÖGV-Zugang verarbeitet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Fehlende Server-Einstellung: {name}")
    return value


def as_bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def fetch_credentials(webmaster_id: int, *, required: bool) -> tuple[str, str] | None:
    """Lädt den persönlichen ÖGV-Zugang einer Webmaster-ID aus der MGA-Site.

    400/404/422 bedeuten bei der automatischen Suche: für diese ID ist kein
    verwendbarer persönlicher ÖGV-Zugang eingerichtet. Bei manuellen Läufen
    ist derselbe Zustand ein harter Fehler. Authentifizierungs-/Serverfehler
    werden niemals als 'kein Zugang' verschluckt.
    """

    target = required_env("MGA_CREDENTIALS_URL")
    token = required_env("MGA_SYNC_TOKEN")
    request = urllib.request.Request(
        target,
        data=json.dumps({"webmasterId": webmaster_id}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in {400, 404, 422}:
            if required:
                raise RuntimeError(
                    f"Für Webmaster-ID {webmaster_id} ist kein persönlicher ÖGV-Zugang eingerichtet."
                ) from error
            return None
        raise RuntimeError(
            f"MGA-Zugangsdienst für Webmaster-ID {webmaster_id} antwortete mit HTTP {error.code}."
        ) from error
    except urllib.error.URLError as error:
        raise RuntimeError("MGA-Zugangsdienst ist nicht erreichbar.") from error

    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    if not username or not password:
        if required:
            raise RuntimeError(
                f"Für Webmaster-ID {webmaster_id} ist kein vollständiger persönlicher ÖGV-Zugang eingerichtet."
            )
        return None

    # GitHub maskiert die Werte in allen nachfolgenden Logs.
    print("::add-mask::" + username)
    print("::add-mask::" + password)
    return username, password


def run_one(webmaster_id: int, username: str, password: str, mode: str) -> None:
    env = os.environ.copy()
    env["WEBMASTER_ID"] = str(webmaster_id)
    env["OEGV_USER"] = username
    env["OEGV_PASSWORD"] = password
    env["OEGV_MODE"] = mode

    print(f"Starte ÖGV-{mode}-Lauf für Webmaster-ID {webmaster_id}.")
    completed = subprocess.run(
        [sys.executable, "oegv-sync/sync.py"],
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"ÖGV-{mode}-Lauf für Webmaster-ID {webmaster_id} ist fehlgeschlagen."
        )
    print(f"ÖGV-{mode}-Lauf für Webmaster-ID {webmaster_id} abgeschlossen.")


def discover_configured_webmasters() -> list[tuple[int, str, str]]:
    """Findet eingerichtete Webmaster ohne irgendeinen globalen Fallback.

    Die MGA-Webmaster-IDs werden fortlaufend vergeben. Der Scan ist bewusst
    großzügig begrenzt, damit neu angelegte Webmaster ohne GitHub-Änderung
    automatisch erfasst werden. Die Grenze kann serverseitig über
    MGA_WEBMASTER_SCAN_MAX angehoben werden, ohne Secrets pro Benutzer anzulegen.
    """

    try:
        scan_max = int(os.environ.get("MGA_WEBMASTER_SCAN_MAX", "500"))
    except ValueError as error:
        raise RuntimeError("MGA_WEBMASTER_SCAN_MAX muss eine ganze Zahl sein.") from error
    if scan_max < 1 or scan_max > 10000:
        raise RuntimeError("MGA_WEBMASTER_SCAN_MAX muss zwischen 1 und 10000 liegen.")

    configured: list[tuple[int, str, str]] = []
    for webmaster_id in range(1, scan_max + 1):
        credentials = fetch_credentials(webmaster_id, required=False)
        if credentials is None:
            continue
        username, password = credentials
        configured.append((webmaster_id, username, password))

    if not configured:
        raise RuntimeError("Kein Webmaster mit persönlichem ÖGV-Zugang gefunden.")
    return configured


def main() -> int:
    run_all = as_bool(os.environ.get("RUN_ALL_WEBMASTERS"))
    mode = os.environ.get("OEGV_MODE", "whi").strip().lower()
    if mode not in {"compare", "add_one", "add_missing", "whi", "diagnose"}:
        raise RuntimeError(f"Unbekannter ÖGV-Vorgang: {mode}")

    if not run_all:
        webmaster_id = int(required_env("WEBMASTER_ID"))
        username, password = fetch_credentials(webmaster_id, required=True)  # type: ignore[misc]
        run_one(webmaster_id, username, password, mode)
        return 0

    # Automatische Läufe sind ausschließlich WHI-Läufe. Kritische Aktionen wie
    # add_missing werden niemals ungefragt für alle Webmaster ausgeführt.
    mode = "whi"
    configured = discover_configured_webmasters()
    print(f"Automatischer Lauf: {len(configured)} Webmaster mit persönlichem ÖGV-Zugang gefunden.")

    failures: list[tuple[int, str]] = []
    for webmaster_id, username, password in configured:
        try:
            run_one(webmaster_id, username, password, mode)
        except Exception as error:  # Jeder Webmaster wird unabhängig verarbeitet.
            failures.append((webmaster_id, str(error)))
            print(f"FEHLER Webmaster-ID {webmaster_id}: {error}", file=sys.stderr)

    if failures:
        failed_ids = ", ".join(str(webmaster_id) for webmaster_id, _ in failures)
        raise RuntimeError(
            f"Automatischer ÖGV-Lauf beendet; fehlgeschlagene Webmaster-IDs: {failed_ids}. "
            "Die übrigen Webmaster wurden trotzdem verarbeitet."
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
