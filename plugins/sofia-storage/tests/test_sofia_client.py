"""Unit tests for SofiaStorageClient (GPFS operations layer)."""
import unittest
from types import SimpleNamespace
from unittest import mock
from unittest.mock import MagicMock

from waldur_site_agent.backend.exceptions import BackendError
from waldur_site_agent.backend.structures import ClientResource

from waldur_site_agent_sofia_storage.client import SofiaStorageClient

GB = 1073741824

MMLS_HEADER = (
    "mmlsquota:fileset:HEADER:version:reserved:reserved:filesystemName:quotaType:"
    "id:name:blockUsage:blockQuota:blockLimit:blockInDoubt:blockGrace:filesUsage:"
    "filesQuota:filesLimit:filesInDoubt:filesGrace:remarks:fid:filesetname:"
)


def fileset_row(usage_kb=123456, quota_kb=209715200, name="proj1"):
    return (
        f"mmlsquota:fileset:1:1:0:0:gpfs:2:1048576:{name}:{usage_kb}:{quota_kb}:220158240:"
        "0:0:1234:1048576:1048576:0:0::1048576:proj1:"
    )


def user_row(usage_kb=5000, quota_kb=104857600, name="vsc1001"):
    return (
        f"mmlsquota:user:1:1:0:0:gpfs:1:12345:{name}:{usage_kb}:{quota_kb}:110060480:"
        "0:0:42:1048576:1048576:0:0::1048576:proj1:"
    )


def make_client(operator=None, **overrides):
    client = SofiaStorageClient.__new__(SofiaStorageClient)
    client.operator = operator if operator is not None else MagicMock()
    client.filesystem = "gpfs"
    client.storage_path = "/gpfs"
    client.home_path = "/home"
    client.unit_factor = GB
    client.vsc_group_prefix = "proj_"
    for key, value in overrides.items():
        setattr(client, key, value)
    return client


class KbToBytesTest(unittest.TestCase):
    def test_string(self):
        self.assertEqual(make_client()._kb_to_bytes("1024"), 1048576)

    def test_float(self):
        self.assertEqual(make_client()._kb_to_bytes(1.5), 1536)

    def test_int(self):
        self.assertEqual(make_client()._kb_to_bytes(2048), 2097152)


class MmlsquotaParseTest(unittest.TestCase):
    def setUp(self):
        self.client = make_client()

    def test_fileset_entry(self):
        result = self.client._parse_mmlsquota_entry(
            MMLS_HEADER + "\n" + fileset_row(), "fileset"
        )
        self.assertEqual(result, (123456 * 1024, 209715200 * 1024))

    def test_user_entry(self):
        result = self.client._parse_mmlsquota_entry(MMLS_HEADER + "\n" + user_row(), "user")
        self.assertEqual(result, (5000 * 1024, 104857600 * 1024))

    def test_header_only_returns_zero(self):
        self.assertEqual(self.client._parse_mmlsquota_entry(MMLS_HEADER, "fileset"), (0, 0))

    def test_warning_lines_are_skipped(self):
        output = MMLS_HEADER + "\n" + "mmlsquota: warning: something" + "\n" + fileset_row()
        result = self.client._parse_mmlsquota_entry(output, "fileset")
        self.assertEqual(result, (123456 * 1024, 209715200 * 1024))

    def test_wrong_entry_type_raises(self):
        with self.assertRaises(BackendError) as ctx:
            self.client._parse_mmlsquota_entry(MMLS_HEADER + "\n" + user_row(), "fileset")
        self.assertIn("no fileset entry", str(ctx.exception))

    def test_short_entry_raises(self):
        short = "mmlsquota:fileset:1:1:0:0:gpfs:2:1048576:proj1:123456"
        with self.assertRaises(BackendError) as ctx:
            self.client._parse_mmlsquota_entry(MMLS_HEADER + "\n" + short, "fileset")
        self.assertIn("at least 12", str(ctx.exception))

    def test_non_numeric_entry_raises(self):
        bad = fileset_row().replace("123456:209715200", "abc:def", 1)
        with self.assertRaises(BackendError) as ctx:
            self.client._parse_mmlsquota_entry(MMLS_HEADER + "\n" + bad, "fileset")
        self.assertIn("Non-numeric", str(ctx.exception))


class FilesetQuotaCommandTest(unittest.TestCase):
    def test_get_fileset_quota(self):
        client = make_client()
        client.execute_command = MagicMock(return_value=MMLS_HEADER + "\n" + fileset_row())
        result = client.get_fileset_quota("proj1")
        self.assertEqual(result, (123456 * 1024, 209715200 * 1024))
        self.assertEqual(
            client.execute_command.call_args.args[0],
            ["sudo", "/usr/lpp/mmfs/bin/mmlsquota", "-Y", "-j", "proj1", "gpfs"],
        )

    def test_get_user_quota_in_fileset(self):
        client = make_client()
        client.execute_command = MagicMock(return_value=MMLS_HEADER + "\n" + user_row())
        result = client.get_user_quota_in_fileset("proj1", "vsc1001")
        self.assertEqual(result, (5000 * 1024, 104857600 * 1024))
        self.assertEqual(
            client.execute_command.call_args.args[0],
            ["sudo", "/usr/lpp/mmfs/bin/mmlsquota", "-Y", "-u", "vsc1001", "gpfs:proj1"],
        )


