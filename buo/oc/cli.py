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
from pathlib import Path
from typing import Optional

import click

from ..utils.paths import SYSTEM_STATE_DIR, state_dir
from .constants import OC_DIR_DEFAULT

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
    anti-zona 3725+/VID<1000; apply volatile di default (--persist opt-in).
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
    if console:
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
                      "✅" if p.validated else "—")
    console.print(table)


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
    smoke = CpuSmoke(reader=None, mock=mock, oc_dir=_path(oc_dir))
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
            console.print(f"[yellow]⚠️ {e}[/]")
            console.print("[dim]La CLI classica (buo oc) resta pienamente "
                          "funzionante.[/]")
        else:
            click.echo(f"⚠️ {e}", err=True)
        sys.exit(1)
