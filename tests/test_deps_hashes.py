#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test A7: supply chain — hash SHA-256 dei tool installati."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.install.deps import DependencyManager
from buo.utils.paths import state_dir


class TestDepsHashes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        self._tmp.cleanup()

    def test_record_hash_creates_file(self):
        mgr = DependencyManager()
        script = Path(self._tmp.name) / "script.sh"
        content = b"#!/bin/sh\necho hi\n"
        script.write_bytes(content)
        dep = {"name": "bc250-test", "commit": "abc123"}
        mgr._record_hash(dep, {"src": "script.sh"}, script)

        path = state_dir() / "deps-hashes.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get(str(script))
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry["sha256"], hashlib.sha256(content).hexdigest())
        self.assertEqual(entry["commit"], "abc123")
        self.assertEqual(entry["dest"], str(script))

    def test_record_hash_updates_existing_file(self):
        mgr = DependencyManager()
        script = Path(self._tmp.name) / "script.sh"
        script.write_bytes(b"v1")
        mgr._record_hash({"name": "x", "commit": "c1"},
                         {"src": "s"}, script)
        # seconda installazione: hash aggiornato
        script.write_bytes(b"v2")
        mgr._record_hash({"name": "x", "commit": "c2"},
                         {"src": "s"}, script)
        data = json.loads(
            (state_dir() / "deps-hashes.json").read_text(encoding="utf-8"))
        self.assertEqual(data[str(script)]["sha256"],
                         hashlib.sha256(b"v2").hexdigest())

    def test_record_hash_unreadable_file_does_not_raise(self):
        mgr = DependencyManager()
        missing = Path(self._tmp.name) / "non-esiste.sh"
        mgr._record_hash({"name": "x", "commit": "c"},
                         {"src": "s"}, missing)  # non deve sollevare


if __name__ == "__main__":
    unittest.main()


