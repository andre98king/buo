#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test dei fail-safe dei moduli "fix" (ace, tlb, acpi).

Questi moduli NON devono mai toccare l'hardware o il filesystem reale
quando i prerequisiti mancano. Qui si verifica il comportamento
fail-closed:

  - ACEComputeFix: repo/binario assente → applied=False e MAI una build/
    install reale; il guard "Mai installare Mesa senza kernel patchato"
    viene riportato (docstring del modulo).
  - TLBKernelFix: patch assente → stato non applicato + messaggio chiaro,
    nessuna modifica di sistema; rollback verificato con mock.
  - ACPIFix: su ostree rifiuta il percorso cpio→/boot (guard boot-failure);
    sui rami supportati (dracut/mkinitcpio/cpio) costruisce la command-line
    corretta senza eseguire nulla di reale; rollback rimuove gli artefatti.

NOTA import: `ace.py`/`tlb.py` non importano affatto `subprocess` o
`run_command` (strutturalmente non possono eseguire comandi); `acpi.py`
usa `subprocess.run` direttamente (NON `buo.utils.shell.run_command`).
I test seguono lo stile d'import reale dei moduli e mockano
`subprocess.run`/`shutil`/`open`/`Path.mkdir` di conseguenza, più
`buo.utils.shell.run_command` come contratto "nessun comando eseguito".
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.fix.ace import ACEComputeFix, ACE_REPO
from buo.fix.tlb import TLBKernelFix, TLB_PATCH_NAME, TLB_PATCH_REPO
from buo.fix.acpi import ACPIFix, AML_CST
from buo.utils.mock import MockHardware


class TestACEComputeFixSafety(unittest.TestCase):
    """ACE: repo assente → fail-safe; repo presente → mai auto-install."""

    def test_missing_repo_returns_safe_failure(self):
        """Senza repo_path: applied=False + warning, nessun comando eseguito."""
        with mock.patch("buo.utils.shell.run_command") as run:
            fix = ACEComputeFix(repo_path=None)
            result = fix.apply()

        self.assertFalse(result["applied"])
        self.assertFalse(result["needs_reboot"])
        self.assertIn("warning", result)
        self.assertIn(ACE_REPO, result["warning"])
        # ace.py non raggiunge mai l'esecutore di comandi di BUO
        run.assert_not_called()

    def test_repo_without_install_sh_is_refused(self):
        """Una directory senza install.sh NON deve innescare build/install."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("buo.utils.shell.run_command") as run:
                fix = ACEComputeFix(repo_path=tmp)
                result = fix.apply()

        self.assertFalse(result["applied"])
        self.assertFalse(result["needs_reboot"])
        self.assertIn("warning", result)
        run.assert_not_called()

    def test_present_repo_never_auto_installs_mesa_alone(self):
        """Guard docstring: MAI installare Mesa senza kernel patchato.

        Anche quando install.sh esiste, BUO NON esegue l'installer: riporta
        il procedimento manuale (kernel+Mesa insieme) e l'avvertenza.
        """
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "install.sh").write_text("#!/bin/sh\n")
            with mock.patch("buo.utils.shell.run_command") as run:
                fix = ACEComputeFix(repo_path=tmp)
                result = fix.apply()

        # fail-safe: l'applicazione reale non parte mai da sola
        self.assertFalse(result["applied"])
        self.assertTrue(result["needs_reboot"])
        self.assertIn("warning", result)
        self.assertIn("MAI installare Mesa senza kernel patchato",
                      result["warning"])
        # BUO non invoca mai l'installer di persona
        run.assert_not_called()

    def test_present_repo_reports_guard_and_logs_no_execution(self):
        """La non-esecuzione viene loggata; il guard Mesa/kernel nel risultato."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "install.sh").write_text("#!/bin/sh\n")
            with self.assertLogs("buo.ACEComputeFix", level="WARNING") as logs:
                fix = ACEComputeFix(repo_path=tmp)
                result = fix.apply()

        joined = "\n".join(logs.output)
        self.assertIn("Applicazione reale del fix ACE non eseguita", joined)
        self.assertIn("MAI installare Mesa senza kernel patchato",
                      result["warning"])


