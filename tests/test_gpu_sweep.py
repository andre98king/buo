#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test della ricerca per-silicio dell'undervolt GPU (design
research/DESIGN_GPU_UV.md, §10 — 14 casi).

Seam di test: GPUUndervoltOptimizer(mock=False, governor=FakeGov,
probe=FakeProbe, monitor=FakeMonitor). FakeProbe consulta la mappa
`gpu_stable_voltages` (freq → tensione minima stabile) e registra le
chiamate; FakeGov cattura le scritture config e il ripristino dei bytes
originali. Nessun hardware reale, nessuna rete.
"""

import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

from buo.config import BUOConfig
from buo.constants import LIMITS
from buo.exceptions import SafetyViolation
from buo.optimize.gpu import GPUUndervoltOptimizer, ProbeResult


class FakeGov:
    """Fake del GovernorWrapper: cattura write_config/start/stop e scrive
    davvero su un file temporaneo (il restore dei bytes originali è
    verificato sul contenuto del file)."""

    def __init__(self, config_path, start_ok=True, stop_ok=True,
                 write_ok=True):
        self.config_path = Path(config_path)
        self.start_ok = start_ok
        self.stop_ok = stop_ok
        self.write_ok = write_ok
        self.writes = []   # [(safe_points, kwargs)]
        self.starts = 0
        self.stops = 0

    def write_config(self, safe_points, **kwargs):
        self.writes.append((list(safe_points), dict(kwargs)))
        if not self.write_ok:
            return False
        self.config_path.write_text(
            f"# config candidata\npoints = {len(safe_points)}\n",
            encoding="utf-8")
        return True

    def write_default_config(self):
        return self.write_config([
            {"freq": 1000, "voltage": 800},
            {"freq": 1500, "voltage": 900},
            {"freq": 2000, "voltage": 1000},
        ])

    def start(self):
        self.starts += 1
        return self.start_ok

    def stop(self):
        self.stops += 1
        return self.stop_ok

    def is_running(self):
        return True


class FakeProbe:
    """Fake del ciclo di vita di un candidato: stabile se `mv` >= tensione
    minima stabile della mappa (freq assente = sempre stabile)."""

    def __init__(self, stable_map=None, confirm_fail=None, raise_on=None,
                 test_seconds=30, confirm_seconds=60):
        self.stable_map = dict(stable_map or {})
        self.confirm_fail = set(confirm_fail or [])
        self.raise_on = set(raise_on or [])
        self.test_seconds = test_seconds
        self.confirm_seconds = confirm_seconds
        self.calls = []   # [(freq, mv, seconds)]

    def __call__(self, freq, mv, seconds):
        self.calls.append((freq, mv, seconds))
        if (freq, mv) in self.raise_on:
            raise RuntimeError("probe simulato crashato")
        stable = True
        min_stable = self.stable_map.get(freq)
        if min_stable is not None:
            stable = mv >= min_stable
        is_confirm = seconds == self.confirm_seconds
        reason = None
        if stable and is_confirm and (freq, mv) in self.confirm_fail:
            stable = False
            reason = "confirm fallita"
        if not stable and reason is None:
            reason = "instabile"
        return ProbeResult(stable, reason, 40.0, 85.0)


class FakeMonitor:
    def __init__(self, violated_at=1):
        self.checks = 0
        self.violated_at = violated_at

    def is_violation(self):
        self.checks += 1
        return self.checks >= self.violated_at

    def get_violation_reason(self):
        return "test violation"


class FakeStress:
    """Fake di StressTest per il ciclo di vita REALE del probe
    (probe=None → self._probe): _run_loaded sempre stabile (rc=0)."""

    def __init__(self, rc=0, gpu_temp=40.0):
        self.rc = rc
        self.gpu_temp = gpu_temp
        self.runs = []   # [(cmd, seconds)]

    def _run_loaded(self, cmd, seconds, reader, budget, on_tick=None):
        if on_tick is not None:
            on_tick()   # un campione sotto carico (fake deterministico)
        self.runs.append((list(cmd), seconds))
        return (self.rc, 40.0, self.gpu_temp, 85.0)


class _SweepBase(unittest.TestCase):
    """Base condivisa: config temporanea + FakeGov + which(furmark) finto."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / "config.toml"
        self.original = (b"# config originale\nmin = 1000\nmax = 2230\n"
                         b"[[safe-points]]\nfrequency = 1500\nvoltage = 900\n")
        self.cfg_path.write_bytes(self.original)
        self.gov = FakeGov(config_path=self.cfg_path)
        # Per default il tool di stress "esiste" (furmark finto)
        self._which_patch = mock.patch(
            "buo.optimize.gpu.which", return_value="/usr/bin/furmark")
        self._which_patch.start()
        self.addCleanup(self._which_patch.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def make_opt(self, probe=None, monitor=None, reader=None, stress=None,
                 governor=None, vddgfx_reader=None):
        return GPUUndervoltOptimizer(
            mock=False,
            governor=governor if governor is not None else self.gov,
            stress=stress,
            monitor=monitor,
            reader=reader,
            probe=probe,
            vddgfx_reader=vddgfx_reader,
        )

    def sweep(self, **overrides):
        # temp_gate NON esiste più come opzione (politica 03/09): il
        # gate termico dei probe È l'HARD LIMITS.gpu.temp_max, letto
        # direttamente in _interpret_probe — non un parametro dello sweep.
        params = {
            "enabled": True,
            "freqs": [1500],
            "step_mv": 25,
            "floor_mv": 700,
            "max_steps": 5,
            "test_seconds": 30,
            "confirm_seconds": 60,
            "max_minutes": 15,
        }
        params.update(overrides)
        return params


class TestSweepAlgorithm(_SweepBase):
    def test_should_find_min_below_community(self):
        """#1: chip stabile a 750 mV → winner 800 (4 discese da 900)."""
        probe = FakeProbe(stable_map={1500: 750})
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(res["source"], "per-silicon")
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 800}])
        # candidati provati: 900, 875, 850, 825, 800 + conferma su 800
        voltages = [c[1] for c in probe.calls]
        self.assertEqual(voltages, [900, 875, 850, 825, 800, 800])
        self.assertEqual(probe.calls[-1], (1500, 800, 60))

    def test_should_respect_floor(self):
        """#2: floor 700 → nessun candidato sotto 700 nei probe."""
        probe = FakeProbe(stable_map={1500: 700})
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200,
                           sweep=self.sweep(floor_mv=700, max_steps=10))
        voltages = [c[1] for c in probe.calls]
        self.assertTrue(voltages, "deve aver provato almeno un candidato")
        self.assertTrue(all(v >= 700 for v in voltages),
                        f"candidati sotto il floor: {voltages}")
        self.assertEqual(res["safe_points"][0]["voltage"], 700)

    def test_should_step_back_on_crash_point(self):
        """#3: 775 fallisce (chip stabile da 800) → lo sweep si ferma lì."""
        probe = FakeProbe(stable_map={1500: 800})
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200, sweep=self.sweep(max_steps=10))
        voltages = [c[1] for c in probe.calls]
        self.assertIn(775, voltages)          # il punto di crash è stato provato
        self.assertNotIn(750, voltages[:-1])  # niente dopo 775 prima della conferma
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 800}])
        self.assertEqual(probe.calls[-1], (1500, 800, 60))

    def test_should_fallback_to_community_when_start_point_fails(self):
        """#4: partenza community 900 fallisce → tabella community, ZERO
        probe successivi, config ripristinata."""
        probe = FakeProbe(stable_map={1500: 950})
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(res["source"], "community_defaults")
        self.assertEqual(res["safe_points"],
                         [{"freq": 1500, "voltage": 900},
                          {"freq": 2000, "voltage": 1000}])
        self.assertEqual(probe.calls, [(1500, 900, 30)])
        self.assertEqual(self.cfg_path.read_bytes(), self.original)

    def test_should_step_up_when_confirm_fails(self):
        """#5: winner 725 fallisce la conferma, 750 passa → winner 750."""
        probe = FakeProbe(stable_map={1500: 725},
                          confirm_fail={(1500, 725)})
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200, sweep=self.sweep(max_steps=10))
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 750}])
        calls = probe.calls
        self.assertIn((1500, 725, 60), calls)   # confirm fallita sul winner
        self.assertIn((1500, 750, 60), calls)   # gradino su → stabile

    def test_should_clamp_final_points_to_floor(self):
        """#15: probe vkmark che SCENDE a 700 (chip 'stabile' anche lì,
        nessun floor SMU rilevato: il probe è troppo leggero) → i
        safe_points FINALI sono clampati al floor di config (800): mai
        punti sotto il floor-FurMark; metadata clamped_to_floor;
        best_efficiency ricalcolato sui punti finali."""
        probe = FakeProbe(stable_map={})       # sempre stabile (artefatto)
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200,
                           sweep=self.sweep(floor_mv=800, max_steps=10))
        self.assertEqual(res["source"], "per-silicon")
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 800}])
        # la discesa è arrivata davvero sotto il floor di config (700)
        self.assertLessEqual(min(v for _, v, _ in probe.calls), 700)
        self.assertTrue(res["sweep"]["clamped_to_floor"])
        self.assertEqual(res["best_efficiency"],
                         {"freq": 1500, "voltage": 800})

    def test_should_clamp_final_points_to_custom_floor(self):
        """Override del floor a 750 → il clamp finale applica 750 (non
        il default 800)."""
        probe = FakeProbe(stable_map={})
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200,
                           sweep=self.sweep(floor_mv=750, max_steps=10))
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 750}])
        self.assertTrue(res["sweep"]["clamped_to_floor"])

    def test_should_not_flag_when_winner_not_raised(self):
        """Vincitore già al floor (max_steps limita la discesa prima di
        superarlo) → nessun clamp, nessun clamped_to_floor nei metadata."""
        probe = FakeProbe(stable_map={})
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200,
                           sweep=self.sweep(floor_mv=800, max_steps=5))
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 800}])
        self.assertNotIn("clamped_to_floor", res["sweep"])


