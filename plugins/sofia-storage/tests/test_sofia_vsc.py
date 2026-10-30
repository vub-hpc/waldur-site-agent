"""Unit tests for VscBackend (VSC account page integration)."""
import unittest
import urllib.error
from unittest.mock import MagicMock

from waldur_site_agent.backend.exceptions import BackendError

from waldur_site_agent_sofia_storage import vsc as vscmod

GROUP = "bproj_my_project"


def http_error(code):
    return urllib.error.HTTPError("https://account.vscentrum.be", code, f"ERR{code}", None, None)


def make_vb(apc=None, group_prefix="proj_"):
    vb = vscmod.VscBackend.__new__(vscmod.VscBackend)
    vb.client = apc if apc is not None else MagicMock()
    vb.group_prefix = group_prefix
    vb.autogroup = {"vsc_id": "ag"}
    return vb


class ProjectGroupNameTest(unittest.TestCase):
    def test_prefix_without_b_gets_b(self):
        self.assertEqual(make_vb().project_group_name("my-project"), "bproj_my_project")

    def test_prefix_with_b_is_kept(self):
        self.assertEqual(make_vb(group_prefix="bproj_").project_group_name("my-project"), "bproj_my_project")

    def test_empty_prefix(self):
        self.assertEqual(make_vb(group_prefix="").project_group_name("my-project"), "my_project")

    def test_dash_becomes_underscore(self):
        self.assertEqual(make_vb().project_group_name("a-b-c"), "bproj_a_b_c")


class IdLookupTest(unittest.TestCase):
    def test_get_vsc_ids(self):
        apc = MagicMock()
        apc.get_account.return_value = MagicMock(vsc_id="vsc1001", vsc_id_number=1000)
        self.assertEqual(make_vb(apc).get_vsc_ids("vsc1001"), ("vsc1001", 1000))

    def test_get_project_group_ids(self):
        apc = MagicMock()
        apc.get_group.return_value = MagicMock(vsc_id=GROUP, vsc_id_number=25000)
        self.assertEqual(make_vb(apc).get_project_group_ids("my-project"), (GROUP, 25000))

    def test_get_project_group_ids_http_error(self):
        apc = MagicMock()
        apc.get_group.side_effect = http_error(500)
        with self.assertRaises(BackendError) as ctx:
            make_vb(apc).get_project_group_ids("my-project")
        self.assertIn("Failed to retrieve information of VSC group", str(ctx.exception))
        self.assertIn("ERR500 (500)", str(ctx.exception))


class GroupMembersTest(unittest.TestCase):
    def test_get_project_group_members(self):
        apc = MagicMock()
        apc.group[GROUP].get.return_value = (200, {"members": ["u1", "u2"]})
        self.assertEqual(make_vb(apc).get_project_group_members("my-project"), ["u1", "u2"])


class AutogroupTest(unittest.TestCase):
    def test_get_autogroup(self):
        apc = MagicMock()
        apc.autogroup["ag"].get.return_value = (200, {"vsc_id": "ag"})
        self.assertEqual(make_vb(apc).get_autogroup("ag"), {"vsc_id": "ag"})

    def test_get_autogroup_not_found(self):
        apc = MagicMock()
        apc.autogroup["ag"].get.side_effect = http_error(404)
        with self.assertRaises(BackendError) as ctx:
            make_vb(apc).get_autogroup("ag")
        self.assertIn("VSC autogroup ag not found", str(ctx.exception))

    def test_update_source_success(self):
        self.assertTrue(make_vb().update_autogroup_source(GROUP))

    def test_update_source_404_mentions_missing_group(self):
        vb = make_vb()
        vb.client.autogroup["ag"].source[GROUP].add.post.side_effect = http_error(404)
        with self.assertRaises(BackendError) as ctx:
            vb.update_autogroup_source(GROUP)
        self.assertIn("group does not exist (404)", str(ctx.exception))

    def test_update_source_500_raises(self):
        vb = make_vb()
        vb.client.autogroup["ag"].source[GROUP].add.post.side_effect = http_error(500)
        with self.assertRaises(BackendError) as ctx:
            vb.update_autogroup_source(GROUP)
        self.assertIn("ERR500 (500)", str(ctx.exception))

    def test_update_source_403_raises(self):
        vb = make_vb()
        vb.client.autogroup["ag"].source[GROUP].add.post.side_effect = http_error(403)
        with self.assertRaises(BackendError) as ctx:
            vb.update_autogroup_source(GROUP)
        self.assertIn("ERR403 (403)", str(ctx.exception))


