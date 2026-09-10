#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
GTT / VRAM dinamica — tetto via KARG (meccanismo documentato).

Fonte: doc ufficiale BC-250 (elektricM/amd-bc250-docs, docs/bios/vram.md).
Con split VRAM 512MB il massimo VRAM dinamica e' 8.25 GB (4GB → 10,
6GB → 11, 8GB → 12): i giochi configurati per >=8 GB superano il tetto e
il driver display crasha — il doc lo indica come *the primary reason for
games crashing with the 512MB split*. Fix mantenendo lo split a 512MB
(RAM quasi tutta disponibile, VRAM dinamica fino a 12 GB):

    rpm-ostree kargs --delete=ttm.pages_limit --append=ttm.pages_limit=3014656

Su Bazzite la transazione gira come unità systemd staccata (mai uccisa a
metà commit). MAI modprobe.d: su ostree non entra nell'initramfs (fix
inerte — bug di campo 10/09/2026) e non è il meccanismo documentato.
"""

import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from ..state.ostree import _run_ostree_txn
from ..utils.distro import detect_distro
from ..utils.logging import LoggerMixin
from ..utils.shell import run_command

# 12 GB di VRAM dinamica con split 512MB (valore del doc ufficiale BC-250).
GTT_LIMIT_DEFAULT = 3014656
GTT_KARG = "ttm.pages_limit"
# Path del parametro EFFETTIVO a runtime e del cmdline (fallback di verifica).
GTT_PARAM_PATH = "/sys/module/ttm/parameters/pages_limit"
CMDLINE_PATH = "/proc/cmdline"
# Conf del vecchio meccanismo (modprobe.d): non serve più, va rimosso.
GTT_CONF = "/etc/modprobe.d/buo-gtt.conf"

CmdRunner = Callable[..., Tuple[int, str, str]]


class GTTTuning(LoggerMixin):
    """Alza il tetto di VRAM dinamica con il karg documentato."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 pages_limit: int = GTT_LIMIT_DEFAULT,
                 param_path: Optional[str] = None,
                 cmdline_path: Optional[str] = None,
                 ostree_runner: Optional[CmdRunner] = None):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.pages_limit = pages_limit
        self.param_path = (Path(param_path) if param_path
                           else Path(GTT_PARAM_PATH))
        self.cmdline_path = (Path(cmdline_path) if cmdline_path
                             else Path(CMDLINE_PATH))
        self._run = ostree_runner or _run_ostree_txn

    # ------------------------------------------------------------------ #

    def _karg(self) -> str:
        return f"{GTT_KARG}={self.pages_limit}"

    def _runtime(self) -> Optional[int]:
        try:
            return int(self.param_path.read_text().strip())
        except (OSError, ValueError):
            return None

    def verify(self) -> bool:
        """True se il tetto è EFFETTIVO.

        Priorità all'effetto: `ttm.pages_limit` a runtime ≥ richiesto. Se il
        parametro non è leggibile (modulo non caricato) si accetta la
        presenza del karg nel cmdline — unica evidenza disponibile.
        """
        if self.mock and self.mock_hw is not None:
            return True
        runtime = self._runtime()
        if runtime is not None:
            return runtime >= self.pages_limit
        try:
            return self._karg() in self.cmdline_path.read_text()
        except OSError:
            return False

    @staticmethod
    def _is_ostree() -> bool:
        try:
            return detect_distro().initramfs_tool == "ostree"
        except Exception:
            return False

    def _current_kargs(self) -> Optional[str]:
        rc, out, _err = self._run(["rpm-ostree", "kargs"],
                                  "buo-gtt-kargs-read", 60)
        return out if rc == 0 else None

    def _cleanup_legacy_conf(self) -> None:
        """Rimuove il conf modprobe.d del meccanismo vecchio (inerte)."""
        if os.path.exists(GTT_CONF):
            run_command(["rm", "-f", GTT_CONF], sudo=True, check=False)

    def apply(self) -> Dict[str, Any]:
        """Imposta il karg `ttm.pages_limit` (idempotente, fail-honest).

        Mai `applied: True` senza transazione riuscita: se i kargs non si
        possono scrivere si ritorna False con warning.
        """
        if self.mock and self.mock_hw is not None:
            return {"applied": True, "pages_limit": self.pages_limit,
                    "needs_reboot": True}
        if not self._is_ostree():
            return {
                "applied": False, "needs_reboot": False,
                "warning": ("sistema non ostree: aggiungi "
                            f"`{self._karg()}` alla cmdline del bootloader "
                            "(meccanismo documentato BC-250)"),
            }

        current = self._current_kargs()
        if current is None:
            return {"applied": False, "needs_reboot": True,
                    "error": "rpm-ostree kargs non leggibile"}
        if self._karg() in current:
            self._cleanup_legacy_conf()
            # già configurato: attivo solo se il runtime lo conferma
            return {"applied": True, "already": True,
                    "pages_limit": self.pages_limit,
                    "needs_reboot": not self.verify()}

        args = ["rpm-ostree", "kargs"]
        if GTT_KARG in current:
            args.append(f"--delete={GTT_KARG}")
        args.append(f"--append={self._karg()}")
        rc, _out, err = self._run(args, "buo-gtt-kargs", 600)
        if rc != 0:
            return {
                "applied": False, "needs_reboot": True, "error": err[:200],
                "warning": ("kargs NON scritti (rpm-ostree rc=%d): tetto VRAM "
                            "dinamica invariato — %s" % (rc, err.strip()[:120])),
            }
        self._cleanup_legacy_conf()
        return {"applied": True, "pages_limit": self.pages_limit,
                "needs_reboot": True}

    def rollback(self) -> bool:
        """Rimuove il karg (e il vecchio conf modprobe.d)."""
        if self.mock and self.mock_hw is not None:
            return True
        ok = True
        try:
            current = self._current_kargs()
            if current and GTT_KARG in current:
                rc, _o, _e = self._run(
                    ["rpm-ostree", "kargs", f"--delete={GTT_KARG}"],
                    "buo-gtt-kargs-rollback", 600)
                ok = rc == 0
            self._cleanup_legacy_conf()
        except Exception:
            ok = False
        return ok
