# 🚀 BC-250 Ultimate Orchestrator (BUO)

<div align="center">

![BC-250](https://img.shields.io/badge/BC--250-ULTIMATE-blue?style=for-the-badge)
![Version](https://img.shields.io/badge/version-1.0.0-green)
![Python](https://img.shields.io/badge/python-3.10+-blue)
![License](https://img.shields.io/badge/license-GPLv3-orange)

**Ottimizzazione automatica per ASRock BC-250 — Un solo comando, il massimo per la tua scheda.**

[Installazione](#-installazione) • [Utilizzo](#-utilizzo) • [Configurazione](#-configurazione) • [Sicurezza](#-sicurezza) • [Documentazione](#-documentazione) • [Contribuire](#-contribuire)

</div>

---

## 📖 Panoramica

**BUO (BC-250 Ultimate Orchestrator)** automatizza l'intero processo di ottimizzazione della scheda ASRock BC-250 (APU AMD Cyan Skillfish, gfx1013):

- 🔍 **Analizza** l'hardware e rileva tutti i problemi noti
- 🔓 **Sblocca**: CPU 6→8 core, GPU 24→40 CU (con health test e maschera dei difetti)
- 🔧 **Risolve**: TLB fault, compute queue (ACE), IOMMU, ACPI C-State, GTT, sensori
- ⚡ **Ottimizza**: undervolt + overclock power-limited per la massima efficienza
- 📊 **Valida**: stress test, verifica dei fix, benchmark before/after con report
- 🛡️ **Protegge**: hard limits immutabili, safety monitor, checkpoint, rollback a cascata

> **Un solo comando:** `sudo buo unleash`

---

## 🎯 Perché BUO?

| Problema | Soluzione BUO |
|:---|:---|
| 6 core CPU bloccati | Sblocco automatico a 8 core (con test di stabilità) |
| 24 CU GPU limitate | Sblocco a 40 CU (con health test per-WGP) |
| Overclock manuale rischioso | Undervolt/overclock automatico entro limiti sicuri |
| VRAM senza sensori | Stima intelligente basata su modello empirico (+ML) |
| TLB fault (crash AI/ML) | Patch kernel automatica |
| Compute queue rotta (−20% FPS) | Fix ACE (kernel+Mesa) |
| Nessun rollback | Rollback a cascata di ogni modifica |
| Processi manuali complessi | Un unico comando, tutto automatico |

---

## ⚡ Performance Attese (dati della community)

| Metrica | Stock | Ottimizzato | Guadagno |
|:---|:---|:---|:---|
| CPU Core | 6 | **8** | +33% |
| GPU CU | 24 | **38-40** | +58-67% |
| Cyberpunk 2077 (1440p) | 46 FPS | **58 FPS** | +25% (con ACE fix) |
| AI Inference (it/s) | 2.1 | **3.4** | +62% |
| Efficienza (FPS/W) | 0.175 | **0.255** | +46% |
| Temperatura GPU | 80°C | **67°C** | −13°C |

---

## 📋 Requisiti

### Hardware
- ASRock BC-250
- Alimentatore ≥ 350W
- Raffreddamento consigliato: 2x ventole 120mm (es. Arctic P12 Pro, push-pull)

### Software
- **Linux**: Fedora 43, Bazzite, CachyOS/Arch (consigliate; Debian parziale)
- **Kernel**: ≥ 6.11 (meglio 6.18+ o kernel CachyOS)
- **Mesa**: ≥ 25.1
- **Python**: ≥ 3.10

---

## 🚀 Installazione

### Metodo Manuale

```bash
# Clona il repository
git clone https://github.com/andre98king/buo.git
cd buo

# Installa le dipendenze
pip install -r requirements.txt

# Installa l'applicazione
sudo pip install -e .

# Crea le directory necessarie
sudo mkdir -p /var/lib/buo /var/log/buo /etc/buo
sudo cp config/buo.yaml /etc/buo/
```

### Verifica dell'Installazione

```bash
# Senza hardware reale (simulazione)
buo status --mock
```

### Primo avvio: BUO si occupa di tutto

Al primo `sudo buo unleash` su hardware reale, BUO **cerca ogni tool nel
sistema e, se manca, lo scarica/installa e lo configura da solo**:

| Tool | Come lo installa BUO |
|:---|:---|
| bc250_smu_oc, bc250-40cu-unlock, bc250-acpi-fix | clone shallow da GitHub → script in `/usr/local/bin` |
| cyan-skillfish-governor | **pacchetto distro**: COPR `filippor/bazzite` (Fedora/Bazzite) o AUR (Arch) → poi config di default sicura scritta automaticamente |
| umr | **pacchetto distro** (`rpm-ostree install umr` su ostree) — attivo al prossimo reboot |
| stress/stress-ng | wrapper automatico se manca `stress` |

Nessun installer di terze parti viene mai eseguito: solo repo note e
pacchetti ufficiali del package manager della distro. Puoi disattivare
questo comportamento in `/etc/buo/buo.yaml` (`deps.auto_install: false`,
`deps.auto_install_governor: false`) o pre-installare tutto con:

```bash
sudo buo install-deps       # scarica e installa i tool ora
buo install-deps --check    # verifica cosa c'è e cosa manca
```

---

## 🎮 Utilizzo

### Comando Principale

```bash
# Ottimizzazione completa (tutto automatico)
sudo buo unleash

# Simulazione senza modifiche
sudo buo unleash --dry-run

# Modalità interattiva (conferma per ogni fase)
sudo buo unleash --interactive
```

### Altri Comandi

```bash
buo status                 # Stato hardware e ottimizzazioni
buo status --mock          # Con hardware simulato
buo probe                  # 🔍 Solo analisi hardware (nessuna modifica)
sudo buo undervolt         # 🔽 Solo undervolt CPU/GPU
sudo buo overclock         # ⬆️ Solo overclock power-limited
sudo buo apply             # ⚙️ Applica la configurazione trovata
buo report                 # Report dell'ultima esecuzione (Markdown)
buo report --format json   # Report in JSON
buo report --dashboard     # Dashboard HTML con grafici before/after
buo report --include-raw   # Include i dati benchmark grezzi
sudo buo rollback          # Rollback a cascata completo
sudo buo rollback --phase gpu_40cu   # Rollback da una fase specifica
sudo buo recover           # Riprende dopo crash/reboot
buo resume                 # ♻️ Riprende dal checkpoint (alias di recover)
buo config                 # Mostra la configurazione corrente
sudo buo config --edit     # Modifica la configurazione (editor)
buo benchmark --mock       # Solo benchmark (simulati)
buo safety-test            # Verifica i safety gates (senza modifiche)
buo safety-monitor         # 🛡️ Solo monitoraggio live (Ctrl+C per uscire)
buo install-deps           # Scarica e installa i tool della community
buo install-deps --check   # Verifica i tool senza scaricare
buo data-collect           # 📥 Raccoglie campioni per il modello VRAM
buo data-collect --vram-sensor /dev/ttyUSB0   # Con termocoppia reale
buo data-upload            # 📤 Carica dati anonimizzati (federated, esplicito)
buo ml-train               # 🧠 Addestra il modello ML VRAM sui dati raccolti
buo tui                    # 🖥️ Cockpit interattivo (dashboard live)
buo tui --mock             # Cockpit con hardware simulato
```

> `buo tui` richiede la dipendenza opzionale **textual**:
> `pip install textual` (o `pip install -e '.[tui]'`). Senza di essa la
> CLI classica resta pienamente funzionante.

### Opzioni di `unleash`

| Opzione | Descrizione |
|:---|:---|
| `--dry-run` | Simula tutto senza modifiche — test sicuro |
| `--interactive` | Chiede conferma per ogni fase e modifica |
| `--skip-benchmark` | Salta i benchmark (più veloce) |
| `--skip-validation` | Salta lo stress test finale |
| `--quick` | Solo undervolt/overclock, senza fix kernel |
| `--mock` | Hardware simulato (sviluppo/test) |
| `--verbose` | Log dettagliato |

---

## 🛡️ Avvisi automatici (semi-automaticità)

BUO trasforma i passi manuali in **avvisi automatici** con conferma
esplicita solo dove serve. Nessuna modifica parte senza verifiche:

| Situazione rilevata | Comportamento di BUO |
|:---|:---|
| Unlock 8 core **senza** fix ACPI (SSDT-CST/PST) | ⛔ **BLOCCATO** (fail-closed): l'unlock CPU non parte; istruzioni per e-tho/bc250-acpi-fix. In `--interactive` puoi confermare il rischio |
| 8 core + 40 CU con PSU < 350W | ⚠️ Avviso budget di potenza (picco FurMark 250-320W); consigliati undervolt + cap GPU 1500 MHz |
| 40 CU attive via runtime UMR (ostree) | 💾 Avviso: restano **volatili** al reboot; in `--interactive` BUO chiede se persisterle (install-service + write-service-table) |
| Toolchain 40-CU mancante (`umr`, live-manager) | 📥 **Auto-install** dal package manager (rpm-ostree/dnf) + avviso se serve reboot |
| Governor GPU non attivo | 📥 **Auto-install** (COPR/AUR) + config di default sicura; altrimenti problema `governor_missing` nel pre-audit |

Tutti gli avvisi compaiono nel log di esecuzione e nel report finale.

---

## ⚙️ Configurazione

File: `/etc/buo/buo.yaml` (esempio in `config/buo.yaml`)

```yaml
hardware:
  psu_wattage: 350        # W — il tuo alimentatore
  cooling_type: "push-pull"

safety:
  cpu_temp_max: 90        # °C — soglie consigliate
  power_budget: 300       # W

deps:
  auto_install: true            # scarica/installa i tool mancanti da solo
  auto_install_governor: true   # installa il governor (COPR/AUR) da solo

phases:
  fix:
    tlb: true             # patch TLB fault
    ace: true             # fix compute queue
    iommu: true           # iommu=off
    acpi: true            # SSDT C-State
    gtt: true             # ttm.pages_limit
```

> ⚠️ Gli **hard limits** (VID ≤ 1325 mV, GPU ≤ 1100 mV, ecc.) sono
> codificati in `buo/constants.py` e **non** possono essere modificati
> dalla configurazione: la sicurezza non è negoziabile.

---

## 🛡️ Sicurezza

### Sequenza garantita: **ANALIZZA → TESTA → MODIFICA**

BUO non modifica nulla prima di aver analizzato e testato:

| Fase | Cosa fa | Modifica? |
|:---|:---|:---|
| **init** | Verifica di sanità: kernel ≥ 6.11, Mesa ≥ 25.1, temperature — altrimenti **BLOCCO** | ❌ No |
| **pre_audit** | Scopre l'hardware, rileva i problemi noti, **benchmark BEFORE** | ❌ No |
| **unlock** | Sblocchi con test di stabilità per ogni passo (core, CU health test per-WGP) | ⚠️ Reversibili (rollback) |
| **fix** | Fix di sistema, ognuno con verifica applicata/rollback | ⚠️ Reversibili (rollback) |
| **optimize** | Undervolt: **test di stabilità reale** su ogni tensione proposta | ⚠️ Test + valori provvisori |
| **apply** | Solo DOPO i test: rende persistente la configurazione trovata | ✅ Sì, la modifica finale |
| **validate** | Stress test + verifica di ogni fix + benchmark AFTER + report | ❌ Solo lettura |

**Principio fail-closed:** se BUO non può eseguire un test di stabilità
reale (es. `bc250-detect` non installato), si **RIFIUTA di procedere**
invece di applicare valori non verificati. In `--dry-run` nessun modulo
tocca l'hardware: tutto è simulato, nulla viene scritto.

BUO implementa inoltre **6 livelli di protezione**:

| Livello | Descrizione |
|:---|:---|
| **1. Hard Limits** | Codificati nel codice, immutabili: VID < 1325mV, GPU < 1100mV |
| **2. Safety Monitor** | Thread separato, sampling 0.5s, ABORT + rollback su violazione |
| **3. Checkpoint** | Stato salvato prima di ogni modifica — ripresa dopo reboot |
| **4. Rollback a Cascata** | 12 livelli, dall'ultima modifica alla più vecchia |
| **5. Dry-Run / Interactive** | Simulazione completa o conferma per ogni passo |
| **6. Backup Automatico** | Ogni file modificato viene salvato prima dell'intervento |

```bash
# Test di sicurezza (senza modifiche)
buo safety-test

# Modalità recovery dopo un problema
sudo buo recover
```

---

## 📁 Struttura del Progetto

```
buo/
├── __init__.py            # Versione ed export
├── __main__.py            # python -m buo
├── cli.py                 # CLI (click + rich)
├── orchestrator.py        # Macchina a stati (unleash)
├── config.py              # Configurazione YAML
├── constants.py           # Hard limits immutabili
├── exceptions.py          # Eccezioni personalizzate
│
├── audit/                 # FASE 0 — pre-audit (hardware, problemi)
├── unlock/                # FASE 1 — sblocchi (CPU, GPU, health, mask + wrapper)
├── fix/                   # FASE 1b — fix (TLB, ACE, IOMMU, ACPI, VRAM, GTT, fan)
├── optimize/              # FASE 2 — undervolt/overclock + governor
├── validate/              # FASE 3 — stress test, verifica fix
├── safety/                # Safety monitor + limiti
├── state/                 # Checkpoint, rollback, recovery, reboot
├── benchmark/             # Benchmark standard (furmark, stress-ng, vkmark)
├── report/                # Report Markdown/JSON
├── models/                # Stima VRAM (empirica + ML opzionale)
└── utils/                 # Logging, shell, mock, distro, paths
```

---

## 📚 Documentazione

- **[Setup su hardware reale](docs/HARDWARE_SETUP.md)** — guida passo-passo per Bazzite + BIOS mod
- **[PROJECT_STATUS.md](PROJECT_STATUS.md)** — stato e roadmap del progetto
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — guida per contribuire
- **`reference/`** — la conversazione completa di progettazione (114 messaggi, con catene di pensiero)

---

## 🙏 Ringraziamenti

Grazie alla comunità BC-250 per il lavoro incredibile:

- **duggasco** — 40-CU unlock e health test
- **filippor** — cyan-skillfish-governor-smu
- **DryhoppedIPA** — bc250-gfx1013-fix (compute queue)
- **bc250-collective** — tool di overclock e ACPI fix
- **Forbidden-Darkness** — UEFI firmware menu
- **RescueMei** — DXE core unlock
- **MastaG** — kernel CachyOS
- **elektricM** — documentazione community
- **Tutta la community** — test, dati, feedback

---

## 📄 Licenza

Doppia licenza:

- **Codice** (`buo/`): [GNU General Public License v3.0](LICENSE)
- **Documentazione e asset**: [Creative Commons Attribution-ShareAlike 4.0](LICENSE-docs)

---

<div align="center">

**⭐ Se ti piace il progetto, metti una stella su GitHub!**

</div>
