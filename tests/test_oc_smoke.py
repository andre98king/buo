#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test di CpuSmoke (spec p3_smoke del motore): reader MOCK + stress finto.
Copertura: pass, thermal, critical, stretch, whea (+whitelist), timeout,
rc≠0, marcatore scritto/pulito, hang da marcatore stale, mock mode,
stress-ng mai --timeout 0. Mai hardware reale.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.oc.constants import SMOKE_STRESS_S
from buo.oc.smoke import CpuSmoke


class FakeReader:
    """Reader mock: temps/freq controllabili."""

    def __init__(self, temp=60.0, freq=3700):
        self.temp = temp
        self.freq = freq

    def get_cpu_temp(self):
        return self.temp

    def get_cpu_freq(self):
        return self.freq


class FakeSeqReader(FakeReader):
    """Reader stateful: consuma una lista di freq (una per campione)."""

    def __init__(self, freqs, temp=60.0):
        super().__init__(temp=temp)
        self._freqs = list(freqs)

    def get_cpu_freq(self):
        return self._freqs.pop(0)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.oc = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def smoke(self, reader, timeout_s=10, **kw):
        s = CpuSmoke(reader, oc_dir=self.oc, timeout_s=timeout_s, **kw)
        # dmesg mockato: nessun subprocess reale (Popen patchato a parte)
        s._dmesg_snapshot = mock.Mock(return_value=[])
        return s

    def _popen(self, first_none=True, returncode=0, timeout=False):
        proc = mock.Mock()
        if timeout:
            proc.poll.side_effect = lambda: None   # mai termina → timeout
        elif first_none:
            proc.poll.side_effect = [None, 0, 0, 0, 0]
        else:
            proc.poll.return_value = 0
        proc.returncode = returncode
        return proc


