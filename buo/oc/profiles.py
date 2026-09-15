#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 BC-250 Community
"""
Profili OC (Stock / Certificato / Custom) + vista silicio + validator
anti-zona. profiles.json è PROPRIETÀ del tool BUO (il motore NON lo legge);
silicon-profile.json è LETTO (read-only) come fonte dei dati certificati.

Scrittura di profiles.json ATOMICA (tmp+fsync+mv, stesso pattern del motore);
file corrotto → WARN + backup .bak + default (fail-soft, mai eccezione).
"""

import base64
import hashlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..constants import GOVERNOR_CONFIG
from .constants import (
    HANG_ZONE2_MIN_FREQ,
    HANG_ZONE2_MIN_VID,
    HANG_ZONE_MIN_FREQ,
    HANG_ZONE_MIN_VID,
    OC_DIR_DEFAULT,
    PROFILES_FILE,
    SCALE_MAX,
    SCALE_MIN,
    SILICON_PROFILE,
    SMU_OC_CONF,
    VID_CAP_HARD,
    WALL_FREQ,
)

logger = logging.getLogger("buo.oc.profiles")

PROFILES_SCHEMA = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Modelli
# ---------------------------------------------------------------------------


@dataclass
class Profile:
    id: str
    name: str
    freq: int
    scale: int
    vid_cap: Optional[int] = None   # VID atteso (mV) — necessario in zona ≥ 3725
    source: str = "user"            # builtin | silicon | user
    validated: bool = False         # true se smoke/L2 già passato su questo silicio
    last_applied: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "name": self.name,
            "freq": self.freq,
            "scale": self.scale,
            "vid_cap": self.vid_cap,
            "source": self.source,
            "validated": self.validated,
            "last_applied": self.last_applied,
        }


# ---------------------------------------------------------------------------
# Vista silicio (lettura read-only di silicon-profile.json)
# ---------------------------------------------------------------------------


