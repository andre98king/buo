#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test C2: lo stress test campiona LIVE e ABORTA il processo reale."""

import logging
import subprocess
import unittest

from buo.config import BUOConfig
from buo.exceptions import SafetyViolation
from buo.validate.stress import StressTest


class _HotReader:
    def get_cpu_temp(self):
        return 99.0

    def get_gpu_temp(self):
        return 40.0

    def get_total_power(self):
        return 85.0


class _CoolReader:
    def get_cpu_temp(self):
        return 50.0

    def get_gpu_temp(self):
        return 45.0

    def get_total_power(self):
        return 120.0


class _BlindReader:
    def get_cpu_temp(self):
        return None

    def get_gpu_temp(self):
        return None

    def get_total_power(self):
        return None


class TestStressAbort(unittest.TestCase):
    def test_hot_cpu_terminates_process_and_raises(self):
        """Temp CPU oltre il limite → processo terminato + SafetyViolation."""
        stress = StressTest(reader=_HotReader())
        t0 = __import__("time").monotonic()
        with self.assertRaises(SafetyViolation) as ctx:
            stress._run_loaded(["sleep", "30"], 30, _HotReader(), 300)
        self.assertIn("CPU", str(ctx.exception))
        # abort entro pochi secondi, non dopo 30s
        self.assertLess(__import__("time").monotonic() - t0, 10)

    def test_cool_reader_runs_to_completion(self):
        stress = StressTest(reader=_CoolReader())
        rc, cpu_max, gpu_max, power_max = stress._run_loaded(
            ["sleep", "2"], 2, _CoolReader(), 300)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(cpu_max, 50.0)
        self.assertGreaterEqual(gpu_max, 45.0)
        self.assertGreaterEqual(power_max, 120.0)

    def test_progress_ticker_logs(self):
        """Ticker di progresso (UX 04/09): le fasi lunghe (validate 10
        min) loggano una riga INFO periodica — il watch-log non deve
        restare silenzioso per minuti."""
        stress = StressTest(reader=_CoolReader())
        with self.assertLogs(stress.logger, level="INFO") as cm:
            stress._run_loaded(["sleep", "3"], 3, _CoolReader(), 300,
                               progress_s=1)
        progress = [m for m in cm.output if "Stress in corso" in m]
        self.assertTrue(progress, "nessuna riga di progresso loggata")
        self.assertIn("CPU 50", progress[0])
        self.assertIn("GPU 45", progress[0])
        self.assertIn("nessun errore", progress[0])

    def test_blind_reader_no_crash(self):
        """Sensori non leggibili → nessun crash, nessuna violazione."""
        stress = StressTest(reader=_BlindReader())
        rc, cpu_max, gpu_max, power_max = stress._run_loaded(
            ["sleep", "1"], 1, _BlindReader(), 300)
        self.assertEqual(rc, 0)

    def test_monitor_violation_terminates(self):
        """Violazione segnalata dal safety monitor → abort immediato."""
        class FakeMonitor:
            def is_violation(self):
                return True

            def get_violation_reason(self):
                return "test violation"

        stress = StressTest(reader=_CoolReader(), safety_monitor=FakeMonitor())
        with self.assertRaises(SafetyViolation) as ctx:
            stress._run_loaded(["sleep", "30"], 30, _CoolReader(), 300)
        self.assertIn("test violation", str(ctx.exception))

    def test_deadline_enforced(self):
        """Timeout globale: il processo non può superare la deadline."""
        stress = StressTest(reader=_CoolReader())
        stress.deadline_grace = 1
        t0 = __import__("time").monotonic()
        rc, *_ = stress._run_loaded(["sleep", "30"], 1, _CoolReader(), 300)
        self.assertLess(rc, 0)  # terminato con segnale (SIGTERM)
        self.assertLess(__import__("time").monotonic() - t0, 10)

    def test_zero_duration_skips_without_spawning(self):
        """BUG di campo: durata 0 deve saltare SENZA spawnare alcun
        processo (stress-ng --timeout 0 = stress infinito)."""
        import unittest.mock as mock
        with mock.patch("buo.validate.stress.subprocess.Popen",
                        side_effect=AssertionError(
                            "durata 0 NON deve spawnare processi")):
            stress = StressTest(reader=_CoolReader())
            result = stress.run(duration_minutes=0, power_budget=300)
        self.assertTrue(result["passed"])
        self.assertTrue(result["skipped"])
        self.assertIsNone(result["cpu_temp_max"])
        self.assertIsNone(result["gpu_temp_max"])

    def test_positive_duration_still_spawns(self):
        """Durata > 0: il percorso normale resta invariato (spawn reale)."""
        import unittest.mock as mock
        spawned = []
        real_popen = __import__("subprocess").Popen

        def _fake_popen(cmd, **kwargs):
            spawned.append(cmd)
            return real_popen(["sleep", "0.2"], **kwargs)

        with mock.patch("buo.validate.stress.subprocess.Popen",
                        side_effect=_fake_popen), \
             mock.patch("buo.validate.stress.which",
                        side_effect=lambda name: f"/usr/bin/{name}"):
            stress = StressTest(reader=_CoolReader())
            result = stress.run(duration_minutes=1, power_budget=300)
        self.assertTrue(spawned, "durata > 0 deve spawnare lo stress")
        self.assertTrue(result["passed"])


