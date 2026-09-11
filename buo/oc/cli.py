#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
CLI del tool OC integrato in BUO — gruppo `buo oc` + comando `buo oc-tui`.

NON è un entry point: i comandi vengono registrati da buo/cli.py
(`cli.add_command(oc_group)` + `cli.add_command(oc_tui, "oc-tui")`).

`buo oc` opera ESCLUSIVAMENTE su OC_DIR (/var/lib/buo/oc): stato del MOTORE
oc3600.sh, profili OC, apply. NON tocca il checkpoint dell'orchestratore
BUO (/var/lib/buo/state.json) — da non confondere con la fase legacy
`buo overclock` (invariata).
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

from ..constants import GPU_FREQ_STEPS
from ..utils.paths import SYSTEM_STATE_DIR, state_dir
from .constants import CU_LIVE_FILE, CU_LIVE_SCHEMA, OC_DIR_DEFAULT

try:
    from rich.console import Console
    from rich.table import Table
except ImportError:  # pragma: no cover
    Console = None

console = Console() if Console else None


def _oc_opts(fn):
    """Opzioni comuni a TUTTI i comandi del gruppo (hook di collaudo)."""
    fn = click.option("--oc-dir", "oc_dir", type=click.Path(),
                      default=None,
                      help=f"Override OC_DIR (default {OC_DIR_DEFAULT})")(
        fn)
    fn = click.option("--dry-run", is_flag=True,
                      help="Simula senza eseguire comandi reali")(fn)
    fn = click.option("--mock", is_flag=True,
                      help="Usa hardware simulato (nessun comando reale)")(fn)
    return fn


def _path(oc_dir: Optional[str]) -> Path:
    return Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)


def _mk_controller(oc_dir, mock, dry_run):
    from .controller import OcController
    return OcController(oc_dir=_path(oc_dir), mock=mock, dry_run=dry_run)


def _warn_if_not_system(oc_dir: Optional[str]) -> None:
    if console is None:
        return
    if state_dir() != SYSTEM_STATE_DIR and not oc_dir:
        console.print("[yellow]⚠️ Stato non di sistema (home) — "
                      "`buo oc` opera su /var/lib/buo/oc[/]")


@click.group("oc")
def oc_group() -> None:
    """⚡ Tool OC integrato (motore oc3600.sh + profili + apply).

    Opera ESCLUSIVAMENTE su OC_DIR (/var/lib/buo/oc) — stato del MOTORE,
    NON il checkpoint dell'orchestratore. Coesiste con la fase legacy
    `buo overclock` (invariata). Sicurezza: mai SMU con governor attivo;
    anti-zona 3725+/VID<1050; apply volatile di default (--persist opt-in).
    """


@oc_group.command("status")
@_oc_opts
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def oc_status(mock, dry_run, oc_dir, as_json) -> None:
    """Riepilogo run + macchina (porting cmd_status)."""
    ctl = _mk_controller(oc_dir, mock, dry_run)
    _warn_if_not_system(oc_dir)
    st = ctl.status()
    if as_json:
        click.echo(json.dumps(st, indent=2, ensure_ascii=False))
        return
    if console is None:
        click.echo(st)
        return
    table = Table(title="OC3600 · stato run")
    table.add_column("Campo")
    table.add_column("Valore")
    table.add_row("fase", str(st["state"].get("phase_label")))
    testing = st["state"].get("testing")
    table.add_row("testing",
                  f"{testing.get('freq')}@{testing.get('vid_cap')} "
                  f"({testing.get('kind')})" if testing and testing.get(
                      "freq") else "-")
    table.add_row("winner", str(st["state"].get("winner")))
    table.add_row("processo",
                  f"ATTIVO (pid {st['process']['pid']})" if st["process"][
                      "active"] else "fermo")
    table.add_row("governor", st["governor"])
    table.add_row("Tctl", f"{st['tctl_c']}°C" if st["tctl_c"] else "n/d")
    table.add_row("stress-ng", str(st["stress_ng_processes"]))
    table.add_row("apply", json.dumps(st["apply"], ensure_ascii=False))
    console.print(table)
    for line in st["log_tail"][-4:]:
        console.print(f"[dim]{line}[/]")


