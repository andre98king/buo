#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Rilevazione della distribuzione Linux e dei metodi di initramfs.

Importante per BUO perché le procedure (ACPI fix, rebuild initramfs,
installazione dei driver) variano tra distro.
"""

import os
from typing import Optional


class DistroInfo:
    """Informazioni sulla distribuzione rilevata."""

    def __init__(self) -> None:
        self.id: str = self._detect_id()
        self.name: str = self._detect_name()
        self.initramfs_tool: str = self._detect_initramfs()
        self.pkg_manager: str = self._detect_pkg_manager()

    # ------------------------------------------------------------------ #

    def _detect_id(self) -> str:
        if os.path.exists("/etc/fedora-release"):
            if os.path.exists("/run/ostree-booted"):
                return "bazzite"
            return "fedora"
        if os.path.exists("/etc/arch-release"):
            return "arch"
        if os.path.exists("/etc/debian_version"):
            return "debian"
        if os.path.exists("/run/ostree-booted"):
            return "ostree"
        if os.path.exists("/etc/os-release"):
            try:
                with open("/etc/os-release") as f:
                    for line in f:
                        if line.startswith("ID="):
                            return line.strip().split("=", 1)[1].strip('"')
            except Exception:
                pass
        return "unknown"

    def _detect_name(self) -> str:
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.strip().split("=", 1)[1].strip('"')
        except Exception:
            pass
        return self.id

    def _detect_initramfs(self) -> str:
        # Bazzite/SteamOS sono ostree-based: l'ACPI fix usa initrd override (cpio)
        if self.id == "bazzite" or self.id == "ostree":
            return "ostree"
        if self.id == "fedora":
            return "dracut"
        if self.id == "arch":
            return "mkinitcpio"
        if self.id == "debian":
            return "initramfs-tools"
        return "unknown"

    def _detect_pkg_manager(self) -> str:
        if self.id in ("fedora", "bazzite"):
            return "dnf" if self.id == "fedora" else "rpm-ostree"
        if self.id == "arch":
            return "pacman"
        if self.id == "debian":
            return "apt"
        return "unknown"

    # ------------------------------------------------------------------ #

    @property
    def is_supported(self) -> bool:
        """Le distro supportate da BUO (dal design finale)."""
        return self.id in ("fedora", "bazzite", "arch", "debian", "ostree")

    def rebuild_initramfs(self, sudo: bool = True) -> tuple:
        """Esegue il rebuild dell'initramfs per la distro corrente.

        Returns:
            (returncode, stdout, stderr) — (0, '', '') se non supportato.
        """
        from .shell import run_command

        if self.initramfs_tool == "dracut":
            return run_command(["dracut", "-f"], sudo=sudo, timeout=300)
        if self.initramfs_tool == "mkinitcpio":
            return run_command(["mkinitcpio", "-P"], sudo=sudo, timeout=300)
        if self.initramfs_tool == "initramfs-tools":
            return run_command(["update-initramfs", "-u"], sudo=sudo, timeout=300)
        return (0, "", "")

    def install_package(self, package: str, sudo: bool = True) -> tuple:
        """Installa un pacchetto con il package manager della distro."""
        from .shell import run_command

        if self.pkg_manager == "dnf":
            return run_command(["dnf", "install", "-y", package], sudo=sudo, timeout=600)
        if self.pkg_manager == "pacman":
            return run_command(["pacman", "-S", "--noconfirm", package],
                               sudo=sudo, timeout=600)
        if self.pkg_manager == "apt":
            return run_command(["apt-get", "install", "-y", package],
                               sudo=sudo, timeout=600)
        return (127, "", f"nessun package manager per {self.id}")

    def __str__(self) -> str:
        return f"{self.name} ({self.id}) — initramfs: {self.initramfs_tool}"


def detect_distro() -> DistroInfo:
    """Rileva e restituisce la distribuzione corrente."""
    return DistroInfo()
