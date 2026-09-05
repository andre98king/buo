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
"""

import os
from typing import Any, Dict

from ..utils.logging import LoggerMixin
from .wrappers.bc250_40cu import BC25040CUWrapper
from .wrappers.bc250_live_manager import BC250LiveManagerWrapper


class GPU40CUUnlock(LoggerMixin):
    """Sblocco delle 40 CU GPU (metodo per distro)."""

    # Conf di boot del live-manager (EnvironmentFile dell'unità systemd);
    # percorso iniettabile nei test.
    boot_conf_path = "/etc/bc250-cu-live-manager.conf"
    # Profilo di boot FULL-DIE: enable_all instrada tutto il die e applica
    # proprio queste maschere (0x1f x4 = 20 WGP = 40 CU). Formato verificato
    # sul campo (cat /etc/bc250-cu-live-manager.conf).
    _FULL_DIE_CONF = (
        "BC250_WGP_MASKS=0x1f,0x1f,0x1f,0x1f\n"
        "UMR_ASIC=cyan_skillfish.gfx1013\n"
    )

    def __init__(self, mock: bool = False, mock_hardware=None,
                 use_wrapper: bool = True):
        self.mock = mock
        self.mock_hw = mock_hardware
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

    def is_enabled(self) -> bool:
        """True se le 40 CU sono già attive (routed)."""
        if self.mock and self.mock_hw is not None:
            return self.mock_hw.get_cu_count() >= 40
        if self.wrapper is not None and self.wrapper.available:
            st = self.wrapper.status().get("parsed_output", {})
            return bool(st.get("full_die", False))
        return False

    def apply(self) -> Dict[str, Any]:
        """Abilita le 40 CU col metodo corretto per la distro."""
        if self.mock and self.mock_hw is not None:
            ok = self.mock_hw.enable_40cu()
            return {
                "applied": ok,
                "cu_count": self.mock_hw.get_cu_count(),
                "needs_reboot": True,
            }

        if self.wrapper is None or not self.wrapper.available:
            return {
                "applied": False,
                "error": "script 40-CU non trovato — esegui: sudo buo install-deps",
            }

        if self.is_ostree:
            return self._apply_runtime_umr()

        return self._apply_kernel_patch()

    def _apply_runtime_umr(self) -> Dict[str, Any]:
        """Ostree: runtime UMR, volatile, nessun reboot, reversibile."""
        self.logger.info("40-CU via runtime UMR (ostree) — enable all...")
        result = self.wrapper.enable_all()
        parsed = result.get("parsed_output", {})
        if result["returncode"] != 0:
            return {
                "applied": False,
                "error": result.get("stderr") or "enable all fallito",
            }
        ok = bool(parsed.get("full_die", False))
        return {
            "applied": ok,
            "cu_count": parsed.get("cu_routed", 40) if ok else 24,
            "needs_reboot": False,  # volatile, nessun reboot
            "method": "runtime_umr",
            "warning": (
                "40 CU attive (volatili, runtime UMR). Per la persistenza "
                "al boot: eseguire la persistenza (conf full-die scritto "
                "da buo + servizio abilitato), validata sul campo."
            ),
        }

    def _apply_kernel_patch(self) -> Dict[str, Any]:
        """Non-ostree: build + enable del modulo amdgpu patchato."""
        self.logger.info("Build del modulo amdgpu patchato...")
        build = self.wrapper.build()
        if build["returncode"] != 0:
            return {"applied": False, "error": build["stderr"] or "build fallita"}

        self.logger.info("Enable 40-CU...")
        enable = self.wrapper.enable()
        if enable["returncode"] != 0:
            return {"applied": False, "error": enable["stderr"] or "enable fallito"}

        return {"applied": True, "cu_count": 40, "needs_reboot": True}

    def persist(self) -> Dict[str, Any]:
        """Persistenza 40 CU al boot (SOLO ostree/runtime UMR, opt-in).

        Root cause (bug campo 05/09): il flusso storico (install-service +
        write-service-table) SNAPSHOTTAVA la tabella WGP LIVE dello script
        → su macchina a 24-CU live (dopo un reboot) il conf di boot veniva
        regredito a MASKS=0x07 (24 CU). Ora il conf full-die (0x1f x4 +
        UMR_ASIC — il target che enable_all applica) è scritto DIRETTAMENTE:
        buo conosce il target e non dipende dallo snapshot. Il servizio di
        boot viene solo garantito presente+enabled. Richiede un reboot per
        l'attivazione.
        """
        if not self.is_ostree:
            return {
                "persisted": False,
                "error": "persistenza runtime UMR solo su ostree",
            }
        if self.wrapper is None or not self.wrapper.available:
            return {"persisted": False, "error": "live-manager non installato"}
        try:
            ok, err = self._write_boot_conf()
            if not ok:
                return {"persisted": False, "error": err}
            ok, err = self._ensure_boot_service()
            if not ok:
                return {"persisted": False, "error": err}
        except Exception as e:
            # persist NON deve mai sollevare: l'orchestratore tratta un
            # fallimento di persistenza come warning, mai bloccante.
            return {"persisted": False,
                    "error": "persistenza 40-CU fallita: %s" % e}
        return {
            "persisted": True,
            "note": "40 CU persistite al boot (richiede reboot per l'attivazione)",
        }

    def _write_boot_conf(self):
        """Scrive il conf di boot full-die (atomico: tmp + os.replace,
        stesso pattern di smoke/verdict). Mai uno snapshot della tabella
        live dello script."""
        path = self.boot_conf_path
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(self._FULL_DIE_CONF)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception as e:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False, "scrittura conf 40-CU fallita: %s" % e
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
                # Runtime UMR: stock-dispatch, nessun reboot
                result = self.wrapper.stock_dispatch()
                return result["returncode"] == 0
            result = self.wrapper.restore()
            return result["returncode"] == 0
        return False
