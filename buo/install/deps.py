#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Dependency Manager — scarica e installa i tool della community.

BUO non reimplementa gli script della community: li ORCHESTRA. Questo
modulo li scarica automaticamente (clone shallow) e li installa nelle
posizioni attese:

    • bc250_smu_oc          → bc250-detect, bc250-apply   (undervolt CPU)
    • bc250-40cu-unlock     → bc250-enable-40cu.sh, health, mask  (GPU)
    • bc250-acpi-fix        → SSDT-CST.aml               (ACPI C-State)
    • cyan-skillfish-governor → solo istruzioni (servizio distro-specifico)

SICUREZZA:
    • clona SOLO repo note e fisse (nessun codice arbitrario)
    • copia SOLO gli script (nessun installer di terze parti eseguito)
    • se il download/installazione fallisce → stato "failed" chiaro;
      l'orchestratore resta fail-closed (non procede senza i tool)
"""

import os
import stat
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.logging import LoggerMixin
from ..utils.paths import deps_dir
from ..utils.shell import run_command, which

# Binari attesi (destinazioni)
BIN_DIR_SYSTEM = "/usr/local/bin"

# ---------------------------------------------------------------------------
# Catalogo delle dipendenze (repo + file da installare)
# ---------------------------------------------------------------------------

DEPS: List[Dict[str, Any]] = [
    {
        "name": "bc250_smu_oc",
        "repo": "https://github.com/bc250-collective/bc250_smu_oc",
        "type": "scripts",
        "required_for": "undervolt CPU (fail-closed senza questo tool)",
        "files": [
            {"src": "bc250_detect.py", "dest": "bc250-detect", "exec": True},
            {"src": "bc250_apply.py", "dest": "bc250-apply", "exec": True},
        ],
    },
    {
        "name": "bc250-40cu-unlock",
        "repo": "https://github.com/duggasco/bc250-40cu-unlock",
        "type": "scripts",
        "required_for": "unlock GPU 40-CU, health test, maschera",
        "files": [
            {"src": "scripts/bc250-enable-40cu.sh",
             "dest": "bc250-enable-40cu.sh", "exec": True},
            {"src": "scripts/bc250-cu-health-test.sh",
             "dest": "bc250-cu-health-test.sh", "exec": True},
            {"src": "scripts/bc250-cu-mask.sh",
             "dest": "bc250-cu-mask.sh", "exec": True},
        ],
    },
    {
        "name": "bc250-acpi-fix",
        "repo": "https://github.com/bc250-collective/bc250-acpi-fix",
        "type": "aml",
        "required_for": "tabelle ACPI C-State (risparmio energetico idle)",
        "files": [{"src": "SSDT-CST.aml", "dest": None, "exec": False}],
    },
    {
        "name": "cyan-skillfish-governor",
        "repo": "https://github.com/filippor/cyan-skillfish-governor",
        "type": "instruct",
        "required_for": "governor GPU dinamico (SMU)",
        "files": [],
        "note": (
            "Servizio distro-specifico: su Fedora/Bazzite usa il COPR o "
            "lo script di evdokim/bazzite-bc-250-governor, su Arch l'AUR "
            "cyan-skillfish-governor-smu. BUO lo clona ma non esegue "
            "installer di terze parti senza conferma."
        ),
    },
]


class DependencyManager(LoggerMixin):
    """Scarica e installa i tool della community."""

    def __init__(self, bin_dir: str = BIN_DIR_SYSTEM):
        self.bin_dir = Path(bin_dir)
        self.base_dir = deps_dir()

    # ------------------------------------------------------------------ #

    def check(self, deps: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Verifica quali dipendenze sono già presenti.

        Returns:
            {name: {"present": bool, "detail": str, ...}}
        """
        result: Dict[str, Any] = {}
        for dep in DEPS:
            if deps and dep["name"] not in deps:
                continue
            result[dep["name"]] = self._check_one(dep)
        return result

    def _check_one(self, dep: Dict[str, Any]) -> Dict[str, Any]:
        checkout = self.base_dir / dep["name"]
        if dep["type"] == "scripts":
            missing = [f["dest"] for f in dep["files"]
                       if not (self.bin_dir / f["dest"]).exists()]
            present = not missing
            return {
                "present": present,
                "type": dep["type"],
                "required_for": dep["required_for"],
                "missing": missing,
                "checkout": str(checkout) if checkout.exists() else None,
            }
        if dep["type"] == "aml":
            aml = checkout / "SSDT-CST.aml"
            return {
                "present": aml.exists(),
                "type": dep["type"],
                "required_for": dep["required_for"],
                "missing": [] if aml.exists() else ["SSDT-CST.aml"],
                "checkout": str(checkout) if checkout.exists() else None,
            }
        # instruct: presente = checkout clonato
        return {
            "present": checkout.exists(),
            "type": dep["type"],
            "required_for": dep["required_for"],
            "missing": [] if checkout.exists() else ["(repo)"],
            "note": dep.get("note", ""),
        }

    # ------------------------------------------------------------------ #

    def install(self, deps: Optional[List[str]] = None,
                sudo: bool = True) -> Dict[str, Any]:
        """
        Clona le repo mancanti e installa gli script.

        Returns:
            {name: {"status": "ok"|"skipped"|"failed", "detail": str}}
        """
        if which("git") is None:
            return {"_error": "git non trovato: installalo con il package "
                              "manager della distro"}

        self.base_dir.mkdir(parents=True, exist_ok=True)
        result: Dict[str, Any] = {}

        for dep in DEPS:
            if deps and dep["name"] not in deps:
                continue

            current = self._check_one(dep)
            if current["present"] and dep["type"] != "instruct":
                result[dep["name"]] = {"status": "skipped",
                                       "detail": "già installata"}
                continue

            result[dep["name"]] = self._install_one(dep, sudo=sudo)

        return result

    def _install_one(self, dep: Dict[str, Any], sudo: bool) -> Dict[str, Any]:
        checkout = self.base_dir / dep["name"]
        try:
            # Clone shallow (solo il ramo default)
            if not checkout.exists():
                rc, out, err = run_command(
                    ["git", "clone", "--depth", "1", dep["repo"], str(checkout)],
                    timeout=300)
                if rc != 0:
                    return {"status": "failed",
                            "detail": f"clone fallito: {err[:200]}"}
            elif not (checkout / ".git").exists():
                return {"status": "failed", "detail": "checkout corrotto"}
        except Exception as e:
            return {"status": "failed", "detail": f"clone in errore: {e}"}

        # Copia gli script nelle destinazioni
        failures = []
        for f in dep["files"]:
            src = checkout / f["src"]
            if not src.exists():
                failures.append(f"{f['src']} non trovato nel checkout")
                continue
            if f["dest"] is None:
                continue  # resta nel checkout (es. .aml)
            dest = self.bin_dir / f["dest"]
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                if self._writable(self.bin_dir):
                    shutil.copy2(src, dest)
                else:
                    tmp = Path("/tmp") / dest.name
                    shutil.copy2(src, tmp)
                    rc, _, err = run_command(
                        ["install", "-m", "755", str(tmp), str(dest)], sudo=sudo)
                    if rc != 0:
                        failures.append(f"{dest}: {err[:120]}")
                if f.get("exec"):
                    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP |
                               stat.S_IXOTH)
            except Exception as e:
                failures.append(f"{f['dest']}: {e}")

        if failures:
            return {"status": "failed", "detail": "; ".join(failures)}

        if dep["type"] == "instruct":
            return {"status": "ok", "detail": dep.get("note", ""),
                    "checkout": str(checkout)}

        return {"status": "ok", "detail": "installata",
                "checkout": str(checkout)}

    @staticmethod
    def _writable(path: Path) -> bool:
        try:
            test = path / ".buo_write_test"
            test.write_text("ok")
            test.unlink()
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #

    def summary(self, status: Dict[str, Any]) -> str:
        """Riepilogo leggibile dello stato delle dipendenze."""
        lines = []
        for name, st in status.items():
            if name == "_error":
                lines.append(f"❌ {st}")
                continue
            if st.get("present"):
                lines.append(f"✅ {name} — presente")
                continue
            icon = {"ok": "✅", "skipped": "✅", "failed": "❌"}.get(
                st.get("status"), "⚠️")
            detail = st.get("detail", "")
            if st.get("type") == "instruct":
                lines.append(f"📖 {name} — {detail or 'istruzioni'}")
            elif st.get("missing"):
                lines.append(f"{icon} {name} — manca: "
                             f"{', '.join(st['missing'])}")
            else:
                lines.append(f"{icon} {name} — {detail or 'non presente'}")
        return "\n".join(lines)
