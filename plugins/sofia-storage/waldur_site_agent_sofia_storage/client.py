import os

from typing import Optional
from vsc.filesystem.gpfs import GpfsOperations

from waldur_site_agent.backend import logger
from waldur_site_agent.backend.clients import BaseClient
from waldur_site_agent.backend.exceptions import BackendError

GPFS_BIN_PATH = "/usr/lpp/mmfs/bin"
DEFAULT_INODE_LIMIT = 1 * 1024**2 # 1M

PROJ_DIR_PERMISSIONS = 0o770
HOME_DIR_PERMISSIONS = 0o700

WALDUR_SCRIPT_PREFIX = "/usr/local/bin/"

class SofiaStorageClient(BaseClient):
    """Client for interaction with GPFS storage via VSC filesystem interface."""

    def __init__(self, filesystem: str, storage_path: str, home_path: str):
        """
        Initialize VSC GPFS backend.
        """
        # Launch GPFS operator
        self.operator = GpfsOperations()

        self.filesystem = filesystem
        self.storage_path = storage_path
        self.home_path = home_path

    def list_filesets(self) -> list | None:
        """List filesets"""
        return self.operator.list_filesystems()

    def create_fileset(self, fileset_name: str):
        """Create a new fileset resource for project."""
        fileset_path = os.path.join(self.storage_path, fileset_name)
        logger.info(f"Creating fileset {fileset_name} at: {fileset_path}")
        self.operator.make_fileset(fileset_path, fileset_name)
 
    def set_quota(self, fileset_name: str, block_limit: int, inode_limit: Optional[int] = DEFAULT_INODE_LIMIT):
        """Set quota for a specific fileset.

        Args:
            fileset_name: Fileset name (or project name)
            block_limit: Block soft limit (in units defined by GPFS, usually KB or MB)
            inode_limit: Inode soft limit
        """
        fileset_path = os.path.join(self.storage_path, fileset_name)
        self.operator.set_fileset_quota(block_limit, fileset_path, fileset_name, inode_soft=inode_limit)

    def get_quota(self, fileset_name: str) -> tuple:
        """Get quota and usage for a specific fileset."""
        self.operator.list_quota()
        fileset_quotas = self.operator.gpfslocalquotas[self.filesystem]['FILESET']
        quota_ids = [
            q for q in fileset_quotas
            if self.operator.get_fileset_name(q, self.filesystem) == fileset_name
        ]
        if len(quota_ids) != 1:
            raise BackendError(f"Multiple fileset quotas found for fileset: {fileset_name}")

        target_quota = fileset_quotas[quota_ids[0]][0]
        block_usage = target_quota.blockUsage
        block_limit = target_quota.blockQuota

        return block_usage, block_limit

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
        waldur_make_homedir_vsc = os.path.join(WALDUR_SCRIPT_PREFIX, "waldur_make_project_vsc")
        command = ["sudo", waldur_make_homedir_vsc, project, str(block_limit), str(owner_uid), str(owner_gid)]
        logger.info(f"Executing: {' '.join(command)}")
        return self.execute_command(command)