class TestTempGate(unittest.TestCase):
    """#6: gate termico per-punto = HARD (politica 03/09: il gate dei
    probe È LIMITS.gpu.temp_max, NON il target operativo 85)."""

    def test_should_fail_when_gpu_temp_above_gate(self):
        r = GPUUndervoltOptimizer._interpret_probe(
            0, LIMITS.gpu.temp_max + 1, LIMITS.gpu.temp_max)
        self.assertFalse(r.stable)
        self.assertIn(str(LIMITS.gpu.temp_max), r.reason)

    def test_should_pass_when_gpu_temp_below_gate(self):
        r = GPUUndervoltOptimizer._interpret_probe(
            0, LIMITS.gpu.temp_max - 5, LIMITS.gpu.temp_max)
        self.assertTrue(r.stable)
        self.assertEqual(r.reason, "ok")

    def test_should_fail_on_nonzero_rc(self):
        r = GPUUndervoltOptimizer._interpret_probe(
            1, 40.0, LIMITS.gpu.temp_max)
        self.assertFalse(r.stable)

    def test_88c_synthetic_is_stable_above_old_gate(self):
        """88°C sintetici (sopra il vecchio gate 85, sotto l'HARD 105)
        NON bocciano il candidato: il delta sintetico-vs-reale è gestito
        dal target operativo, non dal criterio dello sweep."""
        r = GPUUndervoltOptimizer._interpret_probe(
            0, 88.0, LIMITS.gpu.temp_max)
        self.assertTrue(r.stable)
        self.assertEqual(r.reason, "ok")


