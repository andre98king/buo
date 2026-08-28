#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
CLI di BUO — interfaccia a riga di comando con `click` + `rich`.

Comandi (dal design finale):
    • unleash        — comando principale (tutto automatico)
    • status         — stato hardware e ottimizzazioni
    • report         — genera/rilegge il report
    • rollback       — rollback a cascata
    • recover        — riprende dopo crash/reboot
    • config         — mostra la configurazione
    • benchmark      — esegue solo i benchmark
    • safety-test    — verifica i safety gates senza modifiche

Opzioni comuni: --mock, --dry-run, --interactive, --verbose.
"""

import os
import sys
from typing import Optional

import click

from . import __version__
from .config import BUOConfig
from .constants import LIMITS

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.progress import Progress, SpinnerColumn, TextColumn
    _HAS_RICH = True
except Exception:  # pragma: no cover
    _HAS_RICH = False

    class _Console:
        def print(self, *args, **kwargs):
            text = args[0] if args else ""
            print(text)

        def clear(self):
            pass
    Console = _Console
    Panel = Table = Text = None


console = Console()


# ====================================================================== #
# HELPERS
# ====================================================================== #

def _make_orchestrator(mock: bool, dry_run: bool,
                       interactive: bool, verbose: bool,
                       config: Optional[BUOConfig] = None):
    from .orchestrator import Orchestrator
    if config is None:
        config = BUOConfig.load()
    return Orchestrator(
        config=config,
        mock=mock,
        dry_run=dry_run,
        interactive=interactive,
        log_level="DEBUG" if verbose else "INFO",
    )


def show_header() -> None:
    if not _HAS_RICH:
        console.print("🚀 BC-250 ULTIMATE ORCHESTRATOR")
        return
    try:
        from pyfiglet import Figlet
        ascii_art = Figlet(font="slant").renderText("BUO")
    except Exception:
        ascii_art = "BUO\n"
    text = Text(ascii_art, style="bold cyan")
    text.append(f" BC-250 ULTIMATE ORCHESTRATOR v{__version__}\n",
                style="bold white")
    text.append(" Ottimizzazione automatica per ASRock BC-250", style="dim")
    console.print(Panel(text, border_style="blue", width=80))


def _print_limits_table() -> None:
    table = Table(title="🔒 HARD LIMITS (immutabili)", border_style="red",
                  header_style="bold red")
    table.add_column("Componente", style="white")
    table.add_column("Limite", style="bold red")
    table.add_row("CPU VID max", f"{LIMITS.cpu.vid_absolute_max} mV (brick sopra)")
    table.add_row("CPU Temp max", f"{LIMITS.cpu.temp_max} °C")
    table.add_row("GPU Voltage max", f"{LIMITS.gpu.voltage_absolute_max} mV")
    table.add_row("GPU Temp max", f"{LIMITS.gpu.temp_max} °C")
    table.add_row("Power budget", f"{LIMITS.power.power_budget} W")
    console.print(table)


# ====================================================================== #
# CLI
# ====================================================================== #

@click.group()
@click.version_option(version=__version__)
def cli() -> None:
    """BC-250 Ultimate Orchestrator — ottimizzazione automatica."""


# ------------------------------ unleash ------------------------------ #

@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato (no BC-250)")
@click.option("--dry-run", is_flag=True,
              help="Simula tutto senza modificare nulla")
@click.option("--interactive", is_flag=True,
              help="Chiede conferma per ogni fase")
@click.option("--verbose", "-v", is_flag=True, help="Log dettagliato")
@click.option("--skip-benchmark", is_flag=True, help="Salta i benchmark")
@click.option("--skip-validation", is_flag=True, help="Salta lo stress test")
@click.option("--quick", is_flag=True,
              help="Solo undervolt/overclock (senza fix kernel)")
def unleash(mock: bool, dry_run: bool, interactive: bool, verbose: bool,
            skip_benchmark: bool, skip_validation: bool, quick: bool) -> None:
    """
    🚀 Comando principale: analizza → sblocca → ottimizza → valida → report.

    Un solo comando per il massimo delle prestazioni sicure della BC-250.
    """
    show_header()

    if dry_run:
        console.print("\n[bold yellow]⚠️  MODALITÀ DRY-RUN — "
                      "nessuna modifica verrà applicata[/]\n")
    if mock:
        console.print("[dim]🔧 Modalità MOCK (nessun hardware reale)[/]\n")
    if quick:
        console.print("[dim]⚡ Modalità QUICK: fix kernel saltati[/]\n")

    config = BUOConfig.load()
    if skip_benchmark:
        config.benchmark_enabled = False
    if skip_validation:
        config.validation_stress_duration = 0
    if quick:
        config.fix_tlb = config.fix_ace = config.fix_iommu = False
        config.fix_acpi = config.fix_vram = config.fix_gtt = False
        config.fix_fan = False

    orchestrator = _make_orchestrator(mock, dry_run, interactive, verbose,
                                      config=config)
    exit_code = orchestrator.run()

    if exit_code == 0:
        from .utils.paths import report_file_md
        console.print("\n[bold green]✅ OTTIMIZZAZIONE COMPLETATA![/]")
        console.print(f"[dim]📄 Report: {report_file_md()}[/]")
        console.print("[dim]🔄 Rollback: sudo buo rollback[/]")
    else:
        console.print(f"\n[bold red]❌ Ottimizzazione fallita "
                      f"(codice {exit_code})[/]")
        console.print("[dim]📄 Log: /var/log/buo/buo.log[/]")
    sys.exit(exit_code)


# ----------------------- comandi fase standalone ---------------------- #

def _run_phase_command(name: str, phase: str, mock: bool, dry_run: bool,
                       interactive: bool, verbose: bool, sudo_hint: bool,
                       config: Optional[BUOConfig] = None) -> None:
    """Helper per i comandi che eseguono una singola fase."""
    from .orchestrator import Orchestrator
    from .config import BUOConfig

    show_header()
    if sudo_hint:
        console.print("[dim]⚠️  Esegui con sudo: modifica l'hardware "
                      f"({name})[/]\n")
    if config is None:
        config = BUOConfig.load()
    orchestrator = _make_orchestrator(mock, dry_run, interactive, verbose,
                                      config=config)
    exit_code = orchestrator.run(start_phase=phase, stop_after=phase)
    sys.exit(exit_code)


@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
@click.option("--verbose", "-v", is_flag=True, help="Log dettagliato")
def probe(mock: bool, verbose: bool) -> None:
    """🔍 Solo discovery hardware e rilevamento problemi (nessuna modifica)."""
    from .config import BUOConfig
    # probe = sola analisi: niente benchmark (quelli sono di unleash)
    config = BUOConfig.load()
    config.benchmark_enabled = False
    _run_phase_command("probe", "pre_audit", mock, dry_run=True,
                       interactive=False, verbose=verbose, sudo_hint=False,
                       config=config)


@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
@click.option("--dry-run", is_flag=True, help="Simula senza modifiche")
@click.option("--verbose", "-v", is_flag=True, help="Log dettagliato")
def undervolt(mock: bool, dry_run: bool, verbose: bool) -> None:
    """🔽 Solo undervolt CPU/GPU (usa i dati di probe esistenti)."""
    _run_phase_command("undervolt", "optimize", mock, dry_run,
                       interactive=False, verbose=verbose, sudo_hint=True)


@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
@click.option("--dry-run", is_flag=True, help="Simula senza modifiche")
@click.option("--verbose", "-v", is_flag=True, help="Log dettagliato")
def overclock(mock: bool, dry_run: bool, verbose: bool) -> None:
    """⬆️ Solo overclock power-limited (usa i dati di undervolt)."""
    _run_phase_command("overclock", "optimize", mock, dry_run,
                       interactive=False, verbose=verbose, sudo_hint=True)


@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
@click.option("--dry-run", is_flag=True, help="Simula senza modifiche")
@click.option("--verbose", "-v", is_flag=True, help="Log dettagliato")
def apply(mock: bool, dry_run: bool, verbose: bool) -> None:
    """⚙️ Applica la configurazione trovata (governor + overclock)."""
    _run_phase_command("apply", "apply", mock, dry_run,
                       interactive=False, verbose=verbose, sudo_hint=True)


@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def resume(mock: bool) -> None:
    """♻️ Riprende dal checkpoint (alias di recover)."""
    from .orchestrator import Orchestrator
    show_header()
    orchestrator = _make_orchestrator(mock=mock, dry_run=False,
                                      interactive=False, verbose=False)
    exit_code = orchestrator.run()
    sys.exit(exit_code)


@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def safety_monitor(mock: bool) -> None:
    """🛡️ Avvia SOLO il safety monitor (letture ogni 0.5s, Ctrl+C per uscire)."""
    import time
    from .safety.monitor import SafetyMonitor
    from .utils.mock import MockHardware

    show_header()
    hw = MockHardware() if mock else None
    monitor = SafetyMonitor(hardware=hw, abort_callback=lambda r: None,
                            vram_estimation=False)
    monitor.start()
    console.print("[dim]🛡️ Monitor attivo — Ctrl+C per fermare[/]\n")
    try:
        while True:
            readings = monitor.get_last_readings()
            if readings is not None:
                console.print(
                    f"\rCPU {readings.cpu_temp:.1f}°C | "
                    f"GPU {readings.gpu_temp:.1f}°C | "
                    f"VID {readings.cpu_vid}mV | "
                    f"VGPU {readings.gpu_voltage}mV | "
                    f"P {readings.total_power:.1f}W" + " " * 10,
                    end="")
            time.sleep(0.5)
    except KeyboardInterrupt:
        console.print("\n[yellow]⏹️  Monitor fermato[/]")
    finally:
        monitor.stop()


# ------------------------------ status ------------------------------- #

@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def status(mock: bool) -> None:
    """Mostra lo stato corrente dell'hardware e delle ottimizzazioni."""
    show_header()

    orchestrator = _make_orchestrator(mock=mock, dry_run=True,
                                      interactive=False, verbose=False)
    info = orchestrator.status()

    console.print(f"[dim]Fase corrente: {info['current_phase']} | "
                  f"Reboot: {info['reboot_count']}[/]\n")

    hardware = info.get("hardware")
    if hardware is None:
        console.print("[yellow]⚠️ Nessun hardware rilevato "
                      "(usa --mock per simulare)[/]")
        return

    table = Table(title="📊 STATO HARDWARE", border_style="blue",
                  header_style="bold cyan", show_lines=True)
    table.add_column("Componente", style="white")
    table.add_column("Valore", style="green")
    table.add_column("Stato", style="bold")

    table.add_row("CPU Core",
                  f"{hardware.get('cpu_cores', '?')}/8",
                  "✅ OK" if hardware.get("cpu_cores", 0) >= 8 else "⚠️ Parziale")
    table.add_row("CPU Freq", f"{hardware.get('cpu_freq', '?')} MHz", "✅ OK")
    table.add_row("CPU Temp",
                  f"{hardware.get('cpu_temp', '?')}°C",
                  "✅ OK" if hardware.get("cpu_temp", 0) < LIMITS.cpu.temp_max
                  else "🔴 CRITICA")
    table.add_row("GPU CU",
                  f"{hardware.get('gpu_cu', '?')}/40",
                  "✅ OK" if hardware.get("gpu_cu", 0) >= 24 else "⚠️ Ridotte")
    table.add_row("GPU Temp",
                  f"{hardware.get('gpu_temp', '?')}°C",
                  "✅ OK" if hardware.get("gpu_temp", 0) < LIMITS.gpu.temp_max
                  else "🔴 CRITICA")
    table.add_row("Potenza", f"{hardware.get('total_power', '?')} W", "✅ OK")
    table.add_row("40-CU",
                  "✅ Attive" if hardware.get("is_40cu_enabled") else "💤 Stock",
                  "✅" if hardware.get("is_40cu_enabled") else "—")
    table.add_row("Fix", ", ".join(info["applied_fixes"]) or "nessuno", "—")

    console.print(table)


