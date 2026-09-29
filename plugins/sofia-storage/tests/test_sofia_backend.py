"""Unit tests for SofiaStorageBackend."""
import os
import tempfile
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from waldur_site_agent.backend.exceptions import BackendError

from waldur_site_agent_sofia_storage import backend as bmod
from waldur_site_agent_sofia_storage import vsc as vscmod
from waldur_site_agent_sofia_storage.backend import SofiaStorageBackend

GB = 1073741824

COMPONENTS = {
    "storage": {
        "unit_factor": GB,
        "limit": 100,
        "label": "Storage",
        "measured_unit": "GiB",
    }
}
SETTINGS = {"vsc_token": "t", "vsc_autogroup": "ag", "vsc_group_prefix": "proj_"}
GROUP = "bproj_my_project"


def http_error(code):
    return urllib.error.HTTPError("https://account.vscentrum.be", code, f"ERR{code}", None, None)


def make_user_context(users):
    """users: list of (username, role) tuples."""
    return {"team": [SimpleNamespace(username=username, role=role) for username, role in users]}


def make_waldur_resource(backend_id="res-01", storage_limit=100):
    wr = MagicMock()
    wr.project_slug = "my-project"
    wr.project_name = "My Project"
    wr.backend_id = backend_id
    wr.name = "storage-res"
    if storage_limit is None:
        wr.limits = None
    else:
        wr.limits = MagicMock()
        wr.limits.to_dict.return_value = {"storage": storage_limit}
    return wr


def make_backend(settings=None, client=None, vsc_client=None):
    with patch.object(
        bmod, "VscBackend", return_value=vsc_client if vsc_client is not None else MagicMock()
    ), patch.object(
        bmod, "SofiaStorageClient", return_value=client if client is not None else MagicMock()
    ):
        return SofiaStorageBackend(
            settings if settings is not None else SETTINGS, COMPONENTS
        )


class InitTest(unittest.TestCase):
    def test_defaults_without_vsc(self):
        backend = make_backend(settings={"vsc_token": None})
        self.assertIsNone(backend.vsc_client)
        self.assertEqual(backend.storage_fs, "gpfs")
        self.assertEqual(backend.storage_path, "/gpfs")
        self.assertEqual(backend.home_path, "/home")
        self.assertEqual(backend.unit_factor, float(GB))

    def test_vsc_client_created(self):
        vsc_client = MagicMock()
        backend = make_backend(vsc_client=vsc_client)
        self.assertIs(backend.vsc_client, vsc_client)


class GroupModeratorsTest(unittest.TestCase):
    def setUp(self):
        self.backend = make_backend()

    def test_project_moderators_used(self):
        context = make_user_context([("a", "PROJECT.ADMIN"), ("b", "MEMBER"), ("c", "PROJECT.MANAGER")])
        self.assertEqual(self.backend._get_group_moderators(context), ["a", "c"])

    def test_fallback_to_first_user(self):
        context = make_user_context([("a", "MEMBER"), ("b", "MEMBER")])
        self.assertEqual(self.backend._get_group_moderators(context), ["a"])

    def test_empty_team_raises(self):
        with self.assertRaises(BackendError) as ctx:
            self.backend._get_group_moderators(make_user_context([]))
        self.assertIn("neither users nor moderators", str(ctx.exception))


