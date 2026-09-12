#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del rilevamento hardware reale (bug di campo su BC-250).

Copre i 3 bug trovati sulla scheda reale (Bazzite ostree, SSH):
  1. conteggio core CPU (8c/16t → 8, non 12/16) + maschera SMN garbage
  2. conteggio CU GPU su runtime UMR (num_cu assente → live-manager)
  3. rilevamento Mesa senza display (fallback package manager)
"""

import io
import struct
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from buo.audit.hardware import HardwareAudit


def _cpuinfo_8c16t():
    """Synthetic /proc/cpuinfo: 8 physical cores, 16 logical threads."""
    blocks = []
    for i in range(16):
        blocks.append(
            f"processor\t: {i}\n"
            "vendor_id\t: AuthenticAMD\n"
            "cpu family\t: 25\n"
            "model\t\t: 23\n"
            "model name\t: AMD Custom CPU\n"
            "stepping\t: 1\n"
            "microcode\t: 0x8a00009\n"
            f"cpu MHz\t\t: 3200.000\n"
            "cache size\t: 512 KB\n"
            "physical id\t: 0\n"
            f"siblings\t: 16\n"
            f"core id\t\t: {i % 8}\n"
            "cpu cores\t: 8\n"
        )
    return "\n\n".join(blocks) + "\n"


class TestCpuCount(unittest.TestCase):
    def test_count_physical_cores_not_threads(self):
        with mock.patch("builtins.open", side_effect=lambda *a, **k:
                        io.StringIO(_cpuinfo_8c16t())):
            self.assertEqual(HardwareAudit._count_cpuinfo(), 8)


class TestCpuAudit(unittest.TestCase):
    def test_audit_cpu_reports_8_cores_and_ff_mask(self):
        audit = HardwareAudit()
        with mock.patch.object(HardwareAudit, "_read_core_mask_smn",
                               return_value=0xFF), \
             mock.patch.object(HardwareAudit, "_count_cpuinfo",
                               return_value=8):
            cpu = audit._audit_cpu()
        self.assertEqual(cpu["cores"], 8)
        self.assertEqual(cpu["core_mask"], "0xFF")
        self.assertTrue(cpu["unlocked"])

    def test_audit_cpu_marks_unverified_when_smn_read_fails(self):
        """Fail-open: senza lettura SMN autoritativa niente maschera."""
        audit = HardwareAudit()
        with mock.patch.object(HardwareAudit, "_read_core_mask_smn",
                               return_value=None), \
             mock.patch.object(HardwareAudit, "_count_cpuinfo",
                               return_value=8):
            cpu = audit._audit_cpu()
        self.assertEqual(cpu["cores"], 8)
        self.assertIsNone(cpu["core_mask"])
        self.assertIsNone(cpu["unlocked"])

    def test_read_core_mask_smn_returns_ff(self):
        audit = HardwareAudit()
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.geteuid", return_value=0), \
             mock.patch.object(HardwareAudit, "_governor_confirmed_inactive",
                               return_value=True), \
             mock.patch("os.open", return_value=3), \
             mock.patch("os.pwrite"), \
             mock.patch("os.pread", return_value=struct.pack("<I", 0xFF)), \
             mock.patch("os.close"):
            self.assertEqual(audit._read_core_mask_smn(), 0xFF)

    def test_read_core_mask_smn_none_on_garbage(self):
        """Un valore SMN fuori da {0x77, 0xFF} è garbage → unverified."""
        audit = HardwareAudit()
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.geteuid", return_value=0), \
             mock.patch.object(HardwareAudit, "_governor_confirmed_inactive",
                               return_value=True), \
             mock.patch("os.open", return_value=3), \
             mock.patch("os.pwrite"), \
             mock.patch("os.pread", return_value=struct.pack("<I", 0x3C)), \
             mock.patch("os.close"):
            self.assertIsNone(audit._read_core_mask_smn())

    def test_read_core_mask_smn_none_on_io_error(self):
        audit = HardwareAudit()
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.geteuid", return_value=0), \
             mock.patch.object(HardwareAudit, "_governor_confirmed_inactive",
                               return_value=True), \
             mock.patch("os.open", side_effect=OSError("perm")):
            self.assertIsNone(audit._read_core_mask_smn())

    def test_read_core_mask_smn_blocked_with_governor_active(self):
        """REGOLA SMU (freeze 30/08): con governor attivo NON si tocca
        l'SMN — niente maschera fabbricata e nessun accesso PCI."""
        audit = HardwareAudit()
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.geteuid", return_value=0), \
             mock.patch.object(HardwareAudit, "_governor_confirmed_inactive",
                               return_value=False), \
             mock.patch("os.open") as open_, \
             mock.patch("os.pwrite") as pwrite_:
            self.assertIsNone(audit._read_core_mask_smn())
        open_.assert_not_called()
        pwrite_.assert_not_called()

    def test_read_core_mask_smn_blocked_when_governor_state_unknown(self):
        """Fail-closed: stato governor sconosciuto = accesso NON autorizzato."""
        audit = HardwareAudit()
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.geteuid", return_value=0), \
             mock.patch.object(HardwareAudit, "_governor_confirmed_inactive",
                               return_value=None), \
             mock.patch("os.open") as open_:
            self.assertIsNone(audit._read_core_mask_smn())
        open_.assert_not_called()

    def test_governor_confirmed_inactive_semantics(self):
        """Solo `inactive`/`failed` = CONFERMATO fermo → True; `active` →
        False; stati transitori/vuoti/errore → None = NON autorizzato
        (fail-closed: `is-active` esce con rc=3 anche per activating, e un
        accesso SMN partirebbe mentre il governor scrive sull'SMU)."""
        audit = HardwareAudit()
        cases = (("inactive", True), ("failed", True), ("active", False),
                 ("activating", None), ("deactivating", None),
                 ("reloading", None), ("", None))
        for state, expected in cases:
            out = f"ActiveState={state}\nLoadState=loaded\n"
            with mock.patch("subprocess.run",
                            return_value=subprocess.CompletedProcess(
                                [], 0, out, "")):
                self.assertIs(audit._governor_confirmed_inactive(), expected,
                              f"state={state!r}")
        with mock.patch("subprocess.run", side_effect=OSError("no systemctl")):
            self.assertIsNone(audit._governor_confirmed_inactive())