# ------------------------------ report ------------------------------- #

@cli.command()
@click.option("--format", "fmt", type=click.Choice(["markdown", "json"]),
              default="markdown", help="Formato del report")
@click.option("--dashboard", is_flag=True,
              help="Genera una dashboard HTML autonoma (grafici)")
@click.option("--include-raw", is_flag=True,
              help="Includi i dati benchmark grezzi nel report Markdown")
def report(fmt: str, dashboard: bool, include_raw: bool) -> None:
    """Genera (o rilegge) il report dell'ultima esecuzione."""
    from .utils.paths import report_file_json, report_file_md

    if dashboard:
        from .report.dashboard import generate_html_dashboard
        path = generate_html_dashboard(report_file_json())
        console.print(f"[green]✅ Dashboard generata: {path}[/]")
        console.print("[dim]Apri nel browser per vedere i grafici "
                      "before/after[/]")
        return

    path = report_file_json() if fmt == "json" else report_file_md()
    if not path.exists():
        console.print("[yellow]⚠️ Nessun report trovato. "
                      "Esegui prima: sudo buo unleash[/]")
        sys.exit(1)
    console.print(path.read_text(encoding="utf-8"))

    if include_raw and fmt == "markdown":
        import json
        raw = report_file_json()
        if raw.exists():
            data = json.loads(raw.read_text(encoding="utf-8"))
            console.print("\n[bold]📊 Dati benchmark grezzi (JSON):[/]")
            console.print(json.dumps(data.get("benchmarks", {}), indent=2,
                                     ensure_ascii=False))


