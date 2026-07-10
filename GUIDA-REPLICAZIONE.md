# ATP Monitor — Guida alla replicazione per una nuova regione

Runbook operativo per clonare questo sistema di notifica (siti degli Ambiti
Territoriali → Telegram) su **qualsiasi regione italiana**. Pensato per essere
seguito passo-passo da un tecnico, con le insidie reali già annotate.

> **Cosa ottieni:** un bot per provincia che controlla le nuove pubblicazioni del
> sito dell'Ambito Territoriale (WordPress) e le inoltra sul canale Telegram della
> provincia. Zero infrastruttura, zero costi ricorrenti (a parte l'eventuale
> proxy), stato versionato su Git come audit trail.

---

## 0. Architettura in breve

```
GitHub Actions (cron)  →  monitor.py  →  WP REST API (/wp-json/wp/v2/posts)
                                      →  confronto con il file di stato (ID già visti)
                                      →  Telegram Bot API (solo i nuovi)
                                      →  commit del file di stato nel repo
```

Principi di design:

- **Idempotente**: nessuna notifica duplicata (confronto sugli ID già visti).
- **Seed silenzioso**: al primo avvio registra lo storico senza spammare.
- **Fail-safe**: se il fetch fallisce esce con successo (`exit 0`) per non allarmare
  su blocchi transitori; un alert admin scatta solo dopo N fallimenti consecutivi.
- **Audit trail**: ogni rilevamento è un commit Git con timestamp.

---

## 1. Prerequisiti

- Account **GitHub** e **Telegram**.
- **Elenco delle province** della regione e i **siti** dei rispettivi Ambiti
  Territoriali.
- Verifica che ogni sito sia **WordPress con REST API attiva**. Prova nel browser:
  `https://<sito>/wp-json/wp/v2/posts?per_page=1` → deve restituire **JSON**
  (una lista di post). Se dà 404/HTML, quel sito non è compatibile senza adattamenti.
- (Eventuale) un **proxy residenziale** se i siti bloccano gli IP datacenter dei
  runner GitHub — vedi §7.

---

## 2. Mappatura province → endpoint

Compila questa tabella per la tua regione **prima** di iniziare. La **sigla** (targa
automobilistica) diventa il suffisso di secrets, workflow e file di stato.

| Provincia        | Sigla | Sito Ambito Territoriale                     | REST endpoint (`…/wp-json/wp/v2/posts`) |
| ---------------- | ----- | -------------------------------------------- | --------------------------------------- |
| _(es.)_ Cosenza  | `CS`  | https://www.istruzione.calabria.it/cosenza   | `<sito>/wp-json/wp/v2/posts`            |
| …                | …     | …                                            | …                                       |

> **Nota:** alcune regioni hanno **un unico dominio con sottopercorsi**
> (es. `istruzione.calabria.it/<provincia>`), altre **domini separati** per provincia
> (es. `istruzioneatprc.it`). Il `SITE_BASE_URL` va impostato di conseguenza nel
> workflow di ciascuna provincia.

---

## 3. Bot e canali Telegram (per provincia)

Per **ogni** provincia:

1. **Bot** — su Telegram apri **@BotFather** → `/newbot` → scegli nome (es.
   "USP Cosenza") e username (es. `usp_cosenza_bot`) → ricevi il **token**
   (`123456789:AAE…`). Conservalo.
2. **Canale** — crea il canale Telegram della provincia.
3. **Admin** — aggiungi il bot come **amministratore** del canale, con permesso di
   pubblicare messaggi.
4. **CHAT_ID** — pubblica un messaggio qualsiasi nel canale, poi apri
   `https://api.telegram.org/bot<TOKEN>/getUpdates` e leggi
   `"chat":{"id":-100xxxxxxxxxx,...,"type":"channel"}`. Se `getUpdates` è vuoto,
   inoltra un post del canale a **@getidsbot**.

> **Un bot per provincia o uno condiviso?** Tecnicamente un solo bot può postare su
> più canali. Consigliato **uno per provincia** per isolamento (revoca/compromissione
> di un token non ferma gli altri) e coerenza. Nei **canali** il nome del bot non è
> visibile agli iscritti, quindi il riuso è esteticamente pulito; nei **gruppi** il
> nome del bot appare, quindi un bot dedicato è preferibile. I **canali restano
> comunque distinti** (un CHAT_ID per provincia in ogni caso).

---

## 4. Repository GitHub

1. Crea un repo **privato** `atp-<regione>-monitor` (es. `atp-calabria-monitor`).
2. Carica i file del progetto mantenendo la struttura:

   ```
   .
   ├── monitor.py
   ├── requirements.txt
   ├── README.md
   ├── _test_proxy.py                 # healthcheck del proxy (facoltativo ma utile)
   └── .github/workflows/
       ├── monitor_<provincia1>.yml
       ├── monitor_<provincia2>.yml
       └── …                          # un workflow per provincia
   ```

3. Crea un **Personal Access Token** (classic, scope `repo`) da usare come secret
   `BOT_PUSH_TOKEN`: serve ai workflow per committare il file di stato.

---

## 5. Secrets (Settings → Secrets and variables → Actions)