class TestGpuCuCountUrm(unittest.TestCase):
    def test_parse_routed_cus(self):
        self.assertEqual(
            HardwareAudit._parse_routed_cus("  CUs active & routed  : 40/40\n"),
            40)
        self.assertEqual(
            HardwareAudit._parse_routed_cus("  CUs active & routed  : 24/40\n"),
            24)
        self.assertIsNone(HardwareAudit._parse_routed_cus("no such line"))

    def test_audit_gpu_uses_sysfs_num_cu_as_int(self):
        audit = HardwareAudit()
        with mock.patch.object(
                HardwareAudit, "_read_sysfs",
                side_effect=lambda name: "40" if name == "num_cu" else None):
            gpu = audit._audit_gpu()
        self.assertEqual(gpu["cu_count"], 40)
        self.assertEqual(gpu["cu_source"], "sysfs")

    def test_audit_gpu_falls_back_to_live_manager_config(self):
        """num_cu assente (runtime UMR) → cu_count dal conf, MARCATO stale."""
        audit = HardwareAudit()
        with mock.patch.object(HardwareAudit, "_read_sysfs",
                               return_value=None), \
             mock.patch("buo.unlock.wrappers.bc250_live_manager."
                        "BC250LiveManagerWrapper",
                        return_value=mock.Mock(available=False)), \
             mock.patch.object(Path, "exists", return_value=True), \
             mock.patch.object(Path, "read_text",
                               return_value="  CUs active & routed  : 40/40\n"):
            gpu = audit._audit_gpu()
        self.assertEqual(gpu["cu_count"], 40)
        # Il conf è persistenza, non stato live: il consumatore deve saperlo.
        self.assertIn("conf", gpu["cu_source"])

    def test_runtime_status_wins_over_stale_conf(self):
        """Il routing LIVE batte il conf: conf stale 40 CU vs runtime 24.

        Bug: leggere prima il conf riportava 40 CU mentre il routing reale
        era 24 ("applicato ≠ verificato").
        """
        audit = HardwareAudit()
        wrapper = mock.Mock(available=True)
        wrapper.status.return_value = {
            "stdout": "  CUs active & routed  : 24/40\n"}
        with mock.patch("buo.unlock.wrappers.bc250_live_manager."
                        "BC250LiveManagerWrapper", return_value=wrapper), \
             mock.patch.object(Path, "exists", return_value=True), \
             mock.patch.object(Path, "read_text",
                               return_value="  CUs active & routed  : 40/40\n"):
            cu, source = audit._read_cu_count_umr()
        self.assertEqual(cu, 24)
        self.assertEqual(source, "runtime")

    def test_not_determinable_returns_none(self):
        audit = HardwareAudit()
        wrapper = mock.Mock(available=False)
        with mock.patch("buo.unlock.wrappers.bc250_live_manager."
                        "BC250LiveManagerWrapper", return_value=wrapper), \
             mock.patch.object(Path, "exists", return_value=False):
            cu, source = audit._read_cu_count_umr()
        self.assertIsNone(cu)
        self.assertEqual(source, "non determinabile")


