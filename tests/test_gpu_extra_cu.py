#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CU extra GPU (16 CU oltre le 24 stock): maschera VALIDATA, opt-in, verdetto
per-WGP, percorso cumulativo live e guardie.

P1: le 16 CU non sono più il default (opt-in `probe.gpu_extra_cu`) e la
    maschera è derivata dalle WGP validate — mai 0x1f hardcoded.
P2: verdetto durevole per-WGP (resta a 32/36 CU) con retro-compatibilità
    del vecchio `never_enable_all`.
P3: `apply(wgps=[...])` = cumulativo live a runtime (una WGP per volta).
P5: guardie (curva conservativa, azzeramento CC, fail-closed sugli input).

Tutto mock/fake: mai hardware reale, mai file di sistema (i percorsi sono
iniettabili).
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.config import BUOConfig
from buo.constants import (MASK_ALL_40, MASK_STOCK_24, all_wgps,
                           cu_count_from_mask, extra_wgps, mask_from_wgps,
                           parse_wgp, stock_wgps, wgps_from_mask)
from buo.unlock.gpu import GPU40CUUnlock
from buo.unlock.validation import (VERDICT_GPU_WGPS_CONDEMNED, UnlockVerdict,
                                   evidence)


class _FakeWrapper:
    """Wrapper finto del live-manager: registra i comandi, nessun exec."""

    available = True

    #: maschera WGP dello stock (3 WGP per SA) e output "cu_target" da
    #: simulare; None = derivato dal comando (comportamento reale).
    def __init__(self, cu_target_override=None):
        self.calls = []
        self.cu_target = cu_target_override

    def run_with_output(self, cmd, **kw):
        self.calls.append(list(cmd))
        args = [a for a in cmd if a != "-y"]
        mask = self._mask_of(args)
        parsed = {"full_die": mask == extra_wgps() and not self._partial(args),
                  "cu_routed": 24, "cu_total": 40, "cu_target": None,
                  "write_mode": None}
        total = 24 + 2 * len(mask)
        parsed["cu_target"] = (self.cu_target if self.cu_target is not None
                               else total)
        parsed["full_die"] = parsed["cu_target"] == 40
        return {"returncode": 0, "stdout": "", "stderr": "",
                "parsed_output": parsed}

    @staticmethod
    def _partial(args):
        return "enable-wgp" in args

    def _mask_of(self, args):
        """WGP extra risultanti dal comando simulato."""
        if args[:2] == ["enable", "all"]:
            return list(extra_wgps())
        if args and args[0] == "enable-wgp":
            return [w for w in args[1:]]
        return []


def _unlock(tmp, wrapper=None, **kw):
    """GPU40CUUnlock con percorsi/verdetto isolati (mai stato reale)."""
    kw.setdefault("verdict", _verdict(tmp))
    g = GPU40CUUnlock(mock=False, use_wrapper=False, **kw)
    g.is_ostree = True
    g.boot_conf_path = str(Path(tmp) / "cu-live.conf")
    g.governor_conf_path = str(Path(tmp) / "governor.toml")
    if wrapper is not None:
        g.wrapper = wrapper
    return g


def _verdict(tmp, verdict=None, wgps=None, name="verdict.json"):
    v = UnlockVerdict(path=Path(tmp) / name, sim=True)
    if verdict:
        v.set("gpu", verdict, evidence(condemned_wgps=wgps) if wgps
              else evidence(cause="test"))
    return v


def _curve(tmp, volts=(800, 900)):
    """Scrive una config governor coi safe-point dati (mV)."""
    path = Path(tmp) / "governor.toml"
    path.write_text("".join("[[safe-points]]\nfrequency = %d\nvoltage = %d\n"
                            % (1000 + i * 100, v)
                            for i, v in enumerate(volts)), encoding="utf-8")
    return str(path)


# ===================================================================== #
# Maschere (P1)
# ===================================================================== #