class SiliconView:
    """LETTURA read-only dei dati certificati del silicio (motore).

    Assente/corrotto/fingerprint diversa → fail-soft: None (mai eccezione).
    """

    def __init__(self, oc_dir: Optional[Path] = None,
                 silicon_path: Optional[Path] = None):
        self.oc_dir = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
        self._path = Path(silicon_path) if silicon_path else (
            self.oc_dir / SILICON_PROFILE)
        self._data: Optional[Dict] = None

    def load(self) -> Optional[Dict]:
        """Dati silicio parsati: {floor, curve, winner, thermal, confidence,
        hardware_fingerprint, updated_at}; None se assente/corrotto."""
        if self._data is not None:
            return self._data
        try:
            with open(self._path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        self._data = raw
        return raw

    @property
    def updated_at(self) -> Optional[str]:
        data = self.load()
        return data.get("updated_at") if data else None

    def expected_vid(self, freq: int) -> Optional[int]:
        """curve[f].vid_cap (la config certificata del clock); None se ignoto."""
        data = self.load()
        if not data:
            return None
        curve = data.get("curve") or {}
        rec = curve.get(str(freq))
        if not isinstance(rec, dict):
            return None
        try:
            return int(rec["vid_cap"])
        except (KeyError, TypeError, ValueError):
            return None

    def winner(self) -> Optional[Tuple[int, Optional[int]]]:
        """(freq, vid_cap) del winner certificato; None se assente."""
        data = self.load()
        if not data:
            return None
        w = data.get("winner")
        if not isinstance(w, dict):
            return None
        try:
            freq = int(w["freq"])
        except (KeyError, TypeError, ValueError):
            return None
        vid = None
        try:
            vid = int(w["vid_cap"])
        except (KeyError, TypeError, ValueError):
            pass
        return (freq, vid)

    def thermal_max_temperature(self) -> Optional[int]:
        """thermal.max_temperature_smu (per il max_temperature del conf)."""
        data = self.load()
        if not data:
            return None
        th = data.get("thermal") or {}
        try:
            return int(th["max_temperature_smu"])
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Store profili (proprietà del tool)
# ---------------------------------------------------------------------------


def _write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    """Scrittura JSON ATOMICA (tmp+fsync+mv, pattern del motore)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class ProfileStore:
    def __init__(self, oc_dir: Optional[Path] = None,
                 profiles_path: Optional[Path] = None,
                 silicon: Optional[SiliconView] = None):
        self.oc_dir = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
        self._path = Path(profiles_path) if profiles_path else (
            self.oc_dir / PROFILES_FILE)
        self._silicon = silicon if silicon is not None else SiliconView(
            self.oc_dir)
        self._active: Optional[str] = None
        self._last_apply: Dict = {}

    # ----------------------------- default ----------------------------- #

    @staticmethod
    def _default_profiles() -> List[Profile]:
        return [
            Profile(id="stock", name="Stock", freq=3500, scale=0,
                    vid_cap=None, source="builtin", validated=True),
            Profile(id="certified", name="Certificato (dati silicio non "
                    "disponibili)", freq=3500, scale=0, vid_cap=None,
                    source="silicon", validated=False),
        ]

    # ------------------------------ load ------------------------------ #

    def load(self) -> List[Profile]:
        """Profili salvati; default se assenti. Corrotto → WARN + .bak."""
        if not self._path.exists():
            return self._reseed()

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(
                    raw.get("profiles"), list):
                raise ValueError("schema inatteso")
            profiles = [self._from_dict(d) for d in raw["profiles"]]
            self._active = raw.get("active")
            self._last_apply = raw.get("last_apply") or {}
            return self._reseed(profiles, raw.get("updated_at"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning("profiles.json corrotto (%s) — backup .bak e "
                           "default", self._path)
            try:
                self._path.rename(self._path.with_suffix(
                    self._path.suffix + ".bak"))
            except OSError:
                pass
            return self._reseed()

    def _reseed(self, profiles: Optional[List[Profile]] = None,
                saved_at: Optional[str] = None) -> List[Profile]:
        """Ri-semina il profilo Certificato da SiliconView (dati veri del
        silicio) se il dato silicio è più recente del profilo salvato (o se
        il certificato salvato è un segnaposto)."""
        base = profiles if profiles is not None else self._default_profiles()
        out: List[Profile] = []
        certified = None
        for p in base:
            if p.id == "certified":
                certified = p
            else:
                out.append(p)
        cert = self._certified_from_silicon(certified, saved_at)
        # ordine canonico: stock, certified, poi gli altri custom
        ordered = [p for p in out if p.id == "stock"]
        ordered.append(cert)
        ordered += [p for p in out if p.id not in ("stock", "certified")]
        return ordered

    def _certified_from_silicon(self, fallback: Optional[Profile],
                                saved_at: Optional[str]) -> Profile:
        """Voce certificata: valori dal winner/curva del silicio se il dato
        è presente e più recente del salvataggio; altrimenti il salvato (o il
        segnaposto)."""
        silicon = self._silicon
        win = silicon.winner()
        sil_updated = silicon.updated_at
        reseed = False
        if win is not None:
            if saved_at is None:
                reseed = True   # profilo mai salvato → seme dal silicio
            elif sil_updated and saved_at and sil_updated > saved_at:
                reseed = True   # silicio più recente del profilo
        if reseed and win is not None:
            freq, vid = win
            return Profile(
                id="certified",
                name=f"Certificato {freq}@{vid if vid else '?'}",
                freq=freq,
                scale=self._scale_at(freq, vid),
                vid_cap=vid,
                source="silicon",
                validated=True,
                last_applied=fallback.last_applied if fallback else None,
            )
        if fallback is not None:
            return fallback
        return Profile(id="certified", name="Certificato (dati silicio non "
                       "disponibili)", freq=3500, scale=0, vid_cap=None,
                       source="silicon", validated=False)

    def _scale_at(self, freq: int, vid: Optional[int]) -> int:
        """Scale della config certificata: curve[f].scale, poi winner.scale,
        poi 0 (curva stock) — fail-soft, mai inventare valori."""
        data = self._silicon.load()
        if not data:
            return 0
        return _silicon_scale(data, freq)

    @staticmethod
    def _from_dict(d: Dict) -> Profile:
        return Profile(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            freq=int(d.get("freq", 3500)),
            scale=int(d.get("scale", 0)),
            vid_cap=d.get("vid_cap"),
            source=str(d.get("source", "user")),
            validated=bool(d.get("validated", False)),
            last_applied=d.get("last_applied"),
        )

    # ------------------------------ save ------------------------------ #

    def save(self, profiles: List[Profile], active: Optional[str] = None,
             last_apply: Optional[Dict] = None) -> None:
        """Scrittura ATOMICA (tmp+fsync+mv) di profiles.json."""
        self.oc_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "schema_version": PROFILES_SCHEMA,
            "updated_at": _now(),
            "active": active or self._active,
            "profiles": [p.to_dict() for p in profiles],
            "last_apply": last_apply if last_apply is not None
            else self._last_apply,
        }
        _write_json_atomic(self._path, data)

    # ------------------------------- get ------------------------------ #

    def get(self, name_or_id: str) -> Optional[Profile]:
        """Cerca per id o per nome (case-insensitive)."""
        key = name_or_id.strip().lower()
        for p in self.load():
            if p.id.lower() == key or p.name.lower() == key:
                return p
        return None


# ---------------------------------------------------------------------------
# Validator anti-zona (fail-closed: ciò che non si può PROVARE sicuro si blocca)
# ---------------------------------------------------------------------------


class ProfileValidator:
    """REGOLA ANTI-ZONA (utente, dati campo 31/08) + bounds generali."""

    def zone_ok(self, p: Profile) -> Tuple[bool, str]:
        """(ok, motivo). Un profilo NON verificabile in zona → bloccato."""
        if p.scale < SCALE_MIN or p.scale > SCALE_MAX:
            return False, f"scale {p.scale} fuori [{SCALE_MIN}, {SCALE_MAX}]"
        if p.freq >= WALL_FREQ:
            return False, "muro: oltre il tetto documentato"
        if p.freq < 3500:
            # downclock: AMMESSO (profilo "cool"), nessun check di zona
            return True, ""
        if p.vid_cap is not None and p.vid_cap > VID_CAP_HARD:
            return False, f"VID {p.vid_cap} oltre l'hard limit {VID_CAP_HARD}"
        if p.freq >= HANG_ZONE_MIN_FREQ:
            if p.vid_cap is None:
                return False, ("VID non verificabile in zona di hang: usa un "
                               "profilo con VID esplicito o il certificato")
            if p.vid_cap < HANG_ZONE_MIN_VID:
                return False, "zona di hang"
            # Mirror tier-2 dell'engine (02/09, incidente profilo
            # avvelenato): la banda 3800-3870@<=1050 è zona di hang/wedge
            # ALLA SCRITTURA — mai VID < 1125 a f >= 3800.
            if (p.freq >= HANG_ZONE2_MIN_FREQ
                    and p.vid_cap < HANG_ZONE2_MIN_VID):
                return False, "zona di hang (tier-2)"
        return True, ""

    def suggest_vid(self, freq: int,
                    silicon: Optional[SiliconView] = None) -> Optional[int]:
        """VID suggerito dalla curva silicio (per i Custom); None se ignoto
        (il chiamante decide — mai inventare valori)."""
        if silicon is None:
            return None
        return silicon.expected_vid(freq)


def _silicon_scale(sil: Dict, freq: int) -> int:
    """Scale della config certificata per freq: curve[f].scale →
    winner.scale → 0 (curva stock) — mai inventare valori."""
    curve = sil.get("curve") or {}
    rec = curve.get(str(freq))
    if isinstance(rec, dict):
        try:
            return int(rec["scale"])
        except (KeyError, TypeError, ValueError):
            pass
    w = sil.get("winner")
    if isinstance(w, dict):
        try:
            return int(w["scale"])
        except (KeyError, TypeError, ValueError):
            pass
    return 0


# ---------------------------------------------------------------------------
# Fingerprint silicio (mirror fp_capture/fp_hash del motore oc3600.sh)
# ---------------------------------------------------------------------------
# sha256 del JSON canonico dei SOLI campi di silicio non vuoti (cpu model,
# gpu pci id, bios se leggibile) + smu_support SEMPRE. Kernel/driver/distro
# NON entrano (cambiano a ogni update Bazzite → invaliderebbero il riuso).


def silicon_fingerprint(cpu_model: str = "", gpu_pci_id: str = "",
                        bios: str = "", smu_support: bool = False) -> str:
    """Fingerprint SILICON-ONLY dai campi dati (mirror fp_hash del motore)."""
    fields: Dict[str, object] = {}
    if cpu_model:
        fields["cpu_model"] = cpu_model
    if gpu_pci_id:
        fields["gpu_pci_id"] = gpu_pci_id
    if bios:
        fields["bios"] = bios
    fields["smu_support"] = 1 if smu_support else 0
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Campi mock deterministici (stessi default FP_MOCK_* del motore): in
# modalità simulata nessuna lettura reale di /proc/sys (C1).
_MOCK_CPU_MODEL = "AMD BC-250 (Cyan Skillfish)"
_MOCK_GPU_PCI_ID = "1002:1640"
_MOCK_BIOS = "1.90"


def _cpu_model_text() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return ""


def _hex_id(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip().removeprefix("0x")
    except OSError:
        return ""


def _gpu_pci_id() -> str:
    """vendor:device della prima GPU amdgpu; fallback sul PCI 0000:01:00.0.

    La card si cerca con un glob (`card*/device`), mai con un indice fisso:
    l'indice DRM NON è stabile fra i boot (verificato 16/09/2026, la GPU era
    card0 mentre la memoria di progetto diceva card1)."""
    try:
        for dev in sorted(Path("/sys/class/drm").glob("card*/device")):
            try:
                if "DRIVER=amdgpu" not in (dev / "uevent").read_text(
                        encoding="utf-8", errors="ignore"):
                    continue
            except OSError:
                continue
            vid, did = _hex_id(dev / "vendor"), _hex_id(dev / "device")
            if vid and did:
                return f"{vid}:{did}"
    except OSError:
        pass
    alt = Path("/sys/bus/pci/devices/0000:01:00.0")
    vid, did = _hex_id(alt / "vendor"), _hex_id(alt / "device")
    return f"{vid}:{did}" if vid and did else ""


def _bios_version() -> str:
    try:
        r = subprocess.run(["dmidecode", "-s", "bios-version"],
                           capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (r.stdout.strip().splitlines() or [""])[0] if r.returncode == 0 \
        else ""


def _smu_tools_present() -> bool:
    return bool(shutil.which("bc250-detect")
                and shutil.which("bc250-apply"))


def machine_silicon_fingerprint(sim: bool = False,
                                smu_support: Optional[bool] = None) -> str:
    """Fingerprint SILICON-ONLY della macchina corrente.

    sim=True (mock/dry-run) → campi mock deterministici (nessuna lettura
    reale). Reale: /proc/cpuinfo + sysfs drm/pci + dmidecode (bios) +
    presenza tool SMU nel PATH. Fail-soft: campi non leggibili → esclusi
    (mai eccezioni; il JSON risultante può avere il solo smu_support).

    smu_support: override esplicito (None = rilevato dalla presenza dei
    tool nel PATH). Il gate del restore T5 lo forza True: la presenza dei
    tool è stato SOFTWARE (su post-format la toolchain manca ancora: la
    installa _phase_init), non silicio — il confronto deve restare sul
    silicio (il tool assente fallisce fail-closed nell'apply stesso).
    """
    if sim:
        return silicon_fingerprint(cpu_model=_MOCK_CPU_MODEL,
                                   gpu_pci_id=_MOCK_GPU_PCI_ID,
                                   bios=_MOCK_BIOS, smu_support=True)
    try:
        smu = _smu_tools_present() if smu_support is None else smu_support
        return silicon_fingerprint(cpu_model=_cpu_model_text(),
                                   gpu_pci_id=_gpu_pci_id(),
                                   bios=_bios_version(),
                                   smu_support=smu)
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Gate del RIUSO dello stato OC (design UNLEASH_OC_BOUNDARY T1)
# ---------------------------------------------------------------------------


class OCReuseGate:
    """Valutazione del riuso dello stato OC certificato da `buo unleash`.

    Criteri (accordati 06/09): (1) silicon-profile.json valido con
    hardware_fingerprint COERENTE con la macchina corrente; (2) winner
    presente con EVIDENZA di certificazione — ibrida: L2/multiphase nel
    silicon (curve[f].l2_validated) O profilo `certified` di profiles.json
    validato da un apply ok allineato allo stesso winner (mai profili
    avvelenati: lezione 02/09); (3) fuori zona: zone_ok con le regole
    anti-hang statiche incluse il mirror tier-2 3800/1125.

    Solo LETTURA, mai eccezioni (fail-closed: stato ambiguo → niente riuso).
    """

    def __init__(self, oc_dir: Optional[Path] = None,
                 current_fingerprint: Optional[str] = None,
                 silicon: Optional[SiliconView] = None,
                 store: Optional[ProfileStore] = None,
                 validator: Optional[ProfileValidator] = None):
        self.oc_dir = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
        self.current_fingerprint = current_fingerprint
        self.silicon = silicon or SiliconView(self.oc_dir)
        self.store = store or ProfileStore(self.oc_dir,
                                           silicon=self.silicon)
        self.validator = validator or ProfileValidator()

    def candidate(self) -> Tuple[Optional[Profile], str]:
        """(profilo certificato riusabile, nota) — (None, motivo) = il riuso
        NON è consentito (il chiamante esegue la base sicura)."""
        sil = self.silicon.load()
        if not sil:
            return None, "stato OC assente (silicon-profile.json non leggibile)"
        fp = sil.get("hardware_fingerprint")
        if not isinstance(fp, str) or not fp:
            return None, "silicon-profile senza hardware_fingerprint"
        if not self.current_fingerprint:
            return None, "fingerprint corrente non disponibile (fail-closed)"
        if fp != self.current_fingerprint:
            return None, "hardware_fingerprint diversa dalla macchina corrente"
        win = sil.get("winner")
        if not isinstance(win, dict):
            return None, "nessun winner nel silicon-profile"
        try:
            freq = int(win["freq"])
        except (KeyError, TypeError, ValueError):
            return None, "winner senza frequenza valida"
        vid: Optional[int] = None
        try:
            vid = int(win["vid_cap"])
        except (KeyError, TypeError, ValueError):
            pass

        # Evidenza di certificazione (ibrido): L2 nel silicon, altrimenti
        # profilo `certified` validato da un apply ok SULLO STESSO winner.
        curve = sil.get("curve") or {}
        rec = curve.get(str(freq))
        evidence = "l2"
        if not (isinstance(rec, dict) and rec.get("l2_validated") is True):
            cert = self.store.get("certified")
            if cert is None or not cert.validated:
                return None, ("winner non certificato: nessuna evidenza L2 "
                              "nel silicon e profilo certified non validated")
            if cert.freq != freq:
                return None, ("profilo certified non allineato al winner "
                              "silicio (%d vs %d)" % (cert.freq, freq))
            evidence = "apply"

        profile = Profile(
            id="certified",
            name="Certificato %d@%s" % (freq, vid if vid is not None else "?"),
            freq=freq,
            scale=_silicon_scale(sil, freq),
            vid_cap=vid,
            source="silicon",
            validated=True,
        )
        ok, reason = self.validator.zone_ok(profile)
        if not ok:
            return None, "winner in zona di hang: %s" % reason
        return profile, "riuso stato OC certificato (evidenza %s)" % evidence


# ---------------------------------------------------------------------------
# Export/ripristino dello stato OC nel profilo macchina (G2, design T5)
# ---------------------------------------------------------------------------
# Blocco `oc_state` dell'export del profilo (schema §1 DESIGN_T5_EXPORT_G2):
# silicon-profile.json + profiles.json GREZZI (i loro formati nativi) e i
# due conf persistiti come base64 (round-trip byte-identico). Il restore
# riapplica il blocco pre-fasi (gate fingerprint → materialize → GPU → CPU).

OC_STATE_SCHEMA = 1


def export_oc_state(oc_dir: Optional[Path] = None,
                    smu_conf: Optional[str] = None,
                    governor_config: Optional[str] = None
                    ) -> Optional[Dict[str, Any]]:
    """Blocco `oc_state` per l'export del profilo (design T5/G2).

    Path iniettabili (test); default: OC_DIR_DEFAULT, SMU_OC_CONF,
    GOVERNOR_CONFIG. Fail-soft: QUALSIASI sorgente assente/illeggibile →
    None (il chiamante omette il blocco con nota — mai crash dell'export).
    """
    oc = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
    cpu = Path(smu_conf) if smu_conf else Path(SMU_OC_CONF)
    gpu = Path(governor_config) if governor_config else Path(GOVERNOR_CONFIG)
    try:
        silicon = json.loads((oc / SILICON_PROFILE).read_text(
            encoding="utf-8"))
        profiles = json.loads((oc / PROFILES_FILE).read_text(
            encoding="utf-8"))
        if not isinstance(silicon, dict) or not isinstance(profiles, dict) \
                or not isinstance(profiles.get("profiles"), list):
            return None
        cpu_bytes = cpu.read_bytes()
        gpu_bytes = gpu.read_bytes()
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return {
        "schema_version": OC_STATE_SCHEMA,
        "exported_at": _now(),
        "hardware_fingerprint": silicon.get("hardware_fingerprint"),
        "silicon_profile": silicon,
        "profiles": profiles,
        "cpu_conf_b64": base64.b64encode(cpu_bytes).decode("ascii"),
        "gpu_conf_b64": base64.b64encode(gpu_bytes).decode("ascii"),
    }


def materialize_oc_state(silicon_profile: Dict[str, Any],
                         profiles: Dict[str, Any],
                         oc_dir: Optional[Path] = None) -> None:
    """Ricrea OC_DIR dai raw del blocco oc_state (design T5/G2 §3.2).

    silicon-profile.json riscritto com'è (SiliconView lo rilegge senza
    adattatori); profiles.json via la scrittura ATOMICA esistente di
    ProfileStore (i profili raw → oggetti, stesso percorso del load).
    """
    oc = Path(oc_dir) if oc_dir else Path(OC_DIR_DEFAULT)
    raw = profiles.get("profiles")
    if not isinstance(raw, list):
        raise ValueError("profiles del blocco senza lista 'profiles'")
    oc.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(oc / SILICON_PROFILE, silicon_profile)
    store = ProfileStore(oc)
    profs = [ProfileStore._from_dict(d) for d in raw
             if isinstance(d, dict)]
    store.save(profs, active=profiles.get("active"),
               last_apply=profiles.get("last_apply"))
