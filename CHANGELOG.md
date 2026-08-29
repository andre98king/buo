# Changelog

## v1.0.0 (2026-08-30)

### Corretto
- **`buo restore`: restore-mode perso al resume (F-A)** — dopo il reboot il
  nuovo processo non sapeva più di essere in modalità restore; ora il resume
  riprende dal checkpoint in restore-mode.
- **`buo restore`: rollback che toccava i fix pre-esistenti (F-B)** — il
  rollback agiva anche su fix presenti prima del run; ora agisce SOLO sui fix
  applicati dal run corrente (ledger `applied_steps`).
- **Unlock CPU: gate ACPI non ritentato (F-C)** — se il gate ACPI falliva al
  primo tentativo l'unlock non veniva più provato; ora il gate viene ritentato.
- **Undervolt persistente: `--install` senza enable systemd (F-D)** — il
  servizio veniva creato ma non abilitato; BUO ora esegue
  `systemctl enable bc250-smu-oc` esplicito dopo l'install (enable fallito →
  `persist_error`, non bloccante).
- **`stress-ng --timeout 0` = stress INFINITO** — lo stress "saltato" (durata
  0) spawnava comunque stress-ng a palla per fino a 60 s, causando 3 falsi
  abort termici a ~90°C sul campo; ora durata 0 = skip vero, nessuno spawn.
- **"Stress saltato" non persistente al resume** — con `buo restore` senza
  `--validate` la validate rifaceva lo stress completo al resume (nuovo
  processo, config ricaricata); il marcatore `validation_stress_skip` è ora
  persistito nel checkpoint e ripulito a fine ciclo.
- **`buo status` leggeva hardware mock in produzione (C1)** — il dry-run
  forzava il mock anche per letture read-only; ora legge l'hardware reale via
  `RealHardwareReader`, fail-soft (campo non leggibile → "non rilevabile"),
  con la state dir corretta.
- **Config: chiavi sconosciute/annidate ignorate senza avvisi (schema piatto)**
  — una chiave come `safety.cpu.freq_max` annidata veniva ignorata in
  silenzio; ora `_warn_unknown()` avvisa (fail-soft, mai bloccante) e `safety`
  viene serializzato piatto (i valori non vanno più persi nel round-trip).
- **Clamp scale undervolt CPU corretto a [−50, 0]** — la community rifiuta
  scale > 0 (sarebbe overvolt); bounds verificati nel sorgente
  (`bc250_limits.py scale_min=-50 scale_max=0`).