# ----------------------------- rollback ------------------------------ #

@cli.command()
@click.option("--phase", default=None,
              help="Rollback da una fase specifica (es. gpu_40cu)")
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def rollback(phase: Optional[str], mock: bool) -> None:
    """Ripristina TUTTO allo stato originale (rollback a cascata)."""
    show_header()
    orchestrator = _make_orchestrator(mock=mock, dry_run=False,
                                      interactive=False, verbose=False)
    ok = orchestrator.rollback.rollback(from_phase=phase,
                                        reason="comando utente")
    if ok:
        console.print("[bold green]✅ Rollback completato[/]")
    else:
        console.print("[bold red]⚠️ Alcuni livelli di rollback sono "
                      "falliti — controlla /var/log/buo/buo.log[/]")
        sys.exit(1)


# ------------------------------ recover ------------------------------ #

@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def recover(mock: bool) -> None:
    """Riprende l'esecuzione dopo un crash o reboot inaspettato."""
    show_header()
    orchestrator = _make_orchestrator(mock=mock, dry_run=False,
                                      interactive=False, verbose=False)
    plan = orchestrator.recovery_plan()
    console.print(f"[dim]Fase interrotta: {plan['interrupted_phase']}[/]")
    console.print(f"[dim]Reboot eseguiti: {plan['reboot_count']}[/]\n")

    if plan["action"] == "resume":
        console.print("[bold cyan]🔄 Ripresa dalla fase "
                      f"{plan['interrupted_phase']}[/]")
        exit_code = orchestrator.run(start_phase=plan["interrupted_phase"])
        sys.exit(exit_code)
    else:
        console.print("[bold red]⚠️ Fase non verificata — "
                      "consigliato: sudo buo rollback[/]")
        sys.exit(1)