@oc_group.command("run", context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@_oc_opts
@click.argument("engine_flags", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def oc_run(ctx, mock, dry_run, oc_dir, engine_flags) -> None:
    """Lancia oc3600.sh (unità transient buo-oc). Flags engine verbatim:
    --cap-freq, --no-fine, --profile, --budget, --temp-target, --seed-step…"""
    ctl = _mk_controller(oc_dir, mock, dry_run)
    try:
        ctl.start(list(ctx.args))
    except RuntimeError as e:
        if console:
            console.print(f"[red]✗ {e}[/]")
        else:
            click.echo(f"ERRORE: {e}", err=True)
        sys.exit(1)
    if console:
        console.print("[bold green]✓ run avviata[/] (unità buo-oc)")


@oc_group.command("stop")
@_oc_opts
def oc_stop(mock, dry_run, oc_dir) -> None:
    """SIGTERM pulito alla run (exit 40 riprendibile)."""
    ctl = _mk_controller(oc_dir, mock, dry_run)
    ctl.stop()
    if console:
        console.print("[bold green]✓ stop richiesto[/]")


@oc_group.command("reset")
@_oc_opts
@click.option("--yes", is_flag=True, help="Salta la conferma")
def oc_reset(mock, dry_run, oc_dir, yes) -> None:
    """Azzera il checkpoint (state.json+pid). MAI /etc, MAI i log."""
    ctl = _mk_controller(oc_dir, mock, dry_run)
    try:
        ctl.reset(confirm=yes)
    except RuntimeError as e:
        if console:
            console.print(f"[red]✗ {e}[/]")
        else:
            click.echo(f"ERRORE: {e}", err=True)
        sys.exit(1)
    if mock or dry_run:
        # M2: reset in simulazione NON cancella nulla (guard in controller)
        msg = ("⏭ reset saltato: simulazione (nessuna scrittura)")
        if console:
            console.print(f"[yellow]{msg}[/]")
        else:
            click.echo(msg)
    elif console:
        console.print("[bold green]✓ checkpoint azzerato[/]")


@oc_group.command("watch")
@_oc_opts
@click.argument("every", required=False, type=int, default=10)
def oc_watch(mock, dry_run, oc_dir, every) -> None:
    """Vista live CLI ogni N secondi."""
    ctl = _mk_controller(oc_dir, mock, dry_run)
    ctl.watch(every=every or 10)


# --------------------------------------------------------------------------- #
# profiles
# --------------------------------------------------------------------------- #


@oc_group.group("profiles")
def oc_profiles() -> None:
    """Profili Stock / Certificato / Custom."""


@oc_profiles.command("list")
@_oc_opts
def oc_profiles_list(mock, dry_run, oc_dir) -> None:
    """Elenca i profili con active/validated."""
    from .profiles import ProfileStore
    store = ProfileStore(_path(oc_dir))
    profiles = store.load()
    if console is None:
        for p in profiles:
            click.echo(f"{p.id}: {p.name} f={p.freq} s={p.scale} "
                       f"vid={p.vid_cap} validated={p.validated}")
        return
    table = Table(title="Profili OC")
    for col in ("id", "nome", "freq", "scale", "VID", "validated"):
        table.add_column(col)
    for p in profiles:
        table.add_row(p.id, p.name, str(p.freq), str(p.scale),
                      str(p.vid_cap if p.vid_cap is not None else "-"),
                      "sì" if p.validated else "no")
    console.print(table)


def _skip_simulated(action: str) -> None:
    """M2: scritture in --mock/--dry-run → skip esplicito (mai store
    toccato). Chiamata PRIMA della scrittura."""
    msg = f"⏭ {action}: saltato (simulazione — nessuna scrittura)"
    if console:
        console.print(f"[yellow]{msg}[/]")
    else:
        click.echo(msg)


@oc_profiles.command("add")
@_oc_opts
@click.argument("name")
@click.option("--freq", type=int, required=True, help="Frequenza (MHz)")
@click.option("--scale", type=int, required=True,
              help=f"Scale in [{'-50'}, 0]")
@click.option("--vid", "vid_cap", type=int, default=None,
              help="VID atteso (mV) — OBBLIGATORIO per freq ≥ 3725")
@click.option("--source", default="user")
def oc_profiles_add(mock, dry_run, oc_dir, name, freq, scale, vid_cap,
                    source) -> None:
    """Aggiunge un profilo Custom (anti-zona fail-closed)."""
    from .profiles import Profile, ProfileStore, ProfileValidator
    store = ProfileStore(_path(oc_dir))
    validator = ProfileValidator()
    pid = f"custom-{name.lower().replace(' ', '-')}"
    p = Profile(id=pid, name=name, freq=freq, scale=scale, vid_cap=vid_cap,
                source=source, validated=False)
    ok, reason = validator.zone_ok(p)
    if not ok:
        if console:
            console.print(f"[red]✗ profilo rifiutato: {reason}[/]")
        else:
            click.echo(f"ERRORE: {reason}", err=True)
        sys.exit(1)
    if mock or dry_run:
        _skip_simulated(f"profilo {pid} non aggiunto")
        return
    profiles = [x for x in store.load() if x.id != pid]
    profiles.append(p)
    store.save(profiles)
    if console:
        console.print(f"[bold green]✓ profilo {pid} aggiunto[/] "
                      f"({freq}@{scale})")


@oc_profiles.command("rm")
@_oc_opts
@click.argument("name")
def oc_profiles_rm(mock, dry_run, oc_dir, name) -> None:
    """Rimuove un profilo (mai i builtin stock/certified)."""
    from .profiles import ProfileStore
    store = ProfileStore(_path(oc_dir))
    p = store.get(name)
    if p is None:
        if console:
            console.print(f"[red]✗ profilo '{name}' non trovato[/]")
        else:
            click.echo(f"ERRORE: profilo '{name}' non trovato", err=True)
        sys.exit(1)
    if p.id in ("stock", "certified"):
        if console:
            console.print("[red]✗ i profili builtin non si rimuovono[/]")
        else:
            click.echo("ERRORE: i profili builtin non si rimuovono", err=True)
        sys.exit(1)
    if mock or dry_run:
        _skip_simulated(f"profilo {p.id} non rimosso")
        return
    profiles = [x for x in store.load() if x.id != p.id]
    store.save(profiles)
    if console:
        console.print(f"[bold green]✓ profilo {p.id} rimosso[/]")


# --------------------------------------------------------------------------- #
# apply / restore-stock / heal
# --------------------------------------------------------------------------- #


def _mk_apply(oc_dir, mock, dry_run):
    from .apply import ApplyManager
    from .controller import OcController
    from .profiles import ProfileStore, ProfileValidator
    from .smoke import CpuSmoke
    ctl = OcController(oc_dir=_path(oc_dir), mock=mock, dry_run=dry_run)
    store = ProfileStore(_path(oc_dir))
    smoke = CpuSmoke(reader=None, mock=mock, dry_run=dry_run,
                     oc_dir=_path(oc_dir))
    return ApplyManager(ctl, store=store,
                        validator=ProfileValidator(), smoke=smoke,
                        reader=None, mock=mock, dry_run=dry_run,
                        oc_dir=_path(oc_dir))


def _print_outcome(outcome) -> None:
    if console is None:
        click.echo(f"result={outcome.result} profile={outcome.profile} "
                   f"persisted={outcome.persisted} cause={outcome.cause}")
        return
    color = {"ok": "green", "rolled_back": "red", "aborted": "yellow",
             "stale": "red"}.get(outcome.result, "white")
    console.print(f"[{color}]result={outcome.result}[/] "
                  f"profile={outcome.profile} "
                  f"persisted={outcome.persisted}")
    if outcome.cause:
        console.print(f"[{color}]causa: {outcome.cause}[/]")
    for d in outcome.details[-6:]:
        console.print(f"[dim]{d}[/]")


@oc_group.command("apply")
@_oc_opts
@click.argument("name")
@click.option("--persist", is_flag=True,
              help="In più: --install + enable servizio (riapplica al boot) "
                   "— richiede --yes")
@click.option("--yes", is_flag=True, help="Conferma esplicita (--persist)")
def oc_apply(mock, dry_run, oc_dir, name, persist, yes) -> None:
    """Applica un profilo (volatile di default; --persist opt-in)."""
    from .profiles import ProfileStore
    store = ProfileStore(_path(oc_dir))
    p = store.get(name)
    if p is None:
        if console:
            console.print(f"[red]✗ profilo '{name}' non trovato[/]")
        else:
            click.echo(f"ERRORE: profilo '{name}' non trovato", err=True)
        sys.exit(1)
    outcome = _mk_apply(oc_dir, mock, dry_run).apply(p, persist=persist,
                                                     yes=yes)
    _print_outcome(outcome)
    if outcome.result in ("aborted", "rolled_back"):
        sys.exit(1)


@oc_group.command("restore-stock")
@_oc_opts
@click.option("--persist", is_flag=True,
              help="In più: disable servizio bc250-smu-oc (opt-out) — "
                   "richiede --yes")
@click.option("--yes", is_flag=True)
def oc_restore_stock(mock, dry_run, oc_dir, persist, yes) -> None:
    """Ripristina il profilo Stock (boot stock-safe)."""
    outcome = _mk_apply(oc_dir, mock, dry_run).restore_stock(
        persist=persist, yes=yes)
    _print_outcome(outcome)
    if outcome.result in ("aborted", "rolled_back"):
        sys.exit(1)


@oc_group.command("heal")
@_oc_opts
def oc_heal(mock, dry_run, oc_dir) -> None:
    """Sanifica un apply interrotto (governor fermo → backup + riavvio)."""
    outcome = _mk_apply(oc_dir, mock, dry_run).heal()
    _print_outcome(outcome)


# --------------------------------------------------------------------------- #
# cu-live (percorso CUMULATIVO delle 16 CU extra, senza reboot)
# --------------------------------------------------------------------------- #


def _cu_live_path(oc_dir) -> Path:
    return _path(oc_dir) / CU_LIVE_FILE


def read_cu_live(oc_dir) -> list:
    """Prove registrate dal percorso cumulativo ([] se assente/illeggibile).

    Mai eccezione (fail-soft): un file corrotto è "nessuna prova nota", non
    un errore che blocca il test.
    """
    try:
        data = json.loads(_cu_live_path(oc_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or data.get("schema") != CU_LIVE_SCHEMA:
        return []
    tries = data.get("tries")
    if not isinstance(tries, list):
        return []
    return [t for t in tries if isinstance(t, dict)]


def record_cu_live(oc_dir, wgps, out=None) -> bool:
    """Registra una prova RIUSCITA di `cu-live` (fail-soft: mai bloccante).

    Il percorso cumulativo è human-in-the-loop (una WGP per volta + test
    reale): senza traccia, chi conduce il test non sa cosa ha già provato.
    Una ri-esecuzione con le stesse WGP aggiorna la voce (non la duplica).
    """
    entry = {"wgps": sorted(str(w) for w in wgps), "esito": "applicato",
             "cu_count": (out or {}).get("cu_count"),
             "mask": (out or {}).get("mask"),
             "at": datetime.now(timezone.utc).isoformat()}
    tries = [t for t in read_cu_live(oc_dir) if t.get("wgps") != entry["wgps"]]
    tries.append(entry)
    try:
        from .apply import _write_json_atomic
        _write_json_atomic(_cu_live_path(oc_dir),
                           {"schema": CU_LIVE_SCHEMA,
                            "updated_at": entry["at"], "tries": tries})
    except Exception as e:
        # Mai bloccare l'operazione (le CU extra SONO state abilitate): il
        # tracciamento è un diario, non una precondizione.
        if console:
            console.print(f"[yellow]⚠️ tracciamento cu-live non scritto: "
                          f"{e}[/]")
        else:
            click.echo(f"WARN: tracciamento cu-live non scritto: {e}",
                       err=True)
        return False
    return True


def _print_cu_live(oc_dir) -> None:
    """Mostra le prove registrate (`cu-live --show`: nessuna UI nuova)."""
    tries = read_cu_live(oc_dir)
    if console is None:
        click.echo(json.dumps(tries, ensure_ascii=False))
        return
    if not tries:
        console.print(f"[yellow]Nessuna prova registrata[/] "
                      f"({_cu_live_path(oc_dir)})")
        console.print("[dim]`buo oc cu-live SE.SH.WGP` registra ogni prova "
                      "riuscita; il test reale e il suo esito sono a carico "
                      "di chi conduce il protocollo.[/]")
        return
    table = Table(title="cu-live · WGP extra già provate")
    for col in ("provate", "CU", "esito", "quando"):
        table.add_column(col)
    for t in tries:
        table.add_row(",".join(t.get("wgps") or []),
                      str(t.get("cu_count") or "-"),
                      str(t.get("esito") or "-"),
                      str(t.get("at") or "-"))
    console.print(table)


@oc_group.command("cu-live")
@_oc_opts
@click.argument("wgps", required=False)
@click.option("--show", is_flag=True,
              help="Mostra le prove già registrate (nessuna scrittura)")
def oc_cu_live(mock, dry_run, oc_dir, wgps, show) -> None:
    """Abilita a RUNTIME le WGP extra indicate (cumulativo, senza reboot).

    `wgps` = CSV di WGP EXTRA in forma SE.SH.WGP, es. "0.1.3" oppure
    "0.1.3,0.1.4". È la via consigliata "una WGP per volta + test reale"
    (si richiama il comando aggiungendo la WGP successiva), alternativa
    alla maratona per-WGP (~20 reboot, solo con presidio). Richiede
    l'opt-in `phases.probe.gpu_extra_cu=true` e la curva GPU conservativa
    (≤900 mV, o certificata dallo sweep per-silicio): con 40 CU una curva
    aggressiva porta la GPU a 96-107 °C.
    La maschera è DERIVATA (mai 0x1f a mano) e le WGP condannate dal
    verdetto durevole restano escluse; si riparte sempre da 24 CU, quindi
    il routing non include mai una WGP non validata. Il governor è fermato
    da BUO durante l'accesso ai registri (regola SMU) e riavviato dopo.
    Ogni prova riuscita è registrata in oc_dir/gpu-cu-live.json
    (`--show` per vederle).
    """
    from ..unlock.gpu import GPU40CUUnlock
    if show:
        _print_cu_live(oc_dir)
        return
    if not wgps:
        raise click.UsageError("serve una WGP extra (SE.SH.WGP) o --show")
    _warn_if_not_system(oc_dir)
    sim = mock or dry_run
    ids = [w.strip() for w in wgps.split(",") if w.strip()]
    out = GPU40CUUnlock(mock=sim).apply(wgps=ids)
    applied = bool(out.get("applied"))
    if sim:
        _skip_simulated("CU extra non abilitate (nessuna scrittura)")
    if console is None:
        click.echo(json.dumps(out, ensure_ascii=False))
    elif applied:
        console.print(f"[bold green]✓ {'[simulato] ' if sim else ''}"
                      f"CU extra: {out.get('cu_count')} CU[/] "
                      f"(maschera {out.get('mask')}, volatile)")
    else:
        console.print(f"[yellow]⚠️ non applicato: "
                      f"{out.get('reason') or out.get('error')}[/]")
        for key in ("note", "error"):
            if out.get(key):
                console.print(f"[dim]{out[key]}[/]")
    if applied and not sim:
        # le WGP REGISTRATE sono quelle davvero instradate (`wgps` dell'esito:
        # una WGP condannata dal verdetto viene esclusa dalla maschera), non
        # quelle chieste sulla riga di comando
        record_cu_live(oc_dir, out.get("wgps") or ids, out)
    if not applied and not sim:
        sys.exit(1)


# --------------------------------------------------------------------------- #
# sweep-gpu (T4: sweep per-silicio delegato da unleash, design
# research/DESIGN_T4_SWEEP_OC.md)
# --------------------------------------------------------------------------- #


def _oc_sweep_config():
    """Config per i default delle opzioni sweep via BUOConfig.load()
    (legge /etc/buo/buo.yaml quando presente; assente/non leggibile →
    default di codice)."""
    try:
        from ..config import BUOConfig
        return BUOConfig.load()
    except Exception:  # pragma: no cover — config non leggibile
        return None


def _parse_freqs(value, default):
    """Frequenze CSV → lista ordinata, deduplicata, sottoinsieme di
    GPU_FREQ_STEPS (regola di coerenza esistente in config._sweep_freqs);
    liste non ordinate/valori fuori scala mai passate al probe in silenzio."""
    if value is None:
        return default
    try:
        freqs = [int(x.strip()) for x in value.split(",") if x.strip()]
    except ValueError:
        raise click.BadParameter("formato atteso: '1200,1500,2000'")
    freqs = sorted(set(freqs))
    if not freqs:
        raise click.BadParameter("almeno una frequenza richiesta")
    invalid = [f for f in freqs if f not in GPU_FREQ_STEPS]
    if invalid:
        raise click.BadParameter(
            "frequenze non valide (attese in GPU_FREQ_STEPS %s): %s"
            % (GPU_FREQ_STEPS, invalid))
    return freqs


def _print_sweep_report(esito, sim, path) -> None:
    if console is None:
        click.echo("sweep-gpu: source=%s winner=%s esito_scritto=%s"
                   % (esito.get("source"), esito.get("winner"),
                      esito.get("written")))
        return
    if sim:
        console.print("[dim]Simulato (--mock/--dry-run): nessuna scrittura "
                      "su gpu-sweep.json[/]")
    source = esito.get("source")
    if not sim and source == "community_defaults":
        console.print("[yellow]⚠️ Sweep non eseguito (tool di stress o "
                      "governor non disponibili): tabella community "
                      "applicata (non è un errore)[/]")
    if esito.get("governor_stopped"):
        console.print("[yellow]Governor FERMO a fine sweep (curva precedente "
                      "non riapplicata): avvialo con `systemctl start "
                      "cyan-skillfish-governor-smu` (o `buo oc heal`)[/]")
    crashes = [t for t in esito.get("tested") or []
               if t.get("status") == "crash"]
    if crashes:
        for t in crashes:
            v = "?" if t.get("v") is None else t["v"]
            console.print(f"[red]✗ Punto crashato (boot precedente) "
                          f"saltato: {t['f']} MHz @ {v} mV[/]")
    winner = esito.get("winner")
    if winner:
        console.print(f"[bold green]✓ Vincitore: {winner['freq']} MHz @ "
                      f"{winner['voltage']} mV[/]")
    else:
        console.print("[yellow]Nessun vincitore (nessun punto stabile)[/]")
    points = esito.get("safe_points") or []
    if points:
        console.print("Safe points: " + ", ".join(
            f"{p['freq']}@{p['voltage']}" for p in points))
    best = esito.get("best_efficiency") or {}
    if best:
        console.print(f"Best efficiency: {best['freq']} MHz @ "
                      f"{best['voltage']} mV")
    console.print(f"Fonte: {source}")
    notes = []
    smeta = esito.get("sweep") or {}
    if smeta.get("smu_floor_mv") is not None:
        notes.append(f"floor SMU rilevato: {smeta['smu_floor_mv']} mV")
    if esito.get("clamped_to_floor"):
        notes.append("safe_points clampati al floor")
    if smeta.get("duration_s") is not None:
        notes.append(f"durata: {smeta['duration_s']} s")
    if notes:
        console.print("[dim]note: " + " · ".join(notes) + "[/]")
    if esito.get("written"):
        console.print(f"[dim]Esito scritto: {path}[/]")


@oc_group.command("sweep-gpu")
@_oc_opts
@click.option("--freqs", default=None,
              help="Frequenze (MHz) CSV, es. '1200,1500,2000' — "
                   "default dalla config (1200,1500,2000)")
@click.option("--step-mv", type=int, default=None,
              help="Passo di discesa (mV) — default dalla config (25)")
@click.option("--floor-mv", type=int, default=None,
              help="Floor minimo dei safe_points (mV) — default dalla "
                   "config (800)")
def oc_sweep_gpu(mock, dry_run, oc_dir, freqs, step_mv, floor_mv) -> None:
    """Sweep GPU per-silicio (ricerca undervolt; design T4).

    Esito: oc_dir/gpu-sweep.json (scrittura atomica). Fallback community
    con avviso se lo sweep non è possibile (fail-closed di gpu.py): NON è
    un errore. Con --mock/--dry-run: flusso simulato, nessuna scrittura
    (C1).
    """
    from . import gpu_sweep
    _warn_if_not_system(oc_dir)
    cfg = _oc_sweep_config()

    def _cfg(name, dflt):
        return getattr(cfg, name, dflt) if cfg is not None else dflt

    freqs_l = _parse_freqs(freqs, list(_cfg("undervolt_gpu_sweep_freqs",
                                            gpu_sweep.DEFAULT_FREQS)))
    step = (step_mv if step_mv is not None
            else _cfg("undervolt_gpu_sweep_step_mv", gpu_sweep.DEFAULT_STEP_MV))
    floor = (floor_mv if floor_mv is not None
             else _cfg("undervolt_gpu_sweep_floor_mv",
                       gpu_sweep.DEFAULT_FLOOR_MV))
    sweep_opts = {k: _cfg("undervolt_gpu_sweep_" + k, v)
                  for k, v in gpu_sweep.DEFAULT_SWEEP_OPTS.items()}
    path = _path(oc_dir) / gpu_sweep.ESITO_FILE
    sim = mock or dry_run
    try:
        esito = gpu_sweep.run(
            oc_dir=_path(oc_dir), freqs=freqs_l, step_mv=step,
            floor_mv=floor, sweep_opts=sweep_opts, mock=mock,
            dry_run=dry_run)
    except RuntimeError as e:
        if console:
            console.print(f"[red]✗ {e}[/]")
        else:
            click.echo(f"ERRORE: {e}", err=True)
        sys.exit(1)
    except Exception as e:  # sweep fallito (mai esito parziale)
        if console:
            console.print(f"[red]✗ sweep fallito: {e}[/]")
        else:
            click.echo(f"ERRORE: sweep fallito: {e}", err=True)
        sys.exit(1)
    _print_sweep_report(esito, sim, path)


# --------------------------------------------------------------------------- #
# oc-tui (comando TOP-LEVEL, registrato da buo/cli.py)
# --------------------------------------------------------------------------- #


@click.command("oc-tui")
@click.option("--mock", is_flag=True, help="Usa hardware simulato")
@click.option("--oc-dir", "oc_dir", type=click.Path(), default=None)
def oc_tui_command(mock: bool, oc_dir: Optional[str]) -> None:
    """🖥️ Cockpit OC interattiva (textual opzionale)."""
    from .tui_app import run_oc_tui
    try:
        run_oc_tui(mock=mock, oc_dir=_path(oc_dir))
    except RuntimeError as e:
        if console:
            # m5: escape del messaggio (il rich markup interpreterebbe i
            # '[' del suggerimento ".[tui]" come tag → output corrotto)
            from rich.markup import escape
            console.print(f"[yellow]⚠️ {escape(str(e))}[/]")
            console.print("[dim]La CLI classica (buo oc) resta pienamente "
                          "funzionante.[/]")
        else:
            click.echo(f"⚠️ {e}", err=True)
        sys.exit(1)
