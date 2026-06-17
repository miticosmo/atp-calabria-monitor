#!/usr/bin/env python3
"""
Backfill una tantum: pubblica su Telegram gli avvisi degli ultimi N giorni.

Serve a "popolare" un canale o gruppo Telegram appena creato con lo storico recente, in
modo che i nuovi iscritti trovino subito la cronologia.

Va lanciato UNA VOLTA, in locale, dopo aver puntato il bot al canale/gruppo corretto.
Riusa la logica di monitor.py per restare coerente con il bot in produzione.

Configurazione (variabili d'ambiente)
-------------------------------------
TELEGRAM_BOT_TOKEN  (obbligatoria)  Token del bot da @BotFather.
TELEGRAM_CHAT_ID    (obbligatoria)  Il tuo chat_id Telegram.
PROVINCIA           (opzionale)     Nome della provincia (es. "Vibo Valentia", "Reggio Calabria").
SITE_BASE_URL       (opzionale)     URL del sito target.
CATEGORIES          (opzionale)     Slug categorie separate da virgola. Vuoto = tutte.
"""

from __future__ import annotations

import sys
import time
from datetime import date, timedelta

import requests

import monitor  # riuso Config, format_message, matches_filter, load/save_state

DEFAULT_DAYS = 30
THROTTLE_SECONDS = 3.0  # pausa tra un invio e l'altro (margine ampio sul rate-limit)
PER_PAGE = 100


def fetch_range(cfg: monitor.Config, date_from: str, date_to: str) -> list[dict]:
    """Scarica tutti i post nell'intervallo [date_from, date_to], dal piu' vecchio, con paginazione."""
    url = f"{cfg.site_base}/wp-json/wp/v2/posts"
    params = {
        "after": f"{date_from}T00:00:00",
        "before": f"{date_to}T23:59:59",
        "per_page": PER_PAGE,
        "_embed": "1",
        "orderby": "date",
        "order": "asc",
        "page": 1,
    }
    headers = {"User-Agent": "USP-Backfill/1.0"}
    posts: list[dict] = []
    while True:
        resp = requests.get(url, params=params, headers=headers, timeout=monitor.HTTP_TIMEOUT)
        if resp.status_code == 400 and params["page"] > 1:
            break  # pagine esaurite
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        posts.extend(batch)
        total = int(resp.headers.get("X-WP-TotalPages", "1"))
        print(f"  [{cfg.provincia}] pagina {params['page']}/{total} -> {len(batch)} post")
        if params["page"] >= total:
            break
        params["page"] += 1
    return posts


def send_throttled(cfg: monitor.Config, text: str, reply_markup: dict | None = None) -> bool:
    """Invio singolo con gestione nativa del 429 (Too Many Requests)."""
    api = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,  # Disabilitata per rendere compatto lo scroll dello storico
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    for _ in range(3):
        try:
            resp = requests.post(api, json=payload, timeout=monitor.HTTP_TIMEOUT)
        except requests.RequestException as exc:
            print(f"  errore di rete: {exc}", file=sys.stderr)
            return False
        if resp.status_code == 429:
            wait = resp.json().get("parameters", {}).get("retry_after", 5) + 1
            print(f"  rate-limit riscontrato: attendo {wait}s ...")
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            print(f"  errore {resp.status_code}: {resp.text[:200]}", file=sys.stderr)
            return False
        return True
    return False


def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    cfg = monitor.Config()

    today = date.today()
    date_from = (today - timedelta(days=days)).isoformat()
    date_to = today.isoformat()
    print(f"Backfill [{cfg.provincia}]: post dal {date_from} al {date_to} verso la chat {cfg.chat_id}")

    posts = fetch_range(cfg, date_from, date_to)
    print(f"Trovati {len(posts)} post nel periodo selezionato.")

    state = monitor.load_state(cfg.state_file)
    seen = set(state["seen_ids"])

    sent = 0
    for p in posts:
        pid = int(p["id"])
        if monitor.matches_filter(p, cfg.categories):
            # OTTIMIZZAZIONE: Passiamo cfg.provincia alla nuova funzione di monitor per l'intestazione dinamica
            msg_text = monitor.format_message(p, cfg.provincia)
            if send_throttled(cfg, msg_text, monitor.build_buttons(p)):
                sent += 1
                print(f"  inviato [{pid}] {p.get('date','')[:10]}")
                time.sleep(THROTTLE_SECONDS)
            else:
                print(f"  FALLITO [{pid}] - non lo marco come visto, verrà ritentato dal bot", file=sys.stderr)
                continue
        seen.add(pid)

    monitor.save_state(cfg.state_file, seen)
    print(f"\nFatto! Messaggi storici inviati con successo: {sent}. Stato aggiornato ({len(seen)} ID).")
    print("Ricorda di allineare GitHub: git commit -am \"chore: allineato stato dopo backfill\" && git push origin master")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.RequestException as exc:
        print(f"Errore di rete: {exc}", file=sys.stderr)
        sys.exit(1)