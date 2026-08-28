#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test del fallback offline per install-deps (design: DESIGN_OFFLINE_DEPS.md).

Copre: export/import del bundle offline (9 check fail-closed), riuso del
checkout verificato (A7), fix del gate needs_git (tipo "build"), flag
orchestrator/config per il bundle.

Zero rete e zero git reali: checkout finti (directory con file scritti dai
test), run_command/which/_verify_checkout mockati, BUO_DEPS_DIR/BUO_STATE_DIR
temporanee.
"""

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from buo.config import BUOConfig
from buo.install.deps import DEPS, DependencyManager
from buo.orchestrator import Orchestrator
from buo.utils.paths import state_dir

BUNDLE_TYPES = ("scripts", "aml", "build")

SMU_FILES = {
    "bc250_detect.py": "#!/usr/bin/env python3\nprint('detect')\n",
    "bc250_apply.py": "#!/usr/bin/env python3\nprint('apply')\n",
    "bc250_limits.py": "LIMITS = 1\n",
    "stress_helper.py": "HELPER = 1\n",
    "bc250_smu/api.py": "API = 1\n",
}


def catalog_repos():
    """Repo del catalogo che vengono bundlati (mai i package)."""
    return {d["name"]: d for d in DEPS if d["type"] in BUNDLE_TYPES}


def make_checkout(base, name, files=None):
    """Crea un checkout finto (con .git vuoto) in base/<name>."""
    checkout = Path(base) / name
    checkout.mkdir(parents=True, exist_ok=True)
    (checkout / ".git").mkdir(parents=True, exist_ok=True)
    for rel, content in (files or {}).items():
        p = checkout / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return checkout


def fake_checkouts(base):
    """Checkout finti per TUTTI i repo bundlati del catalogo."""
    make_checkout(base, "bc250_smu_oc", SMU_FILES)
    make_checkout(base, "bc250-40cu-unlock", {
        "scripts/bc250-enable-40cu.sh": "echo 40cu\n",
        "scripts/bc250-cu-health-test.sh": "echo health\n",
        "scripts/bc250-cu-mask.sh": "echo mask\n",
        "scripts/bc250-compute-verify.sh": "echo verify\n",
    })
    make_checkout(base, "bc250-cu-live-manager", {
        "bc250-cu-live-manager.sh": "echo live\n",
    })
    make_checkout(base, "bc250_memcfg", {"Makefile": "all:\n\ttrue\n"})
    make_checkout(base, "bc250-acpi-fix", {"SSDT-CST.aml": "aml-data"})


def read_manifest(path):
    """Legge buo-bundle.json da un tarball."""
    with tarfile.open(str(path), "r:gz") as tar:
        f = tar.extractfile("buo-bundle.json")
        return json.loads(f.read().decode("utf-8"))


def rewrite_manifest(src, dst, mutator):
    """Copia src→dst modificando buo-bundle.json con mutator(manifest)."""
    with tarfile.open(str(src), "r:gz") as tin, \
         tarfile.open(str(dst), "w:gz") as tout:
        for m in tin.getmembers():
            if m.name == "buo-bundle.json":
                manifest = json.loads(
                    tin.extractfile(m).read().decode("utf-8"))
                mutator(manifest)
                data = json.dumps(manifest, indent=2,
                                  ensure_ascii=False).encode("utf-8")
                info = tarfile.TarInfo("buo-bundle.json")
                info.size = len(data)
                info.mtime = m.mtime
                tout.addfile(info, io.BytesIO(data))
            else:
                tout.addfile(m, tin.extractfile(m))


def rewrite_member(src, dst, member_name, content):
    """Copia src→dst sostituendo il contenuto del member indicato."""
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    with tarfile.open(str(src), "r:gz") as tin, \
         tarfile.open(str(dst), "w:gz") as tout:
        for m in tin.getmembers():
            if m.name == member_name:
                info = tarfile.TarInfo(m.name)
                info.size = len(data)
                info.mtime = m.mtime
                info.mode = m.mode
                tout.addfile(info, io.BytesIO(data))
            else:
                tout.addfile(m, tin.extractfile(m))


def add_member(src, dst, member_name, content):
    """Copia src→dst aggiungendo un member extra (per i test di sicurezza)."""
    data = content.encode("utf-8")
    with tarfile.open(str(src), "r:gz") as tin, \
         tarfile.open(str(dst), "w:gz") as tout:
        for m in tin.getmembers():
            tout.addfile(m, tin.extractfile(m))
        info = tarfile.TarInfo(member_name)
        info.size = len(data)
        tout.addfile(info, io.BytesIO(data))


class TestOfflineBundle(unittest.TestCase):
    """Export/import del bundle offline (check 1-9 del design)."""

    def setUp(self):
        self._deps_tmp = tempfile.TemporaryDirectory()
        self._bin_tmp = tempfile.TemporaryDirectory()
        self._state_tmp = tempfile.TemporaryDirectory()
        self._bundle_tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_DEPS_DIR"] = self._deps_tmp.name
        os.environ["BUO_STATE_DIR"] = self._state_tmp.name
        self.manager = DependencyManager(bin_dir=self._bin_tmp.name)

    def tearDown(self):
        os.environ.pop("BUO_DEPS_DIR", None)
        os.environ.pop("BUO_STATE_DIR", None)
        for t in (self._deps_tmp, self._bin_tmp, self._state_tmp,
                  self._bundle_tmp):
            t.cleanup()

    @property
    def deps_dir(self) -> Path:
        return Path(self._deps_tmp.name)

    @property
    def bin_dir(self) -> Path:
        return Path(self._bin_tmp.name)

    @property
    def bundle_path(self) -> Path:
        return Path(self._bundle_tmp.name) / "bundle.tar.gz"

    def export_valid(self) -> Path:
        """Checkout finti + export con _verify_checkout mockato."""
        fake_checkouts(self.deps_dir)
        with mock.patch.object(DependencyManager, "_verify_checkout",
                               return_value=None):
            res = self.manager.export_bundle(self.bundle_path)
        self.assertEqual(res["status"], "ok")
        return self.bundle_path

    # ---------------------------- export ------------------------------ #

    def test_export_creates_bundle_with_manifest(self):
        fake_checkouts(self.deps_dir)
        with mock.patch.object(DependencyManager, "_verify_checkout",
                               return_value=None):
            res = self.manager.export_bundle(self.bundle_path)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(self.bundle_path.exists())
        manifest = read_manifest(self.bundle_path)
        self.assertEqual(manifest["format"], "buo-bundle")
        self.assertEqual(manifest["version"], 1)
        self.assertTrue(manifest["buo_version"])
        self.assertTrue(manifest["created_at"])
        repos = catalog_repos()
        self.assertEqual(set(manifest["deps"].keys()), set(repos.keys()))
        for name, dep in repos.items():
            entry = manifest["deps"][name]
            self.assertEqual(entry["repo"], dep["repo"])
            self.assertEqual(entry["commit"], dep["commit"])
            self.assertTrue(entry["tree_sha256"])
        # i package (governor/umr) NON vanno MAI nel bundle
        self.assertNotIn("cyan-skillfish-governor", manifest["deps"])
        self.assertNotIn("umr", manifest["deps"])
        # il checkout è completo, INCLUSO .git
        with tarfile.open(str(self.bundle_path), "r:gz") as tar:
            self.assertIsNotNone(
                tar.getmember("checkouts/bc250_smu_oc/.git"))
            self.assertIsNotNone(
                tar.getmember("checkouts/bc250_smu_oc/bc250_detect.py"))
        # SHA-256 stampato per verifica manuale (sha256sum)
        self.assertEqual(
            res["sha256"],
            hashlib.sha256(self.bundle_path.read_bytes()).hexdigest())

    def test_export_fails_when_checkout_missing(self):
        # nessun checkout → failed, nessun file scritto
        res = self.manager.export_bundle(self.bundle_path)
        self.assertEqual(res["status"], "failed")
        self.assertFalse(self.bundle_path.exists())

    def test_export_fails_when_one_checkout_missing(self):
        fake_checkouts(self.deps_dir)
        shutil.rmtree(self.deps_dir / "bc250-acpi-fix")
        with mock.patch.object(DependencyManager, "_verify_checkout",
                               return_value=None):
            res = self.manager.export_bundle(self.bundle_path)
        self.assertEqual(res["status"], "failed")
        self.assertIn("bc250-acpi-fix", res["detail"])
        self.assertFalse(self.bundle_path.exists())

    def test_export_fails_when_verify_fails(self):
        fake_checkouts(self.deps_dir)
        with mock.patch.object(
                DependencyManager, "_verify_checkout",
                return_value="bc250_smu_oc: checkout sporco (dirty)"):
            res = self.manager.export_bundle(self.bundle_path)
        self.assertEqual(res["status"], "failed")
        self.assertIn("bc250_smu_oc", res["detail"])
        self.assertIn("dirty", res["detail"])
        self.assertFalse(self.bundle_path.exists())

    # ---------------------------- import ------------------------------ #

    def test_import_roundtrip_installs(self):
        bundle = self.export_valid()
        # wipe deps_dir: simula la macchina appena formattata
        shutil.rmtree(self.deps_dir)
        self.deps_dir.mkdir()
        with mock.patch.object(DependencyManager, "_verify_checkout",
                               return_value=None):
            res = self.manager.import_bundle(bundle)
        self.assertEqual(res["status"], "ok")
        for name in catalog_repos():
            self.assertTrue((self.deps_dir / name).exists(),
                            f"checkout {name} mancante dopo l'import")
        # install: copia gli script dal checkout importato (bin_dir temp)
        with mock.patch.object(DependencyManager, "_verify_checkout",
                               return_value=None):
            result = self.manager.install(deps=["bc250_smu_oc"],
                                          offline_bundle=bundle)
        self.assertEqual(result["bc250_smu_oc"]["status"], "ok")
        self.assertTrue((self.bin_dir / "bc250-detect").exists())
        self.assertTrue((self.bin_dir / "bc250_smu" / "api.py").exists())
        # A7: l'impronta SHA-256 del file installato è registrata
        hashes = json.loads(
            (state_dir() / "deps-hashes.json").read_text(encoding="utf-8"))
        self.assertIn(str(self.bin_dir / "bc250-detect"), hashes)

    def test_import_refuses_wrong_commit(self):
        bundle = self.export_valid()
        name = "bc250_smu_oc"
        tampered = Path(self._bundle_tmp.name) / "wrong-commit.tar.gz"
        rewrite_manifest(
            bundle, tampered,
            lambda m: m["deps"][name].__setitem__("commit", "0" * 40))
        res = self.manager.import_bundle(tampered)
        self.assertEqual(res["status"], "failed")
        self.assertIn("obsoleto", res["detail"])
        self.assertIn(name, res["detail"])
        # nessuna modifica (il checkout resta quello finto di partenza)
        self.assertEqual(
            (self.deps_dir / name / "bc250_detect.py").read_text(),
            SMU_FILES["bc250_detect.py"])

    def test_import_refuses_partial_bundle(self):
        bundle = self.export_valid()
        missing_name = "bc250-acpi-fix"
        tampered = Path(self._bundle_tmp.name) / "partial.tar.gz"
        rewrite_manifest(bundle, tampered,
                         lambda m: m["deps"].pop(missing_name))
        res = self.manager.import_bundle(tampered)
        self.assertEqual(res["status"], "failed")
        self.assertIn("parziale", res["detail"])
        self.assertIn(missing_name, res["detail"])

    def test_import_refuses_corrupt_file(self):
        garbage = self.bundle_path
        garbage.write_bytes("questo non è un tarball gzip".encode("utf-8"))
        res = self.manager.import_bundle(garbage)
        self.assertEqual(res["status"], "failed")
        self.assertIn("bundle BUO valido", res["detail"])
        self.assertEqual(list(self.deps_dir.iterdir()), [])

    def test_import_refuses_tampered_tree(self):
        bundle = self.export_valid()
        shutil.rmtree(self.deps_dir)
        self.deps_dir.mkdir()
        tampered = Path(self._bundle_tmp.name) / "tampered-tree.tar.gz"
        rewrite_member(bundle, tampered,
                       "checkouts/bc250_smu_oc/bc250_detect.py",
                       "#!/usr/bin/env python3\nprint('MANOMESSO')\n")
        res = self.manager.import_bundle(tampered)
        self.assertEqual(res["status"], "failed")
        self.assertIn("tree hash", res["detail"])
        self.assertFalse((self.deps_dir / "bc250_smu_oc").exists())

    def test_import_refuses_path_traversal(self):
        bundle = self.export_valid()
        evil1 = Path(self._bundle_tmp.name) / "evil1.tar.gz"
        evil = Path(self._bundle_tmp.name) / "evil.tar.gz"
        add_member(bundle, evil1, "../evil", "pwned")
        add_member(evil1, evil, "/etc/evil", "pwned")
        res = self.manager.import_bundle(evil)
        self.assertEqual(res["status"], "failed")
        self.assertIn("sicuro", res["detail"])

    def test_import_idempotent(self):
        bundle = self.export_valid()
        shutil.rmtree(self.deps_dir)
        self.deps_dir.mkdir()
        with mock.patch.object(DependencyManager, "_verify_checkout",
                               return_value=None):
            res1 = self.manager.import_bundle(bundle)
            res2 = self.manager.import_bundle(bundle)
        self.assertEqual(res1["status"], "ok")
        self.assertEqual(res2["status"], "ok")
        self.assertEqual(res2["imported"], [])  # nulla da rifare
        self.assertEqual(
            (self.deps_dir / "bc250_smu_oc" / "bc250_detect.py").read_text(),
            SMU_FILES["bc250_detect.py"])

    def test_import_conflict_in_deps_dir(self):
        bundle = self.export_valid()
        shutil.rmtree(self.deps_dir)
        self.deps_dir.mkdir()
        # deps_dir ha un checkout in CONFLITTO (contenuto diverso dal bundle)
        make_checkout(self.deps_dir, "bc250_smu_oc", {
            "bc250_detect.py": "#!/usr/bin/env python3\nprint('DIVERSO')\n",
        })
        with mock.patch("buo.install.deps.which", return_value=None):
            res = self.manager.import_bundle(bundle)
        self.assertEqual(res["status"], "failed")
        self.assertIn("conflitto", res["detail"])
        # fail-closed: il checkout conflittuale NON è stato toccato e
        # nessun altro checkout è stato importato
        self.assertEqual(
            (self.deps_dir / "bc250_smu_oc" / "bc250_detect.py").read_text(),
            "#!/usr/bin/env python3\nprint('DIVERSO')\n")
        self.assertFalse((self.deps_dir / "bc250-40cu-unlock").exists())


class TestOfflineInstall(unittest.TestCase):
    """install(offline_bundle=...) e riuso del checkout verificato (A7)."""

    def setUp(self):
        self._deps_tmp = tempfile.TemporaryDirectory()
        self._bin_tmp = tempfile.TemporaryDirectory()
        self._state_tmp = tempfile.TemporaryDirectory()
        self._bundle_tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_DEPS_DIR"] = self._deps_tmp.name
        os.environ["BUO_STATE_DIR"] = self._state_tmp.name
        self.manager = DependencyManager(bin_dir=self._bin_tmp.name)

    def tearDown(self):
        os.environ.pop("BUO_DEPS_DIR", None)
        os.environ.pop("BUO_STATE_DIR", None)
        for t in (self._deps_tmp, self._bin_tmp, self._state_tmp,
                  self._bundle_tmp):
            t.cleanup()

    @property
    def deps_dir(self) -> Path:
        return Path(self._deps_tmp.name)

    @property
    def bin_dir(self) -> Path:
        return Path(self._bin_tmp.name)

    @property
    def bundle_path(self) -> Path:
        return Path(self._bundle_tmp.name) / "bundle.tar.gz"

    def test_install_offline_without_git(self):
        """Macchina senza git + bundle valido → install ok (nessun git)."""
        fake_checkouts(self.deps_dir)
        with mock.patch.object(DependencyManager, "_verify_checkout",
                               return_value=None):
            res = self.manager.export_bundle(self.bundle_path)
        self.assertEqual(res["status"], "ok")
        shutil.rmtree(self.deps_dir)
        self.deps_dir.mkdir()
        with mock.patch("buo.install.deps.which", return_value=None), \
             mock.patch.object(DependencyManager, "_verify_checkout",
                               return_value=None):
            result = self.manager.install(deps=["bc250_smu_oc"],
                                          offline_bundle=self.bundle_path)
        self.assertEqual(result["bc250_smu_oc"]["status"], "ok")
        self.assertTrue((self.bin_dir / "bc250-detect").exists())
        hashes = json.loads(
            (state_dir() / "deps-hashes.json").read_text(encoding="utf-8"))
        self.assertIn(str(self.bin_dir / "bc250-detect"), hashes)

    def test_reuse_verified_checkout_without_network(self):
        """Riuso checkout esistente verificato: install ok, NESSUN clone."""
        make_checkout(self.deps_dir, "bc250_smu_oc", SMU_FILES)
        commit = next(d for d in DEPS
                      if d["name"] == "bc250_smu_oc")["commit"]
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == "git" and cmd[-1] == "HEAD":
                return (0, commit, "")
            return (0, "", "")

        with mock.patch("buo.install.deps.run_command",
                        side_effect=fake_run), \
             mock.patch("buo.install.deps.which",
                        side_effect=lambda t: "/usr/bin/git"
                        if t == "git" else shutil.which(t)):
            result = self.manager.install(deps=["bc250_smu_oc"])
        self.assertEqual(result["bc250_smu_oc"]["status"], "ok")
        self.assertTrue((self.bin_dir / "bc250-detect").exists())
        self.assertFalse(any(c[0] == "git" and "clone" in c for c in calls),
                         "il checkout verificato non deve essere riclonato")

    def test_reuse_dirty_checkout_fails(self):
        """Riuso checkout sporco/a commit errato → failed, niente copiato."""
        make_checkout(self.deps_dir, "bc250_smu_oc", SMU_FILES)
        with mock.patch.object(DependencyManager, "_verify_checkout",
                               return_value="checkout modificato localmente"):
            result = self.manager.install(deps=["bc250_smu_oc"])
        self.assertEqual(result["bc250_smu_oc"]["status"], "failed")
        self.assertFalse((self.bin_dir / "bc250-detect").exists())

    def test_needs_git_includes_build(self):
        """Solo bc250_memcfg (tipo "build") manca + git assente → _error."""
        fake_checkouts(self.deps_dir)
        shutil.rmtree(self.deps_dir / "bc250_memcfg")
        with mock.patch("buo.install.deps.which", return_value=None):
            result = self.manager.install()
        self.assertIn("_error", result)
        self.assertIn("git", result["_error"])


class TestOfflineOrchestratorConfig(unittest.TestCase):
    """Flag/config del bundle nell'orchestratore (T4)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BUO_STATE_DIR"] = self._tmp.name

    def tearDown(self):
        os.environ.pop("BUO_STATE_DIR", None)
        os.environ.pop("BUO_DEPS_DIR", None)
        self._tmp.cleanup()

    def _fake_manager(self, calls):
        def fake_check(self, deps=None):
            return {d["name"]: {"present": False, "type": d["type"]}
                    for d in DEPS}

        def fake_install(self, deps=None, sudo=True, offline_bundle=None):
            calls.append(offline_bundle)
            return {d["name"]: {"status": "ok"} for d in DEPS}

        return (mock.patch.object(DependencyManager, "check", fake_check),
                mock.patch.object(DependencyManager, "install", fake_install))

    def test_unleash_offline_flag_and_config(self):
        calls = []
        check_p, install_p = self._fake_manager(calls)
        with check_p, install_p:
            orch = Orchestrator(config=BUOConfig(), mock=False, dry_run=False,
                                offline_bundle="/tmp/flag-bundle.tar.gz")
            with mock.patch.object(orch, "_configure_installed_governor"):
                orch._ensure_dependencies()
        self.assertEqual(calls, ["/tmp/flag-bundle.tar.gz"])

        # config deps.offline_bundle (senza flag)
        cfg = BUOConfig({"deps": {"offline_bundle": "/cfg/bundle.tar.gz"}})
        with check_p, install_p:
            orch2 = Orchestrator(config=cfg, mock=False, dry_run=False)
            with mock.patch.object(orch2, "_configure_installed_governor"):
                orch2._ensure_dependencies()
        self.assertEqual(calls[-1], "/cfg/bundle.tar.gz")

        # il flag ha precedenza sulla config
        with check_p, install_p:
            orch3 = Orchestrator(config=cfg, mock=False, dry_run=False,
                                 offline_bundle="/tmp/flag.tar.gz")
            with mock.patch.object(orch3, "_configure_installed_governor"):
                orch3._ensure_dependencies()
        self.assertEqual(calls[-1], "/tmp/flag.tar.gz")

    def test_config_parses_offline_bundle(self):
        cfg = BUOConfig({"deps": {"offline_bundle": "/x/bundle.tar.gz"}})
        self.assertEqual(cfg.deps_offline_bundle, "/x/bundle.tar.gz")
        self.assertEqual(BUOConfig().deps_offline_bundle, "")
        self.assertEqual(
            BUOConfig().to_dict()["deps"]["offline_bundle"], "")


if __name__ == "__main__":
    unittest.main()