class TestSafetyAndContract(_SweepBase):
    def test_should_abort_on_monitor_violation(self):
        """#7: monitor violato prima del 2° candidato → SafetyViolation,
        config ripristinata ai bytes originali."""
        probe = FakeProbe(stable_map={1500: 700})
        # check 1 = inizio frequenza, check 2 = 1° candidato (probe),
        # check 3 = 2° candidato → abort prima del probe 2
        monitor = FakeMonitor(violated_at=3)
        opt = self.make_opt(probe=probe, monitor=monitor)
        with self.assertRaises(SafetyViolation) as ctx:
            opt.optimize(start_freq=1200, sweep=self.sweep(max_steps=10))
        self.assertIn("test violation", str(ctx.exception))
        self.assertEqual(len(probe.calls), 1)  # abort prima del 2° candidato
        self.assertEqual(self.cfg_path.read_bytes(), self.original)

    def test_should_keep_output_contract(self):
        """#8: chiavi esatte, punti ordinati/monotoni/clampati,
        best_efficiency = min(v/f), metadata sweep additivi."""
        probe = FakeProbe(stable_map={})
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200,
                           sweep=self.sweep(freqs=[1200, 1500, 2000]))
        self.assertEqual(set(res.keys()),
                         {"safe_points", "best_efficiency", "source", "sweep"})
        self.assertEqual(res["source"], "per-silicon")
        freqs = [p["freq"] for p in res["safe_points"]]
        volts = [p["voltage"] for p in res["safe_points"]]
        self.assertEqual(freqs, sorted(freqs))
        self.assertEqual(volts, sorted(volts))          # monotona non-decrescente
        self.assertTrue(all(v >= 700 for v in volts))   # ≥ floor
        self.assertTrue(all(v <= 1050 for v in volts))  # ≤ max_voltage
        best = min(res["safe_points"], key=lambda p: p["voltage"] / p["freq"])
        self.assertEqual(res["best_efficiency"], best)
        sw = res["sweep"]
        for key in ("enabled", "freqs", "step_mv", "floor_mv", "points_tested",
                    "failed_points", "duration_s", "results"):
            self.assertIn(key, sw)
        self.assertEqual(sw["freqs"], [1200, 1500, 2000])
        self.assertGreaterEqual(sw["points_tested"], 1)
        for entry in sw["results"]:
            self.assertEqual(
                set(entry.keys()),
                {"freq", "voltage", "stable", "gpu_temp_max",
                 "power_max", "reason"})


