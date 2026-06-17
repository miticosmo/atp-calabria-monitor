#!/usr/bin/env python3
"""
USP Reggio Calabria -> Telegram alert bot.

Monitora le nuove pubblicazioni del sito dell'Ambito Territoriale di Reggio
Calabria (WordPress) e invia una notifica Telegram per ogni nuovo articolo.

Approccio architetturale
-------------------------
- NON fa scraping dell'HTML (fragile). Interroga la REST API nativa di
  WordPress: GET /wp-json/wp/v2/posts. Dati strutturati, ID stabili,
  filtro per categoria.
- Lo stato (insieme degli ID gia' notificati) e' persistito su file JSON.
  Su GitHub Actions questo file viene committato nel repo => audit trail
  immutabile di cosa e' stato rilevato e quando.
- Idempotente: rieseguirlo non genera notifiche duplicate.
- Primo avvio "silenzioso": al primissimo run popola lo stato con gli
  articoli gia' presenti SENZA notificarli (evita raffica di messaggi).

Sicurezza
---------
- Token e chat_id letti SOLO da variabili d'ambiente (mai hardcoded).
- Il token non viene mai loggato.
- Solo HTTPS, con timeout su ogni richiesta.

Configurazione (variabili d'ambiente)
-------------------------------------
TELEGRAM_BOT_TOKEN  (obbligatoria)  Token del bot da @BotFather.
TELEGRAM_CHAT_ID    (obbligatoria)  Il tuo chat_id Telegram.
SITE_BASE_URL       (opz.)  Default: https://www.istruzioneatprc.it
CATEGORIES          (opz.)  Slug categorie separati da virgola, es. "grad,doc,recl".
                            Vuoto = tutte le categorie.
PER_PAGE            (opz.)  Quanti post leggere per run. Default: 30.
STATE_FILE          (opz.)  Percorso file di stato. Default: ./state.json
"""

from __future__ import annotations

import html
import json
import logging
import os
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
RETRY_BACKOFF = 5  # secondi (lineare: 5s, 10s, 15s)
TELEGRAM_RATE_DELAY = 1.0  # pausa tra messaggi per non saturare l'API Telegram


class Config:
    """Carica e valida la configurazione dall'ambiente. Fail-fast se manca l'essenziale."""

    def __init__(self) -> None:
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.site_base = os.environ.get("SITE_BASE_URL", DEFAULT_SITE).rstrip("/")
        self.per_page = int(os.environ.get("PER_PAGE", "30"))
        self.state_file = Path(os.environ.get("STATE_FILE", "state.json"))

        raw_cats = os.environ.get("CATEGORIES", "").strip()
        # set di slug in minuscolo; vuoto => nessun filtro (tutte le categorie)
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
        # Stato corrotto: meglio ripartire pulito che crashare in loop.
        log.warning("Stato non leggibile (%s). Reinizializzo.", exc)
        return {"seen_ids": [], "seeded": False}