class TestSmokeOutcome(Base):
    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_pass(self, popen):
        popen.return_value = self._popen(first_none=True)
        r = self.smoke(FakeReader(temp=60, freq=3700)).run(3700, 975)
        self.assertTrue(r.ok)
        self.assertIsNone(r.cause)
        self.assertTrue(r.marker_cleared)

    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_thermal_under_hard_passes(self, popen):
        """Politica 2 livelli (03/09): 86°C e 90°C in smoke (sintetico,
        in game ~70-75°C) PASSANO — prima fallivano col gate 85/90."""
        popen.return_value = self._popen(first_none=True)
        r = self.smoke(FakeReader(temp=86, freq=3700)).run(3700, 975)
        self.assertTrue(r.ok)
        self.assertIsNone(r.cause)
        r90 = self.smoke(FakeReader(temp=90, freq=3700)).run(3700, 975)
        self.assertTrue(r90.ok)

    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_thermal_at_hard_fails(self, popen):
        """Fail termico SOLO all'HARD (LIMITS.cpu.temp_max = 95)."""
        from buo.constants import LIMITS
        popen.return_value = self._popen(first_none=True)
        r = self.smoke(
            FakeReader(temp=LIMITS.cpu.temp_max + 1, freq=3700)
        ).run(3700, 975)
        self.assertFalse(r.ok)
        # il kill anticipato a >= HARD produce "critical"; "thermal" è il
        # ramo di valutazione finale — entrambi = fail (l'importante: <HARD passa)
        self.assertIn(r.cause, ("critical", "thermal"))

    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_stretch(self, popen):
        popen.return_value = self._popen(first_none=True)
        # freq_warmup_s=0: il mock simula il loop intero (il warmup serve
        # solo sul campo per il ramp della freq dopo l'apply)
        r = self.smoke(FakeReader(temp=60, freq=3600),
                       freq_warmup_s=0).run(3700, 975)
        self.assertFalse(r.ok)
        self.assertEqual(r.cause, "stretch")

    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_whea(self, popen):
        popen.return_value = self._popen(first_none=True)
        s = self.smoke(FakeReader(temp=60, freq=3700))
        s._dmesg_snapshot = mock.Mock(side_effect=[
            ["base line"],
            ["base line", "mce: [Hardware Error]: machine check (x)"],
        ])
        r = s.run(3700, 975)
        self.assertFalse(r.ok)
        self.assertEqual(r.cause, "whea")
        self.assertEqual(r.whea_delta, 1)

    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_whea_whitelist_ignored(self, popen):
        popen.return_value = self._popen(first_none=True)
        s = self.smoke(FakeReader(temp=60, freq=3700))
        s._dmesg_snapshot = mock.Mock(side_effect=[
            [],
            ["pcieport 0000:00:01.0: AER: corrected error received"],
        ])
        r = s.run(3700, 975)
        self.assertTrue(r.ok)   # AER corrected → whitelisted

    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_stress_rc_nonzero(self, popen):
        popen.return_value = self._popen(first_none=False, returncode=1)
        r = self.smoke(FakeReader(temp=60, freq=3700)).run(3700, 975)
        self.assertFalse(r.ok)
        self.assertEqual(r.cause, "stress")

    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_timeout(self, popen):
        popen.return_value = self._popen(timeout=True)
        r = self.smoke(FakeReader(temp=60, freq=3700), timeout_s=2).run(
            3700, 975)
        self.assertFalse(r.ok)
        self.assertEqual(r.cause, "timeout")

    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_freq_min_tracked(self, popen):
        popen.return_value = self._popen(first_none=True)
        reader = FakeReader(temp=60, freq=3700)
        r = self.smoke(reader, freq_warmup_s=0).run(3700, 975)
        self.assertEqual(r.freq_min, 3700)

    @mock.patch("buo.oc.smoke.time.monotonic")
    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_freq_cutoff_end_of_run(self, popen, monotonic):
        """Fine run: i worker stress-ng escono in stagger al loro timeout
        (~30s) e cpu0 crolla a idle col parent ancora vivo — campionare lì
        = FALSO stretch con drop dall'8% al 60% in UN campione (osservato
        sul campo a t≈30s: 1398/1552/2028/2883/3194 MHz, artefatto ~1 run
        su 3, anche su STOCK). Il cutoff temporale (elapsed ≥ SMOKE_STRESS_S
        − 1 = 29s) esclude la finestra di rilascio: il campione a 29.6s
        con freq crollata NON conta per freq_min."""
        # started, cond₁, elapsed₁, cond₂, elapsed₂, duration
        monotonic.side_effect = [1000.0, 1001.5, 1001.5,
                                 1029.6, 1029.6, 1002.0]
        proc = self._popen(first_none=True)
        proc.poll.side_effect = [None, None, 0, 0]   # 2 iterazioni del loop
        popen.return_value = proc
        r = self.smoke(FakeSeqReader([3825, 2000]),
                       timeout_s=60, freq_warmup_s=0).run(3825, 975)
        self.assertTrue(r.ok)
        self.assertIsNone(r.cause)
        self.assertEqual(r.freq_min, 3825)   # il 2000 (coda) è stato ignorato

    @mock.patch("buo.oc.smoke.time.monotonic")
    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_freq_cutoff_keeps_midrun_samples(self, popen, monotonic):
        """Un crollo a METÀ run (elapsed 2.5s < cutoff, carico pieno) è
        tracciato: la stretch vera resta rilevata anche col cutoff."""
        monotonic.side_effect = [1000.0, 1001.5, 1001.5,
                                 1002.5, 1002.5, 1003.0]
        proc = self._popen(first_none=True)
        proc.poll.side_effect = [None, None, 0, 0]
        popen.return_value = proc
        r = self.smoke(FakeSeqReader([3825, 2000]),
                       timeout_s=60, freq_warmup_s=0).run(3825, 975)
        self.assertFalse(r.ok)
        self.assertEqual(r.cause, "stretch")
        self.assertEqual(r.freq_min, 2000)


class TestMarker(Base):
    @mock.patch("buo.oc.smoke.subprocess.Popen")
    def test_marker_written_and_cleared(self, popen):
        popen.return_value = self._popen(first_none=True)
        marker = self.oc / "smoke.marker.json"
        s = self.smoke(FakeReader(temp=60, freq=3700))
        original = s._write_marker
        seen = []

        def spy_write(freq, vid):
            original(freq, vid)
            seen.append(marker.exists())   # True subito dopo la scrittura

        s._write_marker = spy_write
        r = s.run(3700, 975)
        self.assertTrue(seen and seen[0])   # marcatore scritto DURANTE lo smoke
        self.assertFalse(marker.exists())   # e pulito alla fine
        self.assertTrue(r.marker_cleared)

    @mock.patch("buo.oc.smoke.boot_epoch", return_value=10**9)
    def test_stale_marker_is_hang_fail_closed(self, _boot):
        marker = self.oc / "smoke.marker.json"
        marker.write_text(json.dumps({"freq": 3700, "vid_cap": 975,
                                      "kind": "smoke", "started_epoch": 1}))
        r = self.smoke(FakeReader(temp=60, freq=3700)).run(3700, 975)
        self.assertFalse(r.ok)
        self.assertEqual(r.cause, "hang")
        self.assertFalse(r.marker_cleared)

    def test_no_stale_without_marker(self):
        self.assertFalse(self.smoke(FakeReader()).stale_smoke_marker())