class TestProbeGateIsHardLimit(_SweepBase):
    """Politica termica a due livelli (03/09): il gate termico dei probe
    dello sweep È l'HARD (LIMITS.gpu.temp_max), NON il target operativo
    85 — un candidato che sotto FurMark tocca 100 °C (sopra il vecchio
    gate, sotto l'HARD) è STABILE e può vincere (prima → fallback
    community). Sopra l'HARD il candidato resta bocciato."""

    def setUp(self):
        super().setUp()
        # il probe reale fa un settle di 2s per candidato: silenzia il
        # sonno nei test (nessuna dipendenza dal timing reale)
        self._sleep_patch = mock.patch("buo.optimize.gpu.time.sleep")
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    def test_candidate_peaking_100c_is_accepted(self):
        stress = FakeStress(gpu_temp=100.0)
        opt = self.make_opt(probe=None, stress=stress, reader=object())
        with mock.patch.object(opt, "_read_dmesg", return_value=[]):
            res = opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(res["source"], "per-silicon")
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 800}])

    def test_candidate_peaking_above_hard_is_rejected(self):
        stress = FakeStress(gpu_temp=LIMITS.gpu.temp_max + 1)
        opt = self.make_opt(probe=None, stress=stress, reader=object())
        with mock.patch.object(opt, "_read_dmesg", return_value=[]):
            res = opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(res["source"], "community_defaults")