class CreateResourceTest(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.vsc = MagicMock()
        self.vsc.get_project_group_ids.return_value = (GROUP, 25000)
        self.vsc.get_vsc_ids.return_value = ("vsc1001", 1000)
        self.backend = make_backend(client=self.client, vsc_client=self.vsc)
        self.context = make_user_context([("mod1", "PROJECT.ADMIN"), ("u1", "MEMBER")])

    def test_missing_user_context_returns_none(self):
        self.assertIsNone(self.backend.create_resource_with_id(make_waldur_resource(), "res-01", None))

    def test_missing_storage_limit_raises(self):
        wr = make_waldur_resource(storage_limit=None)
        with self.assertRaises(BackendError) as ctx:
            self.backend.create_resource_with_id(wr, "res-01", self.context)
        self.assertIn("no positive 'storage' limit", str(ctx.exception))

    def test_zero_storage_limit_raises(self):
        with self.assertRaises(BackendError) as ctx:
            self.backend.create_resource_with_id(
                make_waldur_resource(storage_limit=0), "res-01", self.context
            )
        self.assertIn("no positive 'storage' limit", str(ctx.exception))

    def test_new_fileset_created(self):
        self.client.list_project_filesets.return_value = {}
        info = self.backend.create_resource_with_id(
            make_waldur_resource(storage_limit=100), "res-01", self.context
        )
        self.assertEqual(info.backend_id, "res-01")
        self.assertEqual(info.limits, {"storage": 100})
        self.vsc.update_project_group.assert_called_once()
        self.client.sudo_waldur_make_project_vsc.assert_called_once_with(
            project_dir="res-01", owner_uid=1000, owner_gid=25000
        )
        self.client.set_resource_limits.assert_called_once_with("res-01", {"storage": 100 * GB})
        # post-create home dirs for all team members
        created = {c.args[0] for c in self.client.sudo_waldur_make_homedir_vsc.call_args_list}
        self.assertEqual(created, {"mod1", "u1"})

    def test_existing_resource_reactivated(self):
        self.client.list_project_filesets.return_value = {"res-01": 10 * GB}
        info = self.backend.create_resource_with_id(
            make_waldur_resource(storage_limit=100), "res-01", self.context
        )
        self.assertEqual(info.backend_id, "res-01")
        self.client.sudo_waldur_make_project_vsc.assert_not_called()
        self.client.set_resource_limits.assert_called_once_with("res-01", {"storage": 100 * GB})

    def test_other_active_resource_raises(self):
        self.client.list_project_filesets.return_value = {"other-res": 10 * GB}
        with self.assertRaises(BackendError) as ctx:
            self.backend.create_resource_with_id(
                make_waldur_resource(), "res-01", self.context
            )
        self.assertIn("already has an active storage resource", str(ctx.exception))
        self.client.sudo_waldur_make_project_vsc.assert_not_called()

    def test_single_terminated_fileset_reactivated(self):
        self.client.list_project_filesets.return_value = {"old-res": 0}
        info = self.backend.create_resource_with_id(
            make_waldur_resource(), "res-01", self.context
        )
        self.assertEqual(info.backend_id, "old-res")
        self.client.sudo_waldur_make_project_vsc.assert_not_called()
        self.client.set_resource_limits.assert_called_once_with("old-res", {"storage": 100 * GB})

    def test_multiple_terminated_filesets_raise(self):
        self.client.list_project_filesets.return_value = {"old-1": 0, "old-2": 0}
        with self.assertRaises(BackendError) as ctx:
            self.backend.create_resource_with_id(
                make_waldur_resource(), "res-01", self.context
            )
        self.assertIn("multiple terminated storage resources", str(ctx.exception))

    def test_make_project_failure_raises(self):
        self.client.list_project_filesets.return_value = {}
        self.client.sudo_waldur_make_project_vsc.side_effect = RuntimeError("boom")
        with self.assertRaises(BackendError) as ctx:
            self.backend.create_resource_with_id(
                make_waldur_resource(), "res-01", self.context
            )
        self.assertIn("Failed to make storage directory for resource res-01", str(ctx.exception))


class DeleteResourceTest(unittest.TestCase):
    def test_zeroes_quota(self):
        client = MagicMock()
        backend = make_backend(client=client)
        backend.delete_resource(make_waldur_resource())
        client.set_resource_limits.assert_called_once_with(
            resource_id="res-01", limits_dict={"storage": 0}
        )

    def test_no_backend_id_is_noop(self):
        client = MagicMock()
        backend = make_backend(client=client)
        wr = make_waldur_resource(backend_id=None)
        backend.delete_resource(wr)
        client.set_resource_limits.assert_not_called()

    def test_failure_is_logged_not_raised(self):
        client = MagicMock()
        client.set_resource_limits.side_effect = RuntimeError("boom")
        backend = make_backend(client=client)
        backend.delete_resource(make_waldur_resource())


class AddRemoveUserTest(unittest.TestCase):
    def test_add_user_creates_homedir(self):
        vsc = MagicMock()
        vsc.add_user_to_project.return_value = True
        client = MagicMock()
        backend = make_backend(client=client, vsc_client=vsc)
        self.assertTrue(backend.add_user(make_waldur_resource(), "vsc1001"))
        client.sudo_waldur_make_homedir_vsc.assert_called_once_with("vsc1001")

    def test_add_user_vsc_failure_skips_homedir(self):
        vsc = MagicMock()
        vsc.add_user_to_project.side_effect = BackendError("vsc 500")
        client = MagicMock()
        backend = make_backend(client=client, vsc_client=vsc)
        with self.assertRaises(BackendError):
            backend.add_user(make_waldur_resource(), "vsc1001")
        client.sudo_waldur_make_homedir_vsc.assert_not_called()

    def test_add_user_homedir_failure_keeps_association(self):
        vsc = MagicMock()
        vsc.add_user_to_project.return_value = True
        client = MagicMock()
        client.sudo_waldur_make_homedir_vsc.side_effect = RuntimeError("uid unknown yet")
        backend = make_backend(client=client, vsc_client=vsc)
        self.assertTrue(backend.add_user(make_waldur_resource(), "vsc1001"))

    def test_remove_user_success(self):
        vsc = MagicMock()
        vsc.remove_user_to_project.return_value = True
        backend = make_backend(client=MagicMock(), vsc_client=vsc)
        self.assertTrue(backend.remove_user(make_waldur_resource(), "vsc1001"))
        vsc.remove_user_to_project.assert_called_once_with("my-project", "vsc1001")

    def test_add_remove_without_vsc_raise(self):
        backend = make_backend(settings={"vsc_token": None})
        for method in (backend.add_user, backend.remove_user):
            with self.assertRaises(BackendError) as ctx:
                method(make_waldur_resource(), "vsc1001")
            self.assertIn("without VSC account page integration", str(ctx.exception))


class ProcessExistingUsersTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="sofia-test-home-")
        self.client = MagicMock()
        self.backend = make_backend(
            settings=dict(SETTINGS, home_path=self.home), client=self.client
        )

    def test_only_missing_homedirs_created(self):
        os.makedirs(os.path.join(self.home, "u1"))
        self.backend.process_existing_users({"u1", "u2"})
        self.assertEqual(
            [c.args[0] for c in self.client.sudo_waldur_make_homedir_vsc.call_args_list], ["u2"]
        )

    def test_all_present_noop(self):
        os.makedirs(os.path.join(self.home, "u1"))
        self.backend.process_existing_users({"u1"})
        self.client.sudo_waldur_make_homedir_vsc.assert_not_called()

    def test_failure_is_logged_not_raised(self):
        self.client.sudo_waldur_make_homedir_vsc.side_effect = RuntimeError("boom")
        self.backend.process_existing_users({"u1"})


