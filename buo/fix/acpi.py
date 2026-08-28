#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
ACPI Fix — tabelle SSDT per C-State/P-State.

Dallo studio (messaggi 2, 24):
    • NON esistono install.sh/uninstall.sh: l'installazione è manuale
    • repo: bc250-collective/bc250-acpi-fix (SSDT-CST.aml, SSDT-PST.aml)
    • metodi per distro: ostree (cpio), arch (mkinitcpio), fedora (dracut)

AGGIORNAMENTO (ricerca community, elektricM/amd-bc250-docs):
    • SSDT-CST (C-States): abilita C1/C2/C3 idle (confermato)
    • SSDT-PST (P-States): abilita il frequency scaling 800→3200 MHz via
      cpufreq — CONFERMATO FUNZIONANTE su kernel 6.19.8 (in passato era
      ritenuto "doesn't work"; l'informazione è superata).
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from ..utils.distro import detect_distro
from ..utils.logging import LoggerMixin

ACPI_REPO = "https://github.com/bc250-collective/bc250-acpi-fix"
# Nelle versioni più recenti il fix è in bc250-collective/bc250-acpi-fix;
# alcune fork (mendesrr) espongono anche SSDT-PST. BUO usa solo CST.
AML_CST = "SSDT-CST.aml"


class ACPIFix(LoggerMixin):
    """Installa le tabelle ACPI C-State per la BC-250."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 aml_dir: Optional[str] = None):
        self.mock = mock
        self.mock_hw = mock_hardware
        # Default: cartella .aml scaricata da `buo install-deps` (se presente)
        if aml_dir is None:
            from ..utils.paths import deps_dir
            auto = deps_dir() / "bc250-acpi-fix"
            if (auto / AML_CST).exists():
                aml_dir = str(auto)
        self.aml_dir = Path(aml_dir) if aml_dir else None
        self.distro = detect_distro()

    # ------------------------------------------------------------------ #

    def verify(self) -> bool:
        """True se le tabelle C-State risultano caricate."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.state.is_acpi_fixed
        tables = Path("/sys/firmware/acpi/tables")
        if not tables.exists():
            return False
        try:
            return any("CST" in p.name for p in tables.glob("SSDT*"))
        except Exception:
            return False

    def apply(self) -> Dict[str, Any]:
        """Installa le tabelle C-State secondo il metodo della distro."""
        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.apply_acpi_fix()
            return {"applied": ok, "method": "mock", "needs_reboot": True}

        if self.aml_dir is None:
            return {
                "applied": False,
                "needs_reboot": False,
                "warning": (
                    "Tabelle .aml non disponibili. Esegui "
                    "`sudo buo install-deps` per scaricarle "
                    f"da {ACPI_REPO}, oppure passa --acpi-aml <dir>."
                ),
            }

        aml_cst = Path(self.aml_dir) / AML_CST
        if not aml_cst.exists():
            return {"applied": False, "error": f"{AML_CST} non trovato in {self.aml_dir}"}

        if self.distro.initramfs_tool == "dracut":
            return self._install_dracut(aml_cst)
        if self.distro.initramfs_tool == "mkinitcpio":
            return self._install_mkinitcpio(aml_cst)
        if self.distro.initramfs_tool == "initramfs-tools":
            return self._install_cpio(aml_cst)
        if self.distro.initramfs_tool == "ostree":
            # ⚠️ CRITICO (bug trovato sul campo): scrivere SSDT_ACPI.cpio
            # su /boot di un sistema ostree (Bazzite/SteamOS) ha causato
            # un BOOT FAILURE (scheda irraggiungibile). Su ostree l'ACPI
            # fix è MANUALE, con il metodo corretto della community.
            return {
                "applied": False,
                "needs_reboot": False,
                "warning": (
                    "ACPI fix su Bazzite/ostree: NON automatizzabile in "
                    "sicurezza (un cpio scritto su /boot può rompere il "
                    "boot). Metodo manuale della community: consulta "
                    "docs/HARDWARE_SETUP.md oppure i repo bc250-acpi-fix / "
                    "bazzite-bc-250-toolkit."
                ),
            }
        return {"applied": False, "error": f"distro non supportata: {self.distro.id}"}

    # ------------------------- metodi distro ------------------------- #

    def _install_dracut(self, aml: Path) -> Dict[str, Any]:
        acpi_dir = Path("/etc/dracut.conf.d/acpi")
        acpi_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(aml, acpi_dir / aml.name)
        conf = Path("/etc/dracut.conf.d/buo-acpi-override.conf")
        with open(conf, "w") as f:
            f.write(f'install_items+=" {acpi_dir / aml.name} "\n')
        subprocess.run(["dracut", "-f"], check=False)
        return {"applied": True, "method": "dracut", "needs_reboot": True}

    def _install_mkinitcpio(self, aml: Path) -> Dict[str, Any]:
        override = Path("/etc/initcpio/acpi_override")
        override.mkdir(parents=True, exist_ok=True)
        shutil.copy2(aml, override / aml.name)

        conf = Path("/etc/mkinitcpio.conf")
        if conf.exists():
            content = conf.read_text()
            if "acpi_override" not in content:
                conf.write_text(content.replace("HOOKS=(", "HOOKS=(acpi_override "))
        subprocess.run(["mkinitcpio", "-P"], check=False)
        return {"applied": True, "method": "mkinitcpio", "needs_reboot": True}

    def _install_cpio(self, aml: Path) -> Dict[str, Any]:
        # initrd override: kernel/firmware/acpi/... → cpio → /boot
        tmpdir = tempfile.mkdtemp(prefix="buo-acpi-")
        try:
            tmp = Path(tmpdir)
            target = tmp / "kernel/firmware/acpi"
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(aml, target / aml.name)

            cpio = subprocess.run(
                ["find", "kernel", "-type", "f"],
                cwd=str(tmp), capture_output=True, text=True,
            )
            result = subprocess.run(
                ["cpio", "-H", "newc", "--create"],
                cwd=str(tmp), input=cpio.stdout.encode(),
                capture_output=True,
            )
            shutil.copy2(aml, target / aml.name)  # garantisce presenza
            with open("/boot/SSDT_ACPI.cpio", "wb") as f:
                f.write(result.stdout)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return {"applied": True, "method": "cpio", "needs_reboot": True}

    # ---------------------------------------------------------------- #

    def rollback(self) -> bool:
        """Rimuove le tabelle e ricostruisce l'initramfs."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.remove_acpi_fix()

        removed = False
        for path in [
            Path("/etc/initcpio/acpi_override"),
            Path("/etc/dracut.conf.d/acpi"),
            Path("/boot/SSDT_ACPI.cpio"),
        ]:
            if path.exists():
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    removed = True
                except Exception as e:
                    self.logger.error("Errore rimozione %s: %s", path, e)

        self.distro.rebuild_initramfs()
        return removed
