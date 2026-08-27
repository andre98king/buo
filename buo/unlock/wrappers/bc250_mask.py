#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Wrapper per bc250-cu-mask.sh — maschera selettiva delle WGP difettose.

Analisi dal codice sorgente (confermata nello studio):
    • input: --results <tsv> | --bad <csv wgp> | --bad-cu <csv cu> | --install
    • ogni WGP difettoso maschera 2 CU (wgp*2 e wgp*2+1)
    • output: "options amdgpu bc250_cc_write_mode=3 disable_cu=..."
    • installa in /etc/modprobe.d/bc250-40cu-selective-mask.conf
"""

import re
from typing import Any, Dict, List, Optional

from .base import BaseWrapper


class BC250MaskWrapper(BaseWrapper):
    """Wrapper per la generazione/installazione della maschera CU."""

    def __init__(self, script_path: str = "/usr/local/bin/bc250-cu-mask.sh"):
        super().__init__(script_path, timeout=30)

    def generate(self, results_file: Optional[str] = None,
                 bad_wgp: Optional[List[str]] = None,
                 bad_cu: Optional[List[int]] = None,
                 install: bool = False) -> Dict[str, Any]:
        """
        Genera (e opzionalmente installa) la maschera.

        Args:
            results_file: percorso a results.tsv
            bad_wgp: lista di WGP difettose (es. ["0.0.1"])
            bad_cu: lista di CU difettose (es. [2, 3]) — convertite in WGP
            install: copia in /etc/modprobe.d/ + rebuild initramfs
        """
        args: List[str] = []

        if results_file:
            args += ["--results", results_file]
        if bad_wgp:
            args += ["--bad", ",".join(bad_wgp)]
        if bad_cu:
            # Ogni WGP contiene 2 CU: wgp*2 e wgp*2+1
            wgp_set = sorted({cu // 2 for cu in bad_cu})
            wgp_str = ",".join(
                f"{wgp // 4}.{(wgp % 4) // 2}.{wgp % 2}" for wgp in wgp_set
            )
            args += ["--bad", wgp_str]
        if install:
            args.append("--install")

        return self.run_with_output(args)

    def parse_output(self, stdout: str, stderr: str) -> Dict[str, Any]:
        parsed: Dict[str, Any] = {
            "mask": None,
            "usable_cus": None,
            "total_cus": None,
            "installed": False,
        }
        m = re.search(r"disable_cu=([0-9.,]+)", stdout)
        if m:
            parsed["mask"] = m.group(1)
        m = re.search(r"Usable after mask:\s*(\d+)/(\d+)\s*CUs", stdout)
        if m:
            parsed["usable_cus"] = int(m.group(1))
            parsed["total_cus"] = int(m.group(2))
        if "Installed" in stdout or "install" in stdout.lower():
            parsed["installed"] = True
        return parsed