# ------------------------------ config ------------------------------- #

@cli.command()
@click.option("--edit", is_flag=True, help="Apre la configurazione "
              "nell'editor (crea il file di default se manca)")
def config(edit: bool) -> None:
    """Mostra (o modifica) la configurazione corrente."""
    from .config import BUOConfig
    from .utils.paths import config_dir

    if edit:
        path = config_dir() / "buo.yaml"
        if not path.exists():
            BUOConfig().save(path)
            console.print(f"[dim]Configurazione di default creata: {path}[/]")
        editor = os.environ.get("EDITOR", "nano")
        console.print(f"[dim]Apro {editor} su {path}...[/]")
        import subprocess
        rc = subprocess.call([editor, str(path)])
        if rc == 0:
            console.print("[green]✅ Configurazione salvata "
                          "(gli hard limits restano immutabili)[/]")
        sys.exit(rc if rc else 0)

    show_header()
    cfg = BUOConfig.load()
    d = cfg.to_dict()

    table = Table(title="⚙️ CONFIGURAZIONE", border_style="blue",
                  header_style="bold cyan")
    table.add_column("Parametro", style="white")
    table.add_column("Valore", style="green")
    table.add_row("Modalità", d["mode"])
    table.add_row("PSU", f"{d['psu_wattage']} W")
    table.add_row("Raffreddamento", d["cooling_type"])
    table.add_row("Power budget", f"{d['safety']['power_budget']} W")
    table.add_row("Auto-install deps", "✅" if d["deps"]["auto_install"] else "❌")
    table.add_row("VRAM estimation", "✅" if d["vram_estimation"]["enabled"] else "❌")
    table.add_row("Stress test", f"{d['phases']['validation']['stress_duration']} min")
    console.print(table)
    console.print("\n[dim]📄 File: /etc/buo/buo.yaml | Per modificarla: "
                  "sudo buo config --edit[/]")
    _print_limits_table()