class ResourceLimitsTest(unittest.TestCase):
    def test_get_resource_limits_converts_to_offering_unit(self):
        client = make_client()
        # 2 GiB expressed in KB (the unit mmlsquota reports)
        client.execute_command = MagicMock(
            return_value=MMLS_HEADER + "\n" + fileset_row(quota_kb=2 * GB // 1024)
        )
        self.assertEqual(client.get_resource_limits("res-01"), {"storage": 2.0})

    def test_set_resource_limits_runs_quota_script(self):
        client = make_client()
        client.execute_command = MagicMock(return_value="")
        result = client.set_resource_limits("res-01", {"storage": 10 * GB})
        self.assertEqual(result, "res-01")
        self.assertEqual(
            client.execute_command.call_args.args[0],
            ["sudo", "/usr/local/bin/waldur_set_project_quota", "res-01", str(10 * GB)],
        )

    def test_set_resource_limits_missing_storage(self):
        with self.assertRaises(BackendError) as ctx:
            make_client().set_resource_limits("res-01", {})
        self.assertIn("storage", str(ctx.exception))

    def test_set_resource_limits_script_failure(self):
        client = make_client()
        client.sudo_waldur_set_project_quota = mock.MagicMock(side_effect=RuntimeError("boom"))
        with self.assertRaises(BackendError) as ctx:
            client.set_resource_limits("res-01", {"storage": 10 * GB})
        self.assertIn("Failed to set quota for resource res-01", str(ctx.exception))


class UserLimitsTest(unittest.TestCase):
    def test_get_resource_user_limits_reports_only_real_quotas(self):
        client = make_client()
        client.list_resource_users = MagicMock(return_value=["u1", "u2"])
        client.get_user_quota_in_fileset = MagicMock(
            side_effect=lambda rs, user, silent=False: (0, 50 * GB if user == "u1" else 0)
        )
        self.assertEqual(
            client.get_resource_user_limits("res-01"), {"u1": {"storage": 50.0}}
        )

    def test_set_resource_user_limits_converts_to_bytes(self):
        client = make_client()
        result = client.set_resource_user_limits("res-01", "u1", {"storage": 50})
        self.assertEqual(client.operator.set_user_quota.call_args.args[:2], (50 * GB, "u1"))
        self.assertEqual(client.operator.set_user_quota.call_args.kwargs["obj"], "/gpfs/res-01")
        self.assertIn(str(50 * GB), result)

    def test_set_resource_user_limits_empty_clears_quota(self):
        client = make_client()
        client.set_resource_user_limits("res-01", "u1", {})
        self.assertEqual(client.operator.set_user_quota.call_args.args[:2], (0, "u1"))

    def test_set_resource_user_limits_missing_key_clears_quota(self):
        client = make_client()
        client.set_resource_user_limits("res-01", "u1", {"other": 1})
        self.assertEqual(client.operator.set_user_quota.call_args.args[:2], (0, "u1"))


class CollectProjectQuotasTest(unittest.TestCase):
    def test_returns_fileset_and_user_entries(self):
        client = make_client()
        client.get_fileset_quota = MagicMock(return_value=(10, 100))
        client.list_resource_users = MagicMock(return_value=["u1", "u2"])
        client.get_user_quota_in_fileset = MagicMock(
            side_effect=lambda rs, user, silent=False: (5, 50)
        )
        self.assertEqual(
            client.collect_project_quotas("res-01"),
            [("fileset", 10, 100), ("u1", 5, 50), ("u2", 5, 50)],
        )


class ListProjectFilesetsTest(unittest.TestCase):
    def test_filters_by_project_gid(self):
        client = make_client()
        client.get_fileset_quota = MagicMock(
            side_effect=lambda name, silent=False: (0, 10 * GB if name == "proj-a" else 0)
        )
        gids = {"/gpfs/proj-a": 25000, "/gpfs/proj-b": 999}
        with mock.patch("os.listdir", return_value=["proj-a", "proj-b", "notes.txt"]), \
             mock.patch("os.path.isdir", side_effect=lambda p: p != "/gpfs/notes.txt"), \
             mock.patch("os.stat", side_effect=lambda p: SimpleNamespace(st_gid=gids[p])):
            result = client.list_project_filesets(25000)
        self.assertEqual(result, {"proj-a": 10 * GB})
        client.get_fileset_quota.assert_called_once_with("proj-a", silent=False)

    def test_unreadable_entries_are_skipped(self):
        client = make_client()
        client.get_fileset_quota = MagicMock(return_value=(0, 0))
        with mock.patch("os.listdir", return_value=["proj-a"]), \
             mock.patch("os.path.isdir", return_value=True), \
             mock.patch("os.stat", side_effect=OSError("gone")):
            self.assertEqual(client.list_project_filesets(25000), {})


class ListResourceUsersTest(unittest.TestCase):
    def test_lists_group_members(self):
        client = make_client()
        client.execute_command = MagicMock(return_value="bproj_x:x:25000:u1,u2\n")
        with mock.patch("os.stat", return_value=SimpleNamespace(st_gid=25000)), \
             mock.patch("grp.getgrgid", return_value=SimpleNamespace(gr_name="bproj_x")):
            self.assertEqual(client.list_resource_users("res-01"), ["u1", "u2"])

    def test_missing_resource_raises(self):
        client = make_client()
        with mock.patch("os.stat", side_effect=FileNotFoundError("nope")):
            with self.assertRaises(BackendError) as ctx:
                client.list_resource_users("res-01")
        self.assertIn("not found on local storage", str(ctx.exception))

    def test_inactive_group_returns_empty(self):
        client = make_client()
        with mock.patch("os.stat", return_value=SimpleNamespace(st_gid=25000)), \
             mock.patch("grp.getgrgid", side_effect=KeyError("no group yet")):
            self.assertEqual(client.list_resource_users("res-01"), [])


class ResourceLookupTest(unittest.TestCase):
    def test_found(self):
        client = make_client()
        with mock.patch("os.listdir", return_value=["res-01", "res-02"]):
            result = client.get_resource("res-01")
        self.assertIsInstance(result, ClientResource)
        self.assertEqual(result.name, "res-01")

    def test_not_found(self):
        client = make_client()
        with mock.patch("os.listdir", return_value=["res-02"]):
            self.assertIsNone(client.get_resource("res-01"))


class OperatorDelegationTest(unittest.TestCase):
    def test_create_fileset(self):
        client = make_client()
        client.create_fileset("res-01")
        client.operator.make_fileset.assert_called_once_with("/gpfs/res-01", "res-01")

    def test_set_fileset_quota(self):
        client = make_client()
        client.set_fileset_quota("res-01", 10 * GB)
        client.operator.set_fileset_quota.assert_called_once_with(
            10 * GB, "/gpfs/res-01", "res-01", inode_soft=1 * 1024**2
        )

    def test_set_project_owner(self):
        client = make_client()
        client.set_project_owner("res-01", 1000, 25000)
        client.operator.chmod.assert_called_once_with(0o770, "/gpfs/res-01")
        client.operator.chown.assert_called_once_with(1000, 25000, "/gpfs/res-01")

    def test_make_home_dir_populates(self):
        client = make_client()
        client.operator.create_stat_directory.return_value = True
        self.assertTrue(client.make_home_dir("vsc1001", 1000))
        client.operator.create_stat_directory.assert_called_once_with("/home/vsc1001", 0o700, 1000, 1000)
        client.operator.populate_home_dir.assert_called_once_with(1000, 1000, "/home/vsc1001", [])

    def test_make_home_dir_no_populate_when_operator_says_no(self):
        client = make_client()
        client.operator.create_stat_directory.return_value = False
        self.assertTrue(client.make_home_dir("vsc1001", 1000))
        client.operator.populate_home_dir.assert_not_called()


class WaldurScriptCommandsTest(unittest.TestCase):
    def test_make_homedir_existing_short_circuits(self):
        client = make_client()
        client.execute_command = MagicMock()
        with mock.patch("os.path.isdir", return_value=True):
            result = client.sudo_waldur_make_homedir_vsc("vsc1001")
        self.assertEqual(result, "Home directory of vsc1001 already exists")
        client.execute_command.assert_not_called()

    def test_make_homedir_command(self):
        client = make_client()
        client.execute_command = MagicMock(return_value="")
        with mock.patch("os.path.isdir", return_value=False):
            client.sudo_waldur_make_homedir_vsc("vsc1001")
        self.assertEqual(
            client.execute_command.call_args.args[0],
            ["sudo", "/usr/local/bin/waldur_make_homedir_vsc", "vsc1001"],
        )

    def test_make_project_command(self):
        client = make_client()
        client.execute_command = MagicMock(return_value="")
        client.sudo_waldur_make_project_vsc("proj-x", 1000, 2000)
        self.assertEqual(
            client.execute_command.call_args.args[0],
            ["sudo", "/usr/local/bin/waldur_make_project_vsc", "proj-x", "1000", "2000"],
        )

    def test_set_project_quota_command(self):
        client = make_client()
        client.execute_command = MagicMock(return_value="")
        client.sudo_waldur_set_project_quota("proj-x", 10 * GB)
        self.assertEqual(
            client.execute_command.call_args.args[0],
            ["sudo", "/usr/local/bin/waldur_set_project_quota", "proj-x", str(10 * GB)],
        )


if __name__ == "__main__":
    unittest.main()
