#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
OstreeDeploymentManager — rilevamento del deployment bootato e swap del
default di boot (design: research/DESIGN_OSTREE_REBOOT.md, D1–D8).

Problema: su ostree image-mode (Bazzite) `systemctl reboot` boota SEMPRE
il deployment di DEFAULT (indice 0 dell'ordine di boot), non quello
corrente. Una run di BUO lanciata dal deployment NON-default (es. quello
di backup) che programma reboot (unlock/fix/CU health test) atterra sul
default → il resume service vive nel /etc del deployment di lancio → run
orfana.

Soluzione: a inizio run (EAGER, D1) BUO verifica da quale deployment è
partito il boot (parsing di `/proc/cmdline`, D2) e, se non è il default,
fa `rpm-ostree rollback` (flip deterministico SOLO con esattamente 2
deployment attivi) così il default diventa il deployment corrente. A fine
run (ogni path di uscita) ripristina il default originale, marker-guarded
(D3/D4/D8): mai rollback alla cieca.

FINDING DI CAMPO (03/09, image-mode): il token cmdline è
`ostree=/ostree/boot.N/<os>/<basekey>/<IDX>` — il penultimo componente è
la boot-dir/base key dell'immagine, CONDIVISA tra i deployment (la riga
`linux /ostree/default-<basekey>/vmlinuz` è identica in entrambe le boot
entry), NON un checksum di deployment. L'identificazione del deployment
bootato (checksum + default-ness) viene SOLO da `rpm-ostree status
--json` (`booted: true`); l'IDX cmdline resta come sanity pre-swap
opzionale. Mai confrontare hash cmdline con checksum status.

In mock/dry-run TUTTI i metodi sono no-op inerti: mai letture di
/proc/cmdline, mai rpm-ostree, mai warning.
"""

import json
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from ..exceptions import TimeoutError
from ..utils.logging import LoggerMixin
from ..utils.paths import log_dir
from ..utils.shell import run_command

# (rc, stdout, stderr) di un comando esterno — runner iniettabile nei test
# (regola C1: MAI rpm-ostree/subprocess reali nei test).
CmdRunner = Callable[[List[str]], Tuple[int, str, str]]

# Deadline della transazione staccata (regola AGENTS.md: una txn rpm-ostree
# non va MAI cancellata a metà commit; kargs reale ha impiegato >60s). È il
# timeout del CLIENT systemd-run: allo scadere il client muore ma l'unità
# systemd (e quindi la txn) resta viva e il daemon completa da solo.
OSTREE_TXN_TIMEOUT = 600   # s


@dataclass(frozen=True)
class OstreeBootState:
    """Esito del rilevamento del deployment bootato.

    `booted_checksum` e `is_default_booted` sono AUTOREVOLI solo nello
    stato ritornato da `detect_boot()`: vengono risolti da `rpm-ostree
    status --json` (`booted: true`), l'unica identificazione affidabile
    (finding di campo: il hash nel path cmdline su image-mode è la
    boot-dir/base key condivisa, non un checksum di deployment).
    `parse_cmdline()` stima `is_default_booted` dall'index cmdline
    (entry 0) solo per uso cmdline-only, mai decisionale."""
    is_ostree: bool                 # cmdline contiene ostree= parsabile
    booted_index: Optional[int]     # index cmdline — SOLO sanity pre-swap
    booted_checksum: Optional[str]  # checksum full-64 del booted (da status)
    is_default_booted: bool         # non-ostree (inerte) o posizione booted == 0


@dataclass(frozen=True)
class DeploymentInfo:
    """Un deployment attivo nell'ordine di boot (rpm-ostree status)."""
    index: int          # posizione nell'ordine di boot (0 = default)
    checksum: str
    booted: bool


def _run_ostree_txn(cmd: List[str], unit_name: str,
                    timeout: int = OSTREE_TXN_TIMEOUT
                    ) -> Tuple[int, str, str]:
    """Esegue una transazione rpm-ostree come unità systemd staccata e ne
    attende l'esito con `systemd-run --wait --collect`: l'exit code
    dell'unità È quello del client (niente poll da sbagliare — un rollback
    fallito non può mai diventare un falso successo).

    REGOLA DI CAMPO (AGENTS.md): MAI cancellare/uccidere una transazione
    rpm-ostree a metà commit. Il timeout è del CLIENT systemd-run (via
    run_command): allo scadere muore solo il client, l'unità e la txn
    restano vive e il daemon completa da solo → si ritorna rc=124 e il run
    successivo verifica/ritenta. `--collect` rimuove l'unità a fine esito.
    """
    log_file = log_dir() / "ostree-txn.log"
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    launch = [
        "systemd-run", "--wait", "--collect", f"--unit={unit_name}",
        f"--property=StandardOutput=append:{log_file}",
        f"--property=StandardError=append:{log_file}",
    ] + cmd
    try:
        rc, out, err = run_command(launch, sudo=True, timeout=timeout,
                                   check=False)
    except TimeoutError:
        return 124, "", (
            f"timeout txn rpm-ostree — unità {unit_name} ancora attiva: "
            "non ucciderla, il daemon completerà da solo")
    return rc, out, err