**Condivisi** (una sola volta per repo):

| Secret                    | Valore                                                                 |
| ------------------------- | ---------------------------------------------------------------------- |
| `BOT_PUSH_TOKEN`          | PAT con scope `repo` (auto-commit dello stato)                         |
| `TELEGRAM_ADMIN_CHAT_ID`  | chat_id personale dove ricevere gli alert di sistema                    |
| `PROXY_URL`               | (se serve) URL del proxy in **formato URL** — vedi §7. **Uno solo per tutte le province.** |

**Per provincia** (suffisso = sigla, es. `_CS`):

| Secret                        | Valore                                  |
| ----------------------------- | --------------------------------------- |
| `TELEGRAM_BOT_TOKEN_<SIGLA>`  | token del bot della provincia            |
| `TELEGRAM_CHAT_ID_<SIGLA>`    | chat_id del canale della provincia (`-100…`) |

> ⚠️ I nomi dei secret devono corrispondere **esattamente** a quelli referenziati
> nel workflow (case-sensitive).

---

## 6. Workflow per provincia (template)

Copia questo template in `.github/workflows/monitor_<provincia>.yml`, sostituendo i
segnaposto `<…>`:

```yaml
name: USP <SIGLA> Monitor

on:
  schedule:
    # Sfasa il cron di ogni provincia di qualche minuto rispetto alle altre,
    # per evitare conflitti di push sul file di stato (auto-commit concorrenti).
    - cron: "5 8,11,16,21 * * *"
  workflow_dispatch:

jobs:
  monitor:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          token: ${{ secrets.BOT_PUSH_TOKEN }}
      - uses: actions/setup-python@v5
        with:
          python-version: "3.10"
          cache: "pip"
      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
      - name: Run Monitor Script
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN_<SIGLA> }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID_<SIGLA> }}
          CATEGORIES: ""                                   # vuoto = tutte le categorie
          PROVINCIA: "<Nome Provincia>"
          SITE_BASE_URL: "<https://sito-ambito-territoriale>"
          STATE_FILE: "state_<provincia>.json"
          PER_PAGE: "15"
          PROXY_URL: ${{ secrets.PROXY_URL }}              # innocuo se il secret non esiste
          ADMIN_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN_<SIGLA_ADMIN> }}
          ADMIN_CHAT_ID: ${{ secrets.TELEGRAM_ADMIN_CHAT_ID }}
        run: python monitor.py
      - name: Commit and Push changes
        uses: stefanzweifel/git-auto-commit-action@v5
        with:
          commit_message: "chore: auto-update state_<provincia>.json [skip ci]"
          file_pattern: "state_<provincia>.json"
      - name: Notifica fallimento su Telegram
        if: failure()
        run: |
          curl -s -X POST "https://api.telegram.org/bot${{ secrets.TELEGRAM_BOT_TOKEN_<SIGLA_ADMIN> }}/sendMessage" \
            --data-urlencode chat_id="${{ secrets.TELEGRAM_ADMIN_CHAT_ID }}" \
            --data-urlencode text="⚠️ Workflow FALLITO: USP <SIGLA> Monitor"
```

Variabili `env` principali: vedi la tabella "Configurazione" nel `README.md`.

---

## 7. Proxy (opzionale — per siti che bloccano gli IP datacenter)

**Quando serve:** se il fetch diretto dai runner GitHub viene bloccato — sintomi:
`HTTP 200` con una **pagina di blocco al posto del JSON**, oppure `403`. Il codice
ripiega automaticamente sul proxy se `PROXY_URL` è impostato.

**Come configurarlo (IPRoyal PAYG residenziale):**

1. Acquista il taglio minimo (**1 GB basta**: il fallback è saltuario e i payload
   JSON sono da pochi KB → la banda non scade e dura mesi). **Un solo acquisto, una
   sola credenziale, un solo secret `PROXY_URL`** per tutte le province.
2. Imposta il **geo su Italia** e rotazione automatica.
3. ⚠️ **FORMATO CRITICO.** La dashboard IPRoyal mostra le credenziali come
   `HOST:PORT:USER:PASS` (separate da due punti). Il secret vuole il **formato URL**:

   ```
   Dashboard:  geo.iproyal.com:12323:tuouser:tuapass
   PROXY_URL:  http://tuouser:tuapass@geo.iproyal.com:12323
   ```

   Cioè `http://` + `USER:PASS` + `@` + `HOST:PORT`. Niente virgolette, niente spazi.
   Un formato sbagliato produce l'errore `Failed to parse: ***` nei log (vedi §10).

---

## 8. Primo avvio (seed silenzioso)

Per ogni provincia: **Actions → USP `<SIGLA>` Monitor → Run workflow**.

Il primo run **non invia notifiche**: registra gli articoli già pubblicati e crea
`state_<provincia>.json`. Dal run successivo arrivano solo i **nuovi** avvisi.
Da lì il cron parte da solo.

---

## 9. Test e verifica

**Test del proxy (isolato), in locale:**

```bash
export PROXY_URL="http://USER:PASS@HOST:PORTA"
python _test_proxy.py                          # endpoint di default
python _test_proxy.py https://<altro-sito>/…   # endpoint specifico
```

