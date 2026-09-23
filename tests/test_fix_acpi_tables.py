#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Marker delle tabelle ACPI applicate (fase 2/3, 10/09/2026).

Problema di campo: il gate ostree verifica solo che la boot entry punti a un
nostro blob, NON quali tabelle contiene. Sul campo il blob attivo conteneva
le tabelle VECCHIE del pin dormiente (CST 782 B / PST 926 B) mentre esistono
tabelle 8-core aggiornate (990/1146 B): senza un marker con l'hash delle
tabelle applicate una migrazione resta INERTE (nessuno la riapplica).

Regole:
- il marker si scrive SOLO quando il blob viene davvero (ri)costruito;
- il ramo "già applicato" NON scrive il marker (provenienza ignota: non si
  dichiarano applicate tabelle che non si sono costruite);
- is_stale() = True quando le tabelle applicate non sono certificabili come
  correnti: marker presente con hash diversi (migrazione), oppure marker
  ASSENTE con fix già presente (provenienza ignota → si ricostruisce).
"""

import json
import tempfile
import unittest
from pathlib import Path

from buo.fix.acpi import ACPIFix

KERNEL = "7.2.3-ogc3.1.fc44.x86_64"
ENTRY_TEXT = (
    "title Bazzite (ostree:1)\n"
    "options ostree=/ostree/boot.1/default/abc/1 rhgb quiet\n"
    f"linux /ostree/default-abc/vmlinuz-{KERNEL}\n"
    f"initrd /initramfs-{KERNEL}.img\n"
)
INITRAMFS_SIZE = 20 * 1024 * 1024 + 100


def _aml(sig: bytes = b"SSDT", size: int = 36) -> bytes:
    """Tabella AML minima ma VALIDA (header coerente: `_valid_aml` la accetta)."""
    size = max(size, 36)
    data = bytearray(size)
    data[0:4] = sig
    data[4:8] = size.to_bytes(4, "little")
    data[8] = 1
    return bytes(data)


class TestAcpiTablesMarker(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.entries = self.root / "loader" / "entries"
        self.entries.mkdir(parents=True)
        self.aml = self.root / "aml"
        self.aml.mkdir()
        (self.aml / "SSDT-CST.aml").write_bytes(_aml(size=100))
        (self.aml / "SSDT-PST.aml").write_bytes(_aml(size=200))
        self.initramfs = self.root / f"initramfs-{KERNEL}.img"
        self.initramfs.write_bytes(b"X" * INITRAMFS_SIZE)
        self.entry = self.entries / "ostree-1.conf"
        self.entry.write_text(ENTRY_TEXT)
        self.marker = self.root / "acpi-applied.json"
        self.fix = ACPIFix(mock=False, aml_dir=str(self.aml),
                           boot_dir=str(self.root),
                           marker_path=str(self.marker),
                           cpu_sysfs=str(self.root / "cpu"))
        self.fix.distro.initramfs_tool = "ostree"

    def tearDown(self):
        self._tmp.cleanup()

    # ----------------------------- hash ------------------------------ #

    def test_tables_hash_deterministic_and_sensitive(self):
        h1 = self.fix.tables_hash()
        self.assertRegex(h1, r"^[0-9a-f]{64}$")
        self.assertEqual(h1, ACPIFix(mock=False, aml_dir=str(self.aml),
                                     boot_dir=str(self.root),
                                     marker_path=str(self.marker),
                                     cpu_sysfs=str(self.root / "cpu"))
                         .tables_hash())
        (self.aml / "SSDT-CST.aml").write_bytes(_aml(size=101))
        self.assertNotEqual(h1, self.fix.tables_hash())

    def test_tables_hash_none_without_aml(self):
        fix = ACPIFix(mock=False, aml_dir=str(self.root / "vuoto"),
                      boot_dir=str(self.root), marker_path=str(self.marker),
                      cpu_sysfs=str(self.root / "cpu"))
        self.assertIsNone(fix.tables_hash())

    # ---------------------------- marker ----------------------------- #

    def test_apply_writes_marker(self):
        out = self.fix.apply()
        self.assertTrue(out["applied"], out)
        data = json.loads(self.marker.read_text())
        self.assertEqual(data["tables_sha256"], self.fix.tables_hash())
        self.assertIn("SSDT-CST.aml", data["files"])
        self.assertIn("applied_at", data)

    def test_already_applied_does_not_write_marker(self):
        """Provenienza ignota → non si dichiara nulla."""
        self.fix.apply()
        self.marker.unlink()
        out = self.fix.apply()          # la entry ora punta già al blob
        self.assertTrue(out.get("already"), out)
        self.assertFalse(self.marker.exists())

    def test_is_stale_semantics(self):
        self.assertFalse(self.fix.is_stale())          # nessun fix applicato
        self.fix.apply()
        self.assertFalse(self.fix.is_stale())          # tabelle identiche
        (self.aml / "SSDT-CST.aml").write_bytes(_aml(size=999))
        self.assertTrue(self.fix.is_stale())           # tabelle cambiate

    def test_is_stale_when_provenance_unknown(self):
        """Marker assente col fix PRESENTE = tabelle applicate non
        certificabili → va ricostruito (altrimenti una migrazione delle
        tabelle resta inerte per sempre: il gate guarda la boot entry)."""
        self.fix.apply()
        self.marker.unlink()
        self.assertTrue(self.fix.is_stale())

    def test_force_rebuilds_blob_and_updates_marker(self):
        self.fix.apply()
        old_blob = (self.root / f"initramfs-acpi-{KERNEL}.img").read_bytes()
        (self.aml / "SSDT-CST.aml").write_bytes(_aml(size=999))
        out = self.fix.apply(force=True)
        self.assertTrue(out["applied"], out)
        self.assertFalse(out.get("already"))
        new_blob = (self.root / f"initramfs-acpi-{KERNEL}.img").read_bytes()
        self.assertNotEqual(old_blob, new_blob)
        data = json.loads(self.marker.read_text())
        self.assertEqual(data["tables_sha256"], self.fix.tables_hash())


class TestFirmwareSuppliesTables(unittest.TestCase):
    """Tabelle fornite dal FIRMWARE moddato (campo 23/09/2026).

    Il BIOS community v2.2 (menu con "ACPI patch") spedisce le proprie tabelle
    per gli 8 core: ACPICA respinge in blocco gli oggetti del nostro blob
    (136 `AE_ALREADY_EXISTS` su P000-P00F) e gli idle state arrivano dal
    firmware. Il gate deve riconoscerlo dall'EFFETTO (idle state dei thread
    extra) e saltare il fix invece di riapplicare 252 MB di initramfs.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cpu = self.root / "cpu"
        self.cpu.mkdir()
        self.boot = self.root / "boot"
        (self.boot / "loader" / "entries").mkdir(parents=True)
        self.aml = self.root / "aml"
        self.aml.mkdir()
        (self.aml / "SSDT-CST.aml").write_bytes(_aml(size=100))

    def tearDown(self):
        self._tmp.cleanup()

    def _cpu(self, index: int, states: int = 0) -> None:
        """Un core in sysfs finto, con `states` idle state (0 = nessuno)."""
        d = self.cpu / f"cpu{index}"
        d.mkdir()
        for s in range(states):
            (d / "cpuidle" / f"state{s}").mkdir(parents=True)

    def _fix(self) -> ACPIFix:
        fix = ACPIFix(mock=False, aml_dir=str(self.aml),
                      boot_dir=str(self.boot),
                      marker_path=str(self.root / "marker.json"),
                      cpu_sysfs=str(self.cpu))
        fix.distro.initramfs_tool = "ostree"
        return fix

    def _boot_tree(self) -> Path:
        """Initramfs + entry BLS minimi per far girare `apply` su ostree."""
        (self.boot / f"initramfs-{KERNEL}.img").write_bytes(
            b"X" * INITRAMFS_SIZE)
        entry = self.boot / "loader" / "entries" / "ostree-1.conf"
        entry.write_text(ENTRY_TEXT)
        return entry

    def test_no_extra_threads_is_not_firmware_fix(self):
        for i in range(12):
            self._cpu(i, states=4)
        self.assertFalse(self._fix().firmware_supplies_tables())

    def test_extra_threads_without_idle_states(self):
        """8 core sbloccati ma senza tabelle (firmware stock): fix NECESSARIA."""
        for i in range(16):
            self._cpu(i, states=4 if i < 12 else 0)
        self.assertFalse(self._fix().firmware_supplies_tables())

    def test_extra_threads_with_idle_states(self):
        for i in range(16):
            self._cpu(i, states=4)
        self.assertTrue(self._fix().firmware_supplies_tables())

    def test_verify_true_without_our_blob(self):
        """Entry SENZA blob ma firmware che fornisce le tabelle → attivo."""
        for i in range(16):
            self._cpu(i, states=4)
        (self.boot / "loader" / "entries" / "ostree-1.conf").write_text(
            "title Bazzite\n"
            "options ostree=/ostree/boot.1/default/abc/1\n"
            f"linux /ostree/default-abc/vmlinuz-{KERNEL}\n"
            f"initrd /initramfs-{KERNEL}.img\n")
        fix = self._fix()
        self.assertFalse(fix._is_acpi_blob(f"/initramfs-{KERNEL}.img"))
        self.assertTrue(fix.verify())

    def test_is_stale_false_with_firmware_tables(self):
        """Niente di nostro da certificare → nessuna ricostruzione.

        Stessa situazione di `test_is_stale_when_provenance_unknown` (blob
        sulla entry + marker assente = di norma «ricostruisci»), ma con le
        tabelle fornite dal firmware: ricostruire 252 MB di initramfs per
        oggetti che ACPICA respinge sarebbe solo un riavvio a vuoto.
        """
        for i in range(16):
            self._cpu(i, states=4)
        entry = self._boot_tree()
        fix = self._fix()
        self.assertTrue(fix.apply()["applied"])
        (self.root / "marker.json").unlink()
        self.assertIn("initramfs-acpi-", entry.read_text())   # blob nostro
        self.assertFalse(fix.is_stale())

    def test_apply_still_used_without_firmware_tables(self):
        """Controprova: senza idle state il fix resta la strada normale."""
        for i in range(16):
            self._cpu(i, states=4 if i < 12 else 0)
        self._boot_tree()
        fix = self._fix()
        self.assertFalse(fix.verify())
        self.assertTrue(fix.apply()["applied"])


if __name__ == "__main__":
    unittest.main()