class TestMesaFallback(unittest.TestCase):
    def test_detect_mesa_raw_parses_glxinfo(self):
        audit = HardwareAudit()
        glx = ("name of display: :0\n"
               "OpenGL version string: 4.6 (Compatibility Profile) "
               "Mesa 25.2.4\n")
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout=glx, returncode=0)):
            self.assertEqual(audit._detect_mesa_raw(), "25.2.4")

    def test_detect_mesa_pkg_parses_rpm_version(self):
        audit = HardwareAudit()
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="25.2.4\n",
                                               returncode=0)):
            self.assertEqual(audit._detect_mesa_pkg(), "25.2.4")

    def test_detect_mesa_pkg_none_when_rpm_fails(self):
        audit = HardwareAudit()
        with mock.patch("subprocess.run",
                        return_value=mock.Mock(stdout="", returncode=1)):
            self.assertIsNone(audit._detect_mesa_pkg())

    def test_audit_mesa_uses_rpm_when_glxinfo_headless(self):
        """SSH senza display → glxinfo vuoto → fallback rpm, non null."""
        audit = HardwareAudit()

        def fake_run(cmd, **kwargs):
            if cmd[0] == "glxinfo":
                return mock.Mock(stdout="", returncode=1)
            if cmd[0] == "rpm":
                return mock.Mock(stdout="25.2.4\n", returncode=0)
            raise AssertionError(f"comando inatteso: {cmd}")

        with mock.patch("subprocess.run", side_effect=fake_run):
            mesa = audit._audit_mesa()
        self.assertEqual(mesa["version"], "25.2")
        self.assertTrue(mesa["meets_minimum"])


if __name__ == "__main__":
    unittest.main()