class TestMockMode(Base):
    def test_mock_mode_with_mockhardware(self):
        from buo.utils.mock import MockHardware
        hw = MockHardware()
        s = CpuSmoke(hw, mock=True, oc_dir=self.oc, mock_hardware=hw)
        r = s.run(3500, None)   # MockHardware: cpu_freq 3500, temp 45
        self.assertTrue(r.ok)
        self.assertFalse((self.oc / "smoke.marker.json").exists())

    def test_mock_mode_no_hardware_ok(self):
        s = CpuSmoke(FakeReader(), mock=True, oc_dir=self.oc,
                     mock_hardware=None)
        r = s.run(3700, 975)
        self.assertTrue(r.ok)   # senza letture: nessun gate scatta

    def test_default_stress_cmd_never_timeout_zero(self):
        s = CpuSmoke(FakeReader(), oc_dir=self.oc)
        cmd = s._stress_cmd
        self.assertIn("--timeout", cmd)
        val = cmd[cmd.index("--timeout") + 1]
        self.assertNotEqual(val, "0")          # MAI --timeout 0
        self.assertEqual(val, str(SMOKE_STRESS_S))
        self.assertIn("--verify", cmd)


class TestDryRun(Base):
    """M2: smoke in --dry-run è SIMULATO — nessun stress-ng reale, nessun
    marcatore scritto, ok senza valori inventati (C1: mock_hw assente)."""

    def test_dry_run_no_subprocess_no_marker(self):
        s = CpuSmoke(FakeReader(temp=60, freq=3700), dry_run=True,
                     oc_dir=self.oc)
        with mock.patch("buo.oc.smoke.subprocess.Popen") as popen:
            r = s.run(3700, 975)
        popen.assert_not_called()
        self.assertTrue(r.ok)
        self.assertTrue(r.marker_cleared)
        self.assertFalse((self.oc / "smoke.marker.json").exists())

    def test_dry_run_reads_only_from_mock_hardware(self):
        from buo.utils.mock import MockHardware
        hw = MockHardware()   # cpu_freq 3500, temp 45
        s = CpuSmoke(hw, dry_run=True, oc_dir=self.oc, mock_hardware=hw)
        r = s.run(3500, None)
        self.assertTrue(r.ok)
        self.assertEqual(r.freq_min, 3500)    # lettura dal mock_hw

    def test_dry_run_ignores_stale_marker(self):
        """In dry-run un marcatore stale NON blocca (non è un run reale)."""
        marker = self.oc / "smoke.marker.json"
        marker.write_text(json.dumps({"freq": 3700, "vid_cap": 975,
                                      "kind": "smoke", "started_epoch": 1}))
        s = CpuSmoke(FakeReader(temp=60, freq=3700), dry_run=True,
                     oc_dir=self.oc)
        r = s.run(3700, 975)
        self.assertTrue(r.ok)
        self.assertFalse(r.cause == "hang")


class TestDefaultReader(Base):
    """FIX 2: su path REALE reader=None → RealHardwareReader di default
    (import lazy in __init__); mock/dry-run NON costruiscono MAI un reader
    reale (C1: mai hardware finto, mai letture reali in simulazione)."""

    def test_real_reader_none_builds_hardware_reader(self):
        with mock.patch("buo.safety.reader.RealHardwareReader") as cls:
            s = CpuSmoke(None, oc_dir=self.oc)
        cls.assert_called_once()
        self.assertIs(s.reader, cls.return_value)

    def test_simulated_never_builds_hardware_reader(self):
        with mock.patch("buo.safety.reader.RealHardwareReader") as cls:
            CpuSmoke(None, mock=True, oc_dir=self.oc)
            CpuSmoke(None, dry_run=True, oc_dir=self.oc)
            cls.assert_not_called()

    def test_reader_build_failure_degrades(self):
        """Se RealHardwareReader() fallisce lo smoke degrada (reader=None,
        niente abort): _safe_temp/_safe_freq → None, mai eccezioni."""
        with mock.patch("buo.safety.reader.RealHardwareReader",
                        side_effect=RuntimeError("no hardware")):
            s = CpuSmoke(None, oc_dir=self.oc)
        self.assertIsNone(s.reader)


if __name__ == "__main__":
    unittest.main()