# --------------------------- data-collect ---------------------------- #

@cli.command("data-collect")
@click.option("--samples", default=10, type=int, help="Numero di campioni")
@click.option("--interval", default=1.0, type=float, help="Intervallo (s)")
@click.option("--vram-sensor", default=None,
              help="Sensore VRAM reale (es. /dev/ttyUSB0, termocoppia)")
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def data_collect(samples: int, interval: float, vram_sensor: str,
                 mock: bool) -> None:
    """📥 Raccoglie campioni (sensori + VRAM reale opzionale) per l'ML."""
    from .data.collector import VRAMDataCollector
    from .utils.mock import MockHardware
    from .utils.paths import state_dir

    show_header()
    collector = VRAMDataCollector(
        mock=mock,
        mock_hardware=MockHardware() if mock else None,
        vram_sensor=vram_sensor,
    )
    written = collector.collect(samples=samples, interval=interval)
    path = state_dir() / "dataset" / "vram_dataset.jsonl"
    console.print(f"\n[bold green]✅ {written} campioni salvati in "
                  f"{path}[/]")
    console.print("[dim]Prossimo passo: buo ml-train "
                  "(servono campioni con vram_temp_real)[/]")


# ---------------------------- data-upload ---------------------------- #

@cli.command("data-upload")
def data_upload() -> None:
    """📤 Carica i dati anonimizzati (federated learning, esplicito)."""
    from .data.collector import VRAMDataCollector
    from .utils.paths import state_dir

    show_header()
    path = state_dir() / "dataset" / "vram_dataset.jsonl"
    rows = VRAMDataCollector.load_dataset(path)

    if not rows:
        console.print("[yellow]⚠️ Nessun dato da caricare. "
                      "Prima: buo data-collect[/]")
        sys.exit(1)

    server = os.environ.get("BUO_FEDERATED_SERVER", "")
    console.print(f"📊 Campioni disponibili: {len(rows)} "
                  f"(anonimizzati: {all(r.get('anonymized') for r in rows)})")

    if not server:
        console.print(
            "\n[yellow]⚠️ Nessun server federato configurato.[/]"
            "\n[dim]I dati restano sul tuo dispositivo. Per abilitare "
            "l'upload, imposta la variabile d'ambiente "
            "BUO_FEDERATED_SERVER (es. https://server.example/api/v1/data)."
            "\nEsportazione CSV per condivisione manuale:[/]")
        csv_path = state_dir() / "dataset" / "vram_dataset.csv"
        n = VRAMDataCollector.to_csv(rows, csv_path)
        console.print(f"   → {n} righe in {csv_path}")
        sys.exit(0)

    try:
        import requests
    except ImportError:
        console.print("[red]❌ requests non installato "
                      "(pip install requests)[/]")
        sys.exit(1)

    try:
        resp = requests.post(server, json=rows, timeout=30)
        if resp.ok:
            console.print("[green]✅ Dati caricati sul server federato[/]")
        else:
            console.print(f"[red]❌ Upload fallito: HTTP {resp.status_code}[/]")
            sys.exit(1)
    except Exception as e:
        console.print(f"[red]❌ Upload in errore: {e}[/]")
        sys.exit(1)


