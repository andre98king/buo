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

AGGIORNAMENTO 2 (metodo CONCATENATO validato sul campo, Bazzite/ostree):
    il kernel carica le tabelle ACPI dal PRIMO archivio cpio dentro
    l'initrd. Metodo sicuro su ostree: cpio ACPI + nuovo initramfs
    concatenati in UN blob (/boot/initramfs-acpi-<ver>.img) e boot entry
    puntata al blob con UNA sola riga initrd. L'initramfs originale NON
    viene toccato e la entry viene sempre backup-ata (rollback sicuro).
    ⚠️ Un cpio SEPARATO scritto su /boot (es. /boot/SSDT_ACPI.cpio) ha
    causato boot failure su ostree: non usarlo.

AGGIORNAMENTO 3 (30/08/2026 — ricerca community, repo ATTIVO):
    • il repo ATTIVO del fix è e-tho/bc250-acpi-fix, release v1.1.0
      (18/08/2026). Tabelle: SSDT-CPU (stato idle, include C3),
      SSDT-PST (frequency scaling 800→3200 MHz) e SSDT-STUBS (metodi
      mancanti APTS/AWAK/AFN7 come no-op). Copre le 16 definizioni CPU
      del firmware: funziona su 6-core stock e 8-core sbloccati, ed è
      compatibile con i BIOS 1-5 (1.00/2.00/3.00/5.00 condividono lo
      stesso DSDT).
    • PIN ACPI INVARIATO (fail-closed, supply-chain A7): BUO pinnna
      ancora bc250-collective/bc250-acpi-fix @ 1594d72 (repo morto dal
      23/11/2025, unico commit). Motivo: e-tho fornisce i .aml
      PRECOMPILATI SOLO come asset di release (nel git tree ci sono
      solo i sorgenti .dsl; .gitignore esclude *.aml), mentre il flusso
      A7 di BUO clona il checkout pinnato e consuma i file DAL
      CHECKOUT — non esiste alcun meccanismo di download di asset di
      release. La migrazione richiederebbe un nuovo percorso
      supply-chain + adattamento nomi (SSDT-CST.aml → SSDT-CPU.aml) +
      una decisione funzionale su PST/STUBS: NON forzata finché non c'è
      un meccanismo compatibile (mai migrare verso qualcosa di
      incompatibile).
    ⚠️ WARNING BIOS MODDATI: le tabelle BUO possono CONFLIGGERE con
      tabelle già fornite dal firmware moddato (es. 8-core via BIOS con
      tabelle proprie). I duplicati falliscono il load (README e-tho:
      "duplicates will fail to load"): su un BIOS moddato verificare che
      il firmware non fornisca già le tabelle prima di applicare il fix
      BUO, o rimuovere quelle di BUO.
