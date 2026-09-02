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
                       config: Optional[BUOConfig] = None,
                       offline_bundle: Optional[str] = None):
    from .orchestrator import Orchestrator
    if config is None:
        config = BUOConfig.load()
    return Orchestrator(
        config=config,
        mock=mock,
        dry_run=dry_run,
        interactive=interactive,
        log_level="DEBUG" if verbose else "INFO",
        offline_bundle=offline_bundle,
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
@click.option("--offline-bundle", type=click.Path(), default=None,
              help="Bundle offline dei checkout: importato PRIMA "
                   "dell'auto-install se servono tool git-based mancanti")
def unleash(mock: bool, dry_run: bool, interactive: bool, verbose: bool,
            skip_benchmark: bool, skip_validation: bool, quick: bool,
            offline_bundle: Optional[str]) -> None:
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
                                      config=config,
                                      offline_bundle=offline_bundle)
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
    from .safety.reader import RealHardwareReader
    from .utils.mock import MockHardware

    show_header()
    # C1: in modalità reale letture REALI (hwmon), mai valori fittizi
    hw = MockHardware() if mock else RealHardwareReader()
    monitor = SafetyMonitor(hardware=hw, abort_callback=lambda r: None,
                            vram_estimation=False)
    monitor.start()
    console.print("[dim]🛡️ Monitor attivo — Ctrl+C per fermare[/]\n")
    try:
        while True:
            readings = monitor.get_last_readings()
            if readings is not None:
                def _fmt(v, unit, digits=1):
                    return f"{v:.{digits}f}{unit}" if v is not None else "?"
                console.print(
                    f"\rCPU {_fmt(readings.cpu_temp, '°C')} | "
                    f"GPU {_fmt(readings.gpu_temp, '°C')} | "
                    f"VID {_fmt(readings.cpu_vid, 'mV', 0)} | "
                    f"VGPU {_fmt(readings.gpu_voltage, 'mV', 0)} | "
                    f"P {_fmt(readings.total_power, 'W')}" + " " * 10,
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
    from .safety.reader import RealHardwareReader

    show_header()

    # C1: mai valori fittizi in produzione. `status` è sola lettura:
    # dry_run=False (niente MockHardware come effetto del dry-run) e,
    # senza --mock, letture REALI via RealHardwareReader (hwmon); ogni
    # sensore non leggibile → None → "non rilevabile" (fail-soft, mai
    # inventare valori). Con --mock resta la simulazione invariata.
    orchestrator = _make_orchestrator(mock=mock, dry_run=False,
                                      interactive=False, verbose=False)
    info = orchestrator.status()

    # Fase/Reboot: dallo state dir risolto (state_dir(): SYSTEM_STATE_DIR
    # se scrivibile, altrimenti home). Se NON è quello di sistema, i valori
    # letti sono quelli locali (non lo stato reale) — avviso chiaro,
    # fail-soft, mai crash. Il criterio è lo state dir risolto, non l'euid:
    # root in container può avere /var/lib/buo non scrivibile, e un
    # BUO_STATE_DIR esplicito vale anche da root.
    from .utils.paths import SYSTEM_STATE_DIR, state_dir
    if not mock and state_dir() != SYSTEM_STATE_DIR:
        console.print("[yellow]⚠️ Stato non di sistema: esegui "
                      "`sudo buo status` per lo stato reale[/]")
    console.print(f"[dim]Fase corrente: {info['current_phase']} | "
                  f"Reboot: {info['reboot_count']}[/]\n")

    if mock:
        hardware = info.get("hardware")
    else:
        try:
            hardware = RealHardwareReader().get_system_info()
        except Exception as exc:
            # Fail-soft: lettura hardware anomala (es. /sys non leggibile)
            # → avviso e uscita pulita, mai crash con exit 1.
            console.print(f"[yellow]⚠️ Lettura hardware non riuscita: {exc}[/]")
            return
    if hardware is None:
        console.print("[yellow]⚠️ Nessun hardware rilevato "
                      "(usa --mock per simulare)[/]")
        return

    def _fmt(value, suffix=""):
        """Fail-soft C1: None → 'non rilevabile', mai valori inventati."""
        return f"{value}{suffix}" if value is not None else "non rilevabile"

    def _status_ge(value, threshold, ok_text, ko_text):
        """Stato rispetto a una soglia; None → neutro (non rilevabile)."""
        if value is None:
            return "—"
        return ok_text if value >= threshold else ko_text

    def _status_lt(value, limit, ok_text, crit_text):
        """Stato sotto un limite; None → neutro (non rilevabile)."""
        if value is None:
            return "—"
        return ok_text if value < limit else crit_text

    table = Table(title="📊 STATO HARDWARE", border_style="blue",
                  header_style="bold cyan", show_lines=True)
    table.add_column("Componente", style="white")
    table.add_column("Valore", style="green")
    table.add_column("Stato", style="bold")

    cpu_cores = hardware.get("cpu_cores")
    gpu_cu = hardware.get("gpu_cu")
    cpu_temp = hardware.get("cpu_temp")
    gpu_temp = hardware.get("gpu_temp")
    total_power = hardware.get("total_power")
    is_40cu = hardware.get("is_40cu_enabled")

    table.add_row("CPU Core", _fmt(cpu_cores, "/8"),
                  _status_ge(cpu_cores, 8, "✅ OK", "⚠️ Parziale"))
    table.add_row("Core Mask", _fmt(hardware.get("core_mask")), "—")
    table.add_row("CPU Freq", _fmt(hardware.get("cpu_freq"), " MHz"), "—")
    table.add_row("CPU Temp", _fmt(cpu_temp, "°C"),
                  _status_lt(cpu_temp, LIMITS.cpu.temp_max,
                             "✅ OK", "🔴 CRITICA"))
    table.add_row("CPU VID", _fmt(hardware.get("cpu_vid"), " mV"), "—")
    table.add_row("GPU CU", _fmt(gpu_cu, "/40"),
                  _status_ge(gpu_cu, 24, "✅ OK", "⚠️ Ridotte"))
    table.add_row("GPU Freq", _fmt(hardware.get("gpu_freq"), " MHz"), "—")
    table.add_row("GPU Temp", _fmt(gpu_temp, "°C"),
                  _status_lt(gpu_temp, LIMITS.gpu.temp_max,
                             "✅ OK", "🔴 CRITICA"))
    table.add_row("GPU Volt", _fmt(hardware.get("gpu_voltage"), " mV"), "—")
    table.add_row("GPU Power", _fmt(hardware.get("gpu_power"), " W"), "—")
    table.add_row("Potenza", _fmt(total_power, " W"), "—")
    table.add_row("Ventola", _fmt(hardware.get("fan_speed"), " RPM"), "—")
    table.add_row("Ambiente", _fmt(hardware.get("ambient_temp"), "°C"), "—")
    if is_40cu is None:
        table.add_row("40-CU", "non rilevabile", "—")
    else:
        table.add_row("40-CU",
                      "✅ Attive" if is_40cu else "💤 Stock",
                      "✅" if is_40cu else "—")
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
    table.add_row("PSU", f"{d['hardware']['psu_wattage']} W")
    table.add_row("Raffreddamento", d["hardware"]["cooling_type"])
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
@click.option("--export-bundle", "export_bundle_path", type=click.Path(),
              default=None,
              help="Crea un bundle offline dei checkout verificati "
                   "(nessuna installazione)")
@click.option("--offline", "offline_bundle", type=click.Path(),
              default=None,
              help="Importa e verifica un bundle offline, poi installa")
def install_deps(only_check: bool, export_bundle_path: Optional[str],
                 offline_bundle: Optional[str]) -> None:
    """
    📥 Scarica e installa i tool della community (repo mancanti).

    Clona e installa: bc250_smu_oc (undervolt CPU), bc250-40cu-unlock
    (GPU 40-CU/health/mask), bc250-acpi-fix (tabelle ACPI). Il governor
    (cyan-skillfish-governor-smu) e umr vengono installati come pacchetti
    dal package manager della distro (COPR/AUR), mai da installer esterni.

    Senza rete: genera un bundle su una macchina connessa
    (--export-bundle), copialo su USB e importalo qui (--offline).
    """
    if export_bundle_path and offline_bundle:
        raise click.UsageError(
            "--export-bundle e --offline si escludono a vicenda: "
            "usa uno alla volta")
    if only_check and (export_bundle_path or offline_bundle):
        raise click.UsageError(
            "--check è sola lettura: non combina con "
            "--export-bundle/--offline")

    show_header()
    from .install.deps import DependencyManager

    manager = DependencyManager()

    if export_bundle_path:
        res = manager.export_bundle(export_bundle_path)
        if res["status"] != "ok":
            console.print(f"[red]❌ Export bundle fallito: "
                          f"{res['detail']}[/]")
            sys.exit(1)
        console.print(f"[bold green]✅ Bundle offline creato: "
                      f"{res['path']}[/]")
        console.print(f"[dim]SHA-256: {res['sha256']} "
                      f"(verifica con: sha256sum {res['path']})[/]")
        sys.exit(0)

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

    if offline_bundle:
        console.print("[dim]📦 Import del bundle offline e installazione...[/]\n")
    else:
        console.print("[dim]📥 Download delle repo della community "
                      "(clone shallow)...[/]\n")
    result = manager.install(offline_bundle=offline_bundle)
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
                      "installato, oppure usa --offline con un bundle "
                      "generato su una macchina con rete[/]")
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