def save_state(path: Path, seen_ids: set[int]) -> None:
    """Salva lo stato in modo atomico (write su tmp + rename)."""
    payload = {
        "seen_ids": sorted(seen_ids),
        "seeded": True,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # rename atomico sullo stesso filesystem


# --------------------------------------------------------------------------- #
# Fetch articoli (WordPress REST API)
# --------------------------------------------------------------------------- #


def fetch_posts(cfg: Config) -> list[dict[str, Any]]:
    """
    Recupera gli ultimi post via REST API WordPress.

    Usa _embed per ottenere le categorie inline (evita una seconda chiamata
    e rende il filtro per categoria resiliente). Ordina dal piu' recente.
    """
    url = f"{cfg.site_base}/wp-json/wp/v2/posts"
    params = {"per_page": cfg.per_page, "_embed": "1", "orderby": "date", "order": "desc"}
    headers = {"User-Agent": "USP-Monitor/1.0 (+personal alert bot)"}

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            posts = resp.json()
            log.info("Recuperati %d post dalla REST API.", len(posts))
            return posts
        except (requests.RequestException, ValueError) as exc:
            last_exc = exc
            wait = RETRY_BACKOFF * attempt
            log.warning("Tentativo %d/%d fallito: %s. Riprovo tra %ds.",
                        attempt, MAX_RETRIES, exc, wait)
            if attempt < MAX_RETRIES:
                time.sleep(wait)

    raise RuntimeError(f"Impossibile recuperare i post dopo {MAX_RETRIES} tentativi") from last_exc


def extract_categories(post: dict[str, Any]) -> list[str]:
    """Estrae gli slug delle categorie dai dati _embedded del post."""
    slugs: list[str] = []
    embedded = post.get("_embedded", {})
    for term_group in embedded.get("wp:term", []):
        for term in term_group:
            if term.get("taxonomy") == "category" and term.get("slug"):
                slugs.append(term["slug"].lower())
    return slugs


def category_names(post: dict[str, Any]) -> list[str]:
    """Estrae i nomi leggibili delle categorie (es. 'Docenti', 'Graduatorie') per la visualizzazione."""
    names: list[str] = []
    embedded = post.get("_embedded", {})
    for term_group in embedded.get("wp:term", []):
        for term in term_group:
            if term.get("taxonomy") == "category" and term.get("name"):
                # I nomi WP possono contenere entita' HTML: le decodifichiamo.
                names.append(html.unescape(term["name"]).strip())
    return names


def matches_filter(post: dict[str, Any], wanted: set[str]) -> bool:
    """True se il post va notificato: nessun filtro, oppure interseca le categorie volute."""
    if not wanted:
        return True
    return bool(set(extract_categories(post)) & wanted)


# --------------------------------------------------------------------------- #
# Notifiche Telegram
# --------------------------------------------------------------------------- #


def send_telegram(cfg: Config, text: str) -> bool:
    """Invia un messaggio HTML su Telegram. Restituisce True se ok."""
    api = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        resp = requests.post(api, json=payload, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            # Non logghiamo il token: l'URL non viene mai stampato.
            log.error("Telegram ha risposto %s: %s", resp.status_code, resp.text[:300])
            return False
        return True
    except requests.RequestException as exc:
        log.error("Errore invio Telegram: %s", exc)
        return False


_MESI_IT = (
    "", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno",
    "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre",
)


def format_date_it(iso: str) -> str:
    """Converte una data ISO (YYYY-MM-DD...) in formato italiano esteso, es. '16 giugno 2026'."""
    try:
        y, m, d = iso[:10].split("-")
        return f"{int(d)} {_MESI_IT[int(m)]} {y}"
    except (ValueError, IndexError):
        return iso[:10]


def format_message(post: dict[str, Any]) -> str:
    """
    Costruisce il messaggio HTML per un articolo.

    Nota su sicurezza/encoding: i titoli di WordPress arrivano con entita' HTML
    gia' codificate (es. '&#8211;' = trattino lungo). Le decodifichiamo con
    html.unescape() e poi ri-escapiamo SOLO i caratteri sensibili per Telegram
    (< > &) con html.escape(): evita il doppio-escaping e previene injection.
    """
    raw_title = post.get("title", {}).get("rendered", "(senza titolo)").strip()
    title = html.escape(html.unescape(raw_title))
    link = post.get("link", "")
    date_it = format_date_it(post.get("date", ""))
    names = category_names(post)
    cat_line = ("🏷 " + " · ".join(html.escape(n) for n in names)) if names else ""

    return (
        f"📢 <b>USP Reggio Calabria — Nuovo avviso</b>\n"
        f"➖➖➖➖➖➖➖➖➖➖\n"
        f"<b>{title}</b>\n\n"
        f"🗓 {date_it}\n"
        f"{cat_line}\n\n"
        f'🔗 <a href="{html.escape(link)}">Leggi l\'avviso completo →</a>'
    ).replace("\n\n\n", "\n\n")


# --------------------------------------------------------------------------- #
# Orchestrazione
# --------------------------------------------------------------------------- #


def run() -> int:
    cfg = Config()
    state = load_state(cfg.state_file)
    seen: set[int] = set(state["seen_ids"])
    first_run = not state["seeded"]

    posts = fetch_posts(cfg)
    # Dal piu' vecchio al piu' recente: cosi' le notifiche arrivano in ordine cronologico.
    posts_sorted = sorted(posts, key=lambda p: p.get("date", ""))

    # --- Primo avvio: popola lo stato in silenzio ---
    if first_run:
        for p in posts_sorted:
            seen.add(int(p["id"]))
        save_state(cfg.state_file, seen)
        log.info("Primo avvio: stato inizializzato con %d articoli (nessuna notifica).", len(seen))
        return 0

    # --- Run normale: notifica solo i nuovi che superano il filtro ---
    new_posts = [p for p in posts_sorted if int(p["id"]) not in seen]
    if not new_posts:
        log.info("Nessun nuovo articolo.")
        return 0

    log.info("Trovati %d nuovi articoli (prima del filtro categorie).", len(new_posts))
    notified = 0
    for p in new_posts:
        pid = int(p["id"])
        # Segna comunque come 'visto' per non rivalutarlo ai run successivi,
        # anche se non passa il filtro (evita ricontrolli inutili).
        if matches_filter(p, cfg.categories):
            if send_telegram(cfg, format_message(p)):
                notified += 1
                seen.add(pid)
                time.sleep(TELEGRAM_RATE_DELAY)
            else:
                # Invio fallito: NON marcare come visto, riproveremo al prossimo run.
                log.warning("Notifica fallita per post %s, verra' ritentata.", pid)
        else:
            seen.add(pid)

    save_state(cfg.state_file, seen)
    log.info("Completato. Notifiche inviate: %d.", notified)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(run())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - top-level guard per logging pulito
        log.exception("Errore fatale: %s", exc)
        sys.exit(1)
