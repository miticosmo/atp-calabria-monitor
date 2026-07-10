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
from datetime import date
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
HTTP_TIMEOUT = (10, 60)  # (connect, read) in secondi: il sito sorgente risponde lentamente
MAX_RETRIES = 3  # i retry non aggirano il tarpit/WAF lato server: 3 tentativi bastano per le lentezze vere
RETRY_BACKOFF = 5  # secondi (backoff crescente: 5, 10, 15s)
TELEGRAM_RATE_DELAY = 1.0  # pausa tra messaggi per anti-flood
TELEGRAM_MAX_LEN = 4096  # limite massimo caratteri di un messaggio Telegram
DONATION_URL = "https://paypal.me/cosmopata"  # link "Offri un caffè" mostrato sotto ogni avviso

# Proxy opzionale, applicato SOLO alle richieste verso il sito sorgente (non a Telegram,
# per non sprecare banda). Serve alle province su istruzione.calabria.it, che bloccano
# gli IP datacenter dei runner GitHub. Se PROXY_URL non è impostato, le richieste sono dirette.
PROXY_URL = os.environ.get("PROXY_URL", "").strip()
SOURCE_PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
_SOURCE_USE_PROXY = False  # diventa True quando il tentativo diretto fallisce e si passa al proxy

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


class Config:
    """Carica e valida la configurazione dall'ambiente."""

    def __init__(self) -> None:
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        self.site_base = os.environ.get("SITE_BASE_URL", DEFAULT_SITE).rstrip("/")
        self.per_page = int(os.environ.get("PER_PAGE", "30"))
        self.state_file = Path(os.environ.get("STATE_FILE", "state.json"))
        self.provincia = os.environ.get("PROVINCIA", "Reggio Calabria").strip()

        # Alert admin opzionale dopo N run consecutive con fetch fallito. Il fetch fallito
        # esce con codice 0 (vedi main()) per non intasare l'admin ad ogni blocco transitorio
        # del WAF, quindi il step "failure()" del workflow non scatterebbe mai: questo è
        # l'unico modo per accorgersi se un bot resta bloccato per giorni senza notificarlo.
        self.admin_bot_token = os.environ.get("ADMIN_BOT_TOKEN", "").strip()
        self.admin_chat_id = os.environ.get("ADMIN_CHAT_ID", "").strip()
        self.max_consecutive_failures = int(os.environ.get("MAX_CONSECUTIVE_FAILURES", "3"))
        # Anti-spam degli alert: dopo il primo avviso al superamento della soglia, invia
        # al più un promemoria ogni N run consecutivi ancora falliti (non ad ogni run).
        # 0 = un solo avviso finché il blocco persiste. Default 12 (~2-3 giorni a 5 run/dì).
        self.realert_every_failures = int(os.environ.get("REALERT_EVERY_FAILURES", "12"))

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
    """Carica lo stato del tracciamento degli articoli."""
    if not path.exists():
        return {"seen_ids": [], "seeded": False, "consecutive_failures": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.setdefault("seen_ids", [])
        data.setdefault("seeded", bool(data["seen_ids"]))
        data.setdefault("consecutive_failures", 0)
        return data
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Stato non leggibile (%s). Reinizializzo.", exc)
        return {"seen_ids": [], "seeded": False, "consecutive_failures": 0}


def save_state(path: Path, seen_ids: set[int], *, consecutive_failures: int = 0) -> None:
    """Salva lo stato in modo atomico."""
    payload = {
        "seen_ids": sorted(seen_ids),
        "seeded": True,
        "consecutive_failures": consecutive_failures,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Fetch e parsing articoli (WordPress REST API)
# --------------------------------------------------------------------------- #

def _get_source(url: str, *, params: dict | None = None, headers: dict | None = None) -> requests.Response:
    """GET verso il sito sorgente con fallback automatico al proxy.

    Prima tenta la richiesta diretta (IP del runner GitHub) per non consumare
    banda del proxy; solo se fallisce e PROXY_URL è configurato ripiega sul proxy.
    La scelta viene ricordata per le richieste successive dello stesso run, così
    una volta accertato che il diretto è bloccato non si riprova ogni volta.
    """
    global _SOURCE_USE_PROXY
    if _SOURCE_USE_PROXY and SOURCE_PROXIES:
        resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT, proxies=SOURCE_PROXIES)
        resp.raise_for_status()
        return resp
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        return resp
    except requests.RequestException as exc:
        if not SOURCE_PROXIES:
            raise
        log.warning("Tentativo diretto fallito (%s). Passo al proxy.", exc)
        _SOURCE_USE_PROXY = True
        resp = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT, proxies=SOURCE_PROXIES)
        resp.raise_for_status()
        return resp