# ------------------------------ restore ------------------------------- #

@cli.command()
@click.option("--profile", "profile_path", type=click.Path(),
              default=None, help="File profilo (default: profilo salvato)")
@click.option("--validate", is_flag=True,
              help="Esegue anche lo stress test (default: saltato)")
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
@click.option("--dry-run", is_flag=True, help="Simula senza modifiche")
def restore(profile_path, validate: bool, mock: bool, dry_run: bool) -> None:
    """
    ♻️ RIPRISTINA lo stato salvato (dopo format o aggiornamento).

    Riapplica: toolchain, fix ACPI, unlock CPU/40 CU, undervolt
    persistente e governor — usando il PROFILO salvato, senza
    rilanciare l'auto-tuning. Con --validate esegue anche lo stress.
    """
    from pathlib import Path as _Path
    from .profile import default_profile_path, load_profile

    show_header()
    path = _Path(profile_path) if profile_path else default_profile_path()
    try:
        profile = load_profile(path)
    except ValueError as e:
        console.print(f"[red]❌ {e}[/]")
        console.print("[dim]Suggerimento: esegui prima `sudo buo unleash` "
                      "per creare il profilo, oppure specifica --profile[/]")
        sys.exit(1)

    console.print(f"[bold]♻️ Restore da profilo:[/] [cyan]{path}[/]")
    console.print(f"[dim]  creato: {profile.get('created', '?')} — "
                  f"fix nel profilo: "
                  f"{len(profile.get('applied_fixes', []) or [])}[/]")

    config = BUOConfig.load()
    if not validate:
        config.validation_stress_duration = 0
        console.print("[dim]  stress test: SALTATO (--validate per eseguirlo)[/]")

    orchestrator = _make_orchestrator(mock, dry_run, False, False,
                                      config=config)
    exit_code = orchestrator.run(restore=profile)

    if exit_code == 0:
        console.print("\n[bold green]✅ RIPRISTINO COMPLETATO — "
                      "la macchina è tornata allo stato salvato![/]")
    else:
        console.print(f"\n[bold red]❌ Ripristino fallito (codice {exit_code})[/]")
        console.print("[dim]Log: /var/log/buo/buo.log — "
                      "rollback: sudo buo rollback[/]")
    sys.exit(exit_code)