- 🔴 **Fix display 144Hz (Steam Gaming Mode)**: FPS bloccati a 60-70 perché
  gamescope emetteva a 60Hz (l'EDID del monitor dichiara preferito
  1920x1080@60Hz, col 144Hz solo come DTD 2). Fix supportato: UI Steam →
  Display → "Automatically Set Resolution" OFF → 1920x1080@144; la scelta
  viene salvata in `~/.config/gamescope/modes.cfg`
  (`"<Make> <Model>:<W>x<H>@<refresh> <broadcast>"`, letto da gamescope al
  boot per gli schermi esterni — verificato in `get_saved_mode()`
  DRMBackend.cpp). Risultato: Marvel Rivals da 60-70 a **picco 120 FPS**.
  Documentato (bug #23).
- 🟠 **`bc250-cu-live-manager` riabilitato**: l'unità systemd era sparita
  dopo un cambio deployment (binario+config intatti → 24 CU attive invece
  di 40). Reinstallato con `install-service` + `apply-service` → 40/40 CU,
  `enabled`+`active`. Quirk documentato: eseguire `install-service` da una
  COPIA dello script (da `/usr/local/bin`, symlink, `install` fallisce con
  "stesso file" e lo script esce senza scrivere l'unità). Bug #24.
- 🔴 **40-CU su ostree**: `GPU40CUUnlock` usa ora il **runtime UMR**
  (`bc250-cu-live-manager`) invece del kernel patch (che su Bazzite/ostree
  fallisce per `/usr` read-only). Nuovo metodo `persist()` (opt-in).
- 🔴 `install-deps`: installa la variante **Fedora** di `bc250-enable-40cu.sh`
  (la generica è Debian-oriented e falliva) + `bc250-cu-live-manager` +
  `bc250-compute-verify.sh`.
- `FixVerifier`: aggiunti i checker per `gtt_tuning` (ttm.pages_limit),
  `fan_control` (nct6683), `vram_config` (manuale) — prima "nessuna
  verifica definita".
- Overclock CPU: ora **applica** il punto undervolt validato via
  `bc250-apply --apply` (volatile, non-blocking). Prima era solo calcolato.
- Rimossi import inutili (orchestrator, deps, verify, gpu).

### Validato sul campo
- **`buo restore` end-to-end (29/08, macchina reale)**: format → restore con
  reboot automatico e auto-resume; verificati fix ACPI ostree, build memcfg,
  unlock CPU 8 core, undervolt persistente e governor; **16 thread** e
  C-states confermati; restore completo EXIT 0 con profilo fresco
  (3800@1224), governor+40 CU attivi, temp ~57°C.
- **Sweep undervolt GPU per-silicio (30/08, macchina reale)**: vincitore
  **800 mV @ 1500 MHz** (floor SMU ~800 mV misurato su Cyan Skillfish),
  stabile sotto carico reale (vkmark radv 1080p e FurMark 1080p, 90 s;
  schermo pulito, nessun artefatto). **Cap termico a 1500 MHz**: a 2000 MHz
  sostenuti sotto FurMark la GPU tocca 110°C (limite AMD) anche a 900 mV, a
  1500 MHz resta a ~80°C con FPS identici (il throttle portava già a ~1500);
  cap cablato via `safety.gpu_freq_max`. Beneficio undervolt quantificato:
  **−14°C per 100 mV** (95°C a 900 mV vs 81°C a 800 mV sotto FurMark).
- **VERO undervolt CPU (30/08)**: la scale 0 è la curva stock (nessuno shift
  reale) — con `undervolt.cpu_target_vid` 1000: **3500 MHz @ 999 mV
  (scale −14)**, 73°C vs 87°C sotto stress (stress-ng --verify 5 min),
  zero WHEA, co-load ok, persistito.
- **Gaming (28/08 sera)**: Marvel Rivals picco 120 FPS con 144Hz; monitoraggio partita
  (logger 5s via servizio systemd utente): GPU avg 65°C / max 86°C (breve),
  CPU avg 70°C / max 85°C, potenza GPU max 147W, load max 11.6.
- **UV GPU confermato attivo (28/08)**: curva SMU del governor
  (800/900/1000mV @ 1/1.5/2GHz) + throttle termico 85°C (recupero 75°C).

### Aggiunto
- **Fallback offline per `install-deps`**: bundle delle dipendenze verificato
  con **9 check fail-closed** (tarball, manifest, tree-hash, git, conflitti:
  "verifica TUTTO → poi move") e riuso del checkout deps con verifica A7
  anche senza rete.
- **Ricerca per-silicio dell'undervolt GPU** (sweep governor-based): probe con
  controllo durata reale (FurMark CLI / vkmark; glmark2 escluso), ripristino
  byte-identico della config di gioco a fine probe, fail-closed → tabella
  community (mai punti non testati); **rilevamento del floor SMU sotto carico**
  (VDDGFX applicata vs target, `smu_floor_mv` nei metadata, punti mai sotto il
  floor: una config sotto il floor manda il governor in hang).
- **Stress test separabile CPU/GPU**: `validation.stress_scope`
  (`both`|`cpu`|`gpu`, default `both`).
- **Nuove chiavi config**: `undervolt.cpu_target_vid` (default 1300
  conservativo, per spingere la ricerca in scala negativa) e
  `undervolt.gpu_sweep_floor_mv` (default 700, clampato mai sotto).
- **Igiene pre-rilascio**: placeholder di recupero in `RECOVERY.md`,
  `SECURITY.md` aggiornata (supporto v1.0.0),
  `create_release.sh` cwd-indipendente che esclude i file interni
  (research/, reference/, PROJECT_STATUS.md, docs/BUGS.md, ...).
- Orchestratore completo (`unleash`): pre-audit → sblocchi → fix → ottimizzazione → validazione → report
- CLI completa: `status`, `probe`, `undervolt`, `overclock`, `apply`, `rollback`,
  `recover`, `resume`, `report` (+ `--dashboard`/`--include-raw`), `config` (+ `--edit`),
  `benchmark`, `safety-test`, `safety-monitor`, `install-deps`, `data-collect`,
  `data-upload`, `ml-train`, `tui`
- Hard limits immutabili (VID ≤ 1325 mV, GPU ≤ 1100 mV) non sovrascrivibili da config
- Safety monitor in thread separato (sampling 0.5 s) con abort + rollback
- Checkpoint, rollback a cascata (12 livelli) e recovery dopo reboot
- Auto-download dei tool della community (bc250_smu_oc, 40cu-unlock, acpi-fix)
- Fail-closed: nessuna modifica senza test di stabilità reale
- Dry-run che non tocca mai l'hardware (zero accesso PCI/SMU)
- Verifica di sanità pre-operativa (kernel ≥ 6.11, Mesa ≥ 25.1, temperature)
- Benchmark standard (furmark/glmark2, stress-ng, sysbench, vkmark) before/after
- Stima VRAM empirica (α=0.45, β=0.04) + modello ML addestrabile
- Rilevamento dei 10 problemi noti della BC-250
- Supporto multi-distro (Fedora, Bazzite/ostree, Arch, Debian)
- TUI cockpit live (textual, opzionale)
- Dashboard HTML autonoma del report (nessuna dipendenza)
- CI GitHub Actions (test su Python 3.10/3.12)
- Documentazione completa (README, USER_GUIDE, INSTALL, ARCHITECTURE, FAQ,
  HARDWARE_SETUP, CONTRIBUTING)

### Sicurezza
- **Supply chain (A7)**: checkout deps verificati (rev-parse == commit
  pinnato + porcelain pulito) anche al riuso; impronta **SHA-256** del tool
  installato registrata in `deps-hashes.json` (tamper-evidence).
- **ACPI**: validazione dell'header AML (signature SSDT/DSDT) prima
  dell'installazione; **rollback ACPI completo** (backup della boot entry
  ripristinato, senza residui).
- **Anti-concorrenza**: lock **flock** non bloccante sulla state dir —
  istanze simultanee di buo respinte prima di toccare stato/ledger.
- **memcfg nel catalogo deps con tipo `build`**: la verifica controlla il
  binario installato, non solo il checkout (un build fallito non risulta
  "presente").
- **Persistenza fan**: modulo sensori/ventole nct6683 caricato al boot via
  `/etc/modules-load.d` + `/etc/modprobe.d` (i sensori non spariscono al
  reboot).
- Corretto: l'undervolt in modalità reale non testava la stabilità (ora fail-closed)
- Corretto: il dry-run poteva scrivere registri SMU via fallback diretto (ora simulato)
- Corretto: aggiunta verifica di sanità pre-operativa mancante
- Corretto: il rollback chiamava un handler inesistente (cpu_overclock)