class TestConfigOptions(unittest.TestCase):
    """#9: default e clamp delle 8 opzioni gpu_sweep_* (gpu_sweep_temp_gate
    RIMOSSA con la politica 03/09: il gate dei probe È l'HARD)."""

    def test_defaults(self):
        cfg = BUOConfig()
        self.assertTrue(cfg.undervolt_gpu_sweep_enabled)
        self.assertEqual(cfg.undervolt_gpu_sweep_freqs, [1200, 1500, 2000])
        self.assertEqual(cfg.undervolt_gpu_sweep_step_mv, 25)
        # floor di SICUREZZA (30/08): default = floor FurMark misurato
        # su Cyan Skillfish (~800 mV) — punti sotto il floor in config
        # fanno partire il governor in hang
        self.assertEqual(cfg.undervolt_gpu_sweep_floor_mv, 800)
        self.assertEqual(cfg.undervolt_gpu_sweep_max_steps, 5)
        self.assertEqual(cfg.undervolt_gpu_sweep_test_seconds, 30)
        self.assertEqual(cfg.undervolt_gpu_sweep_confirm_seconds, 60)
        self.assertEqual(cfg.undervolt_gpu_sweep_max_minutes, 15)
        # gpu_sweep_temp_gate rimosso (politica 03/09)
        self.assertFalse(hasattr(cfg, "undervolt_gpu_sweep_temp_gate"))

    def test_clamps(self):
        cfg = BUOConfig({"phases": {"undervolt": {
            "gpu_sweep_floor_mv": 100,
            "gpu_sweep_step_mv": 200,
            "gpu_sweep_max_steps": 99,
            "gpu_sweep_test_seconds": 5,
            "gpu_sweep_confirm_seconds": 5,
            "gpu_sweep_max_minutes": 0,
        }}})
        self.assertEqual(cfg.undervolt_gpu_sweep_floor_mv, 700)   # MAI sotto 700
        self.assertEqual(cfg.undervolt_gpu_sweep_step_mv, 50)
        self.assertEqual(cfg.undervolt_gpu_sweep_max_steps, 10)
        self.assertEqual(cfg.undervolt_gpu_sweep_test_seconds, 15)
        self.assertEqual(cfg.undervolt_gpu_sweep_confirm_seconds, 15)
        self.assertEqual(cfg.undervolt_gpu_sweep_max_minutes, 1)
        # floor oltre l'hard limit assoluto → clampato a 1100
        cfg = BUOConfig({"phases": {"undervolt": {"gpu_sweep_floor_mv": 1500}}})
        self.assertEqual(cfg.undervolt_gpu_sweep_floor_mv, 1100)

    def test_removed_temp_gate_key_warns_not_silent(self):
        """Un vecchio buo.yaml con gpu_sweep_temp_gate → avviso fail-soft
        'chiave IGNORATA' (schema piatto), MAI silenzio né applicazione
        (il gate dei probe È l'HARD, non configurabile)."""
        with self.assertLogs("buo.config", level="WARNING") as cm:
            BUOConfig({"phases": {"undervolt": {
                "gpu_sweep_temp_gate": 85}}})
        self.assertTrue(
            any("gpu_sweep_temp_gate" in msg for msg in cm.output))

    def test_floor_override_750(self):
        """Override del floor a 750 → applicato (clamp finale a 750,
        non al default 800)."""
        cfg = BUOConfig({"phases": {"undervolt": {
            "gpu_sweep_floor_mv": 750}}})
        self.assertEqual(cfg.undervolt_gpu_sweep_floor_mv, 750)

    def test_invalid_floor_warns_and_defaults(self):
        """Valore non numerico → warning (fail-soft) + default 800."""
        with self.assertLogs("buo.config", level="WARNING") as cm:
            cfg = BUOConfig({"phases": {"undervolt": {
                "gpu_sweep_floor_mv": "abc"}}})
        self.assertEqual(cfg.undervolt_gpu_sweep_floor_mv, 800)
        self.assertTrue(
            any("gpu_sweep_floor_mv" in msg for msg in cm.output))

    def test_confirm_zero_means_skip(self):
        cfg = BUOConfig({"phases": {"undervolt": {
            "gpu_sweep_confirm_seconds": 0}}})
        self.assertEqual(cfg.undervolt_gpu_sweep_confirm_seconds, 0)

    def test_freqs_filtered_to_freq_steps(self):
        # fuori dalla griglia FREQ_STEPS → default
        cfg = BUOConfig({"phases": {"undervolt": {"gpu_sweep_freqs": [500, 3000]}}})
        self.assertEqual(cfg.undervolt_gpu_sweep_freqs, [1200, 1500, 2000])
        # non crescenti → default
        cfg = BUOConfig({"phases": {"undervolt": {"gpu_sweep_freqs": [1500, 1200]}}})
        self.assertEqual(cfg.undervolt_gpu_sweep_freqs, [1200, 1500, 2000])
        # non numeriche → default
        cfg = BUOConfig({"phases": {"undervolt": {"gpu_sweep_freqs": ["x", 1500]}}})
        self.assertEqual(cfg.undervolt_gpu_sweep_freqs, [1200, 1500, 2000])

    def test_enabled_accepts_only_bool(self):
        self.assertFalse(BUOConfig({"phases": {"undervolt": {
            "gpu_sweep_enabled": 0}}}).undervolt_gpu_sweep_enabled)
        self.assertTrue(BUOConfig({"phases": {"undervolt": {
            "gpu_sweep_enabled": 1}}}).undervolt_gpu_sweep_enabled)
        self.assertFalse(BUOConfig({"phases": {"undervolt": {
            "gpu_sweep_enabled": False}}}).undervolt_gpu_sweep_enabled)
        # stringa non valida → default True
        self.assertTrue(BUOConfig({"phases": {"undervolt": {
            "gpu_sweep_enabled": "false"}}}).undervolt_gpu_sweep_enabled)

    def test_to_dict_contains_sweep_options(self):
        d = BUOConfig().to_dict()["phases"]["undervolt"]
        for key in ("gpu_sweep_enabled", "gpu_sweep_freqs", "gpu_sweep_step_mv",
                    "gpu_sweep_floor_mv", "gpu_sweep_max_steps",
                    "gpu_sweep_test_seconds", "gpu_sweep_confirm_seconds",
                    "gpu_sweep_max_minutes"):
            self.assertIn(key, d)
        # gpu_sweep_temp_gate rimosso (politica 03/09): non serializzato
        self.assertNotIn("gpu_sweep_temp_gate", d)