# ------------------------------ profile ------------------------------- #

@cli.group()
def profile() -> None:
    """📦 Profilo macchina: export/import per il ripristino (G2)."""


@profile.command("export")
@click.option("--output", "output_path", type=click.Path(),
              default=None, help="File di destinazione")
def profile_export(output_path) -> None:
    """Esporta il profilo macchina corrente su file JSON."""
    from pathlib import Path as _Path
    from .profile import export_profile

    show_header()
    target = _Path(output_path) if output_path else None
    prof = export_profile(target)
    console.print(f"[bold green]✅ Profilo esportato:[/] "
                  f"[cyan]{target or prof}[/]")
    console.print(f"[dim]  fix: {len(prof.get('applied_fixes', []) or [])} — "
                  f"ottimizzazione: "
                  f"{'presente' if prof.get('optimize') else 'ASSENTE'}[/]")


@profile.command("import")
@click.argument("file_path", type=click.Path(exists=True))
@click.option("--output", "output_path", type=click.Path(),
              default=None, help="Dove salvarlo (default: profilo salvato)")
def profile_import(file_path, output_path) -> None:
    """Valida e importa un profilo (lo rende il profilo di restore)."""
    from pathlib import Path as _Path
    from .profile import default_profile_path, load_profile

    show_header()
    try:
        prof = load_profile(_Path(file_path))
    except ValueError as e:
        console.print(f"[red]❌ {e}[/]")
        sys.exit(1)
    target = _Path(output_path) if output_path else default_profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        _Path(file_path).read_text(encoding="utf-8"), encoding="utf-8")
    console.print(f"[bold green]✅ Profilo importato:[/] [cyan]{target}[/]")
    console.print(f"[dim]  fix: {len(prof.get('applied_fixes', []) or [])} — "
                  f"usalo con: sudo buo restore[/]")


# ------------------------------- oc ----------------------------------- #
# Tool OC integrato (design research/DESIGN_BUO_OC_TUI.md): motore
# oc3600.sh + profili + apply. Opera ESCLUSIVAMENTE su OC_DIR
# (/var/lib/buo/oc) — NON tocca il checkpoint dell'orchestratore; coesiste
# con la fase legacy `buo overclock` (invariata). Registrazione lazy.


@cli.group("oc")
def oc_group() -> None:
    """⚡ Tool OC integrato (motore oc3600.sh + profili + apply)."""


def _register_oc() -> None:
    from .oc.cli import oc_group as _oc_group
    from .oc.cli import oc_tui_command as _oc_tui

    # comandi del gruppo (definiti in buo/oc/cli.py)
    for name, cmd in _oc_group.commands.items():
        oc_group.add_command(cmd, name)
    cli.add_command(_oc_tui, "oc-tui")


_register_oc()


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
