import os

from typing import Optional
from vsc.filesystem.gpfs import GpfsOperations

from waldur_site_agent.backend import logger
from waldur_site_agent.backend.clients import BaseClient
from waldur_site_agent.backend.exceptions import BackendError
from waldur_site_agent.backend.structures import ClientResource

from .vsc import vsc_project_group_name

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

    def list_resource_users(self, resource_backend_id: str, silent: bool = False) -> list[str]:
        """Get resource users from local group"""
        resource_group = vsc_project_group_name(resource_backend_id, self.vsc_group_prefix)
        command = ["sudo", "getent", "group", resource_group]
        if not silent:
            logger.info(f"Executing: {' '.join(command)}")
        output = self.execute_command(command, silent=silent)

        try:
            group_entry = output.splitlines()[0]
            _, _, _, group_users = group_entry.split(":")
        except ValueError:
            raise BackendError(f"Failed to retrive users of group: {resource_group}")

        return group_users.split(",")

    def create_fileset(self, fileset_name: str):
        """Create a new fileset resource for project."""
        fileset_path = os.path.join(self.storage_path, fileset_name)
        logger.info(f"Creating fileset {fileset_name} at: {fileset_path}")
        self.operator.make_fileset(fileset_path, fileset_name)

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

        Args:
            resource_id: Backend identifier for the resource.

        Returns:
            Nested dict mapping username to component limits.
            Example: ``{"user1": {"cpu": 30000, "mem": 30720}}``.
        """
        resource_user_limits = {}
        project_users = self.list_resource_users(resource_id, silent=True)
        for user in project_users:
            user_usage, user_limit = self.get_user_quota_in_fileset(resource_id, user, silent=True)
            user_entry = { user: {"storage": float(user_limit) / self.unit_factor}}
            resource_user_limits.update(user_entry)
        return resource_user_limits

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
        """Launch standalone waldur_get_project_quota script"""
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
    
    def sudo_waldur_make_project_vsc(self, project: str, block_limit: int, owner_uid: int, owner_gid: int) -> str:
        """Launch standalone waldur_make_project_vsc script"""
        waldur_make_project_vsc = os.path.join(WALDUR_SCRIPT_PREFIX, "waldur_make_project_vsc")
        command = ["sudo", waldur_make_project_vsc, project, str(block_limit), str(owner_uid), str(owner_gid)]
        logger.info(f"Executing: {' '.join(command)}")
        return self.execute_command(command)
