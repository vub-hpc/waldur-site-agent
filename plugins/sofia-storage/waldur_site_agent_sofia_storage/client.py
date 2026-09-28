import grp
import os
from typing import Optional

from vsc.filesystem.gpfs import GpfsOperations

from waldur_site_agent.backend import logger
from waldur_site_agent.backend.clients import BaseClient
from waldur_site_agent.backend.exceptions import BackendError
from waldur_site_agent.backend.structures import ClientResource

GPFS_BIN_PATH = "/usr/lpp/mmfs/bin"
DEFAULT_INODE_LIMIT = 1 * 1024**2 # 1M

PROJ_DIR_PERMISSIONS = 0o770
HOME_DIR_PERMISSIONS = 0o700

WALDUR_SCRIPT_PREFIX = "/usr/local/bin/"

class SofiaStorageClient(BaseClient):
    """Client for interaction with GPFS storage via VSC filesystem interface."""

    def __init__(self,
            filesystem: str,
            storage_path: str,
            home_path: str,
            unit_factor: Optional[int] = 1,
            vsc_group_prefix: Optional[str] = ""
        ):
        """
        Initialize VSC GPFS backend.
        """
        # Launch GPFS operator
        self.operator = GpfsOperations()

        self.filesystem = filesystem
        self.storage_path = storage_path
        self.home_path = home_path
        self.unit_factor = int(unit_factor)
        self.vsc_group_prefix = vsc_group_prefix

    def get_resource(self, resource_id: str) -> ClientResource | None:
        """Returns Account object from cluster based on the account name."""
        filesets = os.listdir(self.storage_path)

        if resource_id in filesets:
            return ClientResource(name=resource_id)

        return None

    def list_filesets(self) -> dict | None:
        """List filesets"""
        return self.operator.list_filesets(devices=self.filesystem)

    def list_resource_users(
        self,
        resource_backend_id: str,
        silent: bool = False
    ) -> list[str]:
        """Get resource users from local group (avoid VSC AP query)"""
        fileset_path = os.path.join(self.storage_path, resource_backend_id)
        try:
            project_gid = os.stat(fileset_path).st_gid
            project_group = grp.getgrgid(project_gid).gr_name
        except FileNotFoundError:
            raise BackendError(f"Storage resource {resource_backend_id} not found on local storage")
        except KeyError:
            # group still not active
            return []

        command = ["sudo", "getent", "group", project_group]
        if not silent:
            logger.info(f"Executing: {' '.join(command)}")
        output = self.execute_command(command, silent=silent)

        try:
            group_entry = output.splitlines()[0]
            _, _, _, group_users = group_entry.split(":")
        except ValueError:
            raise BackendError(f"Failed to retrive users of group: {project_group}")

        return group_users.split(",")

    def create_fileset(self, fileset_name: str):
        """Create a new fileset resource for project."""
        fileset_path = os.path.join(self.storage_path, fileset_name)
        logger.info(f"Creating fileset {fileset_name} at: {fileset_path}")
        self.operator.make_fileset(fileset_path, fileset_name)

    def list_project_filesets(self, project_gid: int, silent: bool = False) -> dict[str, int]:
        """Return the filesets of the project owning the given GID.

        Filesets are identified by group ownership: on creation each fileset
        is chowned to the project's VSC group (see set_project_owner).

        Returns:
            Mapping of fileset name to its block quota in bytes. A quota of
            0 marks a terminated resource, since termination zeroes (but
            never removes) the fileset quota.
        """
        filesets: dict[str, int] = {}
        for name in os.listdir(self.storage_path):
            path = os.path.join(self.storage_path, name)
            if not os.path.isdir(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_gid != project_gid:
                continue
            _, block_limit = self.get_fileset_quota(name, silent=silent)
            filesets[name] = block_limit
        return filesets

    def _kb_to_bytes(self, kb_units: str | float | int) -> int:
        """Convert KB to bytes"""
        return int(float(kb_units) * 1024)

    def get_resource_limits(self, resource_id: str) -> dict[str, int]:
        """Get current resource limits from the backend.

        Args:
            resource_id: Backend identifier for the resource.

        Returns:
            Component-to-value mapping in backend-native units.
            Example: ``{"cpu": 60000, "mem": 61440}``.
        """
        _, block_limit = self.get_fileset_quota(fileset_name=resource_id)
        return {"storage": float(block_limit) / self.unit_factor}

    def set_resource_limits(self, resource_id: str, limits_dict: dict[str, int]) -> str | None:
        """
        Sets the limits for the fileset with the specified name.
        Runs through external script to elevate permissions with sudo.

        Args:
            resource_id: Backend identifier for the resource.
            limits_dict: {'storage': value}

        Returns:
            resource_id
        """
        try:
            block_limit = int(limits_dict['storage'])
        except KeyError:
            raise BackendError(
                f"Failed to set limits of {resource_id}.Order does not contain limits for any 'storage' component."
            )

        try:
            self.sudo_waldur_set_project_quota(
                project_dir=resource_id,
                block_limit=block_limit,
            )
        except Exception as err:
            raise BackendError(f"Failed to set quota for resource {resource_id}: {err}")

        return resource_id

    def get_fileset_quota(self, fileset_name: str, silent: bool = False) -> tuple:
        """Get quota and usage for a specific fileset."""
        command = ["sudo", "/usr/lpp/mmfs/bin/mmlsquota", "-Y", "-j", fileset_name, self.filesystem]
        if not silent:
            logger.info(f"Executing: {' '.join(command)}")
        output = self.execute_command(command, silent=silent)

        try:
            fs_quota_entry = output.splitlines()[1]
        except IndexError:
            # fileset has no quota/usage
            return (0, 0)

        # mmlsquota:fileset:HEADER:version:reserved:reserved:filesystemName:quotaType:id:name:
        #  blockUsage:blockQuota:blockLimit:blockInDoubt:blockGrace:
        #  filesUsage:filesQuota:filesLimit:filesInDoubt:filesGrace:
        #  remarks:fid:filesetname:
        fs_quota = fs_quota_entry.split(":")
        # convert from KB to bytes
        block_usage = self._kb_to_bytes(fs_quota[10])
        block_limit = self._kb_to_bytes(fs_quota[11])

        return block_usage, block_limit

    def get_resource_user_limits(self, resource_id: str) -> dict[str, dict[str, int]]:
        """Get per-user limits for a resource.

        Only users with an actual per-user quota (block limit > 0) are
        reported: a user without one is bounded by the fileset quota only,
        and reporting the fileset limit would make the core believe a
        per-user limit is set (and try to unset it on every sync).

        Args:
            resource_id: Backend identifier for the resource.

        Returns:
            Nested dict mapping username to component limits.
            Example: ``{"user1": {"storage": 50.0}}``.
        """
        resource_user_limits = {}
        project_users = self.list_resource_users(resource_id, silent=True)
        for user in project_users:
            _, user_limit = self.get_user_quota_in_fileset(resource_id, user, silent=True)
            if user_limit > 0:
                resource_user_limits[user] = {"storage": float(user_limit) / self.unit_factor}
        return resource_user_limits

    def set_resource_user_limits(self, resource_id: str, username: str, limits_dict: dict[str, int]) -> str:
        """Set the per-user storage quota of a user in a fileset.

        The per-user quota is applied in addition to the fileset quota,
        which always bounds the user as well.

        Args:
            resource_id: Backend identifier for the resource (fileset name).
            username: The user the quota applies to.
            limits_dict: Component-to-value mapping in Waldur units (the
                base backend passes the core values through unconverted).
                An empty mapping (or a missing 'storage' entry) clears the
                per-user quota (0 = unlimited within the fileset).

        Returns:
            A confirmation string with the applied quota in bytes.
        """
        fileset_path = os.path.join(self.storage_path, resource_id)
        storage_limit = limits_dict.get("storage")
        if storage_limit is None:
            block_limit = 0
        else:
            block_limit = int(float(storage_limit) * self.unit_factor)
        self.operator.set_user_quota(block_limit, username, obj=fileset_path)
        return f"Per-user storage quota of {username} in fileset {resource_id} set to {block_limit} bytes"

    def get_user_quota_in_fileset(self, fileset_name: str, username: str, silent: bool = False) -> tuple:
        """
        Get quota and usage of a user in a specific fileset.

        Directly execute 'mmlsquota' because vsc.filesystems.gpfs does not
        provide any interface with this functionality.
        """
        device = f"{self.filesystem}:{fileset_name}"
        command = ["sudo", "/usr/lpp/mmfs/bin/mmlsquota", "-Y", "-u", username, device]
        if not silent:
            logger.info(f"Executing: {' '.join(command)}")
        output = self.execute_command(command, silent=silent)

        try:
            user_quota_entry = output.splitlines()[1]
        except IndexError:
            # user has no quota/usage in this fileset
            return (0, 0)

        # mmlsquota:user:HEADER:version:reserved:reserved:filesystemName:quotaType:id:name:
        #  blockUsage:blockQuota:blockLimit:blockInDoubt:blockGrace:
        #  filesUsage:filesQuota:filesLimit:filesInDoubt:filesGrace:
        #  remarks:fid:filesetname:
        user_quota = user_quota_entry.split(":")
        # convert from KB to bytes
        block_usage = self._kb_to_bytes(user_quota[10])
        block_limit = self._kb_to_bytes(user_quota[11])

        return block_usage, block_limit

    def collect_project_quotas(self, project: str) -> list:
        """Return fileset and user quotas"""
        project_quotas = []
        # fileset quota
        block_usage, block_limit = self.get_fileset_quota(project, silent=True)
        fs_quota_entry = ("fileset", block_usage, block_limit)
        project_quotas.append(fs_quota_entry)
        # user quota
        project_users = self.list_resource_users(project, silent=True)
        for user in project_users:
            user_usage, user_limit = self.get_user_quota_in_fileset(project, user, silent=True)
            user_quota_entry = (user, user_usage, user_limit)
            project_quotas.append(user_quota_entry)

        return project_quotas

    def set_fileset_quota(self, fileset_name: str, block_limit: int, inode_limit: Optional[int] = DEFAULT_INODE_LIMIT):
        """Set quota for a specific fileset.

        Args:
            fileset_name: Fileset name (or project name)
            block_limit: Block soft limit (in units defined by GPFS, usually KB or MB)
            inode_limit: Inode soft limit
        """
        fileset_path = os.path.join(self.storage_path, fileset_name)
        self.operator.set_fileset_quota(block_limit, fileset_path, fileset_name, inode_soft=inode_limit)

    def set_project_owner(self, fileset_name: str, owner_uid: int, owner_gid: int):
        """Set ownership of fileset to VSC group"""
        fileset_path = os.path.join(self.storage_path, fileset_name)
        self.operator.chmod(PROJ_DIR_PERMISSIONS, fileset_path)
        self.operator.chown(owner_uid, owner_gid, fileset_path)

    def make_home_dir(self, username: str, uid: int) -> bool:
        """Create home directories for given user"""
        homedir_path = os.path.join(self.home_path, username)
        new_home_dir = self.operator.create_stat_directory(
            homedir_path,
            HOME_DIR_PERMISSIONS,
            uid,
            uid,
        )
        if new_home_dir is not False:
            self.operator.populate_home_dir(
                uid,
                uid,
                homedir_path,
                [],
            )
        return True

    def sudo_waldur_make_homedir_vsc(self, username: str) -> str:
        """Launch standalone waldur_make_homedir_vsc script"""
        homedir_path = os.path.join(self.home_path, username)
        if os.path.isdir(homedir_path):
            return f"Home directory of {username} already exists"

        waldur_make_homedir_vsc = os.path.join(WALDUR_SCRIPT_PREFIX, "waldur_make_homedir_vsc")
        command = ["sudo", waldur_make_homedir_vsc, username]
        logger.info(f"Executing: {' '.join(command)}")
        return self.execute_command(command)

    def sudo_waldur_make_project_vsc(self, project_dir: str, owner_uid: int, owner_gid: int) -> str:
        """Launch standalone waldur_make_project_vsc script"""
        waldur_make_project_vsc = os.path.join(WALDUR_SCRIPT_PREFIX, "waldur_make_project_vsc")
        command = ["sudo", waldur_make_project_vsc, project_dir, str(owner_uid), str(owner_gid)]
        logger.info(f"Executing: {' '.join(command)}")
        return self.execute_command(command)

    def sudo_waldur_set_project_quota(self, project_dir: str, block_limit: int) -> str:
        """Launch standalone waldur_set_project_quota script"""
        waldur_set_project_quota = os.path.join(WALDUR_SCRIPT_PREFIX, "waldur_set_project_quota")
        command = ["sudo", waldur_set_project_quota, project_dir, str(block_limit)]
        logger.info(f"Executing: {' '.join(command)}")
        return self.execute_command(command)