class TestAcpiAudit(unittest.TestCase):
    """ACPI: il segnale è la entry del deployment BOOTATO (ACPIFix.verify).

    Prima l'audit accettava QUALSIASI entry BLS con un blob: con un blob
    residuo su una entry vecchia diceva "fix presente" mentre la macchina
    bootava senza tabelle (fail-open). Su ostree i nomi SSDT*CST in /sys non
    sopravvivono (il kernel li fonde in SSDT1..N) → cst/pst derivano da lì.
    """

    def _distro(self, tool):
        return mock.Mock(initramfs_tool=tool)

    def _ssdt_dir(self, tmp, names):
        tables = Path(tmp) / "tables"
        tables.mkdir()
        for n in names:
            (tables / n).write_text("")
        return tables

    def test_booted_entry_verify_is_the_signal(self):
        audit = HardwareAudit()
        with mock.patch("buo.fix.acpi.detect_distro",
                        return_value=self._distro("ostree")), \
             mock.patch("buo.fix.acpi.ACPIFix.verify",
                        return_value=True) as verify:
            acpi = audit._audit_acpi()
        verify.assert_called_once()
        self.assertTrue(acpi["boot_fix_present"])
        self.assertTrue(acpi["cst_present"])
        self.assertTrue(acpi["pst_present"])

    def test_stale_blob_on_another_entry_is_not_a_fix(self):
        """verify() False (blob su entry NON bootata) → tabelle mancanti."""
        audit = HardwareAudit()
        with mock.patch("buo.fix.acpi.detect_distro",
                        return_value=self._distro("ostree")), \
             mock.patch("buo.fix.acpi.ACPIFix.verify", return_value=False):
            acpi = audit._audit_acpi()
        self.assertFalse(acpi["boot_fix_present"])
        self.assertFalse(acpi["cst_present"])
        self.assertFalse(acpi["pst_present"])

    def test_non_ostree_keeps_table_names_as_signal(self):
        """dracut/initramfs-tools: i nomi in /sys SONO il segnale."""
        import tempfile
        audit = HardwareAudit()
        with tempfile.TemporaryDirectory() as tmp:
            tables = self._ssdt_dir(tmp, ["SSDT-CST", "SSDT-PST"])
            with mock.patch("buo.fix.acpi.detect_distro",
                            return_value=self._distro("dracut")), \
                 mock.patch("buo.fix.acpi.ACPIFix.verify",
                            return_value=False), \
                 mock.patch("buo.audit.hardware.Path",
                            return_value=tables):
                acpi = audit._audit_acpi()
        self.assertTrue(acpi["cst_present"])
        self.assertTrue(acpi["pst_present"])
        # Il blob sulla entry non è il segnale su queste distro
        self.assertFalse(acpi["boot_fix_present"])

class GttConditionalTestCase(unittest.TestCase):
    """`gtt_limited` dipende dall'EFFETTO (ttm.pages_limit runtime).

    Campo 12/09/2026: `buo probe` dichiarava "GTT limitato a ~7.4 GiB" mentre
    il karg era attivo e `mem_info_gtt_total` valeva 11.50 GiB — un problema
    affermato a priori, come i sensori SuperIO.
    """

    def _ids(self, pages):
        from unittest import mock as m
        from buo.audit.problems import ProblemDetector
        with m.patch("buo.audit.problems.gtt_pages_limit",
                     return_value=(pages, "sysfs (rt)")):
            return [(p["id"], p["title"]) for p in ProblemDetector().detect({})]

    def test_no_problem_when_karg_is_effective(self):
        ids = [i for i, _ in self._ids(3014656)]
        self.assertNotIn("gtt_limited", ids)

    def test_problem_when_gtt_is_still_limited(self):
        found = [t for i, t in self._ids(1944679) if i == "gtt_limited"]
        self.assertTrue(found)
        self.assertIn("7.4 GiB", found[0])

    def test_problem_is_honest_when_not_readable(self):
        found = [t for i, t in self._ids(None) if i == "gtt_limited"]
        self.assertTrue(found)
        self.assertIn("non verificabile", found[0])


class SuperIOConditionalTestCase(unittest.TestCase):
    """`superio_missing` dipende dall'EFFETTO, non è un problema "a priori".

    Era fra i problemi dichiarati "sempre presenti su una BC-250 stock": su una
    macchina con ventole e PWM attivi (nct6686 + fan2 > 0) il report affermava
    un difetto che non esisteva.
    """

    def _ids(self, fan_ok):
        from unittest import mock as m
        from buo.audit.problems import ProblemDetector
        with m.patch("buo.fix.fan.sensor_effect",
                     return_value=(fan_ok, "motivo di prova")):
            return [p["id"] for p in ProblemDetector().detect({})]

    def test_no_superio_problem_when_sensors_active(self):
        self.assertNotIn("superio_missing", self._ids(True))

    def test_superio_problem_when_sensors_missing(self):
        self.assertIn("superio_missing", self._ids(False))


if __name__ == "__main__":
    unittest.main()
