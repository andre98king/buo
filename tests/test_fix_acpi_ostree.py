#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del metodo ACPI CONCATENATO per ostree (Bazzite/SteamOS):
cpio ACPI + initramfs in un blob unico, boot entry con UNA sola riga
initrd, backup prima della modifica, fail-closed su ogni anomalia.
"""

import tempfile
import unittest
from unittest import mock
from pathlib import Path

from buo.fix.acpi import ACPIFix

KERNEL = "7.2.0-ogc6.1.fc44.x86_64"
ENTRY_TEXT = (
    "title Bazzite (ostree:1)\n"
    "options ostree=/ostree/boot.0/default/abc/0 rhgb quiet\n"
    f"linux /ostree/default-abc/vmlinuz-{KERNEL}\n"
    f"initrd /initramfs-{KERNEL}.img\n"
)
INITRAMFS_SIZE = 20 * 1024 * 1024 + 100  # > soglia 20MB


def _aml(sig: bytes = b"SSDT", size: int = 36) -> bytes:
    """Header ACPI minimale valido (A4): signature + lunghezza."""
    data = bytearray(36)
    data[0:4] = sig
    data[4:8] = size.to_bytes(4, "little")
    data[8] = 1  # revision
    return bytes(data)


class TestAcpiOstreeConcat(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.entries = self.root / "loader" / "entries"
        self.entries.mkdir(parents=True)
        self.aml = self.root / "aml"
        self.aml.mkdir()
        (self.aml / "SSDT-CST.aml").write_bytes(_aml())
        (self.aml / "SSDT-PST.aml").write_bytes(_aml())
        self.initramfs = self.root / f"initramfs-{KERNEL}.img"
        self.initramfs.write_bytes(b"X" * INITRAMFS_SIZE)
        self.entry = self.entries / "ostree-1.conf"
        self.entry.write_text(ENTRY_TEXT)
        self.fix = ACPIFix(mock=False, aml_dir=str(self.aml),
                           boot_dir=str(self.root))
        self.fix.distro.initramfs_tool = "ostree"  # forza il ramo ostree

    def tearDown(self):
        self._tmp.cleanup()

    def _blob(self) -> Path:
        return self.root / f"initramfs-acpi-{KERNEL}.img"

    def test_apply_concatenates_and_rewrites_entry(self):
        out = self.fix.apply()
        self.assertTrue(out["applied"], f"apply fallito: {out}")
        self.assertEqual(out["method"], "ostree-concat")
        self.assertTrue(out["needs_reboot"])
        # entry: UNA sola riga initrd verso il blob
        text = self.entry.read_text()
        self.assertEqual(len(text.splitlines()), len(ENTRY_TEXT.splitlines()))
        initrd_lines = [l for l in text.splitlines()
                        if l.startswith("initrd ")]
        self.assertEqual(len(initrd_lines), 1)
        self.assertIn(f"/initramfs-acpi-{KERNEL}.img", initrd_lines[0])
        # blob presente, con magic cpio in testa
        self.assertTrue(self._blob().is_file())
        with open(self._blob(), "rb") as f:
            head = f.read(6)
            f.seek(-100, 2)
            tail = f.read()
        self.assertEqual(head, b"070701")  # cpio newc
        self.assertEqual(tail, b"X" * 100)  # coda = initramfs originale
        # backup della entry creato
        self.assertTrue(list(self.entries.glob("ostree-1.conf.bak-*")))

    def test_apply_is_idempotent(self):
        first = self.fix.apply()
        self.assertTrue(first["applied"])
        n_bak = len(list(self.entries.glob("*.conf.bak-*")))
        second = self.fix.apply()
        self.assertTrue(second["applied"])
        self.assertTrue(second.get("already"))
        # nessun backup aggiuntivo né riscrittura
        self.assertEqual(len(list(self.entries.glob("*.conf.bak-*"))), n_bak)
        self.assertEqual(len([l for l in self.entry.read_text().splitlines()
                              if l.startswith("initrd ")]), 1)

    def test_rollback_restores_original_entry(self):
        self.fix.apply()
        self.assertTrue(self.fix.rollback())
        self.assertEqual(self.entry.read_text(), ENTRY_TEXT)

    def test_rollback_noop_without_backup(self):
        self.assertFalse(self.fix.rollback())

    def test_fail_closed_missing_initramfs(self):
        self.initramfs.unlink()
        out = self.fix.apply()
        self.assertFalse(out["applied"])
        self.assertEqual(self.entry.read_text(), ENTRY_TEXT)
        self.assertFalse(self._blob().exists())

    def test_fail_closed_small_initramfs(self):
        self.initramfs.write_bytes(b"X" * 1024)  # troppo piccolo
        out = self.fix.apply()
        self.assertFalse(out["applied"])
        self.assertEqual(self.entry.read_text(), ENTRY_TEXT)

    def test_fail_closed_no_entries(self):
        self.entry.unlink()
        out = self.fix.apply()
        self.assertFalse(out["applied"])
        self.assertIn("nessuna boot entry", out["error"])

    def test_fail_closed_no_aml(self):
        (self.aml / "SSDT-CST.aml").unlink()
        (self.aml / "SSDT-PST.aml").unlink()
        out = self.fix.apply()
        self.assertFalse(out["applied"])
        self.assertEqual(self.entry.read_text(), ENTRY_TEXT)

    def test_fail_closed_already_concatenated_entry_skips(self):
        """Entry già puntata a un blob valido → already, nessuna modifica."""
        blob = self._blob()
        cpio = self.fix._build_acpi_cpio()
        self.assertTrue(cpio)
        blob.write_bytes(cpio + self.initramfs.read_bytes())
        self.entry.write_text(
            ENTRY_TEXT.replace(f"initrd /initramfs-{KERNEL}.img",
                               f"initrd /initramfs-acpi-{KERNEL}.img"))
        out = self.fix.apply()
        self.assertTrue(out["applied"])
        self.assertTrue(out.get("already"))


    # ---------------- risoluzione entry di default (A3) ---------------- #

    def test_default_entry_from_loader_conf(self):
        """La entry scelta è quella in loader.conf, NON la alfabetica."""
        (self.entries / "ostree-0.conf").write_text(
            ENTRY_TEXT.replace("ostree:1", "ostree:0"))
        (self.root / "loader" / "loader.conf").write_text(
            "default ostree-0.conf\n")
        entry = self.fix._default_entry(self.entries)
        self.assertEqual(entry.name, "ostree-0.conf")

    def test_default_entry_from_cmdline_deployment(self):
        """Fallback: entry del deployment attivo (ostree= in cmdline)."""
        (self.entries / "ostree-2.conf").write_text(
            "title B\n"
            "linux /ostree/default-deadbeef/vmlinuz-x\n"
            "initrd /initramfs-x.img\n"
            "options ostree=/ostree/boot.0/default/deadbeef/0 quiet\n")
        real_read = Path.read_text

        def fake_read(self_, *a, **k):
            if str(self_) == "/proc/cmdline":
                return ("BOOT_IMAGE=... "
                        "ostree=/ostree/boot.0/default/deadbeef/0 rhgb quiet")
            return real_read(self_, *a, **k)

        with mock.patch("buo.fix.acpi.Path.read_text", fake_read):
            entry = self.fix._default_entry(self.entries)
        self.assertEqual(entry.name, "ostree-2.conf")

    def test_default_entry_alphabetical_fallback(self):
        """Senza loader.conf e cmdline ostree → prima *.conf."""
        (self.entries / "ostree-0.conf").write_text(
            ENTRY_TEXT.replace("ostree:1", "ostree:0"))
        with mock.patch("buo.fix.acpi.Path.read_text",
                        side_effect=lambda *a, **k: ""):
            entry = self.fix._default_entry(self.entries)
        self.assertEqual(entry.name, "ostree-0.conf")


    # ---------------- validazione .aml (A4) ---------------- #

    def test_invalid_aml_skipped(self):
        """AML con header non valido → scartato, il cpio usa i validi."""
        (self.aml / "SSDT-ROTT.aml").write_bytes(b"NOT-AN-AML!!!")
        cpio = self.fix._build_acpi_cpio()
        self.assertTrue(cpio)
        self.assertNotIn(b"SSDT-ROTT", cpio)
        self.assertIn(b"SSDT-CST", cpio)

    def test_all_invalid_aml_fails_closed(self):
        for f in self.aml.glob("*.aml"):
            f.unlink()
        (self.aml / "SSDT-BAD.aml").write_bytes(b"XXXX" + b"\x00" * 32)
        out = self.fix.apply()
        self.assertFalse(out["applied"])
        self.assertEqual(self.entry.read_text(), ENTRY_TEXT)

    def test_valid_aml_helper_contract(self):
        self.assertTrue(self.fix._valid_aml(_aml()))
        self.assertFalse(self.fix._valid_aml(b"AML-CST-DATA"))
        self.assertFalse(self.fix._valid_aml(b"XXXX" + b"\x00" * 32))
        self.assertFalse(self.fix._valid_aml(b""))


if __name__ == "__main__":
    unittest.main()
