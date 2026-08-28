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
             mock.patch("os.open", return_value=3), \
             mock.patch("os.pwrite"), \
             mock.patch("os.pread", return_value=struct.pack("<I", 0x3C)), \
             mock.patch("os.close"):
            self.assertIsNone(audit._read_core_mask_smn())

    def test_read_core_mask_smn_none_on_io_error(self):
        audit = HardwareAudit()
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch("os.geteuid", return_value=0), \
             mock.patch("os.open", side_effect=OSError("perm")):
            self.assertIsNone(audit._read_core_mask_smn())


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

    def test_audit_gpu_falls_back_to_live_manager_config(self):
        """num_cu assente (runtime UMR) → cu_count dal config live-manager."""
        audit = HardwareAudit()
        with mock.patch.object(HardwareAudit, "_read_sysfs",
                               return_value=None), \
             mock.patch.object(Path, "exists", return_value=True), \
             mock.patch.object(Path, "read_text",
                               return_value="  CUs active & routed  : 40/40\n"):
            gpu = audit._audit_gpu()
        self.assertEqual(gpu["cu_count"], 40)


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