class TestMaskHelpers(unittest.TestCase):
    """Codifica/decodifica: punto UNICO, valori noti e fail-closed."""

    def test_known_masks_round_trip(self):
        """24/32/36/40 CU: maschera ↔ WGP ↔ conteggio coerenti."""
        cases = [
            (MASK_STOCK_24, 24, 0),
            # 2 WGP extra (una per SA su due righe) = 28 CU; 4 = 32; 6 = 36
            (mask_from_wgps(stock_wgps() + extra_wgps()[:2]), 28, 2),
            (mask_from_wgps(stock_wgps() + extra_wgps()[:4]), 32, 4),
            (mask_from_wgps(stock_wgps() + extra_wgps()[:6]), 36, 6),
            (MASK_ALL_40, 40, 8),
        ]
        for mask, cu, n_extra in cases:
            wgps = wgps_from_mask(mask)
            self.assertEqual(mask_from_wgps(wgps), mask, mask)
            self.assertEqual(cu_count_from_mask(mask), cu, mask)
            self.assertEqual(len([w for w in wgps if w in extra_wgps()]),
                             n_extra, mask)
            self.assertIn("0x", mask)

    def test_stock_and_full_are_universe_subsets(self):
        self.assertEqual(len(all_wgps()), 20)
        self.assertEqual(len(extra_wgps()), 8)
        self.assertEqual(len(stock_wgps()), 12)
        self.assertEqual(mask_from_wgps(stock_wgps()), MASK_STOCK_24)
        self.assertEqual(mask_from_wgps(all_wgps()), MASK_ALL_40)
        self.assertEqual(sorted(list(extra_wgps())), sorted(extra_wgps()))
        # nessuna sovrapposizione stock/extra
        self.assertEqual(set(stock_wgps()) & set(extra_wgps()), set())

    def test_wgp_id_stock_case_36cu(self):
        """Caso reale: board stabile a 36 CU (WGP 0.1.3 e 0.1.4 guaste)."""
        mask = mask_from_wgps(stock_wgps() + [w for w in extra_wgps()
                                              if w not in ("0.1.3", "0.1.4")])
        self.assertEqual(mask, "0x1f,0x07,0x1f,0x1f")
        self.assertEqual(cu_count_from_mask(mask), 36)
        self.assertNotIn("0.1.3", wgps_from_mask(mask))

    def test_anomalous_wgp_ids_raise(self):
        for bad in ["0.1", "0.1.3.4", "0.1.5", "2.0.1", "0.2.1", "x.y.z",
                    "", "0.1.-1"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_wgp(bad)

    def test_anomalous_masks_raise(self):
        for bad in ["0x1f,0x07,0x1f",            # 3 campi
                    "0x1f,0x07,0x1f,0x07,0x07",  # 5 campi
                    "0x1f,0x07,0x1f,0x20",       # oltre i 5 bit
                    "0x1f,0x07,0x1f,-1",         # negativo
                    "0x1f,0x07,0x1f,zz",         # non numerico
                    ""]:
            with self.assertRaises(ValueError, msg=bad):
                wgps_from_mask(bad)
            with self.assertRaises(ValueError, msg=bad):
                cu_count_from_mask(bad)


# ===================================================================== #
# Opt-in (P1)
# ===================================================================== #

class TestExtraCuOptIn(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_config_default_is_off(self):
        """Default prudente: 24 CU stock."""
        cfg = BUOConfig()
        self.assertFalse(cfg.probe_gpu_extra_cu)
        self.assertTrue(cfg.probe_gpu_unlock)

    def test_config_old_file_without_key_is_not_an_error(self):
        """Config scritta prima della chiave: nessun KeyError/avviso chiave
        sconosciuta, valore False (retro-compatibile)."""
        with self.assertNoLogs("buo.config", level="WARNING"):
            cfg = BUOConfig({"phases": {"probe": {"gpu_unlock": True}}})
        self.assertFalse(cfg.probe_gpu_extra_cu)

    def test_config_opt_in_true(self):
        cfg = BUOConfig({"phases": {"probe": {"gpu_extra_cu": True}}})
        self.assertTrue(cfg.probe_gpu_extra_cu)
        self.assertIn("gpu_extra_cu", cfg.to_dict()["phases"]["probe"])

    def test_default_reads_config_off(self):
        """extra_cu=None → dalla config; config di default → nessuna
        abilitazione (nessuna scrittura maschera)."""
        w = _FakeWrapper()
        g = _unlock(self.tmp, wrapper=w)
        with mock.patch("buo.config.BUOConfig.load", return_value=BUOConfig()):
            out = g.apply()
        self.assertFalse(out["applied"])
        self.assertEqual(out["reason"], "extra_cu_disabled")
        self.assertEqual(out["mask"], MASK_STOCK_24)
        self.assertEqual(w.calls, [], "nessuna scrittura con opt-in off")

    def test_opt_in_true_from_config_writes(self):
        _curve(self.tmp)
        w = _FakeWrapper()
        g = _unlock(self.tmp, wrapper=w)
        cfg = BUOConfig({"phases": {"probe": {"gpu_extra_cu": True}}})
        with mock.patch("buo.config.BUOConfig.load", return_value=cfg):
            out = g.apply()
        self.assertTrue(out["applied"])
        self.assertEqual(out["cu_count"], 40)
        self.assertTrue(w.calls)

    def test_persist_refused_without_opt_in(self):
        """Opt-out: NIENTE persistenza (24 CU = stato corretto)."""
        g = _unlock(self.tmp, wrapper=_FakeWrapper(), extra_cu=False)
        out = g.persist()
        self.assertFalse(out["persisted"])
        self.assertEqual(out["reason"], "extra_cu_disabled")
        self.assertFalse(Path(g.boot_conf_path).exists())

    def test_persist_refused_on_aggressive_curve(self):
        """La maschera persistita viene instradata AL BOOT dal servizio:
        senza curva conservativa non si persiste (fail-closed)."""
        _curve(self.tmp, (800, 1000))
        g = _unlock(self.tmp, wrapper=_FakeWrapper(), extra_cu=True)
        out = g.persist()
        self.assertFalse(out["persisted"])
        self.assertEqual(out["reason"], "curve_not_conservative")
        self.assertFalse(Path(g.boot_conf_path).exists())


# ===================================================================== #
# Maschera derivata + verdetto per-WGP (P1/P2)
# ===================================================================== #

class TestDerivedMask(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        _curve(self.tmp, (800, 900))

    def test_apply_uses_derived_mask_36cu(self):
        """WGP 0.1.3/0.1.4 condannate → 36 CU (mai enable all)."""
        w = _FakeWrapper()
        g = _unlock(self.tmp, wrapper=w, extra_cu=True,
                    verdict=_verdict(self.tmp, VERDICT_GPU_WGPS_CONDEMNED,
                                     ["0.1.3", "0.1.4"]))
        out = g.apply()
        self.assertTrue(out["applied"])
        self.assertEqual(out["cu_count"], 36)
        self.assertEqual(out["mask"], "0x1f,0x07,0x1f,0x1f")
        cmds = [[a for a in c if a != "-y"] for c in w.calls]
        self.assertNotIn(["enable", "all"], cmds,
                         "mai 'enable all' con WGP condannate")
        self.assertEqual(cmds[0], ["stock-dispatch"])
        self.assertEqual(cmds[1][0], "enable-wgp")
        self.assertNotIn("0.1.3", cmds[1])
        self.assertNotIn("0.1.4", cmds[1])

    def test_all_condemned_legacy_verdict_keeps_24cu(self):
        """never_enable_all (file esistente, senza lista) → 24 CU stock e
        nessuna scrittura."""
        w = _FakeWrapper()
        g = _unlock(self.tmp, wrapper=w, extra_cu=True,
                    verdict=_verdict(self.tmp, "never_enable_all"))
        out = g.apply()
        self.assertFalse(out["applied"])
        self.assertEqual(out["reason"], "all_extra_condemned")
        self.assertEqual(w.calls, [])

    def test_persist_writes_derived_mask(self):
        """Persistenza: conf con la maschera derivata (36 CU), non 0x1f."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["systemctl", "is-enabled"]:
                return 0, "enabled", ""
            return 1, "", ""

        g = _unlock(self.tmp, wrapper=_FakeWrapper(), extra_cu=True,
                    verdict=_verdict(self.tmp, VERDICT_GPU_WGPS_CONDEMNED,
                                     ["0.1.3", "0.1.4"]))
        with mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            out = g.persist()
        self.assertTrue(out["persisted"])
        conf = Path(g.boot_conf_path).read_text(encoding="utf-8")
        self.assertIn("BC250_WGP_MASKS=0x1f,0x07,0x1f,0x1f", conf)
        self.assertNotIn(MASK_ALL_40, conf)

    def test_persist_full_die_when_nothing_condemned(self):
        g = _unlock(self.tmp, wrapper=_FakeWrapper(), extra_cu=True)

        def fake_run(cmd, **kw):
            return (0, "enabled", "") if cmd[:2] == ["systemctl",
                                                     "is-enabled"] else (1, "", "")

        with mock.patch("buo.utils.shell.run_command", side_effect=fake_run):
            out = g.persist()
        self.assertTrue(out["persisted"])
        self.assertIn(MASK_ALL_40, Path(g.boot_conf_path).read_text())

    def test_effect_verification_fails_closed(self):
        """Lo script riporta un conteggio diverso dall'atteso → NON
        applicato (niente 40 CU dichiarate a vuoto)."""
        g = _unlock(self.tmp, wrapper=_FakeWrapper(cu_target_override=24),
                    extra_cu=True)
        out = g.apply()
        self.assertFalse(out["applied"])
        self.assertIn("maschera non applicata", out["error"])


# ===================================================================== #
# Verdetto per-WGP (P2)
# ===================================================================== #

class TestPerWgpVerdict(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "unlock-verdict.json"
        self.addCleanup(self._tmp.cleanup)

    def _verdict(self):
        return UnlockVerdict(path=self.path, sim=True)

    def test_legacy_never_enable_all_without_list(self):
        """File esistente (schema 1, senza lista) → nessuna WGP extra
        ammessa (retro-compatibile)."""
        self.path.write_text(json.dumps({
            "schema": 1,
            "gpu": {"verdict": "never_enable_all",
                    "evidence": {"cause": "gpu_fault"}},
        }), encoding="utf-8")
        v = UnlockVerdict(path=self.path)
        self.assertEqual(v.get("gpu"), "never_enable_all")
        self.assertEqual(sorted(v.condemned_wgps()), sorted(extra_wgps()))

    def test_per_wgp_list(self):
        v = self._verdict()
        v.set("gpu", VERDICT_GPU_WGPS_CONDEMNED,
              evidence(condemned_wgps=["0.1.3", "0.1.4"]))
        self.assertEqual(v.condemned_wgps(), ["0.1.3", "0.1.4"])

    def test_positive_verdict_keeps_previous_condemnation(self):
        """Un verdetto positivo successivo NON fa dimenticare le WGP
        condannate (altrimenti il run dopo riabilita quella guasta)."""
        v = self._verdict()
        v.set("gpu", VERDICT_GPU_WGPS_CONDEMNED,
              evidence(condemned_wgps=["0.1.3"]))
        v.set("gpu", "stable_short", evidence(seconds=60, temp_max=70))
        self.assertEqual(v.get("gpu"), "stable_short")
        self.assertEqual(v.condemned_wgps(), ["0.1.3"])

    def test_explicit_empty_list_clears_after_recertification(self):
        """Ri-certificazione: verdetto positivo + lista VUOTA esplicita
        azzera le condanne (un verdetto di condanna con lista vuota resta
        invece fail-closed, vedi test_malformed_lists_fail_closed)."""
        v = self._verdict()
        v.set("gpu", VERDICT_GPU_WGPS_CONDEMNED,
              evidence(condemned_wgps=["0.1.3"]))
        v.set("gpu", "stable_short", evidence(condemned_wgps=[]))
        self.assertEqual(v.condemned_wgps(), [])

    def test_malformed_lists_fail_closed(self):
        """Lista inattendibile → TUTTE le extra (mai una WGP guasta
        riabilitata per un errore di parsing)."""
        cases = [
            ["boh"], ["0.9.9"], ["0.0.1"],        # id anomalo / WGP stock
            "0.1.3",                              # non lista
            [["0.1.3"]],                          # tipo sbagliato
        ]
        for bad in cases:
            v = UnlockVerdict(path=Path(self._tmp.name) / "v.json", sim=True)
            v.set("gpu", VERDICT_GPU_WGPS_CONDEMNED, evidence(condemned_wgps=bad))
            with self.assertLogs("buo.unlock.validation", level="WARNING"):
                condemned = v.condemned_wgps()
            self.assertEqual(sorted(condemned), sorted(extra_wgps()),
                             repr(bad))

    def test_empty_list_on_condemnation_is_ambiguous(self):
        """Verdetto di condanna con lista VUOTA (ambiguo) → tutte le
        extra: fail-closed, mai 'nessuna condanna' per contraddizione."""
        v = UnlockVerdict(path=Path(self._tmp.name) / "v.json", sim=True)
        v.set("gpu", VERDICT_GPU_WGPS_CONDEMNED, evidence(condemned_wgps=[]))
        self.assertEqual(sorted(v.condemned_wgps()), sorted(extra_wgps()))

    def test_no_verdict_no_condemnation(self):
        self.assertEqual(self._verdict().condemned_wgps(), [])


# ===================================================================== #
# Percorso cumulativo live (P3)
# ===================================================================== #

class TestCumulativeLive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        _curve(self.tmp, (800, 900))

    def test_one_wgp_at_a_time(self):
        """Una WGP per volta a runtime, nessun reboot, maschera cumulativa."""
        w = _FakeWrapper()
        g = _unlock(self.tmp, wrapper=w, extra_cu=True)
        first = g.apply(wgps=["0.1.3"])
        self.assertTrue(first["applied"])
        self.assertEqual(first["cu_count"], 26)
        self.assertEqual(first["mask"], "0x07,0x0f,0x07,0x07")
        self.assertFalse(first["needs_reboot"])
        second = g.apply(wgps=["0.1.3", "0.1.4"])
        self.assertEqual(second["cu_count"], 28)
        self.assertEqual(second["mask"], "0x07,0x1f,0x07,0x07")
        # ogni scrittura riparte da stock: mai una WGP non validata
        cmds = [[a for a in c if a != "-y"] for c in w.calls]
        self.assertEqual([c[0] for c in cmds],
                         ["stock-dispatch", "enable-wgp"] * 2)

    def test_cumulative_wgps_returns_to_stock_first(self):
        """Il percorso cumulativo riparte SEMPRE da 24 CU stock."""
        w = _FakeWrapper()
        g = _unlock(self.tmp, wrapper=w, extra_cu=True)
        g.apply(wgps=["0.0.3"])
        cmds = [[a for a in c if a != "-y"] for c in w.calls]
        self.assertEqual(cmds[0], ["stock-dispatch"])
        self.assertEqual(cmds[1], ["enable-wgp", "0.0.3"])

    def test_invalid_wgp_input_refused_without_writing(self):
        for bad in [["0.0.1"], ["0.1.5"], ["x"], ["0.1.3", "0.0.2"]]:
            w = _FakeWrapper()
            g = _unlock(self.tmp, wrapper=w, extra_cu=True)
            out = g.apply(wgps=bad)
            self.assertFalse(out["applied"], repr(bad))
            self.assertTrue(out["error"], repr(bad))
            self.assertEqual(w.calls, [], repr(bad))

    def test_full_set_uses_enable_all(self):
        """Tutte le 8 WGP validate → 'enable all' (percorso 40 CU)."""
        w = _FakeWrapper()
        g = _unlock(self.tmp, wrapper=w, extra_cu=True)
        out = g.apply()
        self.assertEqual(out["cu_count"], 40)
        cmds = [[a for a in c if a != "-y"] for c in w.calls]
        self.assertEqual(cmds, [["enable", "all"]])
        self.assertEqual(out["mask"], MASK_ALL_40)

    def test_cumulative_path_then_persist(self):
        """Il percorso cumulativo persiste la maschera parziale provata."""
        g = _unlock(self.tmp, wrapper=_FakeWrapper(), extra_cu=True)
        g.apply(wgps=["0.1.3"])
        with mock.patch("buo.utils.shell.run_command",
                        return_value=(0, "enabled", "")):
            out = g.persist()
        self.assertTrue(out["persisted"])
        self.assertIn("BC250_WGP_MASKS=0x07,0x0f,0x07,0x07",
                      Path(g.boot_conf_path).read_text())


# ===================================================================== #
# Guardie (P5)
# ===================================================================== #

class TestIsEnabledTargetMask(unittest.TestCase):
    """`is_enabled` = maschera TARGET instradata (non solo 40 CU)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        _curve(self.tmp)

    def _with_status(self, routed):
        w = _FakeWrapper()
        w.status = lambda: {"parsed_output": {"cu_routed": routed,
                                              "cu_total": 40,
                                              "full_die": routed == 40}}
        return w

    def test_partial_target_routed(self):
        g = _unlock(self.tmp, wrapper=self._with_status(36), extra_cu=True,
                    verdict=_verdict(self.tmp, VERDICT_GPU_WGPS_CONDEMNED,
                                     ["0.1.3", "0.1.4"]))
        self.assertTrue(g.is_enabled())

    def test_partial_target_not_routed(self):
        g = _unlock(self.tmp, wrapper=self._with_status(24), extra_cu=True,
                    verdict=_verdict(self.tmp, VERDICT_GPU_WGPS_CONDEMNED,
                                     ["0.1.3", "0.1.4"]))
        self.assertFalse(g.is_enabled())

    def test_full_target_routed(self):
        g = _unlock(self.tmp, wrapper=self._with_status(40), extra_cu=True)
        self.assertTrue(g.is_enabled())

    def test_stock_not_enabled(self):
        g = _unlock(self.tmp, wrapper=self._with_status(24), extra_cu=True)
        self.assertFalse(g.is_enabled())


class TestGuards(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_aggressive_curve_blocks_extra_cu(self):
        """Curva con point >900 mV (es. 2000@1000) → nessuna scrittura."""
        _curve(self.tmp, (800, 900, 1000))
        w = _FakeWrapper()
        g = _unlock(self.tmp, wrapper=w, extra_cu=True)
        out = g.apply()
        self.assertFalse(out["applied"])
        self.assertEqual(out["reason"], "curve_not_conservative")
        self.assertEqual(w.calls, [])

    def test_conservative_curve_allows(self):
        _curve(self.tmp, (800, 900))
        g = _unlock(self.tmp, wrapper=_FakeWrapper(), extra_cu=True)
        self.assertIs(g.curve_conservative(), True)
        self.assertTrue(g.apply()["applied"])

    def test_unreadable_curve_is_none_and_blocks(self):
        """Config assente/non parsabile = non determinabile (C1) → rifiuto."""
        g = _unlock(self.tmp, wrapper=_FakeWrapper(), extra_cu=True)
        self.assertIsNone(g.curve_conservative())
        out = g.apply()
        self.assertFalse(out["applied"])
        self.assertEqual(out["reason"], "curve_not_conservative")

    def test_mock_skips_curve_gate(self):
        g = _unlock(self.tmp, extra_cu=True)
        g.mock = True
        self.assertTrue(g.curve_conservative())

    def test_mask_write_refuses_non_cc_clearing_commands(self):
        """Fail-closed: solo i comandi che azzerano la config CC (BUO non
        ha accesso a mmCC_GC_SHADER_ARRAY_CONFIG)."""
        w = _FakeWrapper()
        g = _unlock(self.tmp, wrapper=w, extra_cu=True)
        for cmd in (["apply-service"], ["write-service-table"], ["table"]):
            out = g._write_mask(cmd)
            self.assertNotEqual(out["returncode"], 0, repr(cmd))
            self.assertIn("CC", out["stderr"])
        self.assertEqual(w.calls, [])
        for cmd in (["enable", "all"], ["enable-wgp", "0.1.3"],
                    ["disable-wgp", "0.1.3"], ["stock-dispatch"]):
            self.assertEqual(g._write_mask(cmd)["returncode"], 0, repr(cmd))

    def test_kernel_patch_refused_with_condemned_wgps(self):
        """Il kernel patch abilita tutte le CU: con WGP condannate →
        rifiuto (fail-closed)."""
        _curve(self.tmp)
        g = _unlock(self.tmp, extra_cu=True)
        g.is_ostree = False
        g.wrapper = _FakeWrapper()
        g._verdict = _verdict(self.tmp, VERDICT_GPU_WGPS_CONDEMNED, ["0.1.3"])
        out = g.apply()
        self.assertFalse(out["applied"])
        self.assertIn("kernel patch", out["error"])

    def test_wgps_decoder_exported_for_consumers(self):
        """Il decoder è il punto unico anche per i consumatori esterni
        (es. rollback a 24 CU quando le CU extra sono attive)."""
        self.assertEqual(cu_count_from_mask("0x1f,0x07,0x1f,0x1f"), 36)
        self.assertEqual(wgps_from_mask(MASK_STOCK_24), stock_wgps())


if __name__ == "__main__":
    unittest.main()
