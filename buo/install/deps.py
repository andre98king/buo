#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Dependency Manager — scarica e installa i tool della community.

BUO non reimplementa gli script della community: li ORCHESTRA. Questo
modulo li scarica automaticamente (clone con commit pinnato) e li installa
nelle posizioni attese:

    • bc250_smu_oc          → bc250-detect, bc250-apply   (undervolt CPU)
    • bc250-40cu-unlock     → bc250-enable-40cu.sh, health, mask  (GPU)
    • bc250-acpi-fix        → SSDT-CST.aml               (ACPI C-State)
    • cyan-skillfish-governor → pacchetto distro (COPR/AUR) — installato
      automaticamente col package manager, nessun installer di terze parti
    • umr                   → pacchetto distro (runtime UMR su ostree)

SICUREZZA:
    • clona SOLO repo note e fisse, pinnate a un commit esatto e verificato
      (supply-chain: nessun HEAD mobile, nessun codice arbitrario)
    • copia SOLO gli script (nessun installer di terze parti eseguito)
    • i pacchetti (governor/umr) si installano SOLO dal package manager
      della distro (COPR/AUR ufficiali), mai da script
    • se il download/installazione fallisce → stato "failed" chiaro;
      l'orchestratore resta fail-closed (non procede senza i tool)