class TestStressScope(unittest.TestCase):
    """Stress test SEPARABILE CPU/GPU (richiesta utente 30/08): validare
    un solo componente (es. overclock/undervolt CPU) non deve caricare
    l'altro — spike termici e consumi non reali. Spia su which/Popen:
    i comandi effettivamente spawnati devono riflettere lo scope."""

    def _spawn_spy(self):
        """Patch which (tutti i tool presenti) e Popen (spia che registra
        i comandi ed esegue un processo innocuo): ritorna la lista dei
        comandi spawnati."""
        import unittest.mock as mock
        spawned = []
        real_popen = subprocess.Popen

        def _fake_popen(cmd, **kwargs):
            # i kwargs (stdout/stderr=pipe) vengono ignorati: il processo
            # innocuo non crea pipe → niente ResourceWarning
            spawned.append(cmd)
            return real_popen(["sleep", "0.2"])

        patches = [
            mock.patch("buo.validate.stress.which",
                       side_effect=lambda name: f"/usr/bin/{name}"),
            mock.patch("buo.validate.stress.subprocess.Popen",
                       side_effect=_fake_popen),
        ]
        for p in patches:
            p.start()
        self.addCleanup(patches[0].stop)
        self.addCleanup(patches[1].stop)
        return spawned

    def test_scope_cpu_skips_gpu(self):
        """scope="cpu" → spawna SOLO stress-ng, NESSUN tool GPU; il
        risultato indica lo scope usato."""
        spawned = self._spawn_spy()
        result = StressTest(reader=_CoolReader()).run(
            duration_minutes=1, power_budget=300, scope="cpu")
        self.assertEqual(result["scope"], "cpu")
        self.assertTrue(result["passed"])
        cmds = [" ".join(c) for c in spawned]
        self.assertTrue(any("stress-ng" in c for c in cmds),
                        f"manca stress-ng in: {cmds}")
        self.assertFalse(any(("glmark2" in c or "furmark" in c) for c in cmds),
                         f"GPU stressata con scope cpu: {cmds}")

    def test_scope_gpu_skips_cpu(self):
        """scope="gpu" → spawna SOLO il tool GPU, NESSUN stress-ng; il
        risultato indica lo scope usato."""
        spawned = self._spawn_spy()
        result = StressTest(reader=_CoolReader()).run(
            duration_minutes=1, power_budget=300, scope="gpu")
        self.assertEqual(result["scope"], "gpu")
        self.assertTrue(result["passed"])
        cmds = [" ".join(c) for c in spawned]
        self.assertTrue(any(("glmark2" in c or "furmark" in c) for c in cmds),
                        f"manca tool GPU in: {cmds}")
        self.assertFalse(any(("stress-ng" in c or " stress " in c)
                             for c in cmds),
                         f"CPU stressata con scope gpu: {cmds}")

    def test_scope_both_runs_both(self):
        """scope="both" (default) → CPU E GPU spawnati (comportamento
        storico invariato)."""
        spawned = self._spawn_spy()
        result = StressTest(reader=_CoolReader()).run(
            duration_minutes=1, power_budget=300)
        self.assertEqual(result["scope"], "both")
        self.assertTrue(result["passed"])
        cmds = [" ".join(c) for c in spawned]
        self.assertTrue(any("stress-ng" in c for c in cmds))
        self.assertTrue(any(("glmark2" in c or "furmark" in c) for c in cmds))


