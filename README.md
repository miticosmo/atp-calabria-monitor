# USP Reggio Calabria → Telegram Monitor

Bot di notifica che controlla periodicamente le nuove pubblicazioni del sito
dell'**Ambito Territoriale di Reggio Calabria** (https://www.istruzioneatprc.it)
e invia un messaggio Telegram per ogni nuovo articolo.

Niente scraping fragile: interroga la **REST API nativa di WordPress**. Lo stato
viene versionato su Git, quindi ogni rilevamento è tracciato con timestamp
(audit trail immutabile).

---

## Architettura in breve

```
GitHub Actions (cron)  →  monitor.py  →  WP REST API (/wp-json/wp/v2/posts)
                                      →  confronto con il file di stato (ID già visti)
                                      →  Telegram Bot API (solo i nuovi)
                                      →  commit del file di stato nel repo
```

- **Zero infrastruttura**, **zero costi** (GitHub Actions, repo privato).
- Idempotente: nessuna notifica duplicata.
- Primo avvio silenzioso: popola lo stato senza spammare gli articoli esistenti.

---

## Setup (≈ 15 minuti)

### 1. Crea il bot Telegram

1. Su Telegram apri **@BotFather** → comando `/newbot`.
2. Scegli nome e username del bot. BotFather ti restituisce un **token** del tipo
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`. Conservalo.
3. **Importante**: apri il tuo nuovo bot e premi `/start` (un bot non può
   scriverti finché non lo avvii).

### 2. Trova il tuo CHAT_ID

Metodo veloce: scrivi a **@userinfobot** su Telegram, ti risponde con il tuo ID
numerico.

Metodo alternativo: dopo aver scritto al tuo bot, apri nel browser
`https://api.telegram.org/bot<IL_TUO_TOKEN>/getUpdates` e leggi `chat.id`.

### 3. Crea il repository

1. Crea un repo **privato** su GitHub (es. `usp-rc-monitor`).
2. Carica tutti i file di questo progetto, mantenendo la struttura:

   ```
   .
   ├── monitor.py
   ├── requirements.txt
   ├── README.md
   └── .github/
       └── workflows/
           ├── monitor_reggio_calabria.yml
           ├── monitor_vibo_valentia.yml
           ├── monitor_catanzaro.yml
           ├── monitor_cosenza.yml
           └── monitor_crotone.yml
   ```

   Ogni provincia ha il proprio workflow e il proprio file di stato
   (`state_reggio_calabria.json`, `state_vibo_valentia.json`,
   `state_catanzaro.json`, `state_cosenza.json`, `state_crotone.json`), così
   non si sovrappongono e non generano notifiche doppie.

### 4. Imposta i Secrets

Nel repo: **Settings → Secrets and variables → Actions → New repository secret**.
Crea questi due secret (mai scriverli nel codice):

| Nome                  | Valore                    |
| --------------------- | ------------------------- |
| `TELEGRAM_BOT_TOKEN`  | il token di @BotFather    |
| `TELEGRAM_CHAT_ID`    | il tuo chat_id            |
| `BOT_PUSH_TOKEN`      | Personal Access Token con permesso `repo`, usato dai workflow per committare il file di stato |
| `PROXY_URL`           | (solo province su istruzione.calabria.it) URL di un proxy HTTP/HTTPS, usato come fallback quando il sito blocca l'IP datacenter dei runner GitHub |
| `TELEGRAM_ADMIN_CHAT_ID` | chat_id personale dove ricevere gli alert di sistema (workflow falliti, fetch bloccato per più run consecutivi) |

### 5. Primo avvio

Vai su **Actions → USP RC Monitor → Run workflow** (lancio manuale).
Il primo run **non invia notifiche**: registra gli articoli già pubblicati e
crea il file di stato della provincia (es. `state_reggio_calabria.json`). Dal
run successivo riceverai solo i **nuovi** avvisi. Ripeti per `USP VV Monitor`,
`USP CZ Monitor`, `USP CS Monitor` e `USP KR Monitor`.

Da lì il cron parte da solo.

---

## Configurazione

Modificabile nei workflow per provincia in `.github/workflows/`
(`monitor_reggio_calabria.yml`, `monitor_vibo_valentia.yml`), sezione `env`:

