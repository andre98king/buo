#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
GPU Undervolt — safe-points frequenza/voltaggio.

PRINCIPIO DI SICUREZZA (fail-closed e valori verificati):
    Sul hardware REALE BUO NON inventa coppie frequency/voltage: usa i
    safe-points COLLAUDATI dalla community (tabella del governor
    cyan-skillfish-governor-smu, verificata nello studio del codice) e
    li sottopone allo stress test della fase validate prima che vengano
    resi persistenti in fase apply.

    In modalità mock esegue un binary search simulato su MockHardware.

    NOTA (ricerca per-silicio, design research/DESIGN_GPU_UV.md): lo
    sweep governor-based misura la curva V/F del singolo chip e sostituisce
    i default della community mantenendo lo stesso contratto di output. Se
    QUALSIASI prerequisito manca (tool di stress, governor gestibile) o il
    punto di partenza community fallisce → tabella community invariata
    (`source="community_defaults"`, MAI punti non testati).
"""

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..constants import GOVERNOR_CONFIG, GPU_FREQ_STEPS, LIMITS
from ..exceptions import SafetyViolation
from ..utils.logging import LoggerMixin
from ..utils.shell import which
from .governor import GovernorWrapper
from ..validate.stress import StressTest

# Soglia di scostamento VDDGFX reale (SOTTO CARICO) vs target oltre la
# quale l'SMU è considerato al suo FLOOR (misurato sul campo 30/08 su
# Cyan Skillfish): sotto ~800 mV l'SMU NON scende. DATI DI CAMPO:
#   - a target 800 sotto FurMark la VDDGFX reale è 774-799 mV (droop
#     ~7-25: reale SEMPRE SOTTO il target finché l'SMU segue);
#   - sotto il floor reale resta ~790-800 mV mentre il target continua
#     a scendere (reale > target).
# Soglia 15 mV con confronto >=: seguendo reale - target ≤ -1 (mai un
# falso positivo); al primo target sotto il floor reale - target ≥ +15
# (790 a target 775) → il blocco scatta ESATTAMENTE al punto in cui
# l'SMU smette di seguire. Un punto sotto il floor NON è applicabile:
# un probe sotto il floor riporta "STABILE" per ARTEFATTO (gira sempre
# a ~800) e una config finale con punti < floor fa partire il governor
# in hang ("activating" per sempre, GPU senza curva). La lettura è
# SEMPRE sotto carico: a idle l'SMU ABBASSA la tensione (743 mV reale
# a target 800) e il confronto non scatta mai (bug 8b5a062).
SMU_FLOOR_TOLERANCE_MV = 15


@dataclass
class ProbeResult:
    """Esito di un singolo probe (f, v): stabilità + metriche."""
    stable: bool
    reason: Optional[str] = None
    gpu_temp_max: Optional[float] = None
    power_max: Optional[float] = None
    smu_floor: bool = False
    applied_voltage: Optional[int] = None


class GPUUndervoltOptimizer(LoggerMixin):
    """Genera i safe-points della GPU (community-verified o mock)."""

    # Tabella collaudata dal governor — community 2026 (elektricM/amd-bc250-docs):
    # curva FLAT 1000mV in alto. Il vecchio default (2000 MHz @ 960mV) era
    # troppo aggressivo e ha causato un crash GPU sotto stress sul campo.
    # Ceiling 2000 MHz su raffreddamento stock.
    COMMUNITY_SAFE_POINTS: List[Dict[str, int]] = [
        {"freq": 1000, "voltage": 800},
        {"freq": 1500, "voltage": 900},
        {"freq": 2000, "voltage": 1000},
    ]

    FREQ_STEPS = GPU_FREQ_STEPS

    def __init__(self, mock: bool = False, mock_hardware=None,
                 governor=None, stress=None, monitor=None,
                 reader=None, probe=None, vddgfx_reader=None,
                 provisioner=None):
        self.mock = mock
        self.mock_hw = mock_hardware
        # Dipendenze iniettabili (design §7): nei test si passano i fake;
        # in modalità reale senza override si creano i wrapper reali.
        # In mock le dipendenze vengono IGNORATE.
        if not mock:
            self.governor = governor if governor is not None else GovernorWrapper()
            self.stress = stress if stress is not None else StressTest()
        else:
            self.governor = governor
            self.stress = stress
        self.monitor = monitor
        self._reader_override = reader
        self.probe = probe          # callable (f, v, seconds) o None → self._probe
        # Auto-provvigionamento del tool di stress GPU (design
        # AUTOPROVISION P3c): callable → dict {status, needs_reboot,
        # detail, installed} o None (nessun provisioning — default nei
        # test e per uso standalone; l'orchestratore inietta il suo).
        # MAI un reboot per lo sweep (ottimizzazione opzionale con
        # fallback community sicuro).
        self.provisioner = provisioner
        # Reader della VDDGFX reale (mV) per il rilevamento del floor SMU:
        # callable target_mv → mV reali o None (non leggibile). In
        # produzione senza override: debugfs amdgpu_pm_info con sudo.
        self._vddgfx_reader = vddgfx_reader
        self._active_monitor: Optional[Any] = None

    def optimize(self, start_freq: int = 1200,
                 max_voltage: Optional[int] = None,
                 sweep: Optional[Dict[str, Any]] = None,
                 power_budget: Optional[int] = None,
                 monitor=None) -> Dict[str, Any]:
        """
        Restituisce i safe-points della GPU.

        - mock: binary search simulato su MockHardware
        - reale + sweep abilitato: ricerca per-silicio (design GPU_UV)
        - reale senza sweep: tabella community-verified (mai oltre i limiti)

        Args:
            start_freq: soglia minima delle frequenze della curva
            max_voltage: tetto di tensione (default recommended max 1050)
            sweep: opzioni della ricerca per-silicio (§6 del design);
                assente o `enabled` false → tabella community
            power_budget: tetto di potenza per i probe (default LIMITS)
            monitor: SafetyMonitor dell'orchestratore (abort immediato)

        Returns:
            {"safe_points": [...], "best_efficiency": {...}, "source": ...}
        """
        max_voltage = max_voltage or LIMITS.gpu.voltage_recommended_max
        # MAI oltre l'hard limit immutabile
        max_voltage = min(max_voltage, LIMITS.gpu.voltage_absolute_max)

        if self.mock:
            return self._mock_optimize()

        if sweep and sweep.get("enabled"):
            # monitor: priorità a quello passato a runtime (orchestratore),
            # poi a quello iniettato nel costruttore (test)
            self._active_monitor = (monitor if monitor is not None
                                    else self.monitor)
            return self._sweep_real(sweep, start_freq, max_voltage,
                                    power_budget or LIMITS.power.power_budget)

        # ---- MODALITÀ REALE: tabella community, clampata ai limiti ----
        return self._community_result(start_freq, max_voltage)

    # ------------------------------------------------------------------ #

    def _mock_optimize(self) -> Dict[str, Any]:
        if self.mock_hw is not None:
            self.mock_hw.set_gpu_voltage(900)
            self.mock_hw.set_gpu_freq(1500)
        return {
            "safe_points": [
                {"freq": 1200, "voltage": 800},
                {"freq": 1500, "voltage": 900},
                {"freq": 1700, "voltage": 940},
            ],
            "best_efficiency": {"freq": 1500, "voltage": 900, "watt": 100},
            "source": "mock",
        }

    def _find_best_efficiency(self, points: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not points:
            return {}
        return min(points, key=lambda p: p.get("voltage", 1000) / p.get("freq", 1000))

    # ------------------------------------------------------------------ #
    # Ricerca per-silicio (design GPU_UV §3–§5)
    # ------------------------------------------------------------------ #

    def _community_result(self, start_freq: int,
                          max_voltage: int) -> Dict[str, Any]:
        """Tabella community filtrata e clampata (percorso reale attuale)."""
        safe_points = [
            {"freq": p["freq"], "voltage": min(p["voltage"], max_voltage)}
            for p in self.COMMUNITY_SAFE_POINTS
            if p["freq"] >= start_freq
        ]
        self.logger.info(
            "GPU safe-points: tabella community (%d punti, "
            "voltage max %d mV) — validati dallo stress test",
            len(safe_points), max_voltage)
        return {
            "safe_points": safe_points,
            "best_efficiency": self._find_best_efficiency(safe_points),
            "source": "community_defaults",
        }

    def _sweep_real(self, sweep: Dict[str, Any], start_freq: int,
                    max_voltage: int, power_budget: int) -> Dict[str, Any]:
        """Sweep per-silicio governor-based (design §3).

        Fail-closed: se manca un prerequisito (tool di stress, governor
        gestibile) o il punto di partenza community fallisce, si ripiega
        sulla tabella community — MAI punti non testati. La config.toml
        viene SEMPRE ripristinata ai bytes originali a fine sweep (anche
        su eccezione/KeyboardInterrupt) e il governor resta FERMO.
        """
        # ---- prerequisiti (fail-closed: nessuna scrittura se mancano) ----
        if self._gpu_stress_tool() is None:
            # Auto-provvigionamento best-effort del tool di stress
            # (design AUTOPROVISION P3c): MAI un reboot dedicato per lo
            # sweep (ottimizzazione opzionale con fallback community
            # sicuro) — il layer staged si attiva al prossimo reboot.
            if self.provisioner is not None:
                try:
                    prov = self.provisioner()
                except Exception as e:
                    prov = {"status": "failed", "detail": str(e)}
                if prov.get("status") == "ok":
                    if prov.get("needs_reboot"):
                        self.logger.warning(
                            "GPU tool: vkmark installato (staged) — attivo "
                            "al prossimo reboot; questo run usa la tabella "
                            "community (il prossimo farà lo sweep "
                            "per-silicio)")
                    elif self._gpu_stress_tool() is not None:
                        self.logger.info(
                            "GPU tool: vkmark installato (attivo subito) — "
                            "sweep per-silicio avviato")
                else:
                    self.logger.warning(
                        "GPU tool: auto-install vkmark non riuscito (%s) — "
                        "tabella community; installa vkmark e riprova",
                        (prov.get("detail") or "errore").strip()[:160])
            if self._gpu_stress_tool() is None:
                self.logger.warning(
                    "GPU non stressabile (nessun tool furmark/vkmark) → "
                    "tabella community")
                return self._community_result(start_freq, max_voltage)
        if not self.governor.stop():
            self.logger.warning(
                "Governor non gestibile (stop fallito) → tabella community")
            return self._community_result(start_freq, max_voltage)

        freqs = [f for f in sweep["freqs"]
                 if start_freq <= f <= LIMITS.gpu.freq_max]
        if not freqs:
            self.logger.warning(
                "Nessuna frequenza di sweep nel range [%d, %d] → "
                "tabella community", start_freq, LIMITS.gpu.freq_max)
            return self._community_result(start_freq, max_voltage)

        step = sweep["step_mv"]
        # Floor di SICUREZZA (fail-closed): clamp dei safe_points FINALI
        # a >= max(floor config, floor SMU rilevato dal probe) — mai punti
        # sotto il floor configurato (default 800 = floor FurMark della
        # macchina; punti < floor fanno partire il governor in hang).
        floor = max(LIMITS.gpu.voltage_min,
                    min(sweep["floor_mv"], max_voltage))
        # Floor di DISCESA del probe: si misura fino al minimo sicuro
        # (700). Il probe vkmark è TROPPO LEGGERO per vedere il floor
        # FurMark (l'SMU segue il target fino a 700 sotto vkmark) e scende
        # sotto il floor configurato riportando "STABILE" per artefatto:
        # è la clamp finale (floor) ad alzare i punti (direzione sicura:
        # alzare la tensione di un punto validato non può renderlo
        # instabile, curva V/F monotona). Il floor SMU rilevato
        # (smu_floor_mv) ferma le discese successive.
        descend_floor = LIMITS.gpu.voltage_min

        # backup fail-closed: a fine sweep si riscrivono i BYTES originali
        backup = self._read_config_bytes()
        if backup is not None and b"# buo-sweep" in backup:
            # residuo di un run crashato (kill -9 durante lo stress):
            # sostituito con i default community prima di ripartire
            self.logger.warning(
                "Config.toml marcata '# buo-sweep' (run precedente "
                "crashato): riscritti i default community")
            if not self.governor.write_default_config():
                self.logger.error(
                    "Ripristino default community fallito → tabella community")
                return self._community_result(start_freq, max_voltage)
            backup = self._read_config_bytes()

        # stato per i probe (budget, backup da ripristinare). Il gate
        # termico dei probe È l'HARD (politica a due livelli 03/09):
        # LIMITS.gpu.temp_max letto in _interpret_probe — il target
        # operativo (throttle/curva) non è un criterio dello sweep.
        self._probe_power_budget = power_budget
        self._probe_backup = backup

        found: List[Tuple[int, int]] = []
        results: List[Dict[str, Any]] = []
        points_tested = 0
        failed_points = 0
        t_start = time.monotonic()
        # Floor SMU rilevato dal probe (chip-globale, 30/08): appena l'SMU
        # smette di seguire il target, il punto NON è applicabile e non si
        # scende oltre (né qui né alle frequenze successive).
        smu_floor_mv: Optional[int] = None
        try:
            for f in freqs:
                if self._monitor_violation():
                    self._raise_monitor()
                start_v = self._community_voltage(f, step)
                start_v = max(floor, min(start_v, max_voltage))
                candidates = self._descend(start_v, step, descend_floor,
                                           sweep["max_steps"])
                stable_v = None
                floor_reached = False
                for v in candidates:
                    if self._monitor_violation():
                        self._raise_monitor()
                    r = self._probe_call(f, v, sweep["test_seconds"])
                    points_tested += 1
                    results.append({
                        "freq": f, "voltage": v, "stable": r.stable,
                        "gpu_temp_max": r.gpu_temp_max,
                        "power_max": r.power_max, "reason": r.reason,
                    })
                    if r.smu_floor and r.applied_voltage is not None:
                        # FLOOR SMU: il punto (f, v) non è applicabile
                        # (l'SMU gira sempre a ~790-800) — il vincitore è
                        # l'ULTIMO punto in cui la tensione ha SEGUITO il
                        # target (reale ≈ target ± droop), cioè il
                        # candidato PRECEDENTE (v + step): clamp dei
                        # safe_points a quel floor, MAI sotto (una config
                        # < floor fa partire il governor in hang). Se non
                        # c'è un precedente (primo candidato già sotto il
                        # floor) si usa la VDDGFX reale misurata. Le
                        # frequenze successive non scendono oltre.
                        floor_mv = (v + step if stable_v is not None
                                    else r.applied_voltage)
                        smu_floor_mv = (floor_mv if smu_floor_mv is None
                                        else max(smu_floor_mv, floor_mv))
                        floor = max(floor, smu_floor_mv)
                        descend_floor = max(descend_floor, smu_floor_mv)
                        floor_reached = True
                        self.logger.warning(
                            "  Sweep (f=%d, v=%d) FLOOR SMU: VDDGFX reale "
                            "%d mV non segue il target sotto carico "
                            "(+%d mV) — stop discesa, floor %d mV, il "
                            "punto precedente è il vincitore",
                            f, v, r.applied_voltage,
                            r.applied_voltage - v, smu_floor_mv)
                        break
                    if r.stable:
                        stable_v = v
                        self.logger.info(
                            "  Sweep %d@%d: stabile — GPU %s°C, %s W", f, v,
                            f"{r.gpu_temp_max or 0.0:.1f}".replace(".", ","),
                            f"{r.power_max or 0.0:.0f}")
                    else:
                        failed_points += 1
                        self.logger.info(
                            "  Sweep %d@%d: instabile (%s) — risalgo al "
                            "punto precedente", f, v, r.reason)
                        break
                    if (time.monotonic() - t_start
                            > sweep["max_minutes"] * 60):
                        self.logger.warning(
                            "Budget sweep (%d min) esaurito — tengo "
                            "l'ultimo punto stabile per frequenza",
                            sweep["max_minutes"])
                        break
                if stable_v is None:
                    # il punto di PARTENZA (community) è fallito: ambiente
                    # compromesso (calore/raffreddamento/tool) → MAI punti
                    # non testati
                    self.logger.error(
                        "Sweep: punto di partenza community fallito a %d MHz "
                        "(chip instabile a %d mV) — ambiente compromesso → "
                        "tabella community", f, start_v)
                    return self._community_result(start_freq, max_voltage)
                if floor_reached:
                    # il vincitore è il punto precedente, clampato al floor
                    # rilevato (mai sotto: una config < floor fa partire il
                    # governor in hang). Il punto clampato è GIÀ stato
                    # stressato durante la discesa → conferma non necessaria.
                    stable_v = (max(stable_v, smu_floor_mv)
                                if stable_v is not None else smu_floor_mv)
                elif sweep["confirm_seconds"] > 0:
                    (stable_v, points_tested, failed_points) = self._confirm(
                        f, stable_v, start_v, step, sweep["confirm_seconds"],
                        max_voltage, results, points_tested, failed_points)
                found.append((f, stable_v))
        finally:
            # SEMPRE: bytes originali (o delete se prima non esisteva) e
            # governor fermo (stato atteso da _phase_apply)
            self._restore_config(backup)
            self.governor.stop()

        points = self._build_curve(found, floor, max_voltage, smu_floor_mv)
        # Clamp al floor (fail-closed): se la clamp finale ha ALZATO un
        # punto sopra il valore trovato dallo sweep (il probe è sceso
        # sotto il floor configurato, o il floor SMU è stato rilevato
        # dopo), lo segnalo nei metadata (mai punti sotto il floor in
        # output). Alzare la tensione di un punto validato non può
        # renderlo instabile (curva V/F monotona): direzione sicura.
        found_map = {f: v for f, v in found}
        clamped_to_floor = any(
            p["voltage"] > found_map[p["freq"]] for p in points)
        sweep_meta: Dict[str, Any] = {
            "enabled": True,
            "freqs": freqs,
            "step_mv": step,
            "floor_mv": floor,
            "points_tested": points_tested,
            "failed_points": failed_points,
            "duration_s": int(time.monotonic() - t_start),
            "results": results,
        }
        if smu_floor_mv is not None:
            sweep_meta["smu_floor_mv"] = smu_floor_mv
        if clamped_to_floor:
            sweep_meta["clamped_to_floor"] = True
        return {
            "safe_points": points,
            "best_efficiency": self._find_best_efficiency(points),
            "source": "per-silicon",
            # ---- metadati ADDITIVI (non letti da nessun consumatore) ----
            "sweep": sweep_meta,
        }

    def _confirm(self, f: int, winner: int, start_v: int, step: int,
                 seconds: int, max_voltage: int,
                 results: List[Dict[str, Any]], points_tested: int,
                 failed_points: int) -> Tuple[int, int, int]:
        """Rievidenza del vincitore (§3): se fallisce, un gradino su
        (+step); se fallisce anche quello → valore community per questa
        frequenza (sanity già passato a durata breve) con warning."""
        r = self._probe_call(f, winner, seconds)
        points_tested += 1
        results.append({
            "freq": f, "voltage": winner, "stable": r.stable,
            "gpu_temp_max": r.gpu_temp_max, "power_max": r.power_max,
            "reason": r.reason,
        })
        if r.stable:
            return winner, points_tested, failed_points
        failed_points += 1
        up_v = winner + step
        if up_v <= max_voltage:      # mai probe oltre max_voltage
            r2 = self._probe_call(f, up_v, seconds)
            points_tested += 1
            results.append({
                "freq": f, "voltage": up_v, "stable": r2.stable,
                "gpu_temp_max": r2.gpu_temp_max, "power_max": r2.power_max,
                "reason": r2.reason,
            })
            if r2.stable:
                return up_v, points_tested, failed_points
            failed_points += 1
        self.logger.warning(
            "Sweep: conferma fallita a %d MHz — chip marginale, tengo il "
            "valore community %d mV", f, start_v)
        return start_v, points_tested, failed_points

    def _probe(self, f: int, v: int, seconds: int) -> ProbeResult:
        """Ciclo di vita di un candidato (design §3): stop → curva FLAT a v
        su [f-200, f] con max_freq=f → start → stress → stop → ripristino
        della config ORIGINALE (bytes)."""
        self.governor.stop()                     # idempotente
        try:
            f_lo = max(LIMITS.gpu.freq_min, f - 200)
            curve = [{"freq": f_lo, "voltage": v},
                     {"freq": f, "voltage": v}]
            if not self.governor.write_config(curve, min_freq=f_lo,
                                              max_freq=f):
                return ProbeResult(False, "write_config fallita")
            self._mark_sweep_config()
            if not self.governor.start():
                return ProbeResult(False, "governor start fallito")
            if not self._wait_active():
                return ProbeResult(False, "governor non attivo dopo 10s")
            time.sleep(2)                        # settle del governor
            cmd = self._gpu_stress_cmd(seconds)  # furmark → vkmark
            reader = self._get_reader()
            before_dmesg = self._read_dmesg()
            # FLOOR SMU (30/08, fix 2): lettura della VDDGFX REALE SOTTO
            # CARICO — campionamento live durante lo stress, MASSIMO dei
            # campioni (sotto carico la tensione sale verso il target: il
            # massimo è il valore rappresentativo). A IDLE l'SMU ABBASSA
            # la tensione (743 mV reale a target 800) e il confronto
            # reale >= target+soglia non scattava MAI (bug 8b5a062: lo
            # sweep scendeva sotto il floor). Il campionamento è un tick
            # di _run_loaded (on_tick, ~1s) — nessun thread.
            samples: List[int] = []

            def _sample_vddgfx() -> None:
                real_v = self._read_vddgfx(v)
                if real_v is not None:
                    samples.append(real_v)

            try:
                rc, _cpu_t, gpu_t, pw = self.stress._run_loaded(
                    cmd, seconds + 30, reader, self._probe_power_budget,
                    on_tick=_sample_vddgfx)
            except SafetyViolation as e:
                return ProbeResult(False, str(e))
            after_dmesg = self._read_dmesg()
            fault = self._dmesg_fault_detected(before_dmesg, after_dmesg)
            result = self._interpret_probe(rc, gpu_t, LIMITS.gpu.temp_max,
                                           fault, pw)
            # FLOOR SMU: confronto SOTTO CARICO. Se l'SMU non segue il
            # target (reale >= target + soglia), il punto NON è
            # applicabile: il probe lo riporta (stabile per artefatto)
            # ma marcato smu_floor → lo sweep si ferma. Lettura non
            # disponibile → fail-soft (nessun blocco).
            real_v = max(samples) if samples else None
            if real_v is not None and real_v >= v + SMU_FLOOR_TOLERANCE_MV:
                self.logger.warning(
                    "  Probe (f=%d, v=%d) FLOOR SMU: VDDGFX reale %d mV "
                    "non segue il target sotto carico (+%d mV) — punto "
                    "inapplicabile, il precedente è il vincitore",
                    f, v, real_v, real_v - v)
                return ProbeResult(
                    result.stable,
                    "smu_floor (VDDGFX reale %d mV, floor SMU)" % real_v,
                    result.gpu_temp_max, result.power_max,
                    smu_floor=True, applied_voltage=real_v)
            return result
        finally:
            self.governor.stop()
            self._restore_config(self._probe_backup)

    @staticmethod
    def _interpret_probe(rc: int, gpu_t: Optional[float], temp_gate: int,
                         dmesg_fault: bool = False,
                         power_max: Optional[float] = None) -> ProbeResult:
        """Stabilità di un probe: rc==0, GPU sotto il gate HARD, nessun
        fault dmesg. Il gate è l'HARD (LIMITS.gpu.temp_max, politica
        03/09): il target operativo NON boccia i candidati. Sensore non
        leggibile (None) → criterio saltato (C1, mai valori fittizi)."""
        stable = rc == 0
        reason = None
        if stable and gpu_t is not None and gpu_t >= temp_gate:
            stable, reason = False, f"GPU {gpu_t:.0f}°C ≥ {temp_gate}°C"
        if stable and dmesg_fault:
            stable, reason = False, "GPU fault rilevato in dmesg"
        return ProbeResult(stable, reason or "ok", gpu_t, power_max)

    # -------------------------- prerequisiti -------------------------- #

    def _gpu_stress_tool(self) -> Optional[str]:
        """Tool di stress GPU con controllo durata REALE: furmark (CLI
        FurMark 2 documentata) preferito, vkmark fallback. glmark2 NON ha
        un'opzione di durata (--seconds inesistente: verificato sul campo
        con glmark2 2023.01 di Fedora) → non può produrre un probe a
        durata fissa con rc==0 → escluso (fail-closed verso community)."""
        if which("furmark"):
            return "furmark"
        if which("vkmark"):
            return "vkmark"
        return None

    def _gpu_stress_cmd(self, seconds: int) -> List[str]:
        """Comando di stress per `seconds` secondi con rc==0 a fine run.

        Sintassi UFFICIALE FurMark 2 (geeks3d.com/furmark/command-line,
        esempio n.8: stress-and-quit con --max-time) e vkmark con durata
        per-scena (rc=0 dopo N secondi). NOTA: richiedono un display
        (finestra GL/Vulkan); senza display il tool fallisce → probe
        instabile → fallback community (fail-closed)."""
        if which("furmark"):
            return ["furmark", "--demo", "furmark-gl",
                    "--width", "1920", "--height", "1080",
                    "--max-time", str(seconds),
                    "--vsync", "0", "--no-gpumon"]
        return ["vkmark", "--size", "1920x1080",
                "-b", f"desktop:duration={seconds}"]

    # ------------------------- curva e candidate ---------------------- #

    def _community_voltage(self, freq: int, step: int) -> int:
        """Valore community interpolato linearmente da COMMUNITY_SAFE_POINTS
        alla frequenza, arrotondato al multiplo dello step (design: es.
        1200 → 850)."""
        pts = self.COMMUNITY_SAFE_POINTS
        if freq <= pts[0]["freq"]:
            v = pts[0]["voltage"]
        elif freq >= pts[-1]["freq"]:
            v = pts[-1]["voltage"]
        else:
            v = pts[-1]["voltage"]
            for lo, hi in zip(pts, pts[1:]):
                if lo["freq"] <= freq <= hi["freq"]:
                    frac = (freq - lo["freq"]) / (hi["freq"] - lo["freq"])
                    v = lo["voltage"] + frac * (hi["voltage"] - lo["voltage"])
                    break
        return int(round(v / step) * step)

    def _descend(self, start_v: int, step: int, floor: int,
                 max_steps: int) -> List[int]:
        """Candidati [start, start-step, …] finché ≥ floor e ≤ max_steps."""
        candidates: List[int] = []
        v = start_v
        while v >= floor and len(candidates) < max_steps:
            candidates.append(v)
            v -= step
        return candidates

    def _build_curve(self, found: List[Tuple[int, int]], floor: int,
                     max_voltage: int,
                     smu_floor_mv: Optional[int] = None) -> List[Dict[str, int]]:
        """Ordina per frequenza, monotonizza (non-decrescente in voltage),
        clamp a [max(floor, smu_floor_mv), max_voltage]. Mai punti non
        testati. Il floor SMU rilevato (30/08) NON può essere scavalcato:
        un punto sotto il floor non è applicabile (governor in hang)."""
        found = sorted(found, key=lambda p: p[0])
        eff_floor = (max(floor, smu_floor_mv) if smu_floor_mv is not None
                     else floor)
        points: List[Dict[str, int]] = []
        prev_v = eff_floor
        for f, v in found:
            v = max(prev_v, v)
            v = max(eff_floor, min(v, max_voltage))
            points.append({"freq": f, "voltage": v})
            prev_v = v
        return points

    # --------------------- config/governor lifecycle ------------------- #

    def _probe_call(self, f: int, v: int, seconds: int) -> ProbeResult:
        if self.probe is not None:
            return self.probe(f, v, seconds)
        return self._probe(f, v, seconds)

    def _config_path(self) -> Path:
        path = getattr(self.governor, "config_path", None)
        return Path(path) if path else Path(GOVERNOR_CONFIG)

    def _read_config_bytes(self) -> Optional[bytes]:
        try:
            return self._config_path().read_bytes()
        except OSError:
            return None

    def _restore_config(self, backup: Optional[bytes]) -> None:
        """Riscrive i bytes originali della config, o cancella il file se
        prima non esisteva (fail-closed: SEMPRE curva originale a fine
        sweep, anche su eccezione/KeyboardInterrupt)."""
        path = self._config_path()
        try:
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(backup)
        except OSError as e:
            self.logger.error("Ripristino config.toml fallito: %s", e)

    def _mark_sweep_config(self) -> None:
        """Marca la config candidata con '# buo-sweep' in testa (sanazione
        del rischio kill -9: al prossimo run la curva marcata viene
        rilevata e sostituita con i default community)."""
        path = self._config_path()
        try:
            data = path.read_bytes()
            if not data.startswith(b"# buo-sweep"):
                path.write_bytes(b"# buo-sweep\n" + data)
        except OSError:
            self.logger.debug("Marcatura config non riuscita (best-effort)")

    def _wait_active(self, timeout_s: float = 10.0) -> bool:
        """Attende il governor attivo (poll is_running, ≤10s); il settle
        di 2s è gestito dal chiamante (_probe)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.governor.is_running():
                return True
            time.sleep(0.2)
        return False

    # ----------------------------- monitor ----------------------------- #

    def _monitor_violation(self) -> bool:
        m = self._active_monitor
        return m is not None and m.is_violation()

    def _raise_monitor(self) -> None:
        m = self._active_monitor
        raise SafetyViolation(
            m.get_violation_reason() if m is not None else
            "SafetyMonitor: violazione")

    # ------------------------------ dmesg ------------------------------ #

    _DMESG_FAULT_RE = re.compile(
        r"amdgpu.*(GPU reset|fault|timeout)|ring gfx|VM_L2_PROTECTION")

    def _read_dmesg(self) -> List[str]:
        """Best-effort (design §4): [] se dmesg non leggibile (non root /
        dmesg_restrict) → criterio saltato, mai valori fittizi."""
        try:
            r = subprocess.run(["dmesg"], capture_output=True, text=True,
                               timeout=10)
            if r.returncode != 0:
                return []
            return r.stdout.splitlines()
        except Exception:
            return []

    def _dmesg_fault_detected(self, before: List[str],
                              after: List[str]) -> bool:
        """True se tra prima e dopo il probe compare un pattern di GPU
        fault (amdgpu reset/fault/timeout, ring gfx, VM_L2_PROTECTION)."""
        if not before or not after:
            return False
        new_lines = [ln for ln in after if ln not in set(before)]
        return any(self._DMESG_FAULT_RE.search(ln) for ln in new_lines)

    # ------------------------------ reader ----------------------------- #

    def _get_reader(self) -> Any:
        """Reader: override nei test, altrimenti lettore hardware reale."""
        if self._reader_override is not None:
            return self._reader_override
        from ..safety.reader import RealHardwareReader
        return RealHardwareReader()

    def _read_vddgfx(self, target_mv: int) -> Optional[int]:
        """VDDGFX REALE (mV) applicata dall'SMU per il rilevamento del
        floor (30/08): il target scritto in config.toml NON è affidabile
        sotto ~800 mV (l'SMU non scende). Override nei test; in
        produzione via debugfs amdgpu_pm_info (serve root → run_command
        con sudo). None = non leggibile → fail-soft, nessun blocco."""
        if self._vddgfx_reader is not None:
            try:
                return self._vddgfx_reader(target_mv)
            except Exception:
                return None
        try:
            from ..utils.shell import run_command
            rc, out, _ = run_command(
                ["cat", "/sys/kernel/debug/dri/1/amdgpu_pm_info"],
                sudo=True, check=False)
            if rc != 0 or not out:
                return None
            # Formato REALE della riga (verificato sul campo, 30/08):
            # "\t824 mV (VDDGFX)" — il label viene DOPO il valore, senza
            # prefisso "VDDGFX:" (il regex precedente non matchava mai →
            # None → floor mai rilevato → sweep sotto il floor).
            m = re.search(r"(\d+)\s*mV\s*\(VDDGFX\)", out)
            return int(m.group(1)) if m else None
        except Exception:
            return None
