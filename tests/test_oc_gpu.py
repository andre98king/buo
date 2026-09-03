#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test della gestione OC/UV GPU per il tab OC della cockpit unificata
(buo/oc/gpu.py): parse di config.toml (ok/mancante/corrotto), validazione
preset (floor 800, coerenza punti/range/tetto = ultimo safe-point), apply
con GovernorWrapper (fake/mock: mai subprocess), corrispondenza
preset↔curva attiva. Nessun hardware reale, nessuna lettura di /etc.
"""

import tempfile
import unittest
from pathlib import Path

from buo.oc.gpu import (
    DEFAULT_GPU_PRESETS,
    GpuCurve,
    GpuPoint,
    GpuPreset,
    active_gpu_preset,
    apply_gpu_preset,
    curve_matches_preset,
    gpu_apply_text,
    gpu_panel_text,
    gpu_preset_rows,
    parse_gpu_config,
    read_active_curve,
    read_config_text,
    validate_gpu_preset,
)
from buo.optimize.governor import GovernorWrapper


def _config_text(points, min_freq=1000, max_freq=1800,
                 throttling=85, recovery=75) -> str:
    """Testo TOML minimale di config.toml (le sezioni lette dal pannello)."""
    pts = "\n".join(
        f"[[safe-points]]\nfrequency = {f}\nvoltage = {v}\n" for f, v in points)
    return (
        "# config di test\n"
        "[frequency-range]\n"
        f"min = {min_freq}\nmax = {max_freq}\n"
        "[temperature]\n"
        f"throttling = {throttling}\nthrottling_recovery = {recovery}\n"
        + pts
    )


class TestParseConfig(unittest.TestCase):
    def test_parses_valid_config(self):
        text = _config_text([(1000, 800), (1400, 800), (1800, 800)])
        curve = parse_gpu_config(text)
        self.assertIsNotNone(curve)
        assert curve is not None
        self.assertEqual(curve.min_freq, 1000)
        self.assertEqual(curve.max_freq, 1800)
        self.assertEqual(curve.throttling, 85)
        self.assertEqual(curve.recovery, 75)
        self.assertEqual(curve.points, (
            GpuPoint(1000, 800), GpuPoint(1400, 800), GpuPoint(1800, 800)))

    def test_missing_or_empty_text_returns_none(self):
        self.assertIsNone(parse_gpu_config(None))
        self.assertIsNone(parse_gpu_config(""))

    def test_corrupt_text_returns_none(self):
        self.assertIsNone(parse_gpu_config("[[[ non-toml !!"))
        self.assertIsNone(parse_gpu_config("frequency-range = not a table"))

    def test_no_safe_points_returns_none(self):
        text = ("[frequency-range]\nmin = 1000\nmax = 1800\n"
                "[temperature]\nthrottling = 85\n")
        self.assertIsNone(parse_gpu_config(text))

    def test_read_config_text_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inesistente.toml"
            self.assertIsNone(read_config_text(str(path)))


class TestValidatePreset(unittest.TestCase):
    def test_valid_presets_pass(self):
        for p in DEFAULT_GPU_PRESETS:
            ok, reason = validate_gpu_preset(p)
            self.assertTrue(ok, f"{p.id}: {reason}")

    def test_below_floor_rejected(self):
        p = GpuPreset(id="x", name="X", min_freq=1000, max_freq=1800,
                      throttling=85, recovery=75,
                      points=(GpuPoint(1000, 750), GpuPoint(1800, 800)))
        ok, reason = validate_gpu_preset(p)
        self.assertFalse(ok)
        self.assertIn("floor", reason)
        self.assertIn("800", reason)

    def test_point_outside_range_rejected(self):
        p = GpuPreset(id="x", name="X", min_freq=1000, max_freq=1500,
                      throttling=85, recovery=75,
                      points=(GpuPoint(1800, 800),))
        ok, reason = validate_gpu_preset(p)
        self.assertFalse(ok)
        self.assertIn("range", reason)

    def test_ceiling_beyond_last_point_rejected(self):
        """Il tetto duro è l'ULTIMO safe-point: max_freq oltre l'ultimo
        punto creerebbe un cap senza curva (interpolazioni fuori range)."""
        p = GpuPreset(id="x", name="X", min_freq=1000, max_freq=2000,
                      throttling=85, recovery=75,
                      points=(GpuPoint(1000, 800), GpuPoint(1800, 800)))
        ok, reason = validate_gpu_preset(p)
        self.assertFalse(ok)
        self.assertIn("tetto", reason)

    def test_empty_points_rejected(self):
        p = GpuPreset(id="x", name="X", min_freq=1000, max_freq=1800,
                      throttling=85, recovery=75, points=())
        ok, reason = validate_gpu_preset(p)
        self.assertFalse(ok)


class FakeGovernor:
    """Fake di GovernorWrapper: registra le chiamate, mai subprocess."""

    def __init__(self, write_ok=True, restart_ok=True):
        self.write_calls = []
        self.restart_calls = 0
        self._write_ok = write_ok
        self._restart_ok = restart_ok

    def write_config(self, safe_points, min_freq=None, max_freq=None,
                     throttling=None, recovery=None):
        self.write_calls.append({
            "safe_points": safe_points, "min_freq": min_freq,
            "max_freq": max_freq, "throttling": throttling,
            "recovery": recovery,
        })
        return self._write_ok

    def restart(self):
        self.restart_calls += 1
        return self._restart_ok


class TestApplyPreset(unittest.TestCase):
    def test_apply_writes_preset_and_restarts(self):
        gov = FakeGovernor()
        preset = DEFAULT_GPU_PRESETS[0]  # uv-1800
        res = apply_gpu_preset(gov, preset)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(gov.write_calls), 1)
        call = gov.write_calls[0]
        self.assertEqual(call["safe_points"], preset.to_safe_points())
        self.assertEqual(call["min_freq"], 1000)
        self.assertEqual(call["max_freq"], 1800)
        self.assertEqual(call["throttling"], 85)
        self.assertEqual(call["recovery"], 75)
        self.assertEqual(gov.restart_calls, 1)

    def test_invalid_preset_never_written(self):
        gov = FakeGovernor()
        bad = GpuPreset(id="x", name="X", min_freq=1000, max_freq=1800,
                        throttling=85, recovery=75,
                        points=(GpuPoint(1000, 750),))
        res = apply_gpu_preset(gov, bad)
        self.assertFalse(res["ok"])
        self.assertFalse(res["written"])
        self.assertEqual(gov.write_calls, [])
        self.assertEqual(gov.restart_calls, 0)

    def test_write_failure_reported(self):
        gov = FakeGovernor(write_ok=False)
        res = apply_gpu_preset(gov, DEFAULT_GPU_PRESETS[0])
        self.assertFalse(res["ok"])
        self.assertFalse(res["written"])
        self.assertEqual(gov.restart_calls, 0)

    def test_restart_failure_warns_with_restore_hint(self):
        gov = FakeGovernor(write_ok=True, restart_ok=False)
        res = apply_gpu_preset(gov, DEFAULT_GPU_PRESETS[1])
        self.assertFalse(res["ok"])
        self.assertTrue(res["written"])
        self.assertIn("non riavviato", res.get("reason", ""))

    def test_mock_governor_wrapper_no_subprocess(self):
        """GovernorWrapper(mock=True) applica senza subprocess né /etc."""
        gov = GovernorWrapper(mock=True)
        res = apply_gpu_preset(gov, DEFAULT_GPU_PRESETS[0])
        self.assertTrue(res["ok"], res)


class TestActivePreset(unittest.TestCase):
    def test_daily_config_matches_uv1800(self):
        curve = parse_gpu_config(_config_text(
            [(1000, 800), (1400, 800), (1800, 800)], max_freq=1800))
        self.assertIsNotNone(curve)
        preset = active_gpu_preset(curve)
        self.assertIsNotNone(preset)
        self.assertEqual(preset.id, "uv-1800")

    def test_stock_cap_config_matches_stock1500(self):
        curve = parse_gpu_config(_config_text([(1500, 800)], max_freq=1500))
        preset = active_gpu_preset(curve)
        self.assertIsNotNone(preset)
        self.assertEqual(preset.id, "stock-1500")

    def test_custom_curve_no_match(self):
        curve = parse_gpu_config(_config_text(
            [(1000, 800), (1400, 820), (1800, 850)], max_freq=1800))
        self.assertIsNone(active_gpu_preset(curve))

    def test_none_curve_no_match(self):
        self.assertIsNone(active_gpu_preset(None))

    def test_curve_matches_preset_ignores_throttle(self):
        """Il confronto è su punti+range (le soglie termiche possono
        differire senza cambiare identità del preset)."""
        curve = GpuCurve(min_freq=1000, max_freq=1800, throttling=90,
                         recovery=80, points=(GpuPoint(1000, 800),
                                              GpuPoint(1400, 800),
                                              GpuPoint(1800, 800)))
        self.assertTrue(curve_matches_preset(curve, DEFAULT_GPU_PRESETS[0]))


class TestMockFixture(unittest.TestCase):
    def test_mock_curve_is_daily_preset(self):
        """In mock la curva attiva è la fixture (uv-1800) — nessuna /etc."""
        curve = read_active_curve(mock=True)
        self.assertIsNotNone(curve)
        preset = active_gpu_preset(curve)
        self.assertEqual(preset.id if preset else None, "uv-1800")


class TestFormatters(unittest.TestCase):
    def test_panel_text_unreadable(self):
        text = gpu_panel_text(None, None, "?")
        self.assertIn("non leggibile", text)
        self.assertIn("Cosa fare", text)

    def test_panel_text_shows_active_preset_and_curve(self):
        curve = parse_gpu_config(_config_text(
            [(1000, 800), (1400, 800), (1800, 800)], max_freq=1800))
        preset = active_gpu_preset(curve)
        text = gpu_panel_text(curve, preset, "attivo")
        self.assertIn("UV 1800 (daily)", text)
        self.assertIn("1000-1800", text)
        self.assertIn("1000@800", text)
        self.assertIn("governor: attivo", text)

    def test_preset_rows(self):
        rows = gpu_preset_rows(DEFAULT_GPU_PRESETS, active="uv-1800")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][2], "●")
        self.assertEqual(rows[1][2], "")

    def test_apply_text_contains_preset_info(self):
        text = gpu_apply_text(DEFAULT_GPU_PRESETS[0])
        self.assertIn("UV 1800 (daily)", text)
        self.assertIn("1800", text)


class TestGpuSelectionHint(unittest.TestCase):
    """Hint di selezione nel pannello GPU: visibile quando NESSUN preset
    corrisponde alla curva attiva (curva personalizzata), assente quando
    un preset è riconosciuto (●)."""

    def test_custom_curve_shows_selection_hint(self):
        curve = parse_gpu_config(_config_text(
            [(1000, 800), (1400, 820), (1800, 850)], max_freq=1800))
        text = gpu_panel_text(curve, None, "fermo")
        self.assertIn("↑/↓ scegli un preset sotto", text)
        self.assertIn("[g] applica", text)

    def test_known_preset_hides_selection_hint(self):
        curve = parse_gpu_config(_config_text(
            [(1000, 800), (1400, 800), (1800, 800)], max_freq=1800))
        preset = active_gpu_preset(curve)
        text = gpu_panel_text(curve, preset, "attivo")
        self.assertNotIn("scegli un preset sotto", text)

    def test_unreadable_config_keeps_its_cta(self):
        text = gpu_panel_text(None, None, "?")
        self.assertIn("premi g", text)


if __name__ == "__main__":
    unittest.main()
