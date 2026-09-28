"""Tests for the standalone sudo scripts (clean failure behaviour)."""
import contextlib
import io
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# Hermetic: the real waldur_site_agent.common.utils loads every backend
# entry point at import time, which is neither wanted nor needed here.
# Install the fake BEFORE the script modules are imported.
_fake_cfg = MagicMock()
_fake_utils = types.ModuleType("waldur_site_agent.common.utils")
_fake_utils.load_configuration = MagicMock(return_value=_fake_cfg)
sys.modules["waldur_site_agent.common.utils"] = _fake_utils

from waldur_site_agent_sofia_storage import mkhomedir, mkprojdir, setprojquota


def run(mod, argv):
    """Run a script's main(); return (exit_code, stderr_text)."""
    err = io.StringIO()
    with patch.object(sys, "argv", [mod.__name__] + argv), contextlib.redirect_stderr(err):
        try:
            mod.main()
            code = None
        except SystemExit as e:
            code = e.code
    return code, err.getvalue()


def assert_clean_failure(self, mod, argv, expected_message):
    code, err = run(mod, argv)
    self.assertEqual(code, 1)
    self.assertIn(expected_message, err)
    self.assertNotIn("Traceback", err)
    self.assertEqual(err.strip().count("\n"), 0)


class NoOfferingTest(unittest.TestCase):
    def setUp(self):
        self.offering = MagicMock(backend_type="sofia_storage")
        _fake_cfg.offerings = [MagicMock(backend_type="slurm")]

    def test_mkprojdir_no_offering(self):
        assert_clean_failure(
            self, mkprojdir, ["proj-x", "1000", "1000"], "sofia_storage backend not found"
        )

    def test_setprojquota_no_offering(self):
        assert_clean_failure(
            self, setprojquota, ["proj-x", "1073741824"], "sofia_storage backend not found"
        )

    def test_mkhomedir_no_offering(self):
        assert_clean_failure(self, mkhomedir, ["vsc1001"], "sofia_storage backend not found")


class UsernameCheckTest(unittest.TestCase):
    def setUp(self):
        _fake_cfg.offerings = [MagicMock(backend_type="slurm")]
        _fake_utils.load_configuration.reset_mock()

    def test_non_vsc_username_fails_before_config(self):
        assert_clean_failure(self, mkhomedir, ["alice"], "does not correspond to a VSC user")
        _fake_utils.load_configuration.assert_not_called()


class HappyPathTest(unittest.TestCase):
    def setUp(self):
        self.offering = MagicMock(backend_type="sofia_storage")
        _fake_cfg.offerings = [self.offering]

    def test_mkprojdir_defaults(self):
        self.offering.backend_settings = {}
        with patch.object(mkprojdir, "SofiaStorageClient") as client_cls:
            code, err = run(mkprojdir, ["proj-x", "1000", "2000"])
        self.assertIsNone(code)
        client_cls.assert_called_once_with("gpfs", "/gpfs", "/home")
        client_cls.return_value.create_fileset.assert_called_once_with("proj-x")
        client_cls.return_value.set_project_owner.assert_called_once_with(
            fileset_name="proj-x", owner_uid=1000, owner_gid=2000
        )

    def test_setprojquota_explicit_settings(self):
        self.offering.backend_settings = {
            "storage_file_system": "scratch",
            "storage_path": "/scratch",
            "home_path": "/homes",
        }
        with patch.object(setprojquota, "SofiaStorageClient") as client_cls:
            code, err = run(setprojquota, ["proj-x", "53687091200"])
        self.assertIsNone(code)
        client_cls.assert_called_once_with("scratch", "/scratch", "/homes")
        client_cls.return_value.set_fileset_quota.assert_called_once_with(
            fileset_name="proj-x", block_limit=53687091200
        )

    def test_mkhomedir_creates_home(self):
        self.offering.backend_settings = {}
        with patch.object(mkhomedir, "SofiaStorageClient") as client_cls, \
             patch.object(mkhomedir, "VSC") as vsc_cls:
            vsc = vsc_cls.return_value
            vsc.uid_to_uid_number.return_value = 1000
            code, err = run(mkhomedir, ["vsc1001"])
        self.assertIsNone(code)
        vsc_cls.assert_called_once()
        vsc.get_vsc_options.assert_called_once()
        client_cls.return_value.make_home_dir.assert_called_once_with("vsc1001", 1000)


if __name__ == "__main__":
    unittest.main()
