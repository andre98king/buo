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
    • PIN MIGRATO (10/09/2026) → mendesrr/bc250-acpi-fix-updated-8c
      @ 83686c46. Motivo FUNZIONALE misurato sul campo: le tabelle del
      vecchio pin coprono solo P000–P00B (6 core) e sulla macchina 8c/16T
      le CPU 12–15 risultavano SENZA idle e SENZA scaling
      (`cpuidle_states=0`, `pss_freqs=0`, nessuna `policy*` cpufreq:
      girano sempre al massimo). Il fork attivo tiene gli .aml IN-TREE
      (CST 990 B sha256 4ed0dfba…, PST 1146 B sha256 1fb4a2d0…, header
      AML validi) e aggiunge P00C–P00F: compatibile col flusso A7
      checkout-based E con la macchina (il DSDT dichiara P00C–P00F, quindi
      gli External risolvono). Resta valido il fail-closed: il commit è
      pinnato, i file sono consumati DAL CHECKOUT e validati (`_valid_aml`).
    ⚠️ WARNING BIOS MODDATI: le tabelle BUO possono CONFLIGGERE con
      tabelle già fornite dal firmware moddato (es. 8-core via BIOS con
      tabelle proprie). I duplicati falliscono il load (README e-tho:
      "duplicates will fail to load"). CONFERMATO SUL CAMPO (23/09/2026,
      firmware community v2.2 con "ACPI patch" attiva): ACPICA respinge TUTTI
      gli oggetti del blob — 136 `AE_ALREADY_EXISTS` su P000-P00F — e gli idle
      state dei 16 thread arrivano dal firmware. Il gate ora lo rileva da solo
      (`firmware_supplies_tables`: effetto su cpu12-15) e salta il fix, invece
      di riapplicare 252 MB di initramfs per un riavvio a vuoto.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..utils.distro import detect_distro
from ..utils.logging import LoggerMixin

ACPI_REPO = "https://github.com/mendesrr/bc250-acpi-fix-updated-8c"
# Pin A7: commit esatto, .aml consumati DAL CHECKOUT e validati. Il pin è
# migrato dal vecchio bc250-collective (dormiente, tabelle solo 6 core) a
# questo fork attivo con P00C–P00F (8 core): vedi docstring, AGGIORNAMENTO 3.
AML_CST = "SSDT-CST.aml"
# Marker (in state_dir) con l'hash delle tabelle APPLICATE: il gate ostree
# verifica che la entry punti a un blob, non QUALI tabelle contiene.
APPLIED_MARKER = "acpi-applied-tables.json"
# Thread delle CPU PRIMA dell'unlock (6 core × 2 SMT): i thread con indice
# >= a questo sono quelli che il fix ACPI deve dotare di idle/P-state.
STOCK_THREADS = 12