class OstreeDeploymentManager(LoggerMixin):
    """Rilevamento deployment + swap/restore del default di boot."""

    def __init__(self,
                 mock: bool = False,
                 dry_run: bool = False,
                 cmdline_reader: Optional[Callable[[], str]] = None,
                 status_runner: Optional[CmdRunner] = None,
                 txn_runner: Optional[CmdRunner] = None) -> None:
        # mock/dry-run → manager inerte: ogni metodo è no-op, nessun
        # runner viene mai chiamato (inerzia per costruzione).
        self._inert = mock or dry_run
        self._cmdline_reader = cmdline_reader or self._read_proc_cmdline
        # rpm-ostree status --json è READ-ONLY (nessun rischio di txn).
        self._status_runner = status_runner or partial(
            run_command, sudo=True, timeout=60)
        # Le txN (rollback) vanno staccate via systemd-run --wait, MAI
        # cancellate (AGENTS.md); iniettabile nei test.
        self._txn_runner = txn_runner or self._spawn_txn

    @staticmethod
    def _read_proc_cmdline() -> str:
        return Path("/proc/cmdline").read_text(encoding="utf-8",
                                               errors="replace")

    @staticmethod
    def _spawn_txn(cmd: List[str]) -> Tuple[int, str, str]:
        # Millisecondi: uno swap e il restore dello stesso run (es. decline
        # immediato) NON devono generare lo stesso nome di unità.
        return _run_ostree_txn(cmd, f"buo-ostree-{int(time.time() * 1000)}")

    # ------------------------------------------------------------------ #
    # Rilevamento (read-only, MAI spawna rpm-ostree)
    # ------------------------------------------------------------------ #

    def detect_boot(self) -> OstreeBootState:
        """Rileva il deployment bootato: token ostree= dal cmdline (D2) e
        IDENTITÀ del booted da `rpm-ostree status --json` (`booted: true`).

        Il flag `booted` dello status è l'unica fonte affidabile (finding
        di campo: su image-mode il hash cmdline è la base key condivisa).
        `is_default_booted` = posizione del booted == 0. Status illeggibile
        o nessun booted → stato ignoto (checksum None, non-default):
        fail-closed, il guard abortisce con messaggio chiaro."""
        if self._inert:
            return OstreeBootState(False, None, None, True)
        try:
            text = self._cmdline_reader()
        except Exception as e:  # /proc illeggibile → non-ostree (inerte)
            self.logger.warning(
                "cmdline non leggibile (%s): nessun rilevamento ostree — "
                "run trattata come non-ostree", e)
            return OstreeBootState(False, None, None, True)
        parsed = self.parse_cmdline(text or "")
        if not parsed.is_ostree:
            return parsed
        for pos, d in enumerate(self.read_deployments() or []):
            if d.booted:
                return OstreeBootState(True, parsed.booted_index,
                                       d.checksum, pos == 0)
        return OstreeBootState(True, parsed.booted_index, None, False)

    @staticmethod
    def parse_cmdline(text: str) -> OstreeBootState:
        """Parsing del token `ostree=` dal cmdline (D2, cmdline-only).

        Formato reale su image-mode (finding 03/09):
        `ostree=/ostree/boot.N/<os>/<basekey>/<IDX>` — il penultimo
        componente è la boot-dir/base key dell'immagine, CONDIVISA tra i
        deployment: NON è MAI un checksum di deployment (i checksum reali
        sono i nomi delle deploy dir, visibili solo in `rpm-ostree
        status`). Qui si estraggono solo:
        1. prima riga, split per spazi;
        2. token con prefisso `ostree=` assente → non-ostree (inerte);
        3. ultimo componente non vuoto = IDX (entry bootloader: coincide
           con la posizione del booted SOLO pre-swap — sanity opzionale);
        4. IDX non numerico → booted_index=None (ignoto → nessuna sanity).
        `booted_checksum` resta None: mai derivato dal cmdline.
        """
        first_line = (text.splitlines()[0] if text.strip() else "")
        token = None
        for tok in first_line.split():
            if tok.startswith("ostree="):
                token = tok
                break
        if token is None:
            return OstreeBootState(False, None, None, True)
        value = token.split("=", 1)[1]
        parts = [p for p in value.split("/") if p]
        if not parts:
            return OstreeBootState(True, None, None, False)
        index: Optional[int] = None
        try:
            index = int(parts[-1])
        except (TypeError, ValueError):
            index = None
        # stima cmdline-only: entry 0 = default (mai decisionale)
        return OstreeBootState(True, index, None, index == 0)

    # ------------------------------------------------------------------ #
    # Stato deployment (rpm-ostree status --json, read-only)
    # ------------------------------------------------------------------ #

    def read_deployments(self) -> Optional[List[DeploymentInfo]]:
        """Deployment attivi in ordine di boot; None su errore/malformato
        (fail-closed: mai decisioni su uno stato illeggibile)."""
        if self._inert:
            return None
        try:
            rc, out, _ = self._status_runner(
                ["rpm-ostree", "status", "--json"])
        except Exception:
            return None
        if rc != 0 or not out:
            return None
        try:
            data = json.loads(out)
        except (ValueError, TypeError):
            return None
        items = data.get("deployments") if isinstance(data, dict) else None
        if not isinstance(items, list) or not items:
            return None
        deployments: List[DeploymentInfo] = []
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                return None
            checksum = item.get("checksum") or item.get("commit")
            if not isinstance(checksum, str) or not checksum:
                return None  # voce malformata → intero stato inaffidabile
            deployments.append(DeploymentInfo(
                index=idx, checksum=checksum,
                booted=bool(item.get("booted", False))))
        return deployments

    def current_default_checksum(self) -> Optional[str]:
        """Checksum del default attuale (primo dell'ordine di boot)."""
        deps = self.read_deployments()
        if not deps:
            return None
        return deps[0].checksum

    # ------------------------------------------------------------------ #
    # Guardia fail-closed (nessun effetto collaterale)
    # ------------------------------------------------------------------ #

    def verify_swap_preconditions(self, state: OstreeBootState
                                  ) -> Tuple[bool, str]:
        """True solo se lo swap è sicuro: esattamente 2 deployment attivi e
        esattamente uno `booted` (identificato dallo status — campo
        autorevole; nessun checksum cmdline esiste: il hash cmdline è la
        base key, finding di campo). La coerenza cmdline↔status è la
        sanity pre-swap dell'orchestratore (index cmdline, solo senza
        marcatore di swap)."""
        if not state.is_ostree:
            return True, ""  # non-ostree: niente da verificare
        if self._inert:
            return False, "manager inattivo (mock/dry-run)"
        deps = self.read_deployments()
        if deps is None:
            return False, ("stato deployment illeggibile "
                           "(rpm-ostree status --json)")
        if len(deps) != 2:
            return False, (f"{len(deps)} deployment attivi "
                           "(servono esattamente 2)")
        booted = [d for d in deps if d.booted]
        if len(booted) != 1:
            return False, (f"{len(booted)} deployment marcati 'booted' "
                           "(serve esattamente 1)")
        return True, ""

    # ------------------------------------------------------------------ #
    # Operazioni (rpm-ostree rollback via txn_runner iniettabile)
    # ------------------------------------------------------------------ #

    def swap_default(self) -> Tuple[Optional[int], str, str]:
        """Flip del default sul deployment corrente (booted): il prossimo
        reboot atterra qui. Stesso comando del restore: rollback."""
        if self._inert:
            return 0, "", ""
        return self._run_rollback()

    def restore_default(self) -> Tuple[Optional[int], str, str]:
        """Ripristina il default precedente (secondo rollback)."""
        if self._inert:
            return 0, "", ""
        return self._run_rollback()

    def _run_rollback(self) -> Tuple[Optional[int], str, str]:
        try:
            rc, out, err = self._txn_runner(["rpm-ostree", "rollback"])
        except Exception as e:  # runner rotto → fail-closed, mai fingere ok
            return 1, "", str(e)
        # rc=None da runner rotto NON è un successo: propagato com'è
        # (None != 0 → i chiamanti lo trattano come fallimento).
        return rc, out or "", err or ""