def fetch_posts(cfg: Config) -> list[dict[str, Any]]:
    """Recupera gli ultimi post via REST API WordPress."""
    url = f"{cfg.site_base}/wp-json/wp/v2/posts"
    params = {"per_page": cfg.per_page, "_embed": "1", "orderby": "date", "order": "desc"}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    global _SOURCE_USE_PROXY
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = _get_source(url, params=params, headers=headers)
            try:
                posts = resp.json()
            except ValueError as exc:
                # Risposta HTTP 200 ma corpo non-JSON: quasi sempre una pagina di blocco/
                # challenge del WAF (l'IP datacenter del runner viene accettato a livello
                # TCP ma servito con una pagina fittizia invece di un errore HTTP). _get_source
                # non lo rileva perché non solleva RequestException, quindi il fallback al
                # proxy non scatterebbe mai: lo forziamo qui esplicitamente.
                snippet = resp.text[:150].replace("\n", " ")
                log.warning("[%s] Risposta 200 non-JSON (probabile blocco WAF): %r", cfg.provincia, snippet)
                if SOURCE_PROXIES and not _SOURCE_USE_PROXY:
                    log.warning("[%s] Passo al proxy per il prossimo tentativo.", cfg.provincia)
                    _SOURCE_USE_PROXY = True
                raise
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


# Regex robusta per catturare tag ancorati su più linee senza lanciare eccezioni re.error
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
        resp = _get_source(url, headers={"User-Agent": "Mozilla/5.0 (USP-Monitor)"})
        return resp.text
    except requests.RequestException as exc:
        log.warning("Impossibile risolvere l'allegato Albo Pretorio (%s): %s", url, exc)
        return ""


