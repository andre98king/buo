#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
GTT Tuning — aumento del limite GTT (memoria GPU accessibile).

Dallo studio (messaggio 94): Vulkan vede ~10GB di 12GB e il driver
amdgpu limita il GTT a ~7.4 GiB. La soluzione è alzare
`ttm.pages_limit` (e `ttm.page_pool_size`) via modprobe.

Valore consigliato dalla community: ttm.pages_limit=3959290 (~15 GiB)
oppure 4194304 per 16 GiB.
"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from ..utils.logging import LoggerMixin
from ..utils.shell import run_command

GTT_LIMIT_DEFAULT = 3959290
GTT_CONF = "/etc/modprobe.d/buo-gtt.conf"


class GTTTuning(LoggerMixin):
    """Aumenta il limite GTT della GPU."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 pages_limit: int = GTT_LIMIT_DEFAULT):
        self.mock = mock
        self.mock_hw = mock_hardware
        self.pages_limit = pages_limit

    def verify(self) -> bool:
        """True se la configurazione GTT risulta presente."""
        if self.mock and self.mock_hw is not None:
            return True  # mock: assumiamo applicato se richiesto
        try:
            with open("/proc/cmdline") as f:
                return "ttm.pages_limit" in f.read()
        except Exception:
            return os.path.exists(GTT_CONF)

    def apply(self) -> Dict[str, Any]:
        """Scrive /etc/modprobe.d/buo-gtt.conf con il nuovo limite."""
        if self.mock and self.mock_hw is not None:
            return {"applied": True, "pages_limit": self.pages_limit,
                    "needs_reboot": True}

        content = (
            "# BUO GTT tuning — aumenta la memoria GPU accessibile\n"
            f"options ttm pages_limit={self.pages_limit}\n"
            f"options ttm page_pool_size={self.pages_limit}\n"
        )
        try:
            Path("/etc/modprobe.d").mkdir(parents=True, exist_ok=True)
            # Scrittura diretta (BUO gira da root); se i permessi non lo
            # consentono si passa da `install` con sudo (nessuna shell).
            try:
                Path(GTT_CONF).write_text(content, encoding="utf-8")
            except OSError:
                tmpdir = tempfile.mkdtemp(prefix="buo-gtt-")
                try:
                    src = Path(tmpdir) / "buo-gtt.conf"
                    src.write_text(content, encoding="utf-8")
                    rc, _, err = run_command(
                        ["install", "-m", "644", str(src), GTT_CONF],
                        sudo=True)
                    if rc != 0:
                        return {"applied": False, "error": err}
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            return {"applied": True, "pages_limit": self.pages_limit,
                    "needs_reboot": True}
        except Exception as e:
            return {"applied": False, "error": str(e)}

    def rollback(self) -> bool:
        """Rimuove il file modprobe."""
        if os.path.exists(GTT_CONF):
            rc, _, _ = run_command(["rm", "-f", GTT_CONF], sudo=True)
            return rc == 0
        return True
