#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
GPU 40-CU Unlock — metodo per distro (kernel patch o runtime UMR).

METODO CORRETTO PER DISTRO:
    • NON-ostree (Fedora/Arch standard): kernel patch amdgpu via
      `bc250-enable-40cu.sh` (build + enable, richiede reboot).
    • OSTREE (Bazzite/SteamOS): /usr è READ-ONLY → il kernel patch NON
      funziona (build fallisce scrivendo amdgpu_trace.h). Si usa il
      **runtime UMR** via `bc250-cu-live-manager.sh` (scrive CC/SPI/RLC
      da userspace, VOLATILE, nessun reboot, reversibile).

Analisi dallo studio:
    • registri: mmCC_GC_SHADER_ARRAY_CONFIG e
      mmSPI_PG_ENABLE_STATIC_WGP_MASK (entrambi necessari)
    • bc250_cc_write_mode=3 (clear tutti i SE/SH) è la modalità consigliata
    • rischio: chip B-grade con CU difettose → serve il health test

POLITICA CU EXTRA (2026): le 16 CU oltre le 24 di fabbrica sono OPT-IN
(`phases.probe.gpu_extra_cu`) e la maschera è SEMPRE DERIVATA dalle WGP
validate (meno quelle condannate dal verdetto per-WGP). Motivo: l'unlock
è di natura compute (glmark2 +4,4%, compute LLM +1,5-1,6×, ma +30 W/+4 °C
e a 2 GHz con 40 CU 181 W e 96 °C, con efficienza in calo 4,18→2,98
tok/s/W e −11% misurato su carico LLM sostenuto), su ~26% delle board i
CU extra sono realmente difettosi e **un fault GPU su questa APU non è
recuperabile** (niente GPU reset: freeze/schermo nero). Default: 24 CU.
"""

import os
import re
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from ..constants import (GOVERNOR_CONFIG, MASK_ALL_40, MASK_STOCK_24,
                         cu_count_from_mask, extra_wgps, mask_from_wgps,
                         parse_wgp, stock_wgps)
from ..utils.logging import LoggerMixin
from .wrappers.bc250_40cu import BC25040CUWrapper
from .wrappers.bc250_live_manager import BC250LiveManagerWrapper


class GPU40CUUnlock(LoggerMixin):
    """Sblocco delle CU extra GPU (metodo per distro)."""

    # Conf di boot del live-manager (EnvironmentFile dell'unità systemd);
    # percorso iniettabile nei test.
    boot_conf_path = "/etc/bc250-cu-live-manager.conf"
    # Curva GPU del governor: usata dal gate P5 (sotto).
    governor_conf_path = GOVERNOR_CONFIG
    # Tensione massima (mV) ammessa su OGNI safe-point per poter abilitare
    # le CU extra: 1500 MHz / 900 mV è la curva conservativa consigliata.
    # Con 40 CU una curva aggressiva (es. 2000 MHz / 1000 mV) costa 176-181 W
    # e 96 °C, in test sostenuto GPU 107 °C con throughput −10%: si abilitano
    # le CU solo a curva conservativa (o su curva già certificata a ≤900 mV).
    CURVE_MAX_MV = 900
    # Comandi del live-manager che azzerano mmCC_GC_SHADER_ARRAY_CONFIG prima
    # di scrivere SPI/RLC (sequenza CachyOS nota-buona, `apply_target_masks`).
    # BUO NON ha accesso a quel registro: può solo garantirsi di scrivere la
    # maschera con questi comandi e rifiutare ogni altra scrittura (P5).
    _CC_CLEARING_CMDS = (["enable", "all"], ["enable-wgp"], ["disable-wgp"],
                         ["stock-dispatch"])

    def __init__(self, mock: bool = False, mock_hardware=None,
                 use_wrapper: bool = True, extra_cu: Optional[bool] = None,
                 verdict=None, governor=None, wgps: Optional[List[str]] = None):
        self.mock = mock
        self.mock_hw = mock_hardware
        # Opt-in alle CU extra: None = dalla config (`probe.gpu_extra_cu`).
        # Iniettabile perché l'orchestratore costruisce questa classe senza
        # passare la config (una sola fonte: il file buo.yaml).
        self.extra_cu = extra_cu
        # WGP extra da abilitare (percorso cumulativo live): None = tutte
        # quelle validate. Sopravvive tra apply() e persist().
        self.wgps = wgps
        self._verdict = verdict
        self._governor = governor
        self.is_ostree = os.path.exists("/run/ostree-booted")
        if use_wrapper and not mock:
            if self.is_ostree:
                # Runtime UMR (unico metodo funzionante su ostree)
                self.wrapper = BC250LiveManagerWrapper()
            else:
                # Kernel patch (Fedora/Arch standard)
                self.wrapper = BC25040CUWrapper()
        else:
            self.wrapper = None

    # ------------------------------------------------------------------ #
    # Maschera derivata dai dati validati (P1/P2)
    # ------------------------------------------------------------------ #

    def _extra_allowed(self) -> bool:
        """Opt-in alle CU extra. Default PRUDENTE: non richieste = 24 CU.

        Il default (False) è anche il valore di fallback se la config non è
        leggibile → nessuna abilitazione silenziosa.
        """
        if self.extra_cu is not None:
            return bool(self.extra_cu)
        try:
            from ..config import BUOConfig
            return bool(BUOConfig.load().probe_gpu_extra_cu)
        except Exception:
            return False

    def _verdict_obj(self):
        if self._verdict is None:
            from .validation import UnlockVerdict
            self._verdict = UnlockVerdict()
        return self._verdict

    def condemned_wgps(self) -> List[str]:
        """WGP extra condannate dal verdetto durevole (per-WGP, P2).

        Verdetto illeggibile/assente → nessuna condanna. Verdetto con
        lista inattendibile → TUTTE le extra (fail-closed, vedi
        UnlockVerdict.condemned_wgps).
        """
        try:
            return list(self._verdict_obj().condemned_wgps())
        except Exception as e:
            self.logger.warning(
                "Verdetto GPU non leggibile (%s) — nessuna WGP condannata", e)
            return []

    def target_wgps(self) -> List[str]:
        """WGP extra da abilitare = selezionate meno le condannate.

        È l'UNICO punto che decide la maschera: nessun percorso di questa
        classe scrive 0x1f a mano (una WGP guasta instradata = fault GPU
        non recuperabile su questa APU).
        """
        selected = self.wgps if self.wgps is not None else extra_wgps()
        condemned = self.condemned_wgps()
        return [w for w in selected if w not in condemned]

    def target_mask(self) -> str:
        """Maschera conf ASSOLUTA del target: WGP stock + extra validate."""
        return mask_from_wgps(list(stock_wgps()) + self.target_wgps())

    # ------------------------------------------------------------------ #
    # Guardie (P5)
    # ------------------------------------------------------------------ #

    def curve_conservative(self) -> Optional[bool]:
        """True se la curva GPU attiva è conservativa (ogni safe-point
        ≤ CURVE_MAX_MV). None se non determinabile (config assente o
        senza safe-point): C1, mai un giudizio inventato.

        In mock/dry-run la curva è un file di SISTEMA: non si legge (mai
        accesso reale in simulazione, pattern C1) e la guardia non ha
        oggetto → True (simulato).
        """
        if self.mock:
            return True
        try:
            with open(self.governor_conf_path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            return None
        volts = [int(m) for m in
                 re.findall(r"(?m)^\s*voltage\s*=\s*(\d+)", text)]
        if not volts:
            return None
        return max(volts) <= self.CURVE_MAX_MV

    @contextmanager
    def _governor_paused(self):
        """Governor FERMO durante l'accesso UMR (regola assoluta: SMU in
        concorrenza = freeze del SoC). Fail-closed: stato non confermato
        fermo → RuntimeError (abort dell'accesso). In mock è un no-op.

        Idempotente: annidato dentro il `_governor_paused` dell'orchestratore
        vede il governor già fermo e non lo riavvia (riavvio a carico del
        chiamante esterno).
        """
        if self.mock:
            yield
            return
        from ..optimize.governor import (GovernorWrapper,
                                         governor_confirmed_inactive)
        confirmed = governor_confirmed_inactive()
        if confirmed is None:
            raise RuntimeError(
                "Stato del governor non determinabile (in transito o "
                "sconosciuto) — accesso UMR annullato (mai registri GPU con "
                "governor attivo: freeze SoC).")
        if not confirmed:
            governor = self._governor or GovernorWrapper()
            try:
                stopped = governor.stop()
            except Exception:
                stopped = False
            if not stopped:
                raise RuntimeError(
                    "Governor non confermato FERMO — accesso UMR annullato "
                    "(mai registri GPU con governor attivo: freeze SoC).")
        try:
            yield
        finally:
            if not confirmed:
                try:
                    (self._governor or GovernorWrapper()).start()
                except Exception:
                    self.logger.warning(
                        "Riavvio del governor fallito dopo l'accesso UMR")

    # ------------------------------------------------------------------ #

    def is_enabled(self) -> bool:
        """True se la maschera TARGET è già instradata.

        Non solo "40 CU": con WGP condannate il target è la maschera
        parziale validata (es. 36 CU) e il routing va considerato attivo se
        lo stato live ha esattamente quei CU — altrimenti l'orchestratore
        ri-applicherebbe una maschera già attiva (o peggio, leggerebbe il
        routing volatile come "perso").
        """
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.get_cu_count() >= 40
        if self.wrapper is not None and self.wrapper.available:
            st = self.wrapper.status().get("parsed_output", {})
            if st.get("full_die", False):
                return True
            routed = st.get("cu_routed")
            return routed is not None and \
                routed == cu_count_from_mask(self.target_mask())
        return False

    def apply(self, wgps: Optional[List[str]] = None) -> Dict[str, Any]:
        """Abilita le CU extra (opt-in) con la maschera derivata.

        wgps: WGP EXTRA da abilitare (percorso cumulativo live: si aggiunge
        una WGP alla volta a runtime, nessun reboot). None = tutte quelle
        validate. L'ordine di sicurezza è: opt-in → guardia curva → WGP
        condannate escluse → scrittura (stock-dispatch + enable-wgp).
        """
        if wgps is not None:
            try:
                self.wgps = self._validated_wgps(wgps)
            except ValueError as e:
                # fail-closed: input anomalo = nessuna scrittura
                return {"applied": False, "error": str(e)}

        if not self._extra_allowed():
            # Default prudente (P1): nessuna abilitazione silenziosa.
            self.logger.warning(
                "CU extra non richieste (opt-in `phases.probe.gpu_extra_cu`) "
                "— GPU a 24 CU stock: nessuna scrittura maschera")
            return {
                "applied": False, "mask": MASK_STOCK_24,
                "reason": "extra_cu_disabled",
                "note": "16 CU extra non richieste (opt-in: probe."
                        "gpu_extra_cu=true) — GPU a 24 CU stock",
            }

        target = self.target_wgps()
        if not target:
            return {
                "applied": False, "mask": MASK_STOCK_24,
                "reason": "all_extra_condemned",
                "note": "tutte le WGP extra sono condannate dal verdetto "
                        "durevole — GPU a 24 CU stock",
            }

        curve = self.curve_conservative()
        if curve is not True:
            # P5: prima le CU extra la curva deve essere conservativa
            # (1500 MHz / 900 mV) — con 40 CU una curva aggressiva porta la
            # GPU a 96-107 °C e il throughput cala.
            return {
                "applied": False,
                "reason": "curve_not_conservative",
                "error": "curva GPU non conservativa per le CU extra: %s. "
                         "Servono safe-point ≤%d mV (1500 MHz/900 mV) in %s" % (
                             "config illeggibile o senza safe-point"
                             if curve is None
                             else "c'è un safe-point sopra il limite",
                             self.CURVE_MAX_MV, self.governor_conf_path),
            }

        if self.mock:
            # Simulazione: nessuna scrittura reale. MockHardware modella
            # solo stock/full-die (24/40 CU): il full-die aggiorna lo stato
            # simulato, la maschera parziale (26…38 CU, es. 36 CU con 2 WGP
            # condannate) riporta il conteggio DERIVATO dalla maschera —
            # il mock non ha le WGP parziali.
            full_die = target == extra_wgps()
            if full_die and self.mock_hw is not None:
                self.mock_hw.enable_40cu()
            return {
                "applied": True,
                "simulated": True,
                "cu_count": (cu_count_from_mask(self.target_mask())),
                "needs_reboot": full_die,
                "mask": self.target_mask(),
                "wgps": target,
            }

        if self.wrapper is None or not self.wrapper.available:
            return {
                "applied": False,
                "error": "script 40-CU non trovato — esegui: sudo buo install-deps",
            }

        if self.is_ostree:
            return self._apply_runtime_umr(target)

        return self._apply_kernel_patch()

    def _validated_wgps(self, wgps: List[str]) -> List[str]:
        """Valida (e normalizza) la lista di WGP extra richiesta.

        Fail-closed: id non nella forma SE.SH.WGP, oppure WGP STOCK
        (0-2: non è un "extra", la maschera non scende sotto lo stock).
        """
        ids = [str(w).strip() for w in wgps]
        for w in ids:
            parse_wgp(w)
            if w not in extra_wgps():
                raise ValueError(
                    "WGP '%s' non è una delle extra (%s): la maschera non "
                    "scende sotto le 24 CU stock"
                    % (w, ",".join(extra_wgps())))
        return ids

    def _write_mask(self, args: List[str]) -> Dict[str, Any]:
        """UNICO punto di scrittura della maschera WGP (register write UMR).

        Rifiuta (fail-closed) qualunque comando che non sia della sequenza
        che azzera mmCC_GC_SHADER_ARRAY_CONFIG prima di scrivere SPI/RLC:
        scrivere la maschera "tutte le WGP" con la config CC/shader-array
        NON azzerata dà una enumerazione CU sbagliata. BUO non ha accesso a
        quel registro, quindi non può verificarlo: può solo far passare di
        qui tutti i write e rifiutare i comandi fuori dall'elenco noto.
        """
        if not any(args[:len(p)] == p for p in self._CC_CLEARING_CMDS):
            return {"returncode": 1, "stdout": "", "parsed_output": {},
                    "stderr": "comando maschera non ammesso (nessuna garanzia "
                              "di azzeramento CC): %s" % " ".join(args)}
        return self.wrapper.run_with_output(["-y"] + args)

    def _apply_runtime_umr(self, target: List[str]) -> Dict[str, Any]:
        """Ostree: runtime UMR, volatile, nessun reboot, reversibile.

        Ordine di sicurezza: `stock-dispatch` (24 CU pulite) e poi
        `enable-wgp` delle sole WGP validate — MAI `enable all` con WGP
        condannate, nemmeno per un istante: instradare una WGP guasta
        blocca la GPU in modo non recuperabile.
        """
        full_die = target == extra_wgps()
        self.logger.info(
            "CU extra via runtime UMR (ostree): %d WGP validate, maschera %s",
            len(target), self.target_mask())
        with self._governor_paused():
            if full_die:
                result = self._write_mask(["enable", "all"])
            else:
                # cumulativo live: si riparte da stock e si accendono solo
                # le WGP validate (una alla volta, a runtime, nessun reboot)
                stock = self._write_mask(["stock-dispatch"])
                if stock["returncode"] != 0:
                    return {"applied": False,
                            "error": stock.get("stderr")
                            or "stock-dispatch fallito"}
                result = self._write_mask(["enable-wgp"] + target)
        parsed = result.get("parsed_output", {})
        if result["returncode"] != 0:
            return {
                "applied": False,
                "error": result.get("stderr") or "scrittura maschera fallita",
            }
        expected = cu_count_from_mask(self.target_mask())
        ok = bool(parsed.get("full_die", False)) if full_die else (
            parsed.get("cu_target") == expected)
        if not ok:
            # verifica dell'EFFETTO: se lo script non riporta la maschera
            # attesa la scrittura non ha preso (fail-closed: niente stato
            # "40 CU" dichiarato a vuoto)
            return {
                "applied": False,
                "error": "maschera non applicata: attese %d CU, lo script "
                         "riporta %s" % (expected,
                                         parsed.get("cu_target")
                                         or parsed.get("cu_routed")),
            }
        return {
            "applied": True,
            "cu_count": expected,
            "mask": self.target_mask(),
            "wgps": target,
            "needs_reboot": False,  # volatile, nessun reboot
            "method": "runtime_umr",
            "warning": (
                "%d CU attive (volatili, runtime UMR): %s. Per la persistenza "
                "al boot: persistenza (conf con la maschera validata scritto "
                "da buo + servizio abilitato)." % (expected, self.target_mask())
            ),
        }

    def _apply_kernel_patch(self) -> Dict[str, Any]:
        """Non-ostree: build + enable del modulo amdgpu patchato.

        La patch abilita TUTTO il die: se il verdetto condanna una WGP
        extra, il metodo kernel patch NON è utilizzabile (fail-closed,
        niente unlock parziale).
        """
        condemned = self.condemned_wgps()
        if condemned:
            return {
                "applied": False,
                "error": "kernel patch = tutte le CU: WGP condannate (%s) — "
                         "usare il runtime UMR con la maschera validata"
                         % ",".join(condemned),
            }
        self.logger.info("Build del modulo amdgpu patchato...")
        build = self.wrapper.build()
        if build["returncode"] != 0:
            return {"applied": False, "error": build["stderr"] or "build fallita"}

        self.logger.info("Enable CU extra (kernel patch)...")
        enable = self.wrapper.enable()
        if enable["returncode"] != 0:
            return {"applied": False, "error": enable["stderr"] or "enable fallito"}

        return {"applied": True, "cu_count": 40, "needs_reboot": True,
                "mask": MASK_ALL_40}

    def persist(self, wgps: Optional[List[str]] = None) -> Dict[str, Any]:
        """Persistenza al boot della maschera VALIDATA (SOLO ostree/UMR).

        La maschera scritta è DERIVATA (WGP validate, quelle condannate
        escluse) e mai 0x1f hardcoded: persistere il full-die su una board
        con WGP guaste renderebbe PERMANENTE il difetto al boot (un fault
        GPU qui non è recuperabile). Opt-in obbligatorio: senza
        `probe.gpu_extra_cu` non si persiste nulla (24 CU = stato
        corretto). Richiede un reboot per l'attivazione.
        """
        if wgps is not None:
            try:
                self.wgps = self._validated_wgps(wgps)
            except ValueError as e:
                return {"persisted": False, "error": str(e)}
        if not self._extra_allowed():
            return {"persisted": False, "reason": "extra_cu_disabled",
                    "note": "CU extra non richieste (opt-in): nessuna "
                            "persistenza, GPU a 24 CU stock"}
        target = self.target_wgps()
        if not target:
            return {"persisted": False, "reason": "all_extra_condemned",
                    "note": "tutte le WGP extra condannate: nulla da "
                            "persistere (24 CU stock)"}
        # P5: la maschera persistita viene instradata DAL SERVIZIO AL BOOT,
        # senza passare da apply(): la curva conservativa va verificata qui,
        # altrimenti al prossimo avvio le CU extra girerebbero su una curva
        # aggressiva (2 GHz con 40 CU = 176-181 W e 96 °C).
        curve = self.curve_conservative()
        if curve is not True:
            return {"persisted": False, "reason": "curve_not_conservative",
                    "error": "curva GPU non conservativa per le CU extra: %s. "
                             "Servono safe-point ≤%d mV (1500 MHz/900 mV) in "
                             "%s" % (
                                 "config illeggibile o senza safe-point"
                                 if curve is None
                                 else "c'è un safe-point sopra il limite",
                                 self.CURVE_MAX_MV, self.governor_conf_path)}
        if not self.is_ostree:
            return {
                "persisted": False,
                "error": "persistenza runtime UMR solo su ostree",
            }
        if self.wrapper is None or not self.wrapper.available:
            return {"persisted": False, "error": "live-manager non installato"}
        mask = self.target_mask()
        try:
            ok, err = self._write_boot_conf(mask)
            if not ok:
                return {"persisted": False, "error": err}
            ok, err = self._ensure_boot_service()
            if not ok:
                return {"persisted": False, "error": err}
        except Exception as e:
            # persist NON deve mai sollevare: l'orchestratore tratta un
            # fallimento di persistenza come warning, mai bloccante.
            return {"persisted": False,
                    "error": "persistenza CU extra fallita: %s" % e}
        return {
            "persisted": True,
            "mask": mask,
            "cu_count": cu_count_from_mask(mask),
            "note": "%d CU persistite al boot (richiede reboot per "
                    "l'attivazione)" % cu_count_from_mask(mask),
        }

    def _write_boot_conf(self, mask: str):
        """Scrive il conf di boot con la maschera VALIDATA (atomico: tmp +
        os.replace, stesso pattern di smoke/verdict). Mai uno snapshot
        della tabella live dello script e mai 0x1f hardcoded.

        Il servizio di boot applica la maschera con lo stesso percorso
        `apply_target_masks` del live-manager (azzeramento CC prima di
        SPI/RLC), quindi non serve una guardia CC qui: BUO scrive un FILE,
        non un registro.
        """
        conf = ("BC250_WGP_MASKS=%s\n"
                "UMR_ASIC=cyan_skillfish.gfx1013\n" % mask)
        path = self.boot_conf_path
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(conf)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception as e:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False, "scrittura conf CU extra fallita: %s" % e
        return True, ""

    def _ensure_boot_service(self):
        """Garantisce bc250-cu-live-manager.service presente + ENABLED.

        • già enabled → skip (nessuna chiamata, nessun rischio);
        • presente ma disabilitata → systemctl enable;
        • ASSENTE → install-service dalla COPIA in /tmp (quirk 'same
          file' di install-service quando lo script gira da
          /usr/local/bin, symlink ostree — BUGS #24), pattern validato
          da _repair_40cu_service.
        """
        import shutil
        from ..utils.shell import run_command

        unit = "bc250-cu-live-manager"
        rc, out, _ = run_command(["systemctl", "is-enabled", unit],
                                 check=False)
        if rc == 0 and out.strip() == "enabled":
            return True, ""
        rc, _, _ = run_command(["systemctl", "cat", unit], check=False)
        if rc == 0:
            rc, _, err = run_command(["systemctl", "enable", unit],
                                     sudo=True, check=False)
            if rc == 0:
                return True, ""
            return False, err or "systemctl enable fallito"
        lm = "/usr/local/bin/bc250-cu-live-manager"
        if not os.path.exists(lm):
            return False, "live-manager assente: %s" % lm
        tmp = "/tmp/bc250-cu-live-manager"
        try:
            shutil.copy2(lm, tmp)
        except Exception as e:
            return False, "copia live-manager in /tmp fallita: %s" % e
        try:
            rc, _, err = run_command([tmp, "-y", "install-service"],
                                     sudo=True, check=False)
            if rc == 0:
                return True, ""
            return False, err or "install-service fallito (rc=%s)" % rc
        finally:
            try:
                os.remove(tmp)
            except Exception:
                pass

    def rollback(self) -> bool:
        """Torna a 24 CU (metodo per distro)."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.disable_40cu()
        if self.wrapper is not None and self.wrapper.available:
            if self.is_ostree:
                # Runtime UMR: stock-dispatch (register write ⇒ governor
                # fermo), nessun reboot
                with self._governor_paused():
                    result = self._write_mask(["stock-dispatch"])
                return result["returncode"] == 0
            result = self.wrapper.restore()
            return result["returncode"] == 0
        return False


__all__ = ["GPU40CUUnlock"]