class SetResourceLimitsTest(unittest.TestCase):
    def test_zero_rejected(self):
        client = MagicMock()
        backend = make_backend(client=client)
        with self.assertRaises(BackendError) as ctx:
            backend.set_resource_limits("res-01", {"storage": 0})
        self.assertIn("Refusing to set a zero", str(ctx.exception))
        client.set_resource_limits.assert_not_called()

    def test_missing_rejected(self):
        client = MagicMock()
        backend = make_backend(client=client)
        with self.assertRaises(BackendError):
            backend.set_resource_limits("res-01", {})
        client.set_resource_limits.assert_not_called()

    def test_positive_applied_converted(self):
        client = MagicMock()
        backend = make_backend(client=client)
        self.assertIsNone(backend.set_resource_limits("res-01", {"storage": 50}))
        client.set_resource_limits.assert_called_once_with("res-01", {"storage": 50 * GB})


class CollectResourceLimitsTest(unittest.TestCase):
    def setUp(self):
        self.backend = make_backend()

    def test_converts_to_backend_units(self):
        backend_limits, waldur_limits = self.backend._collect_resource_limits(
            make_waldur_resource(storage_limit=100)
        )
        self.assertEqual(backend_limits, {"storage": 100 * GB})
        self.assertEqual(waldur_limits, {"storage": 100})

    def test_zero_rejected(self):
        with self.assertRaises(BackendError):
            self.backend._collect_resource_limits(make_waldur_resource(storage_limit=0))


class MetadataTest(unittest.TestCase):
    def test_reports_limit_in_offering_unit(self):
        client = MagicMock()
        client.get_fileset_quota.return_value = (0, 2 * GB)
        backend = make_backend(client=client)
        self.assertEqual(backend.get_resource_metadata("res-01"), {"storage_limit": 2.0})
        client.get_fileset_quota.assert_called_once_with(fileset_name="res-01", silent=True)

    def test_rounds_to_two_decimals(self):
        block_limit = int(1.234 * GB)
        client = MagicMock()
        client.get_fileset_quota.return_value = (0, block_limit)
        backend = make_backend(client=client)
        self.assertEqual(backend.get_resource_metadata("res-01"), {"storage_limit": 1.23})

    def test_failure_returns_empty(self):
        client = MagicMock()
        client.get_fileset_quota.side_effect = BackendError("no fileset")
        backend = make_backend(client=client)
        self.assertEqual(backend.get_resource_metadata("res-01"), {})


