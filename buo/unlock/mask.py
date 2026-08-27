#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
CU Mask — maschera selettiva delle WGP/CU difettose.

Dopo il health test, le WGP difettose vengono mascherate via
`disable_cu=` nel modprobe (bc250-cu-mask.sh). Ogni WGP difettosa
maschera 2 CU.
"""

from typing import Any, Dict, List, Optional

from ..utils.logging import LoggerMixin
from .wrappers.bc250_mask import BC250MaskWrapper


class CUMask(LoggerMixin):
    """Generazione e installazione della maschera CU."""

    def __init__(self, mock: bool = False, mock_hardware=None):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.wrapper = BC250MaskWrapper() if not mock else None

    def apply(self, defective_cu: Optional[List[int]] = None,
              results_file: Optional[str] = None) -> Dict[str, Any]:
        """
        Applica la maschera per le CU difettose.

        Args:
            defective_cu: lista di CU difettose (dal health test)
            results_file: in alternativa, percorso a results.tsv
        """
        if not defective_cu and not results_file:
            return {"applied": True, "mask": None, "note": "nessuna CU da mascherare"}

        if self.mock and self.mock_hw is not None:
            return {"applied": True, "mask": self.mock_hw.get_wgp_mask()}

        if self.wrapper is None or not self.wrapper.available:
            return {"applied": False, "error": "bc250-cu-mask.sh non trovato"}

        result = self.wrapper.generate(
            results_file=results_file,
            bad_cu=defective_cu,
            install=True,
        )
        return {
            "applied": result["returncode"] == 0,
            "mask": result.get("parsed_output", {}).get("mask"),
            "usable_cus": result.get("parsed_output", {}).get("usable_cus"),
            "stderr": result.get("stderr", ""),
        }

    def rollback(self) -> bool:
        """Rimuove il file della maschera e ricostruisce l'initramfs."""
        import os
        from ..utils.distro import detect_distro
        from ..utils.shell import run_command

        if self.mock and self.mock_hw is not None:
            return True

        mask_file = "/etc/modprobe.d/bc250-40cu-selective-mask.conf"
        removed = False
        if os.path.exists(mask_file):
            rc, _, _ = run_command(["rm", "-f", mask_file], sudo=True)
            removed = rc == 0

        distro = detect_distro()
        distro.rebuild_initramfs()
        return removed
