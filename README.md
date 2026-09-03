# BUO — BC-250 Ultimate Orchestrator

![Versione](https://img.shields.io/badge/versione-1.1.0-green)
![Python](https://img.shields.io/badge/python-%E2%89%A53.10-blue)
![Licenza](https://img.shields.io/badge/licenza-GPL--3.0-orange)

**BUO** è uno strumento a riga di comando (Python, CLI `click`+`rich`, con
TUI opzionale `textual`) che guida in modo **sicuro e verificato**
l'ottimizzazione dell'**ASRock BC-250** (APU AMD Cyan Skillfish): analizza
la scheda, sblocca le risorse nascoste, risolve i problemi noti, trova un
buon compromesso prestazioni/consumi con undervolt e overclock entro limiti
di sicurezza, e valida ogni modifica con stress test reali.

È pensato per chi possiede una BC-250 e preferisce un percorso automatico e
ripetibile a una collezione di script da eseguire a mano.

```bash
sudo buo unleash
```

> **Nota di trasparenza sullo sviluppo.** BUO è nato come progetto personale
> ed è stato sviluppato con l'assistenza di modelli di intelligenza
> artificiale, che hanno contribuito a codice, test e documentazione. I
> valori hardware usati dal progetto **non sono inventati**: ogni
> configurazione proposta è stata validata sul campo con stress test reali,
> mai copiata da altre fonti senza verifica. La base tecnica — sblocchi,
> undervolt/overclock, fix ACPI — è il reverse engineering della community
> BC-250 (elektricM/amd-bc250-docs, bc250_smu_oc, il governor
> cyan-skillfish e altri): BUO orchestra quei tool in modo sicuro e
> automatico. A quella community va il merito principale; questo progetto
> cerca solo di renderne i risultati accessibili e ripetibili.

---

## Come funziona, in breve

Un run completo segue una pipeline a fasi, ognuna con checkpoint e (dove
serve) rollback:

```
init → pre_audit → unlock → fix → optimize → apply → validate → complete
```

- **init / pre_audit** — verifica di sanità (kernel, Mesa, temperature) e
  analisi dell'hardware: nessuna modifica.
- **unlock** — sblocco delle risorse bloccate dal produttore (core CPU e CU
  GPU), con test di stabilità e health check per ogni passo.
- **fix** — correzione dei problemi noti della scheda (es. tabelle ACPI,
  fault TLB, compute queue, GTT, sensori/ventole), ognuno con verifica e
  rollback.
- **optimize** — ricerca di un punto undervolt/overclock sicuro per il tuo
  silicio: ogni candidato viene prima testato sotto stress.
- **apply** — applica e rende persistente la configurazione trovata.
- **validate** — stress test finale, verifica dei fix e report
  before/after.

Tutto è **fail-closed**: se un test di stabilità non può essere eseguito,
BUO si ferma invece di applicare valori non verificati. Ogni file modificato
viene salvato prima dell'intervento e un **safety monitor** controlla
temperature e potenza durante l'esecuzione.

---

## Requisiti

- Una **ASRock BC-250** con Linux (il progetto è sviluppato e validato su
  Bazzite/Fedora ostree; altre distro sono supportate in modo meno
  completo).
- **Python ≥ 3.10**.
- Connessione internet al primo avvio: BUO installa da solo i tool della
  community mancanti (vedi sotto). Senza rete esiste un percorso offline.

## Installazione

```bash
git clone https://github.com/andre98king/buo.git
cd buo

# Consigliato: ambiente virtuale
python3 -m venv .venv && source .venv/bin/activate

pip install -r requirements.txt
sudo pip install -e .            # installa il comando `buo`

# Directory di sistema e configurazione
sudo mkdir -p /var/lib/buo /var/log/buo /etc/buo
sudo cp config/buo.yaml /etc/buo/
```

Verifica dell'installazione (senza hardware reale):

```bash
buo --version
buo status --mock        # stato hardware simulato
buo install-deps --check # quali tool della community sono presenti
```

Per Bazzite/ostree (sistema immutabile) e per l'installazione offline usa
la guida dedicata: [docs/INSTALL.md](docs/INSTALL.md).

### Tool della community

BUO non reinventa nulla: usa i tool che la community BC-250 ha costruito e
li installa da solo al primo run (repo pinnate a commit verificati):

- `bc250_smu_oc` (undervolt/overclock CPU), `bc250-40cu-unlock` (GPU),
  `bc250-cu-live-manager`, `bc250_memcfg`, `bc250-acpi-fix`;
- il governor GPU `cyan-skillfish-governor-smu` e `umr` come pacchetti del
  package manager della distro (mai installer di terze parti).

Se preferisci installarli prima:

```bash
sudo buo install-deps        # scarica e installa i tool mancanti
buo install-deps --check     # verifica cosa c'è e cosa manca
```

Senza rete: genera un bundle offline su una macchina connessa
(`sudo buo install-deps --export-bundle bundle.tar.gz`), copialo su USB e
importalo sulla BC-250 (`sudo buo install-deps --offline bundle.tar.gz`).
Il bundle contiene solo i checkout verificati — dettagli in
[docs/INSTALL.md](docs/INSTALL.md#6-installazione-offline-senza-rete).

## Utilizzo

### Il comando principale

```bash
# Ottimizzazione completa (tutto automatico)
sudo buo unleash

# Prima di toccare nulla: simulazione completa
sudo buo unleash --dry-run

# Primo giro consigliato: conferma per ogni fase
sudo buo unleash --interactive
```

Opzioni principali di `unleash`: `--dry-run` (nessuna modifica),
`--interactive` (conferma per fase), `--quick` (solo undervolt/overclock,
senza fix kernel), `--skip-benchmark`, `--skip-validation`, `--mock`
(hardware simulato), `--offline-bundle <file>` (importa il bundle prima
dell'auto-install).

### Diagnostica e stato (sola lettura)

```bash
buo status                 # stato hardware e ottimizzazioni
buo probe                  # solo analisi (nessuna modifica)
buo doctor                 # diagnostica completa per il supporto
buo report                 # report dell'ultimo run (Markdown)
buo report --dashboard     # report HTML con grafici before/after
buo safety-test            # verifica i gate di sicurezza
buo config                 # mostra la configurazione corrente
```

### Operazioni singole (la maggior parte richiede root)

```bash
sudo buo undervolt         # solo ricerca undervolt CPU/GPU
sudo buo overclock         # solo overclock entro il budget di potenza
sudo buo apply             # applica la configurazione trovata
sudo buo rollback          # annulla ogni modifica (rollback a cascata)
sudo buo rollback --phase <fase>   # rollback da una fase specifica
sudo buo recover           # riprende da un checkpoint dopo un reboot
sudo buo restore           # riporta la macchina allo stato salvato
sudo buo restore --profile profilo.json
sudo buo profile export    # backup del profilo macchina (JSON)
sudo buo profile import profilo.json
```

(`resume` è un alias di `recover`.)

### TUI (opzionale)

```bash
buo tui                    # cockpit interattiva (richiede: pip install textual)
```

### Tool OC avanzato (per-silicio)

Il gruppo `buo oc` gestisce la ricerca di overclock per-silicio: stato del
motore, profili, applicazione sicura con smoke test e rollback automatico.

```bash
sudo buo oc status               # stato del motore e della macchina
buo oc profiles list             # profili Stock / Certificato / Custom
sudo buo oc apply <profilo>      # applica (volatile di default)
sudo buo oc restore-stock        # torna al profilo stock
buo oc-tui                       # cockpit OC interattiva (textual)
```

Altri comandi: `buo oc run`, `oc stop`, `oc reset`, `oc watch`,
`oc profiles add|rm`, `oc heal`. Il motore di ricerca vero e proprio
(`oc3600.sh`) non è distribuito nel repository: `buo oc` opera sulla
directory `/var/lib/buo/oc` di macchine dove il motore è installato. I
comandi di profilo/apply/heal funzionano anche senza motore.

### Strumenti opzionali (dati/ML)

```bash
buo data-collect          # raccoglie campioni (es. per la stima VRAM)
buo data-upload           # invia i dati raccolti (opzionale, richiede requests)
buo ml-train              # addestra il modello ML di stima (opzionale)
```

## Configurazione

File: `/etc/buo/buo.yaml` (esempio nel repository: `config/buo.yaml`).

```yaml
hardware:
  psu_wattage: 350        # W — alimentatore dichiarato

safety:
  cpu_temp_max: 90        # °C — soglie consigliate
  gpu_temp_max: 85        # °C
  gpu_freq_max: 2200      # MHz
  power_budget: 300       # W

deps:
  auto_install: true      # BUO installa da solo i tool mancanti
```

Nota: le chiavi sotto `safety` sono **piatte** (niente sottosezioni come
`safety.cpu.*`); chiavi sconosciute generano un avviso, mai un silenzio.

Gli **hard limits** (VID ≤ 1325 mV, tensione GPU ≤ 1100 mV, ecc.) sono
codificati in `buo/constants.py` e **non** possono essere modificati dalla
configurazione: la sicurezza non è negoziabile.

## Test

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

La suite (oltre 590 test) usa hardware simulato: **non serve una BC-250**
per eseguirla. Alcuni test verificano l'interazione con i tool di sistema
(git, systemd, tar) e in ambienti particolari possono risultare
"ambientali": se un test fallisce, verifica prima che non sia dovuto
all'ambiente prima di aprirne uno nuovo. Con le dipendenze opzionali
installate (`pip install -e '.[tui,ml]'`) la copertura è completa.

## Sicurezza

- **Hard limits immutabili** nel codice (VID CPU ≤ 1325 mV, GPU ≤ 1100 mV):
  nessuna configurazione può superarli.
- **Fail-closed**: nessuna modifica senza test di stabilità reale; in
  `--dry-run` nessun modulo tocca l'hardware.
- **Safety monitor** in thread separato durante i run: temperatura e
  potenza controllate, abort + rollback su violazione.
- **Checkpoint e rollback a cascata**: lo stato è salvato prima di ogni
  modifica; ogni fix è reversibile.
- **Supply chain**: i tool della community sono pinnati a commit esatti e
  verificati (rev-parse + albero pulito + hash); il bundle offline è
  controllato con check fail-closed.
- Le letture che usano il mailbox SMU sono automaticamente disabilitate
  quando il governor SMU è attivo (accesso concorrente vietato).

Per segnalare una vulnerabilità: [SECURITY.md](SECURITY.md).

## Avvertenze

- BUO esegue **overclock e undervolt reali**: comportano rischi di
  instabilità, temperature elevate e usura. Il progetto riduce i rischi con
  limiti immutabili, test e rollback, ma **non può eliminarli**: usalo a
  tuo rischio.
- I risultati dipendono dal singolo silicio: non promettiamo numeri, e
  diffida di chi ne promette.
- BUO è uno strumento per appassionati, sviluppato come progetto personale
  di una community: **nessuna garanzia**, nessun supporto commerciale.
- La scheda BC-250 è una scheda da mining riadattata: il supporto
  ufficiale ASRock per questo uso non esiste.

## Licenza

Doppia licenza:

- **Codice**: [GNU General Public License v3.0](LICENSE)
- **Documentazione**: [Creative Commons Attribution-ShareAlike 4.0](LICENSE-docs)

## Contribuire

Ogni contributo è benvenuto: bug report, test, documentazione, nuove
verifiche sul campo. Leggi [CONTRIBUTING.md](CONTRIBUTING.md) prima di
aprire una issue o una pull request (in particolare: i test si eseguono con
`unittest`, i valori hardware non si inventano mai e gli hard limits non si
toccano).

## Documentazione

- [docs/INSTALL.md](docs/INSTALL.md) — installazione completa (incl. Bazzite/ostree e offline)
- [docs/USER_GUIDE.md](docs/USER_GUIDE.md) — guida all'uso
- [docs/HARDWARE_SETUP.md](docs/HARDWARE_SETUP.md) — setup su hardware reale
- [docs/FAQ.md](docs/FAQ.md) — domande frequenti
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — architettura interna
- [CHANGELOG.md](CHANGELOG.md) — cronologia delle versioni

## Ringraziamenti

Grazie alla community BC-250, che ha fatto il lavoro di base su questa
scheda:

- **elektricM/amd-bc250-docs** — documentazione e reverse engineering
- **bc250-collective** — `bc250_smu_oc` e fix ACPI
- **duggasco** — unlock GPU 40-CU e health test
- **WinnieLV** — `bc250-cu-live-manager`
- **fanoush** — `bc250_memcfg`
- il governor **cyan-skillfish** (e chi lo mantiene nei pacchetti distro)

Senza il loro lavoro questo progetto non esisterebbe.