class UpdateProjectGroupTest(unittest.TestCase):
    def test_creates_new_group(self):
        apc = MagicMock()
        apc.get_group.side_effect = [http_error(404), MagicMock(vsc_id=GROUP, vsc_id_number=25000)]
        result = make_vb(apc).update_project_group(
            "my-project", "My Project", members=["u1"], moderators=["mod1"]
        )
        self.assertEqual(result, (GROUP, 25000))
        apc.create_or_update_group.assert_called_once_with(
            groupname=GROUP,
            moderators=["mod1"],
            members=["u1"],
            info="Group for sofia project: My Project",
            dry_run=False,
        )
        apc.autogroup["ag"].source[GROUP].add.post.assert_called_once()

    def test_updates_existing_group(self):
        apc = MagicMock()
        apc.get_group.return_value = MagicMock(vsc_id=GROUP, vsc_id_number=25000)
        result = make_vb(apc).update_project_group(
            "my-project", "My Project", members=["u1", "u2"], moderators=["mod1"]
        )
        self.assertEqual(result, (GROUP, 25000))
        apc.create_or_update_group.assert_called_once_with(
            groupname=GROUP,
            moderators=["mod1"],
            members=["u1", "u2"],
            info="Group for sofia project: My Project",
            dry_run=False,
        )

    def test_no_members_and_no_moderators(self):
        with self.assertRaises(BackendError) as ctx:
            make_vb().update_project_group("my-project", "My Project", members=[], moderators=[])
        self.assertIn("without any members or moderators", str(ctx.exception))

    def test_new_group_without_moderators(self):
        apc = MagicMock()
        apc.get_group.side_effect = http_error(404)
        with self.assertRaises(BackendError) as ctx:
            make_vb(apc).update_project_group(
                "my-project", "My Project", members=["u1"], moderators=[]
            )
        self.assertIn("without moderators", str(ctx.exception))

    def test_group_lookup_error(self):
        apc = MagicMock()
        apc.get_group.side_effect = http_error(500)
        with self.assertRaises(BackendError) as ctx:
            make_vb(apc).update_project_group(
                "my-project", "My Project", members=["u1"], moderators=["mod1"]
            )
        self.assertIn("Failed to retrieve information of VSC group", str(ctx.exception))

    def test_create_failure(self):
        apc = MagicMock()
        apc.get_group.side_effect = http_error(404)
        apc.create_or_update_group.side_effect = http_error(500)
        with self.assertRaises(BackendError) as ctx:
            make_vb(apc).update_project_group(
                "my-project", "My Project", members=["u1"], moderators=["mod1"]
            )
        self.assertIn("Failed to create VSC group", str(ctx.exception))


class MembershipTest(unittest.TestCase):
    def setUp(self):
        self.apc = MagicMock()
        self.apc.get_group.return_value = MagicMock(vsc_id=GROUP, vsc_id_number=25000)
        self.vb = make_vb(self.apc)

    def test_add_user_success(self):
        self.assertTrue(self.vb.add_user_to_project("my-project", "u1"))
        self.apc.group[GROUP].member["u1"].post.assert_called_once_with(body={"vsc_id": "u1"})

    def test_add_user_http_error(self):
        self.apc.group[GROUP].member["u1"].post.side_effect = http_error(500)
        with self.assertRaises(BackendError) as ctx:
            self.vb.add_user_to_project("my-project", "u1")
        self.assertIn("Failed to add u1 to VSC group", str(ctx.exception))
        self.assertIn("ERR500 (500)", str(ctx.exception))

    def test_add_user_missing_group(self):
        self.apc.get_group.side_effect = http_error(404)
        with self.assertRaises(BackendError) as ctx:
            self.vb.add_user_to_project("my-project", "u1")
        self.assertIn("Failed to retrieve information of VSC group", str(ctx.exception))

    def test_remove_user_success(self):
        self.assertTrue(self.vb.remove_user_to_project("my-project", "u1"))
        self.apc.group[GROUP].member["u1"].delete.assert_called_once()

    def test_remove_user_already_absent(self):
        self.apc.group[GROUP].member["u1"].delete.side_effect = http_error(404)
        # a confirmed absence must be reported as True so the core releases
        # the user's account
        self.assertTrue(self.vb.remove_user_to_project("my-project", "u1"))

    def test_remove_user_http_error(self):
        self.apc.group[GROUP].member["u1"].delete.side_effect = http_error(500)
        with self.assertRaises(BackendError) as ctx:
            self.vb.remove_user_to_project("my-project", "u1")
        self.assertIn("Failed to remove u1 from VSC group", str(ctx.exception))
        self.assertIn("ERR500 (500)", str(ctx.exception))

    def test_remove_user_missing_group(self):
        self.apc.get_group.side_effect = http_error(404)
        with self.assertRaises(BackendError) as ctx:
            self.vb.remove_user_to_project("my-project", "u1")
        self.assertIn("Failed to retrieve information of VSC group", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
