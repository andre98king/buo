# 🏗️ Architettura — BUO

Panoramica tecnica del progetto per chi vuole contribuire o capire
come funziona internamente.

## Flusso di esecuzione (`unleash`)

```
init → pre_audit → unlock → fix → optimize → apply → validate → complete
 │         │         │        │       │          │        │
 │  verifica sanità +    │        │       │          │        │
 │  auto-download deps   │        │       │          │        │
 ▼                       ▼        ▼       ▼          ▼        ▼
preflight           sblocchi    fix    undervolt    persist   stress + report
(kernel/mesa/temp)  CPU+GPU     kernel + OC       config
```

Ogni fase salva un checkpoint in `<state_dir>/state.json`; su errore o
violazione di safety parte il **rollback a cascata** (12 livelli, in
ordine inverso rispetto all'applicazione).

## Moduli

| Modulo | Responsabilità |
|:---|:---|
| `orchestrator.py` | Macchina a stati, coordinamento fasi |
| `cli.py` | CLI (click + rich): unleash, status, probe, undervolt, overclock, apply, rollback, recover, resume, report, config, benchmark, safety-test, safety-monitor, install-deps, data-collect, data-upload, ml-train, tui, doctor |
| `constants.py` | Hard limits immutabili, registri SMN/SMU, percorsi |
| `config.py` | Configurazione YAML (limiti non sovrascrivibili) |
| `audit/` | Discovery hardware + rilevamento problemi noti |
| `unlock/` | Sblocchi CPU (SMN/SMU), GPU 40-CU, health test, maschera + wrapper |
| `fix/` | TLB, ACE, IOMMU, ACPI, VRAM, GTT, ventole (ognuno: apply/verify/rollback) |
| `optimize/` | Undervolt CPU (fail-closed via bc250-detect), GPU (community-verified), overclock power-limited, governor |
| `validate/` | Stress test, verifica fix |
| `safety/` | Safety monitor (thread 0.5s) + limiti |
| `state/` | Checkpoint, rollback a cascata, recovery, reboot |
| `benchmark/` | Benchmark standard (furmark/glmark2, stress-ng, sysbench, vkmark) |
| `report/` | Report Markdown/JSON + dashboard HTML |
| `models/` | Stima VRAM (empirica + ML opzionale) |
| `data/` | Raccolta campioni VRAM e training ML |
| `install/` | Download automatico dei tool della community |
| `utils/` | Logging, shell, mock hardware, distro, paths |

## Principi di sicurezza

1. **Hard limits immutabili** — mai oltre VID 1325 mV / GPU 1100 mV
2. **Fail-closed** — senza test di stabilità reale, nessuna modifica
3. **Dry-run = zero hardware** — tutti i moduli che scrivono sono simulati
4. **Checkpoint prima di ogni modifica** — ripresa dopo reboot
5. **Rollback a cascata** — ogni modifica è reversibile

## Dati e percorsi

| Cosa | Percorso |
|:---|:---|
| Stato/checkpoint | `/var/lib/buo/state.json` (o `~/.local/state/buo`) |
| Report | `/var/lib/buo/report.md` + `report.json` |
| Log | `/var/log/buo/buo.log` |
| Dataset VRAM | `<state_dir>/dataset/vram_dataset.jsonl` |
| Modello ML | `<state_dir>/vram_model.joblib` |
| Checkout community | `/opt/buo-deps` (o `~/.local/share/buo-deps`) |