| Variabile     | Default                    | Descrizione                                                      |
| ------------- | -------------------------- | ---------------------------------------------------------------- |
| `CATEGORIES`  | `grad,doc,recl,avvisi`     | Slug categorie da notificare, separati da virgola. Vuoto = tutte |
| `PROVINCIA`   | `Reggio Calabria`          | Nome provincia mostrato nell'intestazione del messaggio          |
| `PER_PAGE`    | `30`                       | Quanti articoli leggere per run                                  |
| `STATE_FILE`  | `state.json`               | Percorso del file di stato (impostato per provincia nei workflow)|
| `SITE_BASE_URL` | `https://www.istruzioneatprc.it` | URL base del sito                                       |
| `PROXY_URL`   | _(vuoto)_                  | Proxy usato solo se il fetch diretto verso il sito fallisce (province su istruzione.calabria.it) |
| `MAX_CONSECUTIVE_FAILURES` | `3`           | Dopo quanti run consecutivi con fetch fallito viene inviato l'alert admin |
| `ADMIN_BOT_TOKEN` / `ADMIN_CHAT_ID` | _(vuoto)_ | Bot/chat per l'alert di fetch bloccato. Se non impostati, l'alert è disabilitato silenziosamente |

### Slug categorie utili (dal sito)

| Slug    | Categoria          |
| ------- | ------------------ |
| `grad`  | Graduatorie        |
| `doc`   | Docenti            |
| `recl`  | Reclutamento       |
| `mob`   | Mobilità           |
| `avvisi`| Avvisi             |
| `ata`   | A.T.A.             |
| `notizie` | Notizie (generica) |

> Il filtro è in OR: arriva la notifica se l'articolo ha **almeno una** delle
> categorie elencate.

### Frequenza dei controlli

Nel `cron` del workflow. Esempi (orari in **UTC**):

| Cron              | Significato (ora italiana estiva)        |
| ----------------- | ---------------------------------------- |
| `0 5-20 * * *`    | ogni ora, ~07:00–22:00 (default)         |
| `*/30 6-20 * * *` | ogni 30 min, ~08:00–22:00                |
| `0 6 * * *`       | una volta al giorno, ~08:00             |

Usa https://crontab.guru per comporre l'espressione.

---

## Limiti noti (onestà tecnica)

- **Puntualità**: gli scheduled workflow di GitHub possono ritardare di alcuni
  minuti sotto carico. Per un alert non time-critical è irrilevante.
- **Inattività**: GitHub disabilita i cron dopo 60 giorni senza commit nel repo.
  Il bot committa il file di stato ad ogni rilevamento, quindi in pratica resta
  sempre attivo; in periodi morti basta un commit qualsiasi per riattivarlo.
- **Solo nuovi articoli**: la v1 notifica un articolo una volta sola. Non rileva
  *modifiche* a un articolo già visto (vedi sotto).
- **Blocco WAF sulle province istruzione.calabria.it (VV, CZ, CS, KR)**: il sito
  a volte risponde con HTTP 200 ma una pagina di blocco invece del JSON atteso
  (non un errore di rete, quindi il fallback al `PROXY_URL` non scattava). Corretto:
  ora un corpo non-JSON forza esplicitamente il passaggio al proxy. Se il blocco
  persiste per `MAX_CONSECUTIVE_FAILURES` run di fila, viene inviato un alert
  Telegram all'admin (il run in sé continua a uscire con successo, per non
  spammare su singoli blocchi transitori).

---

## Feedback e richieste di funzionalità

Hai un suggerimento, hai trovato un bug o vuoi che venga aggiunta un'altra
provincia/funzionalità? Scrivi direttamente su Telegram a
[@MitiCosmo](https://t.me/MitiCosmo).

---

## Possibili evoluzioni

- **Rilevamento modifiche**: tracciare anche il campo `modified` di ogni post e
  rinotificare se cambia (utile in ottica forense: "questo atto è stato
  aggiornato il giorno X").
- **Fallback RSS**: se la REST API venisse bloccata, switch su `/feed/` con
  `feedparser`.
- **Multi-destinatario / canale**: inviare a un canale Telegram invece che in
  privato (basta cambiare `TELEGRAM_CHAT_ID` con `@nomecanale`).

---

## Esecuzione locale (per test)

```bash
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN="123:ABC..."
export TELEGRAM_CHAT_ID="12345678"
export CATEGORIES="grad,doc"

python monitor.py
```

Il primo run locale crea `state.json` senza notificare. Cancella `state.json`
per ri-fare il "seeding".