# ----------------------------- ml-train ------------------------------ #

@cli.command("ml-train")
def ml_train() -> None:
    """🧠 Addestra il modello ML VRAM sui dati raccolti (data-collect)."""
    from .data.collector import VRAMDataCollector
    from .models.vram_estimator import VRAMMLModel
    from .utils.paths import state_dir

    show_header()
    rows = VRAMDataCollector.load_dataset()
    with_target = [r for r in rows if "vram_temp_real" in r]

    if not with_target:
        console.print(
            "[yellow]⚠️ Nessun campione con temperatura VRAM reale.[/]"
            "\n[dim]Collega una termocoppia e raccogli con: "
            "sudo buo data-collect --vram-sensor /dev/ttyUSB0[/]")
        sys.exit(1)

    console.print(f"🧠 Training su {len(with_target)} campioni "
                  f"(di {len(rows)} totali)...")
    model = VRAMMLModel()
    metrics = model.train(with_target)

    if "error" in metrics:
        console.print(f"[yellow]⚠️ {metrics['error']}[/]")
        console.print("[dim]Installabile con: pip install "
                      "numpy scikit-learn[/]")
        sys.exit(1)

    model_path = state_dir() / "vram_model.joblib"
    saved = model.save(str(model_path))
    console.print(f"[bold green]✅ Modello addestrato "
                  f"({metrics.get('samples', 0)} campioni) "
                  f"e salvato in {model_path}[/]")
    if not saved:
        console.print("[yellow]⚠️ Salvataggio modello fallito[/]")


# ----------------------------- benchmark ----------------------------- #

@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def benchmark(mock: bool) -> None:
    """Esegue solo i benchmark (GPU, CPU, compute)."""
    show_header()
    orchestrator = _make_orchestrator(mock=mock, dry_run=True,
                                      interactive=False, verbose=False)
    results = orchestrator.benchmark.run_all()
    from .benchmark.runner import BenchmarkRunner
    console.print(BenchmarkRunner.to_json(results))


# ------------------------------- tui ---------------------------------- #

@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def tui(mock: bool) -> None:
    """
    🖥️ Apre il cockpit interattivo (dashboard live dell'hardware).

    Richiede la dipendenza opzionale `textual`
    (pip install textual  oppure  pip install -e '.[tui]').
    """
    from .tui import run_tui
    from .utils.mock import MockHardware

    try:
        run_tui(mock=mock,
                mock_hardware=MockHardware() if mock else None)
    except RuntimeError as e:
        from rich.markup import escape
        console.print(f"[yellow]⚠️ {escape(str(e))}[/]")
        console.print("[dim]La CLI classica resta pienamente funzionante "
                      "(sudo buo unleash).[/]")
        sys.exit(1)


# ---------------------------- safety-test ---------------------------- #

