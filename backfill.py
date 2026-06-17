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

_ALLEGATI_RE = re.compile(r'<a\s+[^>]*href=["\']([^"\']+\.(?:pdf|zip|doc|docx|xls|xlsx))["\'][^>]*>(.*?)</a>', re.IGNORECASE)

def extract_attachments(post: dict) -> list[dict[str, str]]:
    content = post.get("content", {}).get("rendered", "")
    matches = _ALLEGATI_RE.findall(content)
    attachments = []
    for url, raw_text in matches:
        clean_text = html.unescape(re.sub(r"<[^>]+>", "", raw_text)).strip()
        if not clean_text or clean_text.lower() in ["scarica", "allegato", "pdf", "clicca qui", "qui"]:
            clean_text = url.split("/")[-1]
        attachments.append({"url": url, "text": clean_text})
    return attachments

def category_names(post: dict) -> list[str]:
    names = []
    embedded = post.get("_embedded", {})
    for term_group in embedded.get("wp:term", []):
        for term in term_group:
            if term.get("taxonomy") == "category" and term.get("name"):
                names.append(html.unescape(term["name"]).strip())
    return names

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

_MESI_IT = ("", "gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto", "settembre", "ottobre", "novembre", "dicembre")

def format_message(post: dict, provincia: str) -> str:
    raw_title = post.get("title", {}).get("rendered", "(senza titolo)").strip()
    title = html.escape(html.unescape(raw_title))

    # Data
    iso = post.get("date", "")
    try:
        y, m, d = iso[:10].split("-")
        date_it = f"{int(d)} {_MESI_IT[int(m)]} {y}"
    except:
        date_it = iso[:10]

    # Categorie
    names = category_names(post)
    cat_line = ("🏷 " + " · ".join(html.escape(n) for n in names)) if names else ""

    # Estratto
    raw_excerpt = post.get("excerpt", {}).get("rendered", "")
    excerpt = html.unescape(re.sub(r"<[^>]+>", "", raw_excerpt)).strip()
    excerpt = re.sub(r"\s*\[?(?:\.\.\.|\u2026)\]?\s*$", "", excerpt).strip()
    if len(excerpt) > 220:
        excerpt = excerpt[:220].rsplit(" ", 1)[0].rstrip() + "…"
    excerpt_line = f"\n<i>{html.escape(excerpt)}</i>\n" if excerpt else ""

    # Allegati
    attachments = extract_attachments(post)
    attachments_block = ""
    if attachments:
        attachments_block = "\n📎 <b>ALLEGATI:</b>\n"
        for att in attachments:
            attachments_block += f"• <a href='{att['url']}'>{html.escape(att['text'])}</a>\n"

    return (
        f"📢 <b>USP {html.escape(provincia)} — Nuovo avviso</b>\n"
        f"➖➖➖➖➖➖➖➖➖➖\n"
        f"<b>{title}</b>\n"
        f"{excerpt_line}"
        f"{attachments_block}\n"
        f"🗓 {date_it}\n"
        f"{cat_line}"
    ).replace("\n\n\n", "\n\n").rstrip()

def send_message(cfg: LocalConfig, text: str, link: str) -> bool:
    api = f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage"
    payload: dict[str, Any] = {
        "chat_id": cfg.chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if link:
        payload["reply_markup"] = {"inline_keyboard": [[{"text": "📄 Apri pagina avviso", "url": link}]]}

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
            if send_message(cfg, format_message(p, cfg.provincia), p.get("link", "")):
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
