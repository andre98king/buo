#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Rilevatore di fault GPU dal journal del kernel del boot corrente (P4).

Su questa APU un fault GPU non è recuperabile (niente GPU reset), quindi i
segnali sono di tipo "crash totale": ring timeout / VM fault di amdgpu,
`GPU reset failed`, `amdgpu_job_timedout`. Il rumore noto NON deve essere
segnalato e lo stato non determinabile deve dare None (C1), mai un esito
inventato. Runner iniettabile: mai journalctl reale.
"""

import unittest

from buo.validate.gpu_faults import (FAULT_PATTERNS, NOISE_MARKERS,
                                     fault_lines, fault_signatures,
                                     gpu_fault_since_boot, noise_markers)


class TestFaultSignatures(unittest.TestCase):
    def test_signatures_exposed(self):
        self.assertTrue(fault_signatures())
        self.assertEqual(fault_signatures(), FAULT_PATTERNS)
        self.assertIn("dal_irq_service_dummy", noise_markers())
        self.assertEqual(noise_markers(), NOISE_MARKERS)


class TestFaultLines(unittest.TestCase):
    """Righe VERE dal campo (kernel 7.x, amdgpu gfx1013)."""

    REAL_FAULTS = [
        "[ 123.456] [drm:amdgpu_job_timedout [amdgpu]] *ERROR* ring gfx_0.0.0 "
        "timeout, signaled seq=..., emitted seq=...",
        "[ 124.001] amdgpu 0000:01:00.0: amdgpu: GPU reset(1) failed",
        "[ 124.100] amdgpu 0000:01:00.0: amdgpu: Failed to reset the GPU",
        "[ 125.000] amdgpu 0000:01:00.0: [gfxhub0] no-retry page fault "
        "(GCVM_L2_PROTECTION_FAULT_STATUS:0x00000000)",
        "[ 130.000] amdgpu 0000:01:00.0: amdgpu: GPU fault detected",
        "[ 131.000] [drm:amdgpu_device_gpu_recover] *ERROR* GPU hang in ring "
        "comp_1.0.0",
        "[ 132.000] amdgpu: ring sdma0 timeout, signaled seq=1, emitted seq=2",
    ]

    NOISE = [
        "amdgpu 0000:01:00.0: dal_irq_service_dummy: 0x00000000",
        "amdgpu 0000:01:00.0: Failed to clear hpd",
        "amdgpu 0000:01:00.0: [drm] vendor infoframe too short",
        "[    0.000] MCE: In-kernel MCE decoding enabled",
    ]

    def test_real_faults_detected(self):
        for line in self.REAL_FAULTS:
            self.assertEqual(fault_lines([line]), [line], line)

    def test_known_noise_excluded(self):
        self.assertEqual(fault_lines(self.NOISE), [])

    def test_noise_wins_over_signature(self):
        """Riga col rumore noto E una firma → scartata (fail-safe: il
        rumore noto non deve produrre un rollback)."""
        line = ("amdgpu 0000:01:00.0: Failed to clear hpd (GPU reset failed)")
        self.assertEqual(fault_lines([line]), [])

    def test_clean_journal(self):
        lines = [
            "[    0.000] Linux version 7.2.1-ogc3.1",
            "amdgpu 0000:01:00.0: amdgpu: SE 2, SH per SE 2, CU per SH 10, "
            "active_cu_number 24",
            "usb 1-2: reset high-speed USB device",
        ]
        self.assertEqual(fault_lines(lines), [])

    def test_only_fault_lines_returned(self):
        out = fault_lines(self.REAL_FAULTS[:2] + self.NOISE)
        self.assertEqual(out, self.REAL_FAULTS[:2])

    def test_case_insensitive(self):
        """Un check case-sensitive ha già causato un falso negativo sul
        campo (lezione 11/09)."""
        self.assertEqual(
            fault_lines(["amdgpu: gpu reset failed"]),
            ["amdgpu: gpu reset failed"])


class TestGpuFaultSinceBoot(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _runner(self, rc=0, out="", raise_exc=None):
        def run(cmd, **kw):
            self.calls.append((cmd, kw))
            if raise_exc:
                raise raise_exc
            return rc, out, ""
        return run

    def test_uses_journalctl_boot_current_kernel_only(self):
        """Solo `-b -k`: il journal completo sarebbe lento e rumoroso."""
        out = gpu_fault_since_boot(runner=self._runner(0, "riga pulita"))
        self.assertEqual(out, [])
        cmd = self.calls[0][0]
        self.assertEqual(cmd[0], "journalctl")
        self.assertIn("-b", cmd)
        self.assertIn("-k", cmd)

    def test_returns_fault_lines(self):
        out = gpu_fault_since_boot(
            runner=self._runner(0, "\n".join(TestFaultLines.REAL_FAULTS)))
        self.assertEqual(out, TestFaultLines.REAL_FAULTS)

    def test_none_when_not_determinable(self):
        """rc != 0, eccezione (journalctl assente) e timeout → None (C1):
        mai "pulito" né "in fault" per uno stato sconosciuto."""
        self.assertIsNone(gpu_fault_since_boot(runner=self._runner(1, "")))
        self.assertIsNone(gpu_fault_since_boot(
            runner=self._runner(raise_exc=FileNotFoundError("no journalctl"))))
        self.assertIsNone(gpu_fault_since_boot(
            runner=self._runner(raise_exc=TimeoutError("timeout"))))

    def test_timeout_forwarded(self):
        gpu_fault_since_boot(runner=self._runner(0, ""), timeout=7)
        self.assertEqual(self.calls[0][1].get("timeout"), 7)

    def test_no_wgp_attribution_in_output(self):
        """Il rilevatore NON attribuisce la colpa a una WGP (dal journal
        non si può): l'esito è solo l'elenco delle righe."""
        out = gpu_fault_since_boot(runner=self._runner(
            0, TestFaultLines.REAL_FAULTS[0]))
        self.assertTrue(all(isinstance(ln, str) for ln in out))
        self.assertNotIn("wgp", " ".join(out).lower())


if __name__ == "__main__":
    unittest.main()