@cli.command()
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def safety_test(mock: bool) -> None:
    """Verifica i safety gates e mostra gli hard limits (senza modifiche)."""
    show_header()
    _print_limits_table()
    orchestrator = _make_orchestrator(mock=mock, dry_run=True,
                                      interactive=False, verbose=False)
    audit = orchestrator.audit.run()
    problems = orchestrator.detector.detect(audit)
    critical = [p for p in problems if p["severity"] == "alta"]
    console.print(f"\n[bold]🔍 Problemi rilevati: {len(problems)} "
                  f"({len(critical)} critici)[/]")
    for p in problems:
        color = "red" if p["severity"] == "alta" else "yellow"
        console.print(f"  [bold {color}]{p['title']}[/] — {p['detail']}")


# ---------------------------- install-deps ---------------------------- #

@cli.command("install-deps")
@click.option("--check", "only_check", is_flag=True,
              help="Solo verifica, senza scaricare nulla")
def install_deps(only_check: bool) -> None:
    """
    📥 Scarica e installa i tool della community (repo mancanti).

    Clona e installa: bc250_smu_oc (undervolt CPU), bc250-40cu-unlock
    (GPU 40-CU/health/mask), bc250-acpi-fix (tabelle ACPI). Il governor
    (cyan-skillfish-governor-smu) e umr vengono installati come pacchetti
    dal package manager della distro (COPR/AUR), mai da installer esterni.
    """
    show_header()
    from .install.deps import DependencyManager

    manager = DependencyManager()

    if only_check:
        status = manager.check()
        console.print("[bold]🔎 Stato dipendenze:[/]\n")
        for line in manager.summary(status).splitlines():
            console.print(f"  {line}")
        missing = [n for n, s in status.items()
                   if not s.get("present") and s.get("type") != "instruct"]
        if missing:
            console.print("\n[yellow]⚠️ Manca: "
                          f"{', '.join(missing)}[/]")
            console.print("[dim]Esegui: sudo buo install-deps[/]")
        else:
            console.print("\n[bold green]✅ Tutte le dipendenze presenti[/]")
        sys.exit(0 if not missing else 1)

    console.print("[dim]📥 Download delle repo della community "
                  "(clone shallow)...[/]\n")
    result = manager.install()
    if "_error" in result:
        console.print(f"[red]❌ {result['_error']}[/]")
        sys.exit(1)

    for line in manager.summary(result).splitlines():
        console.print(f"  {line}")

    failed = [n for n, s in result.items() if s.get("status") == "failed"]
    if failed:
        console.print("\n[red]❌ Installazione non riuscita per: "
                      f"{', '.join(failed)}[/]")
        console.print("[dim]Controlla la connessione e che git sia "
                      "installato[/]")
        sys.exit(1)
    console.print("\n[bold green]✅ Dipendenze installate — ora puoi "
                  "eseguire: sudo buo unleash[/]")


# ------------------------------ doctor -------------------------------- #

@cli.command()
@click.option("--json-output", is_flag=True, help="Output JSON")
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
def doctor(json_output: bool, mock: bool) -> None:
    """
    🩺 Diagnostica completa in un solo comando (sola lettura).

    Raccoglie: ambiente, distro, kernel/Mesa, core/CU, temperature,
    problemi noti, tool della community, config, log. Per il supporto:
    esegui `buo doctor` e incolla tutto l'output.
    """
    from .diagnose import Doctor
    from .utils.mock import MockHardware

    show_header()
    doctor_ = Doctor(mock=mock, mock_hardware=MockHardware() if mock else None)
    report = doctor_.diagnose()

    if json_output:
        console.print(Doctor.to_json(report))
    else:
        console.print(doctor_.to_text(report))


# ====================================================================== #

def main() -> int:
    """Entry point CLI."""
    try:
        cli()
        return 0
    except KeyboardInterrupt:
        console.print("\n[yellow]⚠️ Interrotto dall'utente[/]")
        return 1
    except Exception as e:
        console.print(f"\n[red]❌ Errore: {e}[/]")
        if "--verbose" in sys.argv or "-v" in sys.argv:
            import traceback
            console.print(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