class TestBuildDep(unittest.TestCase):
    """G6: tipo deps 'build' (clone → make → install binario)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()       # bin_dir (+ checkout finti)
        self._deps_tmp = tempfile.TemporaryDirectory()  # deps_dir (checkout per _check_one)
        os.environ["BUO_STATE_DIR"] = self._tmp.name
        os.environ["BUO_DEPS_DIR"] = self._deps_tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()
        self._deps_tmp.cleanup()

    def _memcfg_dep(self):
        return {"name": "bc250_memcfg", "type": "build",
                "binary": "bc250memcfg", "install_as": "bc250_memcfg",
                "required_for": "fix VRAM"}

    def test_build_installs_binary(self):
        mgr = DependencyManager(bin_dir=self._tmp.name)
        checkout = Path(self._tmp.name) / "checkout"
        checkout.mkdir()
        binary = checkout / "bc250_memcfg"
        binary.write_bytes(b"ELFBINARY")
        dep = {"name": "bc250_memcfg", "commit": "abc",
               "binary": "bc250_memcfg"}

        with mock.patch("buo.install.deps.run_command",
                        return_value=(0, "", "")), \
             mock.patch("buo.install.deps.which", return_value="/usr/bin/make"):
            out = mgr._build_and_install(dep, checkout, sudo=False)

        self.assertEqual(out["status"], "ok")
        dest = Path(self._tmp.name) / "bc250_memcfg"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), b"ELFBINARY")

    def test_build_installs_as_install_as(self):
        """Bug sul campo: il make di fanoush/bc250_memcfg produce il
        binario `bc250memcfg` (SENZA underscore); BUO lo installa come
        `bc250_memcfg` (install_as) — il nome canonico atteso da
        vram.py e dalla doc utente."""
        mgr = DependencyManager(bin_dir=self._tmp.name)
        checkout = Path(self._tmp.name) / "checkout"
        checkout.mkdir()
        produced = checkout / "bc250memcfg"     # nome prodotto dal make
        produced.write_bytes(b"ELFBINARY")
        dep = self._memcfg_dep()
        dep["commit"] = "abc"

        with mock.patch("buo.install.deps.run_command",
                        return_value=(0, "", "")), \
             mock.patch("buo.install.deps.which", return_value="/usr/bin/make"):
            out = mgr._build_and_install(dep, checkout, sudo=False)

        self.assertEqual(out["status"], "ok")
        dest = Path(self._tmp.name) / "bc250_memcfg"
        self.assertTrue(dest.exists(), "install_as: binario installato "
                                       "col nome canonico")
        self.assertEqual(dest.read_bytes(), b"ELFBINARY")
        # nessun doppione col nome prodotto dal make
        self.assertFalse((Path(self._tmp.name) / "bc250memcfg").exists())
        # A7: l'hash è registrato per la destinazione reale (install_as)
        data = json.loads(
            (state_dir() / "deps-hashes.json").read_text(encoding="utf-8"))
        self.assertIn(str(dest), data)

    def test_build_check_requires_installed_binary(self):
        """Gap 2: dep 'build' presente SOLO se il binario installato
        esiste in bin_dir. Il checkout da solo (es. dopo un build
        fallito) NON basta: altrimenti i run successivi credono il dep
        presente e non lo reinstallano (bug sul campo)."""
        mgr = DependencyManager(bin_dir=self._tmp.name)
        # checkout presente (come dopo un make fallito) ma binario assente
        (Path(self._deps_tmp.name) / "bc250_memcfg").mkdir()
        st = mgr._check_one(self._memcfg_dep())
        self.assertFalse(st["present"])
        self.assertIn("bc250_memcfg", st["missing"])

    def test_build_check_present_with_installed_binary_no_checkout(self):
        """Binario installato → dep presente ANCHE senza checkout (il
        checkout serve solo per ricompilare)."""
        mgr = DependencyManager(bin_dir=self._tmp.name)
        dest = Path(self._tmp.name) / "bc250_memcfg"
        dest.write_bytes(b"ELFBINARY")
        st = mgr._check_one(self._memcfg_dep())
        self.assertTrue(st["present"])
        self.assertEqual(st["missing"], [])

    def test_build_check_falls_back_to_binary_name(self):
        """Senza install_as, il check build guarda bin_dir/<binary>."""
        mgr = DependencyManager(bin_dir=self._tmp.name)
        dep = self._memcfg_dep()
        dep.pop("install_as")
        (Path(self._tmp.name) / "bc250memcfg").write_bytes(b"ELFBINARY")
        st = mgr._check_one(dep)
        self.assertTrue(st["present"])

    def test_build_fails_without_make(self):
        mgr = DependencyManager(bin_dir=self._tmp.name)
        dep = {"name": "bc250_memcfg", "commit": "abc"}
        with mock.patch("buo.install.deps.which", return_value=None):
            out = mgr._build_and_install(dep, Path("/nonexistent"),
                                         sudo=False)
        self.assertEqual(out["status"], "failed")
        self.assertIn("make", out["detail"])

    def test_build_fails_when_make_errors(self):
        mgr = DependencyManager(bin_dir=self._tmp.name)
        checkout = Path(self._tmp.name) / "checkout"
        checkout.mkdir()
        dep = {"name": "bc250_memcfg", "commit": "abc",
               "binary": "bc250_memcfg"}
        with mock.patch("buo.install.deps.run_command",
                        return_value=(1, "", "make: errore")), \
             mock.patch("buo.install.deps.which", return_value="/usr/bin/make"):
            out = mgr._build_and_install(dep, checkout, sudo=False)
        self.assertEqual(out["status"], "failed")
        self.assertIn("make", out["detail"])

    def test_memcfg_in_catalog(self):
        from buo.install.deps import _build_deps
        names = [d["name"] for d in _build_deps()]
        self.assertIn("bc250_memcfg", names)
        dep = next(d for d in _build_deps() if d["name"] == "bc250_memcfg")
        self.assertEqual(dep["type"], "build")
        self.assertTrue(dep["commit"])
        # bug sul campo: il make produce `bc250memcfg` (senza underscore),
        # ma il contratto con vram.py/doc utente è `bc250_memcfg`
        self.assertEqual(dep["binary"], "bc250memcfg")
        self.assertEqual(dep["install_as"], "bc250_memcfg")