class TestPrereqsAndFallbacks(_SweepBase):
    def test_should_fallback_when_no_stress_tool(self):
        """#10: nessun tool di stress → nessuna scrittura, community."""
        probe = FakeProbe()
        opt = self.make_opt(probe=probe)
        with mock.patch("buo.optimize.gpu.which", return_value=None):
            res = opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(res["source"], "community_defaults")
        self.assertEqual(probe.calls, [])
        self.assertEqual(self.gov.writes, [])
        self.assertEqual(self.cfg_path.read_bytes(), self.original)

    def test_should_fallback_when_governor_not_stoppable(self):
        gov = FakeGov(config_path=self.cfg_path, stop_ok=False)
        probe = FakeProbe()
        opt = self.make_opt(probe=probe, governor=gov)
        res = opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(res["source"], "community_defaults")
        self.assertEqual(probe.calls, [])
        self.assertEqual(self.cfg_path.read_bytes(), self.original)

    def test_should_clamp_starts_to_max_voltage(self):
        """#11: max_voltage=900 → partenze clampate a 900."""
        probe = FakeProbe()
        opt = self.make_opt(probe=probe)
        res = opt.optimize(start_freq=1200, max_voltage=900,
                           sweep=self.sweep(freqs=[1200, 1500, 2000]))
        voltages = [c[1] for c in probe.calls]
        self.assertTrue(voltages)
        self.assertTrue(all(v <= 900 for v in voltages),
                        f"tensioni oltre max_voltage: {voltages}")
        # a 2000 la partenza community (1000) è clampata a 900
        self.assertIn((2000, 900, 30), probe.calls)
        for point in res["safe_points"]:
            self.assertLessEqual(point["voltage"], 900)

    def test_should_keep_mock_invariant(self):
        """#12: ramo mock invariato (canned, source="mock")."""
        opt = GPUUndervoltOptimizer(mock=True)
        res = opt.optimize()
        self.assertEqual(res["source"], "mock")
        self.assertEqual(res["safe_points"], [
            {"freq": 1200, "voltage": 800},
            {"freq": 1500, "voltage": 900},
            {"freq": 1700, "voltage": 940},
        ])
        # in mock le dipendenze iniettate vengono ignorate
        opt2 = GPUUndervoltOptimizer(mock=True, governor=self.gov,
                                     probe=FakeProbe())
        self.assertEqual(opt2.optimize(sweep=self.sweep())["source"], "mock")

    def test_should_fallback_when_governor_start_fails(self):
        """#13: governor start fallito → point fail; è il sanity →
        community; nessuna config residua."""
        gov = FakeGov(config_path=self.cfg_path, start_ok=False)
        opt = self.make_opt(governor=gov, probe=None,
                            stress=object(), reader=object())
        res = opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(res["source"], "community_defaults")
        self.assertEqual(gov.starts, 1)
        self.assertEqual(self.cfg_path.read_bytes(), self.original)

    def test_should_restore_config_when_probe_raises(self):
        """#14: probe che solleva → config ripristinata ai bytes originali."""
        probe = FakeProbe(raise_on={(1500, 900)})
        opt = self.make_opt(probe=probe)
        with self.assertRaises(RuntimeError):
            opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(self.cfg_path.read_bytes(), self.original)
        # il governor è stato fermato anche in caso di eccezione
        self.assertGreaterEqual(self.gov.stops, 1)

    def test_stress_cmd_uses_documented_furmark_syntax(self):
        """Il comando stress usa la sintassi UFFICIALE FurMark 2
        (stress-and-quit con --max-time, geeks3d.com/furmark/command-line)
        — mai --duration/--seconds inventati."""
        with mock.patch("buo.optimize.gpu.which",
                        return_value="/usr/bin/furmark"):
            opt = self.make_opt(probe=FakeProbe())
            cmd = opt._gpu_stress_cmd(30)
        self.assertEqual(cmd[0], "furmark")
        self.assertIn("--demo", cmd)
        self.assertIn("furmark-gl", cmd)
        self.assertIn("--max-time", cmd)
        self.assertIn("30", cmd)
        self.assertNotIn("--seconds", cmd)
        self.assertNotIn("--duration", cmd)

    def test_stress_cmd_vkmark_fallback(self):
        """Senza furmark, fallback vkmark con durata per-scena (rc=0 dopo
        N secondi) — l'unico fallback con controllo durata reale."""
        def fake_which(tool):
            return "/usr/bin/vkmark" if tool == "vkmark" else None
        with mock.patch("buo.optimize.gpu.which", side_effect=fake_which):
            opt = self.make_opt(probe=FakeProbe())
            cmd = opt._gpu_stress_cmd(30)
        self.assertEqual(cmd[0], "vkmark")
        self.assertIn("desktop:duration=30", cmd)

    def test_stress_tool_excludes_glmark2(self):
        """glmark2 non ha controllo durata (--seconds inesistente): NON
        deve essere accettato come tool di stress (fail-closed community)."""
        def fake_which(tool):
            return "/usr/bin/glmark2" if tool == "glmark2" else None
        with mock.patch("buo.optimize.gpu.which", side_effect=fake_which):
            opt = self.make_opt(probe=FakeProbe())
            self.assertIsNone(opt._gpu_stress_tool())
            res = opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(res["source"], "community_defaults")
        self.assertEqual(self.gov.writes, [])