class ACPIFix(LoggerMixin):
    """Installa le tabelle ACPI C-State per la BC-250."""

    def __init__(self, mock: bool = False, mock_hardware=None,
                 aml_dir: Optional[str] = None,
                 boot_dir: Optional[str] = None,
                 marker_path: Optional[str] = None,
                 cpu_sysfs: Optional[str] = None):
        self.mock = mock
        self.mock_hw = mock_hardware
        # Root del boot (ESP): /boot di default, iniettabile nei test
        self.boot_dir = Path(boot_dir) if boot_dir else Path("/boot")
        # Radice sysfs delle CPU: iniettabile nei test (l'ermeticità della
        # suite dipende dal NON leggere l'hardware vero).
        self.cpu_sysfs = (Path(cpu_sysfs) if cpu_sysfs
                          else Path("/sys/devices/system/cpu"))
        # Default: cartella .aml scaricata da `buo install-deps` (se presente)
        if aml_dir is None:
            from ..utils.paths import deps_dir
            auto = deps_dir() / "bc250-acpi-fix"
            if (auto / AML_CST).exists():
                aml_dir = str(auto)
        self.aml_dir = Path(aml_dir) if aml_dir else None
        self.distro = detect_distro()
        # Marker delle tabelle APPLICATE (hash): il gate verifica che l'entry
        # punti a un blob, non QUALI tabelle contiene — senza marker una
        # migrazione delle tabelle resta inerte (campo 10/09/2026).
        self._marker_path = Path(marker_path) if marker_path else None

    @property
    def marker_path(self) -> Path:
        """Path del marker (risolto lazy: nessun I/O di stato nel costruttore)."""
        if self._marker_path is None:
            from ..utils.paths import state_dir
            self._marker_path = state_dir() / APPLIED_MARKER
        return self._marker_path

    # ------------------------------------------------------------------ #
    # Marker tabelle applicate (hash) — ASTRAZIONE: mai dichiarare
    # applicate tabelle che non si sono costruite.
    # ------------------------------------------------------------------ #

    def tables_hash(self) -> Optional[str]:
        """sha256 deterministico delle tabelle .aml correnti (None se assenti)."""
        if not self.aml_dir or not Path(self.aml_dir).is_dir():
            return None
        files = sorted(p for p in Path(self.aml_dir).glob("*.aml")
                       if p.is_file())
        if not files:
            return None
        h = hashlib.sha256()
        for p in files:
            h.update(p.name.encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
        return h.hexdigest()

    def applied_tables_hash(self) -> Optional[str]:
        """hash registrato nell'ultimo apply (None se marker assente/rotto)."""
        try:
            data = json.loads(self.marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("tables_sha256")
        return value if isinstance(value, str) and value else None

    def firmware_supplies_tables(self) -> bool:
        """True se C-State/P-State degli 8 core arrivano dal FIRMWARE moddato.

        Verifica dell'EFFETTO, non della provenienza: i thread oltre gli
        STOCK_THREADS stock devono avere idle state in cpuidle. Con un BIOS
        che fornisce le proprie tabelle (menu con "ACPI patch") gli oggetti
        del nostro blob vengono respinti da ACPICA (`AE_ALREADY_EXISTS`) e il
        fix è inutile: applicarlo costerebbe un initramfs da 252 MB e un
        riavvio a vuoto. Se invece gli idle state mancano (firmware stock, o
        "ACPI patch" spenta) il fix resta necessario e la strada normale.
        """
        try:
            extra = [p for p in self.cpu_sysfs.glob("cpu[0-9]*")
                     if int(p.name[3:]) >= STOCK_THREADS]
            return bool(extra) and all(
                (p / "cpuidle" / "state1").exists() for p in extra)
        except (OSError, ValueError):  # pragma: no cover - difesa I/O
            return False

    def is_stale(self) -> bool:
        """True se le tabelle APPLICATE non sono certificabili come correnti.

        Due casi:
        - marker presente e hash diversi → tabelle superate (migrazione);
        - marker ASSENTE ma fix già presente (la entry punta a un nostro
          blob) → provenienza IGNOTA: non si può dichiarare quali tabelle
          girano, quindi si ricostruisce. Senza questo caso una migrazione
          delle tabelle resterebbe inerte per sempre (il gate guarda la
          entry, non il contenuto del blob).
        Nessun fix presente → False (ci pensa il normale `apply`).
        Tabelle fornite dal FIRMWARE → False: non c'è nulla di nostro da
        certificare (e ricostruire il blob sarebbe un lavoro inutile).
        """
        if self.firmware_supplies_tables():
            return False
        applied = self.applied_tables_hash()
        current = self.tables_hash()
        if applied and current:
            return applied != current
        return applied is None and current is not None and self.verify()

    def _write_marker(self, blob: Optional[str]) -> None:
        """Registra le tabelle applicate (fail-soft: mai eccezioni)."""
        current = self.tables_hash()
        if current is None:
            return
        files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(Path(self.aml_dir).glob("*.aml"))
                 if p.is_file()}
        data = {
            "tables_sha256": current,
            "files": files,
            "blob": blob,
            "applied_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            self.marker_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.marker_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self.marker_path)
        except OSError as e:  # pragma: no cover - difesa I/O
            self.logger.warning("Marker tabelle ACPI non scritto: %s", e)

    # ------------------------------------------------------------------ #

    def verify(self) -> bool:
        """True se le tabelle C-State sono ATTIVE (dal boot o dal firmware).

        Su ostree (initramfs concatenato) l'unico segnale onesto è la entry
        del deployment **bootato** che punta a un nostro blob: la versione
        precedente accettava QUALSIASI entry, quindi un blob residuo su una
        entry vecchia faceva risultare il fix applicato mentre la macchina
        bootava senza tabelle — fail-open silenzioso, perché ogni transazione
        ostree rigenera le entry e la più nuova (senza blob) diventa quella di
        default (campo 11/09/2026). Sulle distro dracut/initramfs-tools il
        segnale resta il nome delle tabelle in /sys (lì gli override NON
        vengono rinominati in SSDT1..N).

        PRIMA di tutto questo: se le tabelle le fornisce il firmware moddato
        (`firmware_supplies_tables`) il fix è già in effetto e non c'è nulla
        da applicare — è il caso del BIOS community v2.2 col menu "ACPI
        patch", dove il nostro blob viene respinto in blocco.
        """
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.state.is_acpi_fixed
        if self.firmware_supplies_tables():
            return True
        if self.distro.initramfs_tool == "ostree":
            loader = self.boot_dir / "loader" / "entries"
            if not loader.is_dir():
                return False
            entry = self._default_entry(loader)
            if entry is None:
                return False
            try:
                text = entry.read_text(errors="replace")
            except Exception:
                return False
            m = re.search(r"^initrd\s+(\S+)", text, re.M)
            return bool(m and self._is_acpi_blob(m.group(1)))
        tables = Path("/sys/firmware/acpi/tables")
        if not tables.exists():
            return False
        try:
            return any("CST" in p.name for p in tables.glob("SSDT*"))
        except Exception:
            return False

    def apply(self, force: bool = False) -> Dict[str, Any]:
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
            out = self._install_ostree(force=force)
            if out.get("applied") and not out.get("already"):
                # Marker SOLO quando il blob è stato davvero costruito:
                # sul ramo "già applicato" la provenienza delle tabelle è
                # ignota (non si dichiara ciò che non si è costruito).
                self._write_marker(out.get("blob"))
                out["tables_sha256"] = self.tables_hash()
            return out
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
        2) entry del deployment attualmente bootato (valore `ostree=`
           COMPLETO da /proc/cmdline: boot.N + hash + indice — il solo
           hash NON basta: le due entry condividono la boot-dir key);
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
            # CASO CAMPO 10/09: il match va fatto sul valore ostree= INTERO.
            # Il vecchio regex era hardcoded su `boot.0` (sulla macchina è
            # `boot.1`) e catturava solo l'hash: con due entry che
            # condividono la boot-dir key (indice /0 e /1) sceglieva la
            # prima alfabetica → fix applicato all'entry SBAGLIATA (inerte
            # al boot).
            m = re.search(r"(?:^|\s)ostree=(/ostree/\S+)", cmdline)
            if m:
                pat = re.compile(r"(?:^|\s)ostree=" + re.escape(m.group(1))
                                 + r"(?:\s|$)")
                for entry in sorted(loader.glob("*.conf")):
                    if pat.search(entry.read_text(errors="replace")):
                        return entry
        except Exception:
            pass
        entries = sorted(loader.glob("*.conf"))
        return entries[0] if entries else None

    def _install_ostree(self, force: bool = False) -> Dict[str, Any]:
        """Bazzite/ostree: initramfs CONCATENATO (metodo validato).

        cpio ACPI + initramfs in un blob unico → boot entry (systemd-boot)
        puntata al blob con UNA sola riga initrd. Fail-closed:
        - entry di default risolta come systemd-boot (loader.conf →
          deployment attivo da /proc/cmdline → fallback alfabetico);
        - backup della entry prima di ogni modifica;
        - verifica magic cpio sul blob prima di sostituire la entry;
        - nessuna modifica se qualcosa non quadra (initramfs assente/
          troppo piccolo, blob non valido, righe initrd != 1).

        `force=True` ricostruisce il blob anche se la entry è già a posto
        (serve per MIGRARE le tabelle: il gate non vede quali contiene).
        In quel caso la base è l'initramfs ORIGINALE, non il blob
        precedente (concatenarlo di nuovo anniderebbe i cpio).
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
        ver = Path(m_linux.group(1)).name.replace("vmlinuz-", "")
        blob = self.boot_dir / f"initramfs-acpi-{ver}.img"
        # Idempotenza: la entry punta già a un nostro blob valido
        if self._is_acpi_blob(cur_initrd) and not force:
            return {"applied": True, "method": "ostree-concat",
                    "needs_reboot": False, "already": True}

        if self._is_acpi_blob(cur_initrd):
            # Rebuild forzato: base = initramfs originale del kernel
            src = self.boot_dir / f"initramfs-{ver}.img"
        else:
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