class TestTLBKernelFixSafety(unittest.TestCase):
    """TLB: patch mancante → non applicata; rollback verificato con mock."""

    def test_missing_patch_returns_unapplied_with_message(self):
        """patch_path assente → applied=False + messaggio, nessuna modifica."""
        with mock.patch("buo.utils.shell.run_command") as run:
            fix = TLBKernelFix(patch_path=None)
            result = fix.apply()

        self.assertFalse(result["applied"])
        self.assertFalse(result["needs_reboot"])
        self.assertIn("warning", result)
        self.assertIn("Patch TLB non trovata", result["warning"])
        self.assertIn(TLB_PATCH_REPO, result["warning"])
        run.assert_not_called()

    def test_present_patch_never_auto_applies(self):
        """Anche con la patch presente, l'applicazione resta manuale."""
        with tempfile.TemporaryDirectory() as tmp:
            patch = Path(tmp, TLB_PATCH_NAME)
            patch.write_text("--- a/driver\n+++ b/driver\n")
            with mock.patch("buo.utils.shell.run_command") as run:
                fix = TLBKernelFix(patch_path=str(patch))
                result = fix.apply()

        self.assertFalse(result["applied"])
        self.assertTrue(result["needs_reboot"])
        self.assertIn("warning", result)
        run.assert_not_called()

    def test_rollback_mock_removes_fix(self):
        """In mock, rollback() ripulisce lo stato TLB."""
        hw = MockHardware()
        hw.state.is_tlb_fixed = True
        fix = TLBKernelFix(mock=True, mock_hardware=hw)

        self.assertTrue(fix.rollback())
        self.assertFalse(hw.state.is_tlb_fixed)

    def test_rollback_real_is_safe_noop(self):
        """In produzione rollback() avvisa e non esegue comandi reali."""
        with mock.patch("buo.utils.shell.run_command") as run:
            fix = TLBKernelFix(mock=False)
            self.assertTrue(fix.rollback())
        run.assert_not_called()