"""

import os
import shlex
import stat
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils.logging import LoggerMixin
from ..utils.paths import deps_dir
from ..utils.shell import run_command, which

# Binari attesi (destinazioni)
BIN_DIR_SYSTEM = "/usr/local/bin"


def _distro_id() -> str:
    """id distro (fedora/bazzite/arch/debian...) con fallback robusto."""
    try:
        from ..utils.distro import detect_distro
        d = detect_distro()
        return getattr(d, "id", "") or ""
    except Exception:
        return ""


def _40cu_files() -> List[Dict[str, Any]]:
    """File dello script 40-CU corretti per la distro.

    Bug sul campo (28/08/2026): la variante GENERICA di
    bc250-enable-40cu.sh è Debian-oriented (usa apt per i sorgenti kernel)
    e fallisce su Fedora/Bazzite. Su Fedora serve la variante -fedora
    (build contro kernel-devel). Su OSTREE il kernel patch NON funziona
    (/usr read-only): le 40 CU vanno via runtime UMR (bc250-cu-live-manager).
    """
    fedora_like = _distro_id() in ("fedora", "bazzite", "rhel", "centos")
    enable_src = ("scripts/bc250-enable-40cu-fedora.sh" if fedora_like
                  else "scripts/bc250-enable-40cu.sh")
    return [
        {"src": enable_src, "dest": "bc250-enable-40cu.sh", "exec": True},
        {"src": "scripts/bc250-cu-health-test.sh",
         "dest": "bc250-cu-health-test.sh", "exec": True},
        {"src": "scripts/bc250-cu-mask.sh",
         "dest": "bc250-cu-mask.sh", "exec": True},
        {"src": "scripts/bc250-compute-verify.sh",
         "dest": "bc250-compute-verify.sh", "exec": True},
    ]


def _build_deps() -> List[Dict[str, Any]]:
    """Catalogo delle dipendenze (con selezione distro-aware per la 40-CU).

    Tipi:
        scripts   — clone repo + copia script in /usr/local/bin
        aml       — clone repo + tabelle ACPI (restano nel checkout)
        package   — installazione con il package manager della distro
                    (governor, umr): nessun clone, nessun installer di
                    terze parti — pacchetto ufficiale della distro/COPR/AUR
    """
    return [
        {
            "name": "bc250_smu_oc",
            "repo": "https://github.com/bc250-collective/bc250_smu_oc",
            "type": "scripts",
            "commit": "43d6b4c6e38c57bc9ec8908c44675ce7d5fd3d2f",
            "required_for": "undervolt CPU (fail-closed senza questo tool)",
            "files": [
                {"src": "bc250_detect.py", "dest": "bc250-detect", "exec": True},
                {"src": "bc250_apply.py", "dest": "bc250-apply", "exec": True},
                # bc250_detect importa stress_helper e la libreria bc250_smu:
                # senza di loro lo script fallisce subito (bug sul campo)
                {"src": "bc250_limits.py", "dest": "bc250_limits.py", "exec": False},
                {"src": "stress_helper.py", "dest": "stress_helper.py", "exec": False},
            ],
            "copy_dirs": [
                {"src": "bc250_smu", "dest": "bc250_smu"},
            ],
        },
        {
            "name": "bc250-40cu-unlock",
            "repo": "https://github.com/duggasco/bc250-40cu-unlock",
            "type": "scripts",
            "commit": "ae7c30c78e253a5e2c6af0e9c090f807b825191c",
            "required_for": "unlock GPU 40-CU (kernel patch, non-ostree), "
                           "health test, maschera, verifier",
            "files": _40cu_files(),
        },
        {
            "name": "bc250-cu-live-manager",
            "repo": "https://github.com/WinnieLV/bc250-cu-live-manager",
            "type": "scripts",
            "commit": "a929085d791f126ce76a60eb609610820fb08066",
            "required_for": "40-CU runtime UMR su ostree (il kernel patch "
                           "non funziona: /usr read-only). Richiede anche "
                           "umr (rpm-ostree install umr).",
            "files": [
                {"src": "bc250-cu-live-manager.sh",
                 "dest": "bc250-cu-live-manager", "exec": True},
            ],
        },
        {
            "name": "bc250-acpi-fix",
            "repo": "https://github.com/bc250-collective/bc250-acpi-fix",
            "type": "aml",
            "commit": "1594d72f11d674bd7e46f4e51eee4216155e52fb",
            "required_for": "tabelle ACPI C-State (risparmio energetico idle)",
            "files": [{"src": "SSDT-CST.aml", "dest": None, "exec": False}],
        },
        {
            "name": "cyan-skillfish-governor",
            "type": "package",
            "required_for": "governor GPU dinamico (SMU)",
            "check_bins": ["cyan-skillfish-governor-smu"],
            "check_files": [
                "/usr/lib/systemd/system/cyan-skillfish-governor-smu.service",
            ],
            "pkg_map": {
                "fedora": "cyan-skillfish-governor-smu",
                "bazzite": "cyan-skillfish-governor-smu",
                "ostree": "cyan-skillfish-governor-smu",
                "arch": "cyan-skillfish-governor-smu",
            },
            "copr": "filippor/bazzite",  # Fedora/Bazzite (non-ostree: dnf)
            "aur": True,                  # Arch: via yay/paru (AUR)
            "note": (
                "Pacchetto ufficiale: COPR filippor/bazzite su Fedora/Bazzite, "
                "AUR cyan-skillfish-governor-smu su Arch. BUO abilita il repo "
                "e installa da solo (niente installer di terze parti). Su "
                "ostree l'installazione è attiva al prossimo reboot."
            ),
        },
        {
            "name": "umr",
            "type": "package",
            "required_for": "runtime UMR 40-CU su ostree "
                           "(bc250-cu-live-manager legge i registri via umr)",
            "check_bins": ["umr"],
            "pkg_map": {
                "fedora": "umr",
                "bazzite": "umr",
                "ostree": "umr",
            },
            "only_ostree": True,  # non richiesto sul flusso kernel-patch
            "note": (
                "umr (AMD Userspace Register): necessario per le 40 CU via "
                "runtime UMR. Su Bazzite: rpm-ostree install umr (attivo al "
                "prossimo reboot)."
            ),
        },
    ]


DEPS: List[Dict[str, Any]] = _build_deps()


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
            # Anche le directory di supporto (es. libreria bc250_smu)
            for d in dep.get("copy_dirs", []):
                if not (self.bin_dir / d["dest"]).exists():
                    missing.append(f"{d['dest']}/")
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
        if dep["type"] == "package":
            # Presente = binari del pacchetto trovati nel PATH (oppure un
            # file di riferimento, es. l'unit systemd del governor — il
            # binario può avere un nome diverso dal servizio). Su distro
            # dove il pacchetto NON è richiesto (only_ostree su non-ostree),
            # risulta presente (non serve installarlo).
            bins = dep.get("check_bins", [])
            files = dep.get("check_files", [])
            present = any(which(b) is not None for b in bins) or any(
                os.path.exists(f) for f in files)
            if dep.get("only_ostree") and not os.path.exists("/run/ostree-booted"):
                present = True
                bins = []  # non richiesto: nessun binario da segnalare
            missing = [b for b in bins if which(b) is None]
            return {
                "present": present,
                "type": dep["type"],
                "required_for": dep["required_for"],
                "missing": missing,
                "detail": "binari: " + ", ".join(bins),
                "note": dep.get("note", ""),
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
        # git serve SOLO per le dipendenze clonate (scripts/aml): i
        # package (governor, umr) si installano col package manager della
        # distro e non devono essere bloccati da un git assente.
        needs_git = any(
            dep["type"] in ("scripts", "aml")
            and not self._check_one(dep)["present"]
            for dep in DEPS
            if deps is None or dep["name"] in deps
        )
        if needs_git and which("git") is None:
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

        # Fallback `stress → stress-ng`: bc250-detect usa il binario
        # `stress` (via stress_helper); se assente ma c'è stress-ng
        # (compatibile), installiamo un wrapper — niente reboot ostree.
        result["system"] = self._ensure_stress(sudo=sudo)

        return result

    def _ensure_stress(self, sudo: bool) -> Dict[str, Any]:
        """Wrapper `stress` → `stress-ng` se il primo manca."""
        if which("stress") is not None:
            return {"status": "ok", "detail": "stress presente"}
        stress_ng = which("stress-ng")
        if stress_ng is None:
            return {"status": "failed",
                    "detail": "né stress né stress-ng disponibili "
                              "(serve uno dei due per il test di stabilità)"}
        wrapper = (
            "#!/bin/sh\n"
            "# Wrapper BUO: stress mancante -> stress-ng (compatibile)\n"
            f'exec {shlex.quote(stress_ng)} "$@"\n'
        )
        dest = self.bin_dir / "stress"
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if self._writable(self.bin_dir):
                dest.write_text(wrapper, encoding="utf-8")
            else:
                tmpdir = tempfile.mkdtemp(prefix="buo-")
                try:
                    tmp = Path(tmpdir) / "buo-stress-wrapper"
                    tmp.write_text(wrapper, encoding="utf-8")
                    rc, _, err = run_command(
                        ["install", "-m", "755", str(tmp), str(dest)],
                        sudo=sudo)
                    if rc != 0:
                        return {"status": "failed", "detail": err[:120]}
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP |
                       stat.S_IXOTH)
            return {"status": "ok",
                    "detail": f"wrapper stress -> {stress_ng} creato"}
        except Exception as e:
            return {"status": "failed", "detail": str(e)}

    def _install_one(self, dep: Dict[str, Any], sudo: bool) -> Dict[str, Any]:
        # I pacchetti si installano col package manager della distro
        # (nessun clone, nessun installer di terze parti).
        if dep["type"] == "package":
            return self._install_package(dep, sudo=sudo)

        checkout = self.base_dir / dep["name"]
        try:
            # Clone pinnato a un commit esatto e VERIFICATO (supply-chain:
            # niente HEAD mobile, il checkout è sempre il commit atteso).
            if not checkout.exists():
                rc, out, err = run_command(
                    ["git", "clone", "--no-checkout", dep["repo"],
                     str(checkout)],
                    timeout=300)
                if rc != 0:
                    return {"status": "failed",
                            "detail": f"clone fallito: {err[:200]}"}
                commit = dep["commit"]
                rc, out, err = run_command(
                    ["git", "-C", str(checkout), "fetch", "--depth", "1",
                     "origin", commit],
                    timeout=300)
                if rc != 0:
                    return {"status": "failed",
                            "detail": f"fetch commit fallito: {err[:200]}"}
                rc, out, err = run_command(
                    ["git", "-C", str(checkout), "checkout", "--detach",
                     commit],
                    timeout=300)
                if rc != 0:
                    return {"status": "failed",
                            "detail": f"checkout commit fallito: {err[:200]}"}
                rc, out, err = run_command(
                    ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                    timeout=60)
                resolved = out.strip() if out else ""
                if rc != 0 or resolved != commit:
                    self.logger.error("commit inatteso: atteso=%s reale=%r",
                                      commit, resolved)
                    return {"status": "failed",
                            "detail": "verifica commit fallita"}
                self.logger.info("checkout %s pinnato su %s",
                                 dep["name"], resolved)
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
                    tmpdir = tempfile.mkdtemp(prefix="buo-")
                    try:
                        tmp = Path(tmpdir) / dest.name
                        shutil.copy2(src, tmp)
                        rc, _, err = run_command(
                            ["install", "-m", "755", str(tmp), str(dest)],
                            sudo=sudo)
                        if rc != 0:
                            failures.append(f"{dest}: {err[:120]}")
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)
                if f.get("exec"):
                    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP |
                               stat.S_IXOTH)
            except Exception as e:
                failures.append(f"{f['dest']}: {e}")

        # Copia le directory di supporto (es. la libreria bc250_smu)
        for d in dep.get("copy_dirs", []):
            src_dir = checkout / d["src"]
            if not src_dir.is_dir():
                failures.append(f"{d['src']} (dir) non trovata nel checkout")
                continue
            dest_dir = self.bin_dir / d["dest"]
            try:
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                if self._writable(self.bin_dir):
                    shutil.copytree(src_dir, dest_dir)
                else:
                    tmpdir = tempfile.mkdtemp(prefix="buo-")
                    try:
                        tmp = Path(tmpdir) / d["dest"]
                        shutil.copytree(src_dir, tmp)
                        rc, _, err = run_command(
                            ["cp", "-r", str(tmp), str(self.bin_dir)],
                            sudo=sudo)
                        if rc != 0:
                            failures.append(f"{dest_dir}: {err[:120]}")
                    finally:
                        shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception as e:
                failures.append(f"{d['dest']}: {e}")

        if failures:
            return {"status": "failed", "detail": "; ".join(failures)}

        if dep["type"] == "instruct":
            return {"status": "ok", "detail": dep.get("note", ""),
                    "checkout": str(checkout)}

        return {"status": "ok", "detail": "installata",
                "checkout": str(checkout)}

    def _install_package(self, dep: Dict[str, Any], sudo: bool) -> Dict[str, Any]:
        """Installa un pacchetto con il package manager della distro.

        Supporta (senza installer di terze parti):
            • Fedora non-ostree: dnf + COPR filippor/bazzite (governor)
            • Bazzite/ostree: rpm-ostree install (richiede reboot per
              l'attivazione — lo segnala con needs_reboot=True)
            • Arch: AUR via yay/paru (governor)
            • Debian: pacchetti non ufficiali → istruzioni chiare
        """
        from ..utils.distro import detect_distro
        distro = detect_distro()
        pkg = dep.get("pkg_map", {}).get(distro.id)
        if not pkg:
            return {
                "status": "failed",
                "detail": f"nessun pacchetto per {distro.id}: "
                          f"{dep.get('note', '')}",
            }

        # Fedora e Bazzite: il governor vive nel COPR filippor/bazzite.
        # Su Bazzite il COPR enable scrive in /etc/yum.repos.d (funziona
        # anche su ostree) e va fatto PRIMA di rpm-ostree install.
        if dep.get("copr") and distro.id in ("fedora", "bazzite"):
            rc, _, err = run_command(
                ["dnf", "copr", "enable", "-y", dep["copr"]],
                sudo=sudo, timeout=120)
            if rc != 0:
                return {"status": "failed",
                        "detail": f"COPR enable fallito: {err[:150]}"}

        # Arch: il governor è nell'AUR (yay/paru)
        if dep.get("aur") and distro.id == "arch":
            helper = which("yay") or which("paru")
            if helper is None:
                return {"status": "failed",
                        "detail": "serve yay o paru per l'AUR "
                                  "(cyan-skillfish-governor-smu)"}
            rc, _, err = run_command(
                [helper, "-S", "--noconfirm", pkg], sudo=sudo, timeout=600)
            if rc != 0:
                return {"status": "failed", "detail": err[:150]}
            return {"status": "ok", "detail": f"{pkg} installato (AUR)"}

        rc, _, err = distro.install_package(pkg, sudo=sudo)
        if rc != 0:
            return {"status": "failed", "detail": err[:200] or f"{pkg} fallito"}
        needs_reboot = distro.pkg_manager == "rpm-ostree"
        return {
            "status": "ok",
            "detail": f"{pkg} installato"
                      + (" (attivo al prossimo reboot)" if needs_reboot else ""),
            "needs_reboot": needs_reboot,
        }

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
