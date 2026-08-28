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

import hashlib
import io
import json
import os
import shlex
import stat
import shutil
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .. import __version__
from ..utils.logging import LoggerMixin
from ..utils.paths import deps_dir
from ..utils.shell import run_command, which

# Binari attesi (destinazioni)
BIN_DIR_SYSTEM = "/usr/local/bin"

# Formato del bundle offline (design: DESIGN_OFFLINE_DEPS.md sez. 3.3)
BUNDLE_FORMAT = "buo-bundle"
BUNDLE_VERSION = 1
# Tipi di dipendenza che vivono in un checkout git (clonabili/bundlati);
# i `package` (governor/umr) NON si bundlano MAI.
BUNDLE_TYPES = ("scripts", "aml", "build")


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
            "name": "bc250_memcfg",
            "repo": "https://github.com/fanoush/bc250_memcfg",
            "type": "build",
            "commit": "829e8d64f23c5ad1e1d662f4eab488f31e0daa72",
            "required_for": "fix VRAM (split VRAM dedicata via "
                           "bc250_memcfg --set-vram)",
            "binary": "bc250_memcfg",
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
        # Nomi dei repo la cui verifica è già avvenuta via import del bundle
        # offline (tree-hash + commit): su macchine senza git il riuso di
        # questi checkout non richiede una nuova verifica git.
        self._bundle_imported: Set[str] = set()

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
                sudo: bool = True,
                offline_bundle: Optional[Union[str, Path]] = None
                ) -> Dict[str, Any]:
        """
        Clona le repo mancanti e installa gli script.

        Se `offline_bundle` è dato e servono tool git-based mancanti, il
        bundle viene importato e verificato PRIMA del calcolo di needs_git
        (i repo soddisfatti dal bundle non richiedono git). Se l'import
        fallisce → {"_error": ...} (fail-closed, niente installato).

        Returns:
            {name: {"status": "ok"|"skipped"|"failed", "detail": str}}
        """
        if offline_bundle:
            # Import solo se serve: se i tool git-based ci sono già tutti,
            # il bundle non viene toccato (mai import inutile).
            git_based_missing = any(
                dep["type"] in BUNDLE_TYPES
                and not self._check_one(dep)["present"]
                for dep in DEPS
                if deps is None or dep["name"] in deps
            )
            if git_based_missing:
                res = self.import_bundle(offline_bundle)
                if res["status"] != "ok":
                    return {"_error": res["detail"]}

        # git serve SOLO per CLONARE i checkout mancanti (scripts/aml/build):
        # i package (governor, umr) si installano col package manager della
        # distro e non devono essere bloccati da un git assente. Un checkout
        # GIÀ presente (riuso o bundle offline) non richiede git. Fix bug
        # latente: il tipo "build" (bc250_memcfg) clona via git ma oggi era
        # escluso dal gate → errore confuso a metà clone.
        needs_git = any(
            dep["type"] in BUNDLE_TYPES
            and not self._check_one(dep)["present"]
            and not (self.base_dir / dep["name"]).exists()
            for dep in DEPS
            if deps is None or dep["name"] in deps
        )
        if needs_git and which("git") is None:
            return {"_error": "git non trovato: installalo con il package "
                              "manager della distro, oppure usa un bundle "
                              "offline (buo install-deps --offline <file>)"}

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
                # A7: verifica commit + pulizia (rev-parse == atteso, status
                # --porcelain vuoto): il clone fresco è sempre VERIFICATO.
                err_verify = self._verify_checkout(dep, checkout)
                if err_verify is not None:
                    return {"status": "failed", "detail": err_verify}
                self.logger.info("checkout %s pinnato su %s",
                                 dep["name"], dep["commit"])
            elif (dep["name"] in self._bundle_imported
                  and which("git") is None):
                # Checkout appena importato da un bundle offline verificato
                # (tree-hash + commit già controllati in import_bundle) su
                # una macchina senza git: nessuna verifica git richiesta.
                self.logger.info("checkout %s importato dal bundle offline "
                                 "(verificato in import)", dep["name"])
            else:
                # Riuso del checkout esistente: A7 — il commit e la pulizia
                # vanno VERIFICATI anche qui (buco chiuso: prima il riuso
                # non verificava il commit atteso).
                err_verify = self._verify_checkout(dep, checkout)
                if err_verify is not None:
                    return {"status": "failed",
                            "detail": f"checkout esistente non valido: "
                                      f"{err_verify}"}
                self.logger.info("checkout %s riusato e verificato su %s",
                                 dep["name"], dep["commit"])
        except Exception as e:
            return {"status": "failed", "detail": f"clone in errore: {e}"}

        # G6: tipo "build" — compila (make) e installa il binario
        if dep["type"] == "build":
            return self._build_and_install(dep, checkout, sudo=sudo)

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
                # A7: registra l'impronta SHA-256 del file installato
                # (tamper-evidence: rileva modifiche dopo l'installazione)
                self._record_hash(dep, f, dest)
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

    def _build_and_install(self, dep: Dict[str, Any], checkout: Path,
                           sudo: bool) -> Dict[str, Any]:
        """G6: compila un tool community (make) e installa il binario.

        Verifica prerequisiti di build (make, compilatore) con messaggio
        chiaro, poi copia il binario in bin_dir con hash registrato.
        """
        if which("make") is None:
            return {"status": "failed",
                    "detail": f"{dep['name']}: 'make' non trovato "
                              "(installare gcc/make con il package manager)"}
        rc, out, err = run_command(["make"], cwd=str(checkout), timeout=600)
        if rc != 0:
            return {"status": "failed",
                    "detail": f"{dep['name']}: make fallito: {err[:200]}"}
        binary = checkout / dep.get("binary", dep["name"])
        if not binary.is_file():
            return {"status": "failed",
                    "detail": f"{dep['name']}: binario non prodotto: "
                              f"{dep.get('binary')}"}
        dest = self.bin_dir / binary.name
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if self._writable(self.bin_dir):
                shutil.copy2(binary, dest)
            else:
                tmpdir = tempfile.mkdtemp(prefix="buo-")
                try:
                    tmp = Path(tmpdir) / binary.name
                    shutil.copy2(binary, tmp)
                    rc, _, err = run_command(
                        ["install", "-m", "755", str(tmp), str(dest)],
                        sudo=sudo)
                    if rc != 0:
                        return {"status": "failed",
                                "detail": f"{dest}: {err[:120]}"}
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP |
                       stat.S_IXOTH)
            self._record_hash(dep, {"src": "binary"}, dest)
            return {"status": "ok",
                    "detail": f"{binary.name} compilato e installato"}
        except Exception as e:
            return {"status": "failed", "detail": f"{binary.name}: {e}"}

    def _record_hash(self, dep: Dict[str, Any], f: Dict[str, Any],
                     dest: Path) -> None:
        """A7: registra l'SHA-256 del file installato (tamper-evidence).

        Il file `deps-hashes.json` nella dir di stato permette di
        rilevare modifiche ai tool della community DOPO l'installazione
        (es. da parte di un processo malevolo o di un aggiornamento
        manuale).
        """
        try:
            digest = hashlib.sha256(dest.read_bytes()).hexdigest()
        except Exception:
            return
        record = {
            "name": dep["name"],
            "commit": dep.get("commit"),
            "src": f["src"],
            "dest": str(dest),
            "sha256": digest,
        }
        try:
            from ..utils.paths import state_dir
            path = state_dir() / "deps-hashes.json"
            data = {}
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}
            key = record["dest"]
            data[key] = record
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        except Exception:
            pass  # il record hash non deve mai bloccare l'installazione

    # ------------------------------------------------------------------ #
    # Offline bundle (design: DESIGN_OFFLINE_DEPS.md)
    # ------------------------------------------------------------------ #

    def _verify_checkout(self, dep: Dict[str, Any],
                         checkout: Path) -> Optional[str]:
        """Verifica A7 di un checkout: .git presente, git rev-parse HEAD ==
        dep['commit'], git status --porcelain vuoto.

        Returns: None se ok, altrimenti messaggio di errore (fail-closed).
        """
        commit = dep.get("commit")
        if not commit:
            return f"{dep['name']}: nessun commit pinnato nel catalogo"
        if not checkout.exists() or not (checkout / ".git").exists():
            return f"{dep['name']}: checkout corrotto (manca .git)"
        if which("git") is None:
            return (f"{dep['name']}: git non disponibile — impossibile "
                    f"verificare il checkout")
        try:
            rc, out, err = run_command(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                timeout=60)
            resolved = out.strip() if out else ""
            if rc != 0 or resolved != commit:
                self.logger.error("commit inatteso: atteso=%s reale=%r",
                                  commit, resolved)
                return f"{dep['name']}: verifica commit fallita"
            rc, out, _ = run_command(
                ["git", "-C", str(checkout), "status", "--porcelain"],
                timeout=60)
            if rc != 0 or out.strip():
                return (f"{dep['name']}: checkout modificato localmente "
                        f"(possibile tampering)")
        except Exception as e:
            return f"{dep['name']}: verifica checkout in errore: {e}"
        return None

    @staticmethod
    def _tree_sha256(root: Path) -> str:
        """Hash deterministico dell'albero di lavoro di un checkout (ignora .git).

        Input per ogni entry: relpath (sorted), modalità (stat.S_IMODE), e
        contenuto (file) o target (symlink). Copre symlink legittimi nei repo.
        """
        entries: List[Tuple[str, int, Any]] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            base = Path(dirpath)
            for name in filenames:
                p = base / name
                try:
                    rel = p.relative_to(root)
                    st = p.lstat()
                except (OSError, ValueError):
                    continue
                if ".git" in rel.parts:
                    continue
                mode = stat.S_IMODE(st.st_mode)
                if stat.S_ISLNK(st.st_mode):
                    try:
                        entries.append((str(rel), mode, os.readlink(p)))
                    except OSError:
                        continue
                elif stat.S_ISREG(st.st_mode):
                    try:
                        entries.append((str(rel), mode, p.read_bytes()))
                    except OSError:
                        continue
        h = hashlib.sha256()
        for rel, mode, payload in sorted(entries, key=lambda e: e[0]):
            h.update(rel.encode("utf-8"))
            h.update(b"\x00")
            h.update(str(mode).encode("ascii"))
            h.update(b"\x00")
            h.update(payload if isinstance(payload, bytes)
                     else payload.encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest()

    @staticmethod
    def _read_bundle_manifest(bundle: Path) -> Optional[Dict[str, Any]]:
        """Legge 'buo-bundle.json' dal tarball senza estrarlo; None se invalido."""
        try:
            with tarfile.open(str(bundle), "r:gz") as tar:
                member = tar.getmember("buo-bundle.json")
                if not member.isfile():
                    return None
                f = tar.extractfile(member)
                if f is None:
                    return None
                data = json.loads(f.read().decode("utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def export_bundle(self, dest: Union[str, Path],
                      deps: Optional[List[str]] = None) -> Dict[str, Any]:
        """Crea il bundle offline dei checkout pinnati.

        Fail-closed: se UN solo checkout manca, non è al commit atteso o non
        è pulito → status "failed" con l'elenco, NESSUN file scritto (export
        = solo copie di checkout già VERIFICATI, mai materiale non verificato).

        Returns:
            {"status": "ok", "path": str, "sha256": str}
            | {"status": "failed", "detail": str}
        """
        dest = Path(dest)
        repos = [d for d in DEPS
                 if d["type"] in BUNDLE_TYPES
                 and (deps is None or d["name"] in deps)]

        # Fase 1: verifica TUTTI i checkout prima di scrivere qualsiasi cosa.
        verified: Dict[str, Tuple[Dict[str, Any], Path]] = {}
        problems: List[str] = []
        for dep in repos:
            checkout = self.base_dir / dep["name"]
            if not checkout.exists():
                problems.append(f"{dep['name']}: checkout mancante")
                continue
            err = self._verify_checkout(dep, checkout)
            if err is not None:
                problems.append(err)
                continue
            verified[dep["name"]] = (dep, checkout)
        if problems:
            return {"status": "failed",
                    "detail": "checkout non verificabili: "
                              + "; ".join(problems)}

        # Fase 2: crea il tarball in un file temporaneo, poi move atomico.
        tmp_path: Optional[Path] = None
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix="buo-bundle-",
                                            suffix=".tar.gz",
                                            dir=str(dest.parent))
            os.close(fd)
            tmp_path = Path(tmp_name)
            with tarfile.open(str(tmp_path), "w:gz") as tar:
                manifest = {
                    "format": BUNDLE_FORMAT,
                    "version": BUNDLE_VERSION,
                    "buo_version": __version__,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "deps": {},
                }
                for name in sorted(verified):
                    dep, checkout = verified[name]
                    manifest["deps"][name] = {
                        "repo": dep["repo"],
                        "commit": dep["commit"],
                        "tree_sha256": self._tree_sha256(checkout),
                    }
                payload = json.dumps(manifest, indent=2,
                                     ensure_ascii=False).encode("utf-8")
                info = tarfile.TarInfo("buo-bundle.json")
                info.size = len(payload)
                info.mtime = int(time.time())
                tar.addfile(info, io.BytesIO(payload))
                # checkout COMPLETI, incluso .git (shallow, pochi MB)
                for name in sorted(verified):
                    _, checkout = verified[name]
                    tar.add(str(checkout), arcname=f"checkouts/{name}")
            shutil.move(str(tmp_path), str(dest))
            tmp_path = None
        except Exception as e:
            if tmp_path is not None:
                try:
                    tmp_path.unlink()
                except Exception:
                    pass
            return {"status": "failed",
                    "detail": f"export bundle fallito: {e}"}
        sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        return {"status": "ok", "path": str(dest), "sha256": sha}

    def import_bundle(self, bundle: Union[str, Path]) -> Dict[str, Any]:
        """Importa e VERIFICA un bundle in deps_dir (solo checkout: non installa).

        Due fasi fail-closed: (1) verifica TUTTO — tarball valido, manifest
        conforme, commit attesi == catalogo, completezza, estrazione sicura,
        tree-hash, git se disponibile, conflitti con deps_dir — senza toccare
        nulla; (2) solo se tutto ok → sposta i checkout verificati in deps_dir.

        Returns:
            {"status": "ok", "imported": [nomi...], "detail": str}
            | {"status": "failed", "detail": str}
        """
        bundle = Path(bundle)
        # check 1 — tarball valido
        try:
            tar = tarfile.open(str(bundle), "r:gz")
        except Exception:
            return {"status": "failed",
                    "detail": f"{bundle}: non è un bundle BUO valido"}
        with tar:
            # check 2 — manifest presente e conforme
            manifest = self._read_bundle_manifest(bundle)
            if manifest is None:
                return {"status": "failed",
                        "detail": "manifest assente o non valido: "
                                  "non è un bundle BUO valido"}
            if (manifest.get("format") != BUNDLE_FORMAT
                    or manifest.get("version") != BUNDLE_VERSION):
                return {"status": "failed",
                        "detail": "bundle generato da una versione BUO non "
                                  "supportata (format/version non riconosciuti)"}
            m_deps = manifest.get("deps")
            if not isinstance(m_deps, dict) or not m_deps:
                return {"status": "failed", "detail": "manifest senza repo"}

            catalog = {d["name"]: d for d in DEPS
                       if d["type"] in BUNDLE_TYPES}
            # check 3 — commit attesi == catalogo (mai abbassare il pin)
            for name, entry in m_deps.items():
                dep = catalog.get(name)
                if dep is None:
                    return {"status": "failed",
                            "detail": f"repo sconosciuto nel bundle: {name}"}
                if (not isinstance(entry, dict)
                        or entry.get("commit") != dep["commit"]):
                    return {"status": "failed",
                            "detail": (
                                f"bundle obsoleto: atteso {dep['commit']} per "
                                f"{name}, bundle ha "
                                f"{entry.get('commit') if isinstance(entry, dict) else '?'}. "
                                "Rigenera il bundle con: sudo buo install-deps "
                                "--export-bundle <file> su una macchina con rete")}
            # check 4 — completezza: ogni repo del catalogo DEVE esserci
            for name in catalog:
                if name not in m_deps:
                    return {"status": "failed",
                            "detail": f"bundle parziale: manca {name}. "
                                      "Rigenera il bundle completo con: sudo "
                                      "buo install-deps --export-bundle <file>"}

            # check 5-6-7 — staging sicuro + tree-hash + git (se disponibile)
            staging = Path(tempfile.mkdtemp(prefix="buo-bundle-import-"))
            try:
                err = self._extract_bundle(tar, m_deps, staging)
                if err is not None:
                    return {"status": "failed", "detail": err}
                for name, entry in m_deps.items():
                    checkout = staging / "checkouts" / name
                    if not checkout.is_dir():
                        return {"status": "failed",
                                "detail": f"bundle incompleto: manca il "
                                          f"checkout di {name}"}
                    actual = self._tree_sha256(checkout)
                    if actual != entry.get("tree_sha256"):
                        return {"status": "failed",
                                "detail": f"bundle corrotto o manomesso "
                                          f"(tree hash non corrisponde per {name})"}
                if which("git") is not None:
                    for name in m_deps:
                        dep = catalog[name]
                        checkout = staging / "checkouts" / name
                        err = self._verify_checkout(dep, checkout)
                        if err is not None:
                            return {"status": "failed",
                                    "detail": f"checkout {name} non verificato: {err}"}

                # check 8 — conflitti con deps_dir (pre-scan PRIMA di ogni move)
                self.base_dir.mkdir(parents=True, exist_ok=True)
                for name, entry in m_deps.items():
                    dest = self.base_dir / name
                    if dest.exists() and not self._checkout_matches(
                            dest, catalog[name], entry.get("tree_sha256")):
                        return {"status": "failed",
                                "detail": (
                                    f"checkout esistente in deps_dir in conflitto "
                                    f"per {name}: commit diverso o non "
                                    f"verificabile — rimuovilo manualmente o "
                                    f"rigenera il bundle")}

                # check 9 — applicazione: move atomico dei soli assenti
                imported: List[str] = []
                for name in sorted(m_deps):
                    dest = self.base_dir / name
                    if dest.exists():
                        continue
                    shutil.move(str(staging / "checkouts" / name), str(dest))
                    imported.append(name)
                # i checkout verificati (importati o già presenti) sono
                # riusabili senza git nel giro di install() di questa run
                self._bundle_imported.update(m_deps.keys())
                detail = ("checkout importati: " + ", ".join(imported)
                          if imported else "tutti i checkout già presenti")
                return {"status": "ok", "imported": imported, "detail": detail}
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def _extract_bundle(self, tar: tarfile.TarFile, m_deps: Dict[str, Any],
                        staging: Path) -> Optional[str]:
        """Estrae in staging i membri sotto checkouts/<nome-in-manifest>.

        Fail-closed: path assoluti, componenti '..' o percorsi che
        attraversano un symlink già estratto → rifiuto dell'intero import.
        I membri fuori dall'allowlist (il manifest) sono ignorati con
        warning, mai installati né eseguiti.
        """
        known = set(m_deps.keys())
        for member in tar.getmembers():
            name = member.name
            parts = name.split("/")
            if name.startswith("/") or ".." in parts:
                return (f"bundle non sicuro: member con path non consentito "
                        f"({name}) — import rifiutato")
            if name == "buo-bundle.json":
                continue  # già letto e verificato
            if len(parts) < 2 or parts[0] != "checkouts" or parts[1] not in known:
                self.logger.warning(
                    "bundle: membro ignorato (fuori dall'allowlist del "
                    "manifest): %s", name)
                continue
            if not (member.isdir() or member.isreg() or member.issym()):
                return (f"bundle non sicuro: member di tipo non supportato "
                        f"({name}) — import rifiutato")
            # nessun componente del percorso può essere un symlink già
            # estratto (evita scrittura fuori da staging attraverso symlink)
            cur = staging
            for part in parts[:-1]:
                cur = cur / part
                if cur.is_symlink():
                    return (f"bundle non sicuro: symlink nel percorso di "
                            f"{name} — import rifiutato")
            try:
                tar.extract(member, path=str(staging))
            except Exception as e:
                return f"estrazione fallita: {e}"
        return None

    def _checkout_matches(self, checkout: Path, dep: Dict[str, Any],
                          tree_sha256: Optional[str]) -> bool:
        """True se un checkout già in deps_dir è verificato allo stesso
        commit (idempotenza → skip); False se in conflitto/non verificabile."""
        if which("git") is None:
            if not tree_sha256:
                return False
            try:
                return self._tree_sha256(checkout) == tree_sha256
            except Exception:
                return False
        return self._verify_checkout(dep, checkout) is None

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
