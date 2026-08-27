# Changelog

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