class TestSmuFloor(_SweepBase):
    """FLOOR SMU Cyan Skillfish (misurato sul campo 30/08): sotto ~800 mV
    l'SMU NON scende. FIX 2 (sotto carico): la lettura VDDGFX avviene
    DURANTE lo stress del probe (campionamento live, MASSIMO dei campioni)
    — a IDLE l'SMU abbassa la tensione (743 mV reale a target 800) e il
    confronto reale > target+soglia non scattava MAI (bug 8b5a062: lo
    sweep scendeva sotto il floor). Il fake reader è REALISTICO: sotto
    carico reale = target - droop (~15) finché l'SMU segue (target ≥
    800); sotto il floor la VDDGFX resta incollata a ~790.

    Usano il ciclo di vita REALE del probe (probe=None → self._probe) con
    FakeGov/FakeStress: la lettura VDDGFX è iniettata come `vddgfx_reader`
    (callable target_mv → mV reali sotto carico, o None = non leggibile)."""

    def setUp(self):
        super().setUp()
        # il probe reale fa un settle di 2s per candidato: nei test si
        # silenzia il sonno (nessuna dipendenza dal timing reale)
        self._sleep_patch = mock.patch("buo.optimize.gpu.time.sleep")
        self._sleep_patch.start()
        self.addCleanup(self._sleep_patch.stop)

    def test_should_stop_at_smu_floor_and_winner_is_floor(self):
        """Sotto carico reale = target - droop per target ≥ 800 e 790 sotto
        (il floor): lo sweep si ferma al primo target NON seguito (775) e
        il vincitore è l'ULTIMO punto in cui la tensione ha SEGUITO il
        target (800, NON 750/700); nessun punto sotto il floor nei
        safe_points; metadata smu_floor_mv=800."""
        stress = FakeStress()
        opt = self.make_opt(probe=None, stress=stress, reader=object(),
                            vddgfx_reader=lambda target:
                            target - 15 if target >= 800 else 790)
        with mock.patch.object(opt, "_read_dmesg", return_value=[]):
            res = opt.optimize(start_freq=1200,
                               sweep=self.sweep(max_steps=10))
        self.assertEqual(res["source"], "per-silicon")
        # il vincitore è il floor rilevato, MAI sotto
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 800}])
        for point in res["safe_points"]:
            self.assertGreaterEqual(point["voltage"], 800)
        self.assertEqual(res["sweep"]["smu_floor_mv"], 800)
        # l'ultimo entry è il punto in cui il floor è stato rilevato:
        # STRESSATO (la lettura è sotto carico) e riportato smu_floor
        entries = res["sweep"]["results"]
        self.assertEqual(entries[-1]["voltage"], 775)
        self.assertIn("smu_floor", entries[-1]["reason"])
        self.assertEqual(len(stress.runs), 6,
                         "i 6 candidati 900→775 vengono stressati, il "
                         "floor è rilevato al 6° sotto carico")

    def test_should_keep_behavior_when_vddgfx_unavailable(self):
        """Lettura VDDGFX non disponibile (None, anche sotto carico) →
        comportamento attuale: nessun blocco, conferma eseguita, nessun
        smu_floor_mv."""
        stress = FakeStress()
        opt = self.make_opt(probe=None, stress=stress, reader=object(),
                            vddgfx_reader=lambda target: None)
        with mock.patch.object(opt, "_read_dmesg", return_value=[]):
            res = opt.optimize(start_freq=1200, sweep=self.sweep())
        self.assertEqual(res["source"], "per-silicon")
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 800}])
        self.assertNotIn("smu_floor_mv", res["sweep"])
        self.assertEqual(len(stress.runs), 6)   # 5 discese + 1 conferma

    def test_should_not_flag_floor_when_smu_follows_target(self):
        """SMU che segue SEMPRE il target (reale = target - droop anche
        sotto 800) → nessun falso positivo: la discesa arriva al floor di
        config (700) senza blocchi SMU."""
        stress = FakeStress()
        opt = self.make_opt(probe=None, stress=stress, reader=object(),
                            vddgfx_reader=lambda target: target - 15)
        with mock.patch.object(opt, "_read_dmesg", return_value=[]):
            res = opt.optimize(start_freq=1200,
                               sweep=self.sweep(max_steps=10))
        self.assertEqual(res["safe_points"], [{"freq": 1500, "voltage": 700}])
        self.assertNotIn("smu_floor_mv", res["sweep"])

    def test_read_vddgfx_parses_real_output_format(self):
        """BUG di campo (30/08): il regex cercava 'VDDGFX: <n> mV' ma la
        riga REALE di amdgpu_pm_info è '\\t824 mV (VDDGFX)' (label DOPO il
        valore, senza prefisso) → il reader restituiva SEMPRE None → floor
        mai rilevato. Il parsing deve leggere il formato reale."""
        import re as _re
        from buo.optimize.gpu import GPUUndervoltOptimizer
        real_output = (
            "0 MHz (PSTATE_MCLK)\n"
            "\t824 mV (VDDGFX)\n"
            "\t1081 mV (VDDNB)\n"
        )
        opt = GPUUndervoltOptimizer(mock=True)
        with mock.patch("buo.utils.shell.run_command",
                        return_value=(0, real_output, "")):
            value = opt._read_vddgfx(800)
        self.assertEqual(value, 824)
        # output senza la riga VDDGFX → None (fail-soft)
        with mock.patch("buo.utils.shell.run_command",
                        return_value=(0, "0 MHz (PSTATE_MCLK)\n", "")):
            self.assertIsNone(opt._read_vddgfx(800))
        # comando fallito → None
        with mock.patch("buo.utils.shell.run_command",
                        return_value=(1, "", "err")):
            self.assertIsNone(opt._read_vddgfx(800))


if __name__ == "__main__":
    unittest.main()
