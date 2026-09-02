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

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .constants import (
    HANG_ZONE_MIN_FREQ,
    HANG_ZONE_MIN_VID,
    OC_DIR_DEFAULT,
    PROFILES_FILE,
    SCALE_MAX,
    SCALE_MIN,
    SILICON_PROFILE,
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
        curve = data.get("curve") or {}
        rec = curve.get(str(freq))
        if isinstance(rec, dict):
            try:
                return int(rec["scale"])
            except (KeyError, TypeError, ValueError):
                pass
        w = data.get("winner")
        if isinstance(w, dict):
            try:
                return int(w["scale"])
            except (KeyError, TypeError, ValueError):
                pass
        return 0

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
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

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
        return True, ""

    def suggest_vid(self, freq: int,
                    silicon: Optional[SiliconView] = None) -> Optional[int]:
        """VID suggerito dalla curva silicio (per i Custom); None se ignoto
        (il chiamante decide — mai inventare valori)."""
        if silicon is None:
            return None
        return silicon.expected_vid(freq)