Stampa l'**IP di uscita** (deve essere italiano) e classifica la risposta
dell'endpoint (JSON valido / origine giù / blocco anti-bot / challenge WAF).

**Lettura dei log su GitHub Actions:** apri il run → job `monitor` → step
**Run Monitor Script**. Cerca `Recuperati N post dalla REST API` = fetch riuscito.

> 🔴 **Trappola importante:** un run **verde ≠ fetch riuscito**. Il codice fa
> `exit 0` anche quando il fetch fallisce (per non spammare l'admin). Non fidarti
> del pallino verde: **leggi sempre i log** e cerca `Recuperati N post`. Se vedi
> invece `Fetch non riuscito`, il canale non ha ricevuto nulla.

---

## 10. Troubleshooting (lezioni imparate sul campo)

| Sintomo nei log | Causa reale | Rimedio |
| --------------- | ----------- | ------- |
| `Failed to parse: ***` dopo "Passo al proxy" | `PROXY_URL` **malformato** (manca `http://` o è nel formato `host:port:user:pass`) | Riscrivi il secret in formato URL (§7) |
| `503 … No server is available to handle this request` | **Origine del sito giù/sovraccarica** (errore HAProxy). NON è un blocco né un problema di proxy | **Riprova più tardi.** Né proxy né scraping API aiutano: il server non risponde a nessuno |
| `HTTP 200` + corpo **non-JSON** (pagina HTML) | **Challenge WAF** anti-bot (guarda l'IP/headers) | Proxy residenziale; se non basta, scraping API |
| `403` / `429` | Accesso negato / rate-limit lato IP | Cambia geo/IP del proxy o usa scraping API |
| Run **verde** ma canale muto | Fetch fallito silenzioso (`exit 0`) | Leggi i log; l'alert admin scatta dopo `MAX_CONSECUTIVE_FAILURES` |
| Workflow rosso: `Configurazione mancante: TELEGRAM_BOT_TOKEN` | Secret della provincia non impostato | Aggiungi `TELEGRAM_BOT_TOKEN_<SIGLA>` / `TELEGRAM_CHAT_ID_<SIGLA>` |
| `push … non-fast-forward` lavorando in locale | La history è divergente per l'auto-commit dello stato dei workflow | `git pull --rebase origin master` poi `git push` |
| `Unable to create '.git/index.lock'` | Lock stantio da una sessione git interrotta | Verifica che nessun processo git giri, poi rimuovi il file `.git/*.lock` |

> **Distinzione chiave:** *503 "no server available"* = **il sito è giù** (nessuna
> tecnica di aggiramento serve). *200 non-JSON* = **il sito ti blocca** (lì il proxy
> o la scraping API ha senso). Non confondere i due: spendere per una scraping API su
> un sito semplicemente offline è denaro buttato.

---

## 11. Manutenzione

- **Cron dormienti:** GitHub disabilita gli scheduled workflow dopo **60 giorni senza
  commit**. L'auto-commit del file di stato tiene il repo attivo; in periodi morti
  basta un commit qualsiasi.
- **Banda proxy:** consumo trascurabile (fallback saltuario, JSON da KB). 1 GB PAYG
  dura mesi/anni. Con IPRoyal la banda non scade.
- **Aggiungere una provincia:** 1 bot + 1 canale + 2 secrets (`_SIGLA`) + 1 workflow
  (copia il template §6) + 1 primo avvio manuale. Il file di stato si crea da solo.

---

## Appendice A — Checklist rapida (nuova regione)

- [ ] Elenco province + siti + verifica REST API (`/wp-json/wp/v2/posts` → JSON)
- [ ] Tabella province → sigla → endpoint (§2)
- [ ] Per provincia: bot + canale + bot admin del canale + CHAT_ID (§3)
- [ ] Repo privato `atp-<regione>-monitor` + upload progetto (§4)
- [ ] `BOT_PUSH_TOKEN` (PAT scope `repo`) + `TELEGRAM_ADMIN_CHAT_ID` (§5)
- [ ] Secrets per provincia `TELEGRAM_BOT_TOKEN_<SIGLA>` / `TELEGRAM_CHAT_ID_<SIGLA>` (§5)
- [ ] (Se i siti bloccano) `PROXY_URL` in formato URL corretto (§7)
- [ ] Un workflow per provincia dal template, con `SITE_BASE_URL` e `STATE_FILE` giusti (§6)
- [ ] Primo avvio manuale per provincia → verifica `Recuperati N post` nei log (§8–9)

## Appendice B — Convenzioni di naming

- **Sigla provincia** = targa automobilistica (RC, CS, KR, CZ, VV, …).
- **Secret bot/chat:** `TELEGRAM_BOT_TOKEN_<SIGLA>`, `TELEGRAM_CHAT_ID_<SIGLA>`.
- **File di stato:** `state_<provincia_estesa>.json` (es. `state_reggio_calabria.json`).
- **Workflow:** `.github/workflows/monitor_<provincia_estesa>.yml`, `name: USP <SIGLA> Monitor`.