class UsageReportTest(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.backend = make_backend(client=self.client)

    def test_report_shape(self):
        self.client.collect_project_quotas.return_value = [
            ("fileset", 100 * GB, 200 * GB),
            ("u1", 5 * GB, 50 * GB),
        ]
        report = self.backend._get_usage_report(["res-01"])
        self.assertEqual(
            report,
            {
                "res-01": {
                    "TOTAL_ACCOUNT_USAGE": {"storage": 100.0},
                    "u1": {"storage": 5.0},
                }
            },
        )

    def test_batch_isolation(self):
        self.client.collect_project_quotas.side_effect = [
            BackendError("boom"),
            [("fileset", 10 * GB, 100 * GB)],
        ]
        report = self.backend._get_usage_report(["bad-res", "good-res"])
        self.assertEqual(set(report), {"good-res"})
        self.assertEqual(report["good-res"]["TOTAL_ACCOUNT_USAGE"], {"storage": 10.0})

    def test_single_failure_excludes_resource(self):
        self.client.collect_project_quotas.side_effect = BackendError("boom")
        self.assertEqual(self.backend._get_usage_report(["bad-res"]), {})


class VersionTest(unittest.TestCase):
    def test_version_is_non_empty(self):
        # derived from the installed distribution (pyproject.toml is the
        # single source of truth), or "0.0.0" without an installation
        from waldur_site_agent_sofia_storage import __version__

        self.assertIsInstance(__version__, str)
        self.assertTrue(__version__)


class MiscTest(unittest.TestCase):
    def test_ping_ok(self):
        client = MagicMock()
        self.assertTrue(make_backend(client=client).ping())

    def test_ping_failure(self):
        client = MagicMock()
        client.list_filesets.side_effect = RuntimeError("no gpfs")
        self.assertFalse(make_backend(client=client).ping())

    def test_ping_raise(self):
        client = MagicMock()
        client.list_filesets.side_effect = RuntimeError("no gpfs")
        with self.assertRaises(RuntimeError):
            make_backend(client=client).ping(raise_exception=True)

    def test_list_components(self):
        self.assertEqual(make_backend().list_components(), ["storage"])

    def test_noop_lifecycle(self):
        backend = make_backend()
        self.assertTrue(backend.downscale_resource("res-01"))
        self.assertTrue(backend.pause_resource("res-01"))
        self.assertTrue(backend.restore_resource("res-01"))


class CoreBatchMembershipTest(unittest.TestCase):
    """Core add_users_to_resource/remove_users_from_resource over the real
    VscBackend: one failing user must not abort the rest of the batch."""

    @staticmethod
    def install_per_user(apc, action, failing, failing_code=500):
        # MagicMock.__getitem__ returns the same child mock for every key, so
        # per-user behaviour is installed via the member mock's __getitem__.
        members = {}

        def get_item(mock_self, key):
            m = members.get(key)
            if m is None:
                m = MagicMock()
                if key == failing:
                    getattr(m, action).side_effect = http_error(failing_code)
                members[key] = m
            return m

        apc.group[GROUP].member.__getitem__ = get_item
        return members

    def make_backend_with_apc(self, apc):
        vb = vscmod.VscBackend.__new__(vscmod.VscBackend)
        vb.client = apc
        vb.group_prefix = "proj_"
        vb.autogroup = {"vsc_id": "ag"}
        return make_backend(vsc_client=vb)

    def make_apc(self):
        apc = MagicMock()
        apc.get_group.return_value = MagicMock(vsc_id=GROUP, vsc_id_number=25000)
        return apc

    def test_remove_batch_partial_failure(self):
        apc = self.make_apc()
        self.install_per_user(apc, "delete", "bad")
        backend = self.make_backend_with_apc(apc)
        removed = backend.remove_users_from_resource(make_waldur_resource(), {"bad", "good"})
        self.assertEqual(set(removed), {"good"})

    def test_add_batch_partial_failure(self):
        apc = self.make_apc()
        self.install_per_user(apc, "post", "bad")
        backend = self.make_backend_with_apc(apc)
        added = backend.add_users_to_resource(make_waldur_resource(), {"bad", "good"})
        self.assertEqual(added, {"good"})


if __name__ == "__main__":
    unittest.main()
