#!/usr/bin/env python3
"""
Backfill autonomo e corazzato per popolamento storico canali USP.
"""

from __future__ import annotations

import json
import html
import os
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

DEFAULT_DAYS = 30
THROTTLE_SECONDS = 2.5
PER_PAGE = 100
HTTP_TIMEOUT = 30
TELEGRAM_MAX_LEN = 4096  # limite massimo caratteri di un messaggio Telegram

# Emoji dedicata per dare priorità visiva alle categorie principali.
# Mappa unificata RC + VV: i due siti usano slug diversi per le stesse categorie.
CATEGORY_EMOJI = {
    # Personale
    "doc": "👨‍🏫",                                  # Docenti
    "ata": "🗂️",                                    # ATA
    "dir": "👔", "ds": "👔", "dirig": "👔",          # Dirigenti / Dirigenti scolastici
    "irc": "✝️",                                     # Insegnanti di religione
    "personale-educativo": "🧑‍🏫",                  # Personale educativo
    # Procedure
    "grad": "🎓", "graduatorie": "🎓",               # Graduatorie
    "recl": "📋", "reclutamento": "📋",              # Reclutamento
    "mob": "🔄", "news": "🔄",                       # Mobilità (VV usa lo slug "news")
    "utilass": "🔁",                                 # Utilizzazioni - Assegnazioni
    "gps": "📊",                                     # GPS
    "conc": "📝", "concdoc2016": "📝",               # Concorsi
    "organico": "🏫", "organici": "🏫",              # Organico
    # Comunicazioni
    "notizie": "📰", "ccn": "📰",                    # Notizie / Circolari-Comunicazioni-Notizie
    "newscuole": "📰", "news-scuole": "📰",          # News Scuole
    "circusr": "📄", "circmiur": "📄",               # Circolari USR / MIUR
    "avvisi": "⚠️",                                  # Avvisi
    "int": "❓",                                     # Interpelli
    "attinotifica": "📌", "atti-di-notifica": "📌",  # Atti di notifica
    "modulistica": "🧾",                             # Modulistica
    # Utenti
    "stu": "🎒", "us": "🎒",                         # Studenti / Utenti scuola
    "gen": "👪",                                     # Genitori
    # Eventi / Varie
    "event": "📅",                                   # Eventi
    "form": "📚",                                    # Formazione
    "calscol": "🗓️",                                 # Calendari scolastici
    "esami-di-stato": "📖",                          # Esami di Stato / Maturità
    "sns": "⛪", "scuole-paritarie": "⛪",            # Scuole paritarie
    "cessazione-pensione": "🏖️",                     # Cessazioni - Pensioni
}

# Fallback per "famiglie" di categorie con tante varianti per anno
_CATEGORY_PREFIX = {
    "immruolo": "🪪",     # Immissioni in ruolo (varie annualità)
    "esastato": "📖",     # Esami di Stato (varie annualità)
    "ematurita": "📖",    # Esami di Maturità
}

_MONTHS_IT = {
    "01": "gennaio", "02": "febbraio", "03": "marzo", "04": "aprile",
    "05": "maggio", "06": "giugno", "07": "luglio", "08": "agosto",
    "09": "settembre", "10": "ottobre", "11": "novembre", "12": "dicembre",
}