class TestStressScopeConfig(unittest.TestCase):
    """Config validation.stress_scope: default both, valori noti
    accettati, valori sconosciuti → warning + default both (fail-soft,
    mai bloccante)."""

    def test_default_is_both(self):
        cfg = BUOConfig()
        self.assertEqual(cfg.validation_stress_scope, "both")
        self.assertEqual(cfg.to_dict()["phases"]["validation"]
                         ["stress_scope"], "both")

    def test_known_values_accepted(self):
        for scope in ("both", "cpu", "gpu"):
            cfg = BUOConfig({"phases": {"validation": {
                "stress_scope": scope}}})
            self.assertEqual(cfg.validation_stress_scope, scope)

    def test_invalid_value_warns_and_defaults_to_both(self):
        with self.assertLogs("buo.config", level="WARNING") as cm:
            cfg = BUOConfig({"phases": {"validation": {
                "stress_scope": "apu"}}})
        self.assertEqual(cfg.validation_stress_scope, "both")
        self.assertTrue(any("stress_scope" in m for m in cm.output),
                        f"manca l'avviso stress_scope in: {cm.output}")

    def test_stress_scope_is_a_known_key(self):
        """stress_scope è nello schema _KNOWN_PHASE_KEYS: nessun avviso
        'chiave IGNORATA' (schema piatto, fix 30/08)."""
        logger = logging.getLogger("buo.config")
        records = []
        handler = logging.Handler()
        handler.emit = lambda r: records.append(r.getMessage())
        logger.addHandler(handler)
        try:
            BUOConfig({"phases": {"validation": {"stress_scope": "cpu"}}})
        finally:
            logger.removeHandler(handler)
        self.assertFalse(any("IGNORATA" in m for m in records),
                         f"avvisi inattesi: {records}")


class TestTwoLevelThermalPolicy(unittest.TestCase):
    """Politica termica a due livelli (approvata 03/09): la validate
    PASSA se durante lo stress NON scatta l'HARD (CPU 95 / GPU 105);
    il vecchio gate operativo (CPU 90 / GPU 85) non è più un criterio
    di bocciatura. L'HARD resta fail-closed (abort sopra 95/105)."""

    class _WarmReader:
        """Picco SINTETICO sopra il vecchio gate (90/85) ma sotto l'HARD
        (95/105): prima della politica veniva bocciato ingiustamente
        (dato campo: gaming ~75°C vs FurMark ~91°C, delta ~16°C)."""

        def get_cpu_temp(self):
            return 93.0

        def get_gpu_temp(self):
            return 92.0

        def get_total_power(self):
            return 85.0

    def _spawn_spy(self):
        """Patch which/Popen come TestStressScope: spawna processi innocui."""
        import unittest.mock as mock
        real_popen = subprocess.Popen

        def _fake_popen(cmd, **kwargs):
            return real_popen(["sleep", "0.2"])

        patches = [
            mock.patch("buo.validate.stress.which",
                       side_effect=lambda name: f"/usr/bin/{name}"),
            mock.patch("buo.validate.stress.subprocess.Popen",
                       side_effect=_fake_popen),
        ]
        for p in patches:
            p.start()
        self.addCleanup(patches[0].stop)
        self.addCleanup(patches[1].stop)

    def test_validate_passes_above_old_gate_below_hard(self):
        """GPU 92 / CPU 93 (sopra il vecchio gate 85/90, sotto l'HARD
        105/95): la run di validazione PASSA."""
        self._spawn_spy()
        result = StressTest(reader=self._WarmReader()).run(
            duration_minutes=1, power_budget=300)
        self.assertTrue(result["passed"])
        self.assertEqual(result["cpu_temp_max"], 93.0)
        self.assertEqual(result["gpu_temp_max"], 92.0)

    def test_no_abort_just_below_hard(self):
        """Bordo appena sotto l'HARD (CPU 94 / GPU 104): nessun abort."""
        class _EdgeReader(self._WarmReader):
            def get_cpu_temp(self):
                return 94.0

            def get_gpu_temp(self):
                return 104.0

        rc, cpu_max, gpu_max, power_max = StressTest(
            reader=_EdgeReader())._run_loaded(
            ["sleep", "2"], 2, _EdgeReader(), 300)
        self.assertEqual(rc, 0)
        self.assertEqual(cpu_max, 94.0)
        self.assertEqual(gpu_max, 104.0)

    def test_hard_abort_above_95_cpu(self):
        """HARD rispettato: CPU a 96°C (> 95) → abort."""
        class _OverCpuReader(self._WarmReader):
            def get_cpu_temp(self):
                return 96.0

        with self.assertRaises(SafetyViolation):
            StressTest(reader=_OverCpuReader())._run_loaded(
                ["sleep", "30"], 30, _OverCpuReader(), 300)

    def test_hard_abort_above_105_gpu(self):
        """HARD rispettato: GPU a 106°C (> 105) → abort."""
        class _OverGpuReader(self._WarmReader):
            def get_gpu_temp(self):
                return 106.0

        with self.assertRaises(SafetyViolation):
            StressTest(reader=_OverGpuReader())._run_loaded(
                ["sleep", "30"], 30, _OverGpuReader(), 300)


if __name__ == "__main__":
    unittest.main()
