# Changelog

## Unreleased (2026-08-28, sessione serale)

### Corretto
- 🔴 **Fix display 144Hz (Steam Gaming Mode)**: FPS bloccati a 60-70 perché
  gamescope emetteva a 60Hz (l'EDID del monitor dichiara preferito
  1920x1080@60Hz, col 144Hz solo come DTD 2). Fix supportato: UI Steam →
  Display → "Automatically Set Resolution" OFF → 1920x1080@144; la scelta
  viene salvata in `~/.config/gamescope/modes.cfg`
  (`"<Make> <Model>:<W>x<H>@<refresh> <broadcast>"`, letto da gamescope al
  boot per gli schermi esterni — verificato in `get_saved_mode()`
  DRMBackend.cpp). Risultato: Marvel Rivals da 60-70 a **picco 120 FPS**.
  Documentato in BUGS.md #23 e COMMUNITY_NOTES.md §2d.
- 🟠 **`bc250-cu-live-manager` riabilitato**: l'unità systemd era sparita
  dopo un cambio deployment (binario+config intatti → 24 CU attive invece
  di 40). Reinstallato con `install-service` + `apply-service` → 40/40 CU,
  `enabled`+`active`. Quirk documentato: eseguire `install-service` da una
  COPIA dello script (da `/usr/local/bin`, symlink, `install` fallisce con
  "stesso file" e lo script esce senza scrivere l'unità). BUGS.md #24.

### Validato sul campo (28/08 sera)
- **Gaming**: Marvel Rivals picco 120 FPS con 144Hz; monitoraggio partita
  (logger 5s via servizio systemd utente): GPU avg 65°C / max 86°C (breve),
  CPU avg 70°C / max 85°C, potenza GPU max 147W, load max 11.6.
- **UV GPU confermato attivo**: curva SMU del governor
  (800/900/1000mV @ 1/1.5/2GHz) + throttle termico 85°C (recupero 75°C).
- **CPU senza UV** (VID stock, schedutil): l'undervolt CPU resta da fare;
  i limiti di sicurezza sono già in `buo.yaml` (vid max 1300).

## Unreleased (2026-08-28)

### Corretto
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

## v1.0.0 (2026-08-27)

### Aggiunto
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
  HARDWARE_SETUP, PROJECT_STATUS, CONTRIBUTING)

### Sicurezza
- Corretto: l'undervolt in modalità reale non testava la stabilità (ora fail-closed)
- Corretto: il dry-run poteva scrivere registri SMU via fallback diretto (ora simulato)
- Corretto: aggiunta verifica di sanità pre-operativa mancante
- Corretto: il rollback chiamava un handler inesistente (cpu_overclock)
