#!/usr/bin/env python3
"""
USP Monitor -> Telegram alert bot.

Monitora le nuove pubblicazioni del sito dell'Ambito Territoriale impostato
(WordPress) e invia una notifica Telegram per ogni nuovo articolo adattando la provincia.
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
    """Carica e valida la configurazione dall'ambiente."""

    def __init__(self) -> None:
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.site_base = os.environ.get("SITE_BASE_URL", DEFAULT_SITE).rstrip("/")
        self.per_page = int(os.environ.get("PER_PAGE", "30"))
        self.state_file = Path(os.environ.get("STATE_FILE", "state.json"))
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
    """Carica lo stato."""
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


# REGEX POTENZIATA: Supporta ritorni a capo [\s\S]*? e query parameters (?:\?[^"\']*)? dopo l'estensione
_ALLEGATI_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']+\.(?:pdf|zip|doc|docx|xls|xlsx)(?:\?[^"\']*)?)["\'][[^>]*>([\s\S]*?)</a>',
    re.IGNORECASE
)


def extract_attachments(post: dict[str, Any]) -> list[dict[str, str]]:
    """Estrae tutti i link ai file allegati salvaguardando la formattazione."""
    content = post.get("content", {}).get("rendered", "")
    attachments = []
    matches = _ALLEGATI_RE.findall(content)
    for url, text in matches:
        # Rimuove tag interni al testo del link (es: span o strong)
        clean_text = re.sub(r'<[^>]+>', '', text).strip()
        if not clean_text:
            # Se il testo è vuoto estrae il nome del file dall'URL rimovendo eventuali query string
            clean_text = url.split("/")[-1].split("?")[0]
        if len(clean_text) > 25:
            clean_text = clean_text[:22] + "..."

        # IMPORTANTE: Eseguiamo l'escape del testo per evitare crash di parsing HTML su Telegram
        safe_name = html.escape(clean_text)
        attachments.append({"name": safe_name, "url": url.strip()})
    return attachments


def match_categories(post: dict[str, Any], cfg: Config) -> bool:
    """Verifica se il post appartiene ad almeno una delle categorie scelte."""
    if not cfg.categories:
        return True
    terms = post.get("_embedded", {}).get("wp:term", [])
    for term_list in terms:
        for term in term_list:
            if term.get("taxonomy") == "category":
                slug = term.get("slug", "").lower()
                if slug in cfg.categories:
                    return True
    return False


def format_message(post: dict[str, Any], cfg: Config, attachments: list[dict[str, str]]) -> str:
    """Costruisce il layout grafico del messaggio in HTML per Telegram."""
    title = html.escape(re.sub(r'<[^>]+>', '', post.get("title", {}).get("rendered", "")))
    excerpt = html.escape(re.sub(r'<[^>]+>', '', post.get("excerpt", {}).get("rendered", ""))).strip()

    if len(excerpt) > 280:
        excerpt = excerpt[:277] + "..."

    date_str = post.get("date", "").replace("T", " ")

    msg = f"📢 <b>USP {cfg.provincia} — Nuovo avviso</b>\n\n"
    msg += f"📌 <b>{title}</b>\n\n"
    if excerpt:
        msg += f"📝 <i>{excerpt}</i>\n\n"
    msg += f"📅 Pubblicato il: {date_str}\n"

    if attachments:
        msg += "\n📎 <b>Allegati rilevati:</b>\n"
        for att in attachments:
            msg += f"• <a href='{att['url']}'>{att['name']}</a>\n"

    return msg


def send_telegram_message(cfg: Config, text: str, reply_markup: dict | None = None) -> bool:
    """Invia il payload strutturato alle API di Telegram."""
    url = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    payload = {
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code != 200:
            log.error("[%s] Errore API Telegram (%d): %s", cfg.provincia, resp.status_code, resp.text)
            return False
        return True
    except Exception as exc:
        log.error("[%s] Errore di rete nell'invio a Telegram: %s", cfg.provincia, exc)
        return False


# --------------------------------------------------------------------------- #
# Core Loop Principale
# --------------------------------------------------------------------------- #

def main() -> None:
    cfg = Config()
    state = load_state(cfg.state_file)
    seen_ids = set(state.get("seen_ids", []))
    seeded = state.get("seeded", False)

    try:
        posts = fetch_posts(cfg)
    except Exception as err:
        log.error("[%s] Arresto run per errore fetch: %s", cfg.provincia, err)
        sys.exit(1)

    valid_posts = [p for p in posts if p.get("id") and match_categories(p, cfg)]

    if not valid_posts:
        log.info("[%s] Nessun articolo corrisponde ai filtri di categoria imposti.", cfg.provincia)
        save_state(cfg.state_file, seen_ids)
        return

    if not seeded:
        log.info("[%s] Inizializzazione: salvo silenziosamente %d articoli storici.", cfg.provincia, len(valid_posts))
        for post in valid_posts:
            seen_ids.add(post["id"])
        save_state(cfg.state_file, seen_ids)
        log.info("[%s] Database di stato sincronizzato. Pronto per le notifiche.", cfg.provincia)
        return

    new_posts = [p for p in valid_posts if p["id"] not in seen_ids]
    new_posts.reverse()

    if not new_posts:
        log.info("[%s] Nessun nuovo avviso pubblicato rispetto all'ultimo controllo.", cfg.provincia)
        save_state(cfg.state_file, seen_ids)
        return

    log.info("[%s] Rilevati %d nuovi avvisi! Preparo l'invio...", cfg.provincia, len(new_posts))

    success_count = 0
    for post in new_posts:
        p_id = post["id"]
        attachments = extract_attachments(post)
        text = format_message(post, cfg, attachments)

        inline_keyboard = [[{"text": "📄 Apri avviso", "url": post.get("link", "")}]]
        if attachments:
            for att in attachments[:2]:  # Massimo due bottoni veloci sotto il testo
                inline_keyboard.append([{"text": f"⬇️ {att['name']}", "url": att['url']}])

        reply_markup = {"inline_keyboard": inline_keyboard}

        if send_telegram_message(cfg, text, reply_markup):
            seen_ids.add(p_id)
            success_count += 1
            time.sleep(TELEGRAM_RATE_DELAY)
        else:
            log.warning("[%s] Invio fallito per post %d. Verrà ritentato.", cfg.provincia, p_id)

    save_state(cfg.state_file, seen_ids)
    log.info("[%s] Run terminata. Notifiche inviate con successo: %d.", cfg.provincia, success_count)


if __name__ == "__main__":
    main()
