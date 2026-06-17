#!/usr/bin/env python3
"""
USP Monitor -> Telegram alert bot.

Monitora le nuove pubblicazioni del sito dell'Ambito Territoriale impostato
(WordPress) e invia una notifica Telegram per ogni nuovo articolo adattando la provincia.

Configurazione (variabili d'ambiente)
-------------------------------------
TELEGRAM_BOT_TOKEN  (obbligatoria)  Token del bot da @BotFather.
TELEGRAM_CHAT_ID    (obbligatoria)  Il tuo chat_id Telegram.
PROVINCIA           (opzionale)     Nome della provincia (es. "Vibo Valentia", "Reggio Calabria").
SITE_BASE_URL       (opzionale)     Default: https://www.istruzioneatprc.it
CATEGORIES          (opzionale)     Slug categorie separati da virgola. Vuoto = tutte.
PER_PAGE            (opzionale)     Quanti post leggere per run. Default: 30.
STATE_FILE          (opzionale)     Percorso file di stato. Default: ./state.json
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests

# --------------------------------------------------------------------------- #
# Configurazione e logging
# --------------------------------------------------------------------------- #

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("usp-monitor")

DEFAULT_SITE = "https://www.istruzioneatprc.it"
HTTP_TIMEOUT = 30  # secondi
MAX_RETRIES = 3
RETRY_BACKOFF = 5  # secondi
TELEGRAM_RATE_DELAY = 1.0  # pausa tra messaggi per non saturare l'API Telegram


class Config:
    """Carica e valida la configurazione dall'ambiente. Fail-fast se manca l'essenziale."""

    def __init__(self) -> None:
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.site_base = os.environ.get("SITE_BASE_URL", DEFAULT_SITE).rstrip("/")
        self.per_page = int(os.environ.get("PER_PAGE", "30"))
        self.state_file = Path(os.environ.get("STATE_FILE", "state.json"))

        # PARAMETRO DINAMICO: Recupera il nome della provincia dall'ambiente
        self.provincia = os.environ.get("PROVINCIA", "Reggio Calabria").strip()

        raw_cats = os.environ.get("CATEGORIES", "").strip()
        self.categories = {c.strip().lower() for c in raw_cats.split(",") if c.strip()}

        missing = [
            name
            for name, val in (
                ("TELEGRAM_BOT_TOKEN", self.bot_token),
                ("TELEGRAM_CHAT_ID", self.chat_id),
            )
            if not val
        ]
        if missing:
            raise SystemExit(
                f"Configurazione mancante: {', '.join(missing)}. "
                "Impostali come variabili d'ambiente / GitHub Secrets."
            )


# --------------------------------------------------------------------------- #
# Persistenza stato
# --------------------------------------------------------------------------- #


def load_state(path: Path) -> dict[str, Any]:
    """Carica lo stato. Restituisce {'seen_ids': [...], 'seeded': bool}."""
    if not path.exists():
        return {"seen_ids": [], "seeded": False}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("seen_ids", [])
        data.setdefault("seeded", bool(data["seen_ids"]))
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Stato non leggibile (%s). Reinizializzo.", exc)
        return {"seen_ids": [], "seeded": False}


def save_state(path: Path, seen_ids: set[int]) -> None:
    """Salva lo stato in modo atomico."""
    payload = {
        "seen_ids": sorted(seen_ids),
        "seeded": True,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Fetch e parsing articoli (WordPress REST API)
# --------------------------------------------------------------------------- #


def fetch_posts(cfg: Config) -> list[dict[str, Any]]:
    """Recupera gli ultimi post via REST API WordPress."""
    url = f"{cfg.site_base}/wp-json/wp/v2/posts"
    params = {"per_page": cfg.per_page, "_embed": "1", "orderby": "date", "order": "desc"}
    headers = {"User-Agent": "USP-Monitor/1.0 (+personal alert bot)"}

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            posts = resp.json()
            log.info("[%s] Recuperati %d post dalla REST API.", cfg.provincia, len(posts))
            return posts
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            wait = RETRY_BACKOFF * attempt
            log.warning("[%s] Tentativo %d/%d fallito: %s. Riprovo tra %ds.",
                        cfg.provincia, attempt, MAX_RETRIES, exc, wait)
            if attempt < MAX_RETRIES:
                time.sleep(wait)

    raise RuntimeError(f"Impossibile recuperare i post dopo {MAX_RETRIES} tentativi") from last_exc


# Regex per estrarre tutti i link che portano a file allegati nel testo
_ALLEGATI_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+\.(?:pdf|zip|doc|docx|xls|xlsx))["\'][^>]*>(.*?)</a>', re.IGNORECASE)

def extract_attachments(post: dict[str, Any]) -> list[dict[str, str]]:
    """Estrae tutti i link ai file allegati (PDF, ZIP, Excel, Word)"""
