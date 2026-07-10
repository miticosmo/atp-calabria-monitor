#!/usr/bin/env python3
"""
Healthcheck del proxy (IPRoyal) per il monitor ATP Calabria.

Verifica, SENZA mai stampare la credenziale, che:
  1. il proxy risponda e fornisca un IP di uscita (idealmente italiano);
  2. l'endpoint WordPress che di norma blocca il runner GitHub torni JSON
     valido quando la richiesta passa dal proxy.

Uso:
    export PROXY_URL="http://USER:PASS@HOST:PORTA"
    python _test_proxy.py                       # default: Cosenza
    python _test_proxy.py https://www.istruzione.calabria.it/crotone
"""

from __future__ import annotations

import os
import sys

import requests

DEFAULT_SITE = "https://www.istruzione.calabria.it/cosenza"
TIMEOUT = (10, 60)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def _ko(msg: str) -> None:
    print(f"  \033[31m✗\033[0m {msg}")


def main() -> int:
    proxy = os.environ.get("PROXY_URL", "").strip()
    if not proxy:
        print("PROXY_URL non impostato. Esporta la variabile e riprova.")
        return 2

    site = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SITE).rstrip("/")
    endpoint = f"{site}/wp-json/wp/v2/posts?per_page=1"
    proxies = {"http": proxy, "https": proxy}
    failures = 0

    # 1) Il proxy instrada e fornisce un IP di uscita?
    print("\n[1] Verifica uscita proxy")
    try:
        ip = requests.get("https://api.ipify.org", proxies=proxies, timeout=TIMEOUT).text.strip()
        _ok(f"IP di uscita: {ip}")
        try:
            geo = requests.get(f"https://ipapi.co/{ip}/country_name/", timeout=TIMEOUT).text.strip()
            (_ok if geo.lower() == "italy" else print)(f"  Paese IP: {geo}")
        except requests.RequestException:
            pass
    except requests.RequestException as exc:
        _ko(f"Proxy non raggiungibile: {exc}")
        return 1  # inutile proseguire

    # 2) L'endpoint ATP torna JSON valido passando dal proxy?
    print(f"\n[2] Verifica endpoint via proxy\n    {endpoint}")
    try:
        r = requests.get(endpoint, headers={"User-Agent": UA}, proxies=proxies, timeout=TIMEOUT)
        ctype = r.headers.get("Content-Type", "")
        body = r.text[:200].replace("\n", " ")
        print(f"  HTTP {r.status_code} · Content-Type: {ctype or 'n/d'}")
        try:
            data = r.json()
            n = len(data) if isinstance(data, list) else 1
            _ok(f"Risposta JSON valida ({n} post). Proxy OK per questo sito.")
        except ValueError:
            failures += 1
            # Non-JSON: classifico la causa reale invece di dare per scontato "WAF".
            # Un 5xx (o la firma HAProxy "no server is available") = origine giù,
            # NON un blocco: né proxy né scraping API possono rimediare, si riprova
            # più tardi. Un 200 non-JSON = vera challenge anti-bot. Un 401/403/429 =
            # accesso negato/limitato lato IP.
            if r.status_code >= 500 or "no server is available" in body.lower():
                _ko(f"Errore lato server ({r.status_code}): {body!r}")
                _ko("Origine del sito irraggiungibile (giù/sovraccarico), NON un blocco.")
                _ko("→ Né proxy né scraping API aiutano: riprova più tardi.")
            elif r.status_code in (401, 403, 429):
                _ko(f"Accesso negato/limitato ({r.status_code}): {body!r}")
                _ko("→ Blocco anti-bot lato IP: valuta scraping API o cambia geo/IP del proxy.")
            else:
                _ko(f"Risposta 200 NON-JSON (probabile challenge WAF): {body!r}")
                _ko("→ Il residenziale non basta per questo sito: valuta una scraping API.")
    except requests.RequestException as exc:
        failures += 1
        _ko(f"Richiesta fallita via proxy: {exc}")

    print("\n" + ("ESITO: ✓ proxy settato bene" if failures == 0 else "ESITO: ✗ problemi rilevati"))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