def _format_date(date_raw: str) -> tuple[str, str]:
    """Restituisce (data italiana con orario, etichetta relativa Oggi/Ieri)."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2}):(\d{2}))?", date_raw)
    if not m:
        return date_raw, ""
    year, month, day, hh, mm = m.groups()
    formatted = f"{int(day)} {_MONTHS_IT.get(month, month)} {year}"
    if hh and mm:
        formatted += f", {hh}:{mm}"
    relative = ""
    try:
        delta = (date.today() - date(int(year), int(month), int(day))).days
        if delta == 0:
            relative = "🆕 Oggi"
        elif delta == 1:
            relative = "Ieri"
    except ValueError:
        pass
    return formatted, relative


def _format_categories(post: dict[str, Any]) -> str:
    """Costruisce la stringa categorie con emoji dedicata, senza duplicati."""
    parts: list[str] = []
    seen: set[str] = set()
    terms = post.get("_embedded", {}).get("wp:term", [])
    for term_list in terms:
        for term in term_list:
            if term.get("taxonomy") != "category":
                continue
            name = term.get("name", "")
            if not name or name in seen:
                continue
            seen.add(name)
            slug = term.get("slug", "").lower()
            emoji = CATEGORY_EMOJI.get(slug, "")
            if not emoji:
                for prefix, prefix_emoji in _CATEGORY_PREFIX.items():
                    if slug.startswith(prefix):
                        emoji = prefix_emoji
                        break
            parts.append(f"{emoji} {name}".strip())
    return " · ".join(parts)


def _extract_author(post: dict[str, Any]) -> str:
    """Estrae il nome dell'autore dall'oggetto _embedded."""
    authors = post.get("_embedded", {}).get("author", [])
    if authors and isinstance(authors, list):
        return (authors[0].get("name") or "").strip()
    return ""


def _truncate_telegram(msg: str) -> str:
    """Garantisce che il messaggio non superi il limite di Telegram."""
    if len(msg) <= TELEGRAM_MAX_LEN:
        return msg
    return msg[:TELEGRAM_MAX_LEN - 2].rsplit("\n", 1)[0] + "\n…"

class LocalConfig:
    def __init__(self) -> None:
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.site_base = os.environ.get("SITE_BASE_URL", "https://www.istruzioneatprc.it").rstrip("/")
        self.state_file = Path(os.environ.get("STATE_FILE", "state.json"))
        self.provincia = os.environ.get("PROVINCIA", "Reggio Calabria").strip()
        raw_cats = os.environ.get("CATEGORIES", "").strip()
        self.categories = {c.strip().lower() for c in raw_cats.split(",") if c.strip()}

        if not self.bot_token or not self.chat_id:
            raise SystemExit("Errore: TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti nell'ambiente.")

def fetch_range(cfg: LocalConfig, date_from: str, date_to: str) -> list[dict]:
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
    posts: list[dict] = []
    while True:
        try:
            resp = requests.get(url, params=params, headers={"User-Agent": "USP-Backfill/1.0"}, timeout=HTTP_TIMEOUT)
            if resp.status_code == 400 and params["page"] > 1:
                break
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            posts.extend(batch)
            total = int(resp.headers.get("X-WP-TotalPages", "1"))
            print(f"  [{cfg.provincia}] Pagina {params['page']}/{total} recuperata.")
            if params["page"] >= total:
                break
            params["page"] += 1
        except Exception as e:
            print(f"Errore durante il recupero dei post: {e}", file=sys.stderr)
            break
    return posts

_ALLEGATI_RE = re.compile(
    r'<a\s+[^>]*href=["\']([^"\']+\.(?:pdf|zip|doc|docx|xls|xlsx)(?:\?[^"\']*)?)["\'][^>]*>([\s\S]*?)</a>',
    re.IGNORECASE
)

# Link al visualizzatore "Albo Pretorio" (RC): non è un file diretto ma una pagina
# che contiene gli allegati reali. Va seguito per estrarne i PDF/ZIP effettivi.
_ALBO_RE = re.compile(
    r'href=["\']([^"\']*albopretorio/\?action=visatto[^"\']*)["\']',
    re.IGNORECASE
)


def _fetch_html(url: str) -> str:
    """Scarica una pagina HTML restituendo stringa vuota in caso di errore."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (USP-Backfill)"},
            timeout=HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        print(f"  Impossibile risolvere l'allegato Albo Pretorio ({url}): {exc}", file=sys.stderr)
        return ""


def extract_attachments(post: dict) -> list[dict[str, str]]:
    """Estrae i link ai file allegati pulendone il testo di anteprima.

    Logica identica a monitor.py per garantire un output uniforme tra le province.
    Gestisce sia i link diretti a file (VV) sia il visualizzatore Albo Pretorio (RC),
    che viene seguito per estrarre i file reali pubblicati sulla pagina dell'atto.
    """
    content = post.get("content", {}).get("rendered", "")

    pairs: list[tuple[str, str]] = list(_ALLEGATI_RE.findall(content))
    for albo_url in _ALBO_RE.findall(content):
        page = _fetch_html(html.unescape(albo_url.strip()))
        if page:
            pairs.extend(_ALLEGATI_RE.findall(page))

    attachments: list[dict[str, str]] = []
    seen: set[str] = set()
    for url, text in pairs:
        url = url.strip()
        if url in seen:
            continue
        seen.add(url)

        clean_text = re.sub(r'<[^>]+>', '', text).strip()
        if not clean_text:
            clean_text = url.split("/")[-1].split("?")[0]

        clean_text = html.unescape(clean_text)
        if len(clean_text) > 25:
            clean_text = clean_text[:22] + "..."

        safe_name = html.escape(clean_text)
        attachments.append({"name": safe_name, "url": url})
    return attachments