class TestACPIFixSafety(unittest.TestCase):
    """ACPI: rami distro (dracut/mkinitcpio/cpio/ostree) + rollback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.aml_dir = Path(self._tmp.name)
        (self.aml_dir / AML_CST).write_text("DefinitionBlock")

    def tearDown(self):
        self._tmp.cleanup()

    def _distro(self, tool, distro_id):
        d = mock.MagicMock()
        d.initramfs_tool = tool
        d.id = distro_id
        return d

    # ------------------------------ ostree ----------------------------- #

    def test_ostree_concatenated_no_cpio_on_boot(self):
        """Ostree: metodo CONCATENATO (mai cpio separato su /boot).

        Senza entries systemd-boot → fail-closed: applied False, nessun
        cpio scritto su /boot, nessuna esecuzione esterna.
        """
        subprocess_run = mock.MagicMock()
        with tempfile.TemporaryDirectory() as boot_td, \
             mock.patch("buo.fix.acpi.detect_distro",
                        return_value=self._distro("ostree", "bazzite")), \
             mock.patch("buo.fix.acpi.subprocess.run", subprocess_run):
            fix = ACPIFix(mock=False, aml_dir=str(self.aml_dir),
                          boot_dir=boot_td)
            result = fix.apply()

        self.assertFalse(result["applied"])
        self.assertIn("entries", result["error"])
        # guard boot-failure: nessun cpio separato su /boot
        self.assertFalse((Path(boot_td) / "SSDT_ACPI.cpio").exists())
        subprocess_run.assert_not_called()

    # ------------------------------ dracut ----------------------------- #

    def test_dracut_builds_correct_command(self):
        """Fedora/dracut: `dracut -f`, nessuna esecuzione reale."""
        subprocess_run = mock.MagicMock()
        with mock.patch("buo.fix.acpi.detect_distro",
                        return_value=self._distro("dracut", "fedora")), \
             mock.patch("buo.fix.acpi.subprocess.run", subprocess_run), \
             mock.patch("buo.fix.acpi.shutil.copy2") as copy2, \
             mock.patch("pathlib.Path.mkdir"), \
             mock.patch("builtins.open", mock.mock_open()):
            fix = ACPIFix(mock=False, aml_dir=str(self.aml_dir))
            result = fix.apply()

        self.assertTrue(result["applied"])
        self.assertEqual(result["method"], "dracut")
        subprocess_run.assert_called_once_with(["dracut", "-f"], check=False)
        copy2.assert_called_once()

    # ---------------------------- mkinitcpio --------------------------- #

    def test_mkinitcpio_builds_correct_command(self):
        """Arch/mkinitcpio: `mkinitcpio -P`, conf patchata solo in memoria."""
        subprocess_run = mock.MagicMock()
        with mock.patch("buo.fix.acpi.detect_distro",
                        return_value=self._distro("mkinitcpio", "arch")), \
             mock.patch("buo.fix.acpi.subprocess.run", subprocess_run), \
             mock.patch("buo.fix.acpi.shutil.copy2") as copy2, \
             mock.patch("pathlib.Path.mkdir"), \
             mock.patch("pathlib.Path.read_text",
                        return_value="HOOKS=(base udev)"), \
             mock.patch("pathlib.Path.write_text"):
            fix = ACPIFix(mock=False, aml_dir=str(self.aml_dir))
            result = fix.apply()

        self.assertTrue(result["applied"])
        self.assertEqual(result["method"], "mkinitcpio")
        subprocess_run.assert_called_once_with(["mkinitcpio", "-P"], check=False)
        copy2.assert_called_once()

    # ------------------------------ cpio ------------------------------- #

    def test_cpio_builds_correct_command(self):
        """Debian/initramfs-tools: find+cpio, cpio scritto (mockato) su /boot."""
        subprocess_run = mock.MagicMock()
        find_result = mock.MagicMock()
        find_result.stdout = "kernel/firmware/acpi/SSDT-CST.aml"
        cpio_result = mock.MagicMock()
        cpio_result.stdout = b"070701-fake-cpio"
        subprocess_run.side_effect = [find_result, cpio_result]
        mocked_open = mock.mock_open()

        with mock.patch("buo.fix.acpi.detect_distro",
                        return_value=self._distro("initramfs-tools", "debian")), \
             mock.patch("buo.fix.acpi.subprocess.run", subprocess_run), \
             mock.patch("buo.fix.acpi.shutil.copy2"), \
             mock.patch("buo.fix.acpi.shutil.rmtree"), \
             mock.patch("buo.fix.acpi.tempfile.mkdtemp",
                        return_value="/tmp/buo-acpi-test"), \
             mock.patch("pathlib.Path.mkdir"), \
             mock.patch("builtins.open", mocked_open):
            fix = ACPIFix(mock=False, aml_dir=str(self.aml_dir))
            result = fix.apply()

        self.assertTrue(result["applied"])
        self.assertEqual(result["method"], "cpio")
        self.assertEqual(subprocess_run.call_count, 2)
        cmds = [c.args[0] for c in subprocess_run.call_args_list]
        self.assertIn(["find", "kernel", "-type", "f"], cmds)
        self.assertIn(["cpio", "-H", "newc", "--create"], cmds)
        # il ramo supportato scrive l'override su /boot (qui solo mockato)
        mocked_open.assert_called_once_with("/boot/SSDT_ACPI.cpio", "wb")

    # ---------------------- distro non supportata ---------------------- #

    def test_unknown_distro_refuses(self):
        """Distro non supportata → rifiuto (fail-closed), nessun comando."""
        subprocess_run = mock.MagicMock()
        with mock.patch("buo.fix.acpi.detect_distro",
                        return_value=self._distro("unknown", "alpine")), \
             mock.patch("buo.fix.acpi.subprocess.run", subprocess_run):
            fix = ACPIFix(mock=False, aml_dir=str(self.aml_dir))
            result = fix.apply()

        self.assertFalse(result["applied"])
        self.assertIn("error", result)
        self.assertIn("distro non supportata", result["error"])
        subprocess_run.assert_not_called()

    # ------------------------------ rollback --------------------------- #

    def test_rollback_removes_expected_artifacts(self):
        """rollback() rimuove i due override dir e il cpio su /boot + rebuild."""
        rmtree = mock.MagicMock()
        distro = self._distro("mkinitcpio", "arch")

        dirs = {"/etc/initcpio/acpi_override", "/etc/dracut.conf.d/acpi"}
        boot_file = "/boot/SSDT_ACPI.cpio"

        def fake_exists(self):
            return str(self) in dirs or str(self) == boot_file

        def fake_is_dir(self):
            return str(self) in dirs

        with mock.patch("buo.fix.acpi.detect_distro", return_value=distro), \
             mock.patch("buo.fix.acpi.shutil.rmtree", rmtree), \
             mock.patch("pathlib.Path.exists", autospec=True,
                        side_effect=fake_exists), \
             mock.patch("pathlib.Path.is_dir", autospec=True,
                        side_effect=fake_is_dir), \
             mock.patch("pathlib.Path.unlink", autospec=True) as unlink:
            fix = ACPIFix(mock=False, aml_dir=str(self.aml_dir))
            removed = fix.rollback()

        self.assertTrue(removed)
        self.assertEqual(rmtree.call_count, 2)
        rmtree.assert_any_call(Path("/etc/initcpio/acpi_override"))
        rmtree.assert_any_call(Path("/etc/dracut.conf.d/acpi"))
        unlink.assert_called_once()
        self.assertEqual(str(unlink.call_args.args[0]), boot_file)
        distro.rebuild_initramfs.assert_called_once()


if __name__ == "__main__":
    unittest.main()