"""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from ..utils.distro import detect_distro
from ..utils.logging import LoggerMixin

ACPI_REPO = "https://github.com/bc250-collective/bc250-acpi-fix"
# Pin A7 invariato (fail-closed): BUO consuma i .aml dal CHECKOUT del
# repo pinnato (bc250-collective, morto dal 23/11/2025). Il repo ATTIVO
# e-tho/bc250-acpi-fix (v1.1.0) vende i .aml precompilati solo come
# asset di release (nel tree solo .dsl) — incompatibile col flusso
# checkout-based: vedi docstring, AGGIORNAMENTO 3.
AML_CST = "SSDT-CST.aml"


class ACPIFix(LoggerMixin):
    """Installa le tabelle ACPI C-State per la BC-250."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 aml_dir: Optional[str] = None,
                 boot_dir: Optional[str] = None):
        self.mock = mock
        self.mock_hw = mock_hardware
        # Root del boot (ESP): /boot di default, iniettabile nei test
        self.boot_dir = Path(boot_dir) if boot_dir else Path("/boot")
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
        if self.distro.initramfs_tool == "ostree":
            # Metodo concatenato: fix applicato = la boot entry DI DEFAULT
            # punta a un nostro blob (initramfs-acpi-*.img) con cpio in
            # testa. Se il default non è ancora risolvibile, accetta
            # qualsiasi entry già puntata a un blob valido.
            loader = self.boot_dir / "loader" / "entries"
            if not loader.is_dir():
                return False
            entry = self._default_entry(loader)
            candidates = [entry] if entry else []
            candidates += [e for e in sorted(loader.glob("*.conf"))
                           if e not in candidates]
            for e in candidates:
                try:
                    text = e.read_text(errors="replace")
                except Exception:
                    continue
                m = re.search(r"^initrd\s+(\S+)", text, re.M)
                if m and self._is_acpi_blob(m.group(1)):
                    return True
            return False
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
            # Bazzite/SteamOS: metodo CONCATENATO validato sul campo
            # (cpio ACPI + initramfs in un blob, UNA riga initrd).
            return self._install_ostree()
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

    def _default_entry(self, loader: Path) -> Optional[Path]:
        """Risolve la boot entry che systemd-boot userà al prossimo boot.

        Priorità (come systemd-boot):
        1) `default` in loader.conf (es. "ostree-1.conf");
        2) entry del deployment attualmente bootato (ostree= da
           /proc/cmdline → hash del boot manifest);
        3) prima *.conf in ordine alfabetico (fallback).
        """
        conf = loader.parent / "loader.conf"
        if conf.is_file():
            try:
                for line in conf.read_text(errors="replace").splitlines():
                    line = line.strip()
                    if line.startswith("default") and len(line.split()) > 1:
                        val = line.split(None, 1)[1].strip().strip('"')
                        name = Path(val).name
                        if not name.endswith(".conf"):
                            name += ".conf"
                        cand = loader / name
                        if cand.is_file():
                            return cand
            except Exception:
                pass
        try:
            cmdline = Path("/proc/cmdline").read_text(errors="replace")
            m = re.search(r"ostree=/ostree/boot\.0/default/([0-9a-f]+)",
                          cmdline)
            if m:
                for entry in sorted(loader.glob("*.conf")):
                    if m.group(1) in entry.read_text(errors="replace"):
                        return entry
        except Exception:
            pass
        entries = sorted(loader.glob("*.conf"))
        return entries[0] if entries else None

    def _install_ostree(self) -> Dict[str, Any]:
        """Bazzite/ostree: initramfs CONCATENATO (metodo validato).

        cpio ACPI + initramfs in un blob unico → boot entry (systemd-boot)
        puntata al blob con UNA sola riga initrd. Fail-closed:
        - entry di default risolta come systemd-boot (loader.conf →
          deployment attivo da /proc/cmdline → fallback alfabetico);
        - backup della entry prima di ogni modifica;
        - verifica magic cpio sul blob prima di sostituire la entry;
        - nessuna modifica se qualcosa non quadra (initramfs assente/
          troppo piccolo, blob non valido, righe initrd != 1).
        """
        if self.aml_dir is None:
            return {"applied": False, "error": "aml_dir non disponibile"}

        loader = self.boot_dir / "loader" / "entries"
        if not loader.is_dir():
            return {"applied": False,
                    "error": f"directory entries non trovata: {loader}"}

        entry = self._default_entry(loader)
        if entry is None:
            return {"applied": False, "error": "nessuna boot entry (*.conf)"}
        text = entry.read_text(errors="replace")
        m_linux = re.search(r"^linux\s+(\S+)", text, re.M)
        m_initrd = re.search(r"^initrd\s+(\S+)", text, re.M)
        if not m_linux or not m_initrd:
            return {"applied": False,
                    "error": f"entry senza righe linux/initrd: {entry.name}"}

        cur_initrd = m_initrd.group(1)
        # Idempotenza: la entry punta già a un nostro blob valido
        if self._is_acpi_blob(cur_initrd):
            return {"applied": True, "method": "ostree-concat",
                    "needs_reboot": False, "already": True}

        ver = Path(m_linux.group(1)).name.replace("vmlinuz-", "")
        blob = self.boot_dir / f"initramfs-acpi-{ver}.img"
        src = self.boot_dir / cur_initrd.lstrip("/")
        if not src.is_file() or src.stat().st_size < 20 * 1024 * 1024:
            return {"applied": False,
                    "error": f"initramfs originale non valido: {src}"}

        cpio_bytes = self._build_acpi_cpio()
        if not cpio_bytes or len(cpio_bytes) < 64:
            return {"applied": False, "error": "cpio ACPI non generato (nessun .aml?)"}

        # Concatenazione in temp + rename atomico
        tmp = blob.with_suffix(".img.tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(cpio_bytes)
                with open(src, "rb") as fin:
                    shutil.copyfileobj(fin, f)
            if not self._is_cpio(tmp):
                tmp.unlink(missing_ok=True)
                return {"applied": False, "error": "blob non inizia con magic cpio"}
        except Exception as e:  # pragma: no cover - difesa I/O
            tmp.unlink(missing_ok=True)
            return {"applied": False, "error": f"concatenazione fallita: {e}"}
        tmp.replace(blob)

        # Backup della entry (timestamp unico)
        from datetime import datetime
        backup = entry.with_name(
            entry.name + f".bak-{datetime.now():%Y%m%d-%H%M%S}")
        backup.write_text(text)

        # Riscrittura: UNA sola riga initrd verso il blob
        new_text = re.sub(
            r"^initrd\s+.*$", f"initrd /initramfs-acpi-{ver}.img",
            text, flags=re.M)
        if len(re.findall(r"^initrd\s+(\S+)", new_text, re.M)) != 1:
            entry.write_text(text)  # rollback immediato
            blob.unlink(missing_ok=True)
            return {"applied": False, "error": "verifica riga initrd unica fallita"}
        entry.write_text(new_text)
        subprocess.run(["sync"], check=False)

        return {"applied": True, "method": "ostree-concat",
                "needs_reboot": True, "entry": entry.name, "blob": blob.name}

    @staticmethod
    def _valid_aml(data: bytes) -> bool:
        """Validazione ACPI table header (A4): signature SSDT/DSDT +
        lunghezza dichiarata coerente. Un .aml corrotto/malevolo finirebbe
        nell'initramfs ed essere interpretato dal kernel con privilegi
        massimi: si accettano SOLO file validi."""
        if len(data) < 36:
            return False
        sig = data[:4]
        if sig not in (b"SSDT", b"DSDT"):
            return False
        declared = int.from_bytes(data[4:8], "little")
        return 36 <= declared <= len(data)

    def _build_acpi_cpio(self) -> Optional[bytes]:
        """cpio newc con kernel/firmware/acpi/*.aml — pura Python.

        Nessuna dipendenza esterna (cpio/dracut): l'archivio segue il
        formato newc ("070701") che il parser initramfs del kernel
        accetta per le tabelle ACPI (kernel/firmware/acpi/).
        """
        amls = sorted(self.aml_dir.glob("*.aml")) if self.aml_dir else []
        if not amls:
            return None

        def _header(name: bytes, size: int, ino: int) -> bytes:
            return b"".join([
                b"070701",
                f"{ino:08x}".encode(),      # ino
                b"000081a4",                # mode 0100644
                b"00000000",                # uid
                b"00000000",                # gid
                b"00000001",                # nlink
                b"00000000",                # mtime
                f"{size:08x}".encode(),     # filesize
                b"00000000",                # devmajor
                b"00000000",                # devminor
                b"00000000",                # rdevmajor
                b"00000000",                # rdevminor
                f"{len(name) + 1:08x}".encode(),  # namesize (con NUL)
                b"00000000",                # check
            ])

        out = bytearray()
        ino = 1
        for aml in amls:
            data = aml.read_bytes()
            if not self._valid_aml(data):
                self.logger.warning(
                    "ACPI: %s scartato (header AML non valido)", aml.name)
                continue
            name = f"kernel/firmware/acpi/{aml.name}".encode()
            out += _header(name, len(data), ino)
            out += name + b"\x00"
            while len(out) % 4:
                out += b"\x00"
            out += data
            while len(out) % 4:
                out += b"\x00"
            ino += 1
        trailer = b"TRAILER!!!"
        out += _header(trailer, 0, ino)
        out += trailer + b"\x00"
        while len(out) % 4:
            out += b"\x00"
        return bytes(out)

    @staticmethod
    def _is_cpio(path: Path) -> bool:
        """Magic del formato cpio newc: '070701'."""
        try:
            with open(path, "rb") as f:
                return f.read(6) == b"070701"
        except Exception:
            return False

    def _is_acpi_blob(self, initrd_path: str) -> bool:
        """True se initrd punta a un nostro blob già concatenato."""
        name = Path(initrd_path).name
        if not name.startswith("initramfs-acpi-"):
            return False
        blob = self.boot_dir / name
        return blob.is_file() and self._is_cpio(blob)

    # ---------------------------------------------------------------- #

    def rollback(self) -> bool:
        """Rimuove le tabelle e ricostruisce l'initramfs."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.remove_acpi_fix()

        # ostree: ripristina il backup della boot entry (metodo concatenato)
        if self.distro.initramfs_tool == "ostree":
            loader = self.boot_dir / "loader" / "entries"
            if not loader.is_dir():
                return False
            for bak in sorted(loader.glob("*.conf.bak-*"), reverse=True):
                entry = loader / (bak.name.split(".bak-")[0])
                if not entry.exists():
                    continue
                try:
                    entry.write_text(bak.read_text(errors="replace"))
                    return True
                except Exception as e:  # pragma: no cover
                    self.logger.error("Rollback ostree fallito per %s: %s",
                                      entry, e)
            return False

        removed = False
        for path in [
            Path("/etc/initcpio/acpi_override"),
            Path("/etc/dracut.conf.d/acpi"),
            # A5: la conf dracut scritta da apply() referenzia i file
            # rimossi: senza rimuoverla il rollback resterebbe rotto
            Path("/etc/dracut.conf.d/buo-acpi-override.conf"),
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

        # A5: mkinitcpio — ripristina la riga HOOKS=(acpi_override …)
        mkinit = Path("/etc/mkinitcpio.conf")
        if mkinit.exists():
            try:
                content = mkinit.read_text(errors="replace")
                if "acpi_override" in content:
                    mkinit.write_text(
                        content.replace("HOOKS=(acpi_override ", "HOOKS=("))
                    removed = True
            except Exception as e:
                self.logger.error("Ripristino mkinitcpio fallito: %s", e)

        self.distro.rebuild_initramfs()
        return removed