def matches_filter(post: dict, wanted: set[str]) -> bool:
    if not wanted:
        return True
    slugs = []
    embedded = post.get("_embedded", {})
    for term_group in embedded.get("wp:term", []):
        for term in term_group:
            if term.get("taxonomy") == "category" and term.get("slug"):
                slugs.append(term["slug"].lower())
    return bool(set(slugs) & wanted)

def format_message(post: dict, provincia: str) -> str:
    """Costruisce la stessa struttura grafica di monitor.py (format_message)."""
    # Rimozione dei tag grezzi dall'HTML
    raw_title = re.sub(r'<[^>]+>', '', post.get("title", {}).get("rendered", ""))
    raw_excerpt = re.sub(r'<[^>]+>', '', post.get("excerpt", {}).get("rendered", ""))

    # Unescape preventivo per convertire i codici nativi WordPress in testo leggibile
    clean_title = html.unescape(raw_title).strip()
    clean_excerpt = html.unescape(raw_excerpt).strip()
    clean_excerpt = re.sub(r'\[&hellip;\]|\[\.\.\.\]', '...', clean_excerpt)

    if len(clean_excerpt) > 280:
        clean_excerpt = clean_excerpt[:277] + "..."

    title = html.escape(clean_title)
    excerpt = html.escape(clean_excerpt)

    # Data con orario di pubblicazione + etichetta relativa (Oggi/Ieri)
    date_formatted, relative_label = _format_date(post.get("date", ""))

    # Categorie con emoji dedicata per priorità visiva
    categories_str = _format_categories(post)

    # Autore dell'articolo
    author = _extract_author(post)

    # COSTRUZIONE DEL MESSAGGIO IDENTICA A monitor.py
    msg = f"📢 <b>USP {provincia} — Nuovo avviso</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"<b>{title}</b>\n\n"

    if excerpt and excerpt != "...":
        msg += f"<i>{excerpt}</i>\n\n"  # Inserisce il testo dell'articolo in corsivo

    date_line = f"📅 {date_formatted}"
    if relative_label:
        date_line += f" · {relative_label}"
    msg += date_line + "\n"

    if categories_str:
        msg += f"🏷️ {categories_str}\n"

    if author:
        msg += f"✍️ {html.escape(author)}\n"

    attachments = extract_attachments(post)
    if attachments:
        msg += f"\n📎 <b>Allegati rilevati ({len(attachments)}):</b>\n"
        for att in attachments:
            safe_url = html.escape(att['url'])
            msg += f"• <a href='{safe_url}'>{att['name']}</a>\n"

    return _truncate_telegram(msg)

def send_message(cfg: LocalConfig, text: str, reply_markup: dict | None = None) -> bool:
    api = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    for _ in range(3):
        resp = requests.post(api, json=payload, timeout=HTTP_TIMEOUT)
        if resp.status_code == 429:
            wait = resp.json().get("parameters", {}).get("retry_after", 5) + 1
            time.sleep(wait)
            continue
        return resp.status_code == 200
    return False

def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DAYS
    cfg = LocalConfig()

    date_from = (date.today() - timedelta(days=days)).isoformat()
    date_to = date.today().isoformat()
    print(f"Avvio Backfill per [{cfg.provincia}] dal {date_from} al {date_to}")

    posts = fetch_range(cfg, date_from, date_to)
    print(f"Elaborazione di {len(posts)} articoli...")

    # Gestione stato locale
    seen_ids = set()
    if cfg.state_file.exists():
        try:
            seen_ids = set(json.loads(cfg.state_file.read_text(encoding="utf-8")).get("seen_ids", []))
        except:
            pass

    sent = 0
    for p in posts:
        pid = int(p["id"])
        if matches_filter(p, cfg.categories):
            attachments = extract_attachments(p)
            text = format_message(p, cfg.provincia)

            inline_keyboard = [[{"text": "📄 Apri pagina avviso", "url": p.get("link", "")}]]
            for att in attachments[:2]:  # Massimo due pulsanti rapidi sotto l'avviso
                inline_keyboard.append([{"text": f"⬇️ {att['name']}", "url": att["url"]}])
            reply_markup = {"inline_keyboard": inline_keyboard}

            if send_message(cfg, text, reply_markup):
                sent += 1
                print(f"  Inviato [{pid}]")
                seen_ids.add(pid)
                time.sleep(THROTTLE_SECONDS)
            else:
                print(f"  Errore invio [{pid}]", file=sys.stderr)
        else:
            seen_ids.add(pid)

    # Salva stato aggiornato
    payload = {"seen_ids": sorted(list(seen_ids)), "seeded": True, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    cfg.state_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nCompletato! Messaggi storici inviati con successo: {sent}.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