def extract_attachments(post: dict[str, Any]) -> list[dict[str, str]]:
    """Estrae i link ai file allegati pulendone il testo di anteprima.

    Gestisce due casi:
    1. link diretti a file nel contenuto (es. Vibo Valentia);
    2. link al visualizzatore Albo Pretorio (es. Reggio Calabria), che viene
       seguito per estrarre i file reali pubblicati sulla pagina dell'atto.
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


def match_categories(post: dict[str, Any], cfg: Config) -> bool:
    """Verifica se il post appartiene ad almeno una delle categorie abilitate."""
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
    """Costruisce la struttura grafica esatta richiesta nel mockup visivo."""
    # Rimozione dei tag grezzi dall'HTML
    raw_title = re.sub(r'<[^>]+>', '', post.get("title", {}).get("rendered", ""))
    raw_excerpt = re.sub(r'<[^>]+>', '', post.get("excerpt", {}).get("rendered", ""))

    # Unescape preventivo per convertire i codici nativi WordPress (es. &#8211;) in testo leggibile
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

    # COSTRUZIONE DEL MESSAGGIO CON SEPARATORI GEOMETRICI
    msg = f"📢 <b>Ambito Territoriale di {cfg.provincia}</b>\n"
    msg += "🔔 <b>Nuovo avviso</b>\n"
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

    if attachments:
        msg += f"\n📎 <b>Allegati rilevati ({len(attachments)}):</b>\n"
        for att in attachments:
            safe_url = html.escape(att['url'])
            msg += f"• <a href='{safe_url}'>{att['name']}</a>\n"

    msg += (
        "\n💙 <i>Questo canale è gratuito e senza scopo di lucro. Tenerlo online ha "
        "però un costo: se ti è utile, offrimi un caffè, bastano pochi secondi ed è "
        "un grande aiuto per mantenerlo attivo. Grazie!</i>\n"
    )

    return _truncate_telegram(msg)


def notify_admin_fetch_failure(cfg: Config, consecutive_failures: int, err: Exception) -> None:
    """Avvisa l'amministratore quando il fetch fallisce per N run consecutive.

    Il fetch fallito esce con codice 0 (per non allertare su singoli blocchi
    transitori del WAF), quindi lo step 'failure()' del workflow GitHub Actions
    non scatta mai in questo caso. Senza questo alert un bot potrebbe restare
    bloccato per giorni in silenzio: nessun errore visibile, nessuna notifica,
    solo un canale che smette di ricevere avvisi.
    """
    if not cfg.admin_bot_token or not cfg.admin_chat_id:
        return
    url = f"https://api.telegram.org/bot{cfg.admin_bot_token}/sendMessage"
    text = (
        f"⚠️ <b>USP Monitor — {html.escape(cfg.provincia)}</b>\n"
        f"Fetch fallito per {consecutive_failures} run consecutive.\n"
        f"Ultimo errore: {html.escape(str(err))[:300]}\n"
        f"Il canale potrebbe non ricevere nuovi avvisi finché il blocco persiste."
    )
    try:
        resp = requests.post(
            url,
            json={"chat_id": cfg.admin_chat_id, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if resp.status_code != 200:
            log.error("[%s] Alert admin non inviato (%d): %s", cfg.provincia, resp.status_code, resp.text)
    except Exception as exc:
        log.error("[%s] Errore di rete nell'invio dell'alert admin: %s", cfg.provincia, exc)


def send_telegram_message(cfg: Config, text: str, reply_markup: dict | None = None) -> bool:
    """Invia il payload formattato alle API di Telegram."""
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
    consecutive_failures = state.get("consecutive_failures", 0)

    try:
        posts = fetch_posts(cfg)
    except Exception as err:
        # Fetch fallito (tipicamente blocco WAF/timeout verso gli IP dei runner).
        # Non è un bug del bot di per sé: esce con successo così lo stato resta
        # invariato e il prossimo run orario recupera eventuali avvisi nel frattempo.
        # Se però il fallimento si ripete per troppi run consecutivi, il blocco è
        # probabilmente persistente (non transitorio): avvisiamo l'admin, perché
        # altrimenti il canale può restare silenzioso per giorni senza che nessuno
        # se ne accorga (exit 0 = "successo" agli occhi di GitHub Actions).
        consecutive_failures += 1
        log.warning("[%s] Fetch non riuscito (%d run consecutivi), salto questo run: %s",
                    cfg.provincia, consecutive_failures, err)
        # Anti-spam: avvisa l'admin SOLO al primo superamento della soglia, poi al più un
        # promemoria ogni `realert_every_failures` run. Senza questo, l'alert ripartirebbe
        # a OGNI run finché il blocco persiste (4 province x 5 run/dì = decine di notifiche).
        over_threshold = consecutive_failures - cfg.max_consecutive_failures
        if over_threshold == 0 or (
            cfg.realert_every_failures > 0
            and over_threshold > 0
            and over_threshold % cfg.realert_every_failures == 0
        ):
            notify_admin_fetch_failure(cfg, consecutive_failures, err)
        save_state(cfg.state_file, seen_ids, consecutive_failures=consecutive_failures)
        sys.exit(0)

    # Fetch riuscito: azzera il contatore dei fallimenti consecutivi.
    consecutive_failures = 0

    valid_posts = [p for p in posts if p.get("id") and match_categories(p, cfg)]

    if not valid_posts:
        log.info("[%s] Nessun articolo corrisponde ai filtri di categoria imposti.", cfg.provincia)
        save_state(cfg.state_file, seen_ids, consecutive_failures=consecutive_failures)
        return

    if not seeded:
        log.info("[%s] Inizializzazione: salvo silenziosamente %d articoli storici.", cfg.provincia, len(valid_posts))
        for post in valid_posts:
            seen_ids.add(post["id"])
        save_state(cfg.state_file, seen_ids, consecutive_failures=consecutive_failures)
        log.info("[%s] Database di stato sincronizzato. Pronto per le notifiche.", cfg.provincia)
        return

    new_posts = [p for p in valid_posts if p["id"] not in seen_ids]
    new_posts.reverse()

    if not new_posts:
        log.info("[%s] Nessun nuovo avviso pubblicato rispetto all'ultimo controllo.", cfg.provincia)
        save_state(cfg.state_file, seen_ids, consecutive_failures=consecutive_failures)
        return

    log.info("[%s] Rilevati %d nuovi avvisi! Preparo l'invio...", cfg.provincia, len(new_posts))

    success_count = 0
    for post in new_posts:
        p_id = post["id"]
        attachments = extract_attachments(post)
        text = format_message(post, cfg, attachments)

        inline_keyboard = [[{"text": "📄 Apri pagina avviso", "url": post.get("link", "")}]]
        if attachments:
            for att in attachments[:2]:  # Massimo due pulsanti rapidi sotto l'avviso
                inline_keyboard.append([{"text": f"⬇️ {att['name']}", "url": att['url']}])
        inline_keyboard.append([{"text": "☕ Offrimi un caffè per il progetto", "url": DONATION_URL}])

        reply_markup = {"inline_keyboard": inline_keyboard}

        if send_telegram_message(cfg, text, reply_markup):
            seen_ids.add(p_id)
            success_count += 1
            time.sleep(TELEGRAM_RATE_DELAY)
        else:
            log.warning("[%s] Invio fallito per post %d. Verrà ritentato.", cfg.provincia, p_id)

    save_state(cfg.state_file, seen_ids, consecutive_failures=consecutive_failures)
    log.info("[%s] Run terminata. Notifiche inviate con successo: %d.", cfg.provincia, success_count)


if __name__ == "__main__":
    main()
