"""VSC integration module for waldur-vub-storage-plugin.

This module provides integration with VSC account page APIs to create
and manage user groups for Waldur projects.
"""

from urllib.request import HTTPError
from vsc.accountpage.client import AccountpageClient

from waldur_site_agent.backend import logger
from waldur_site_agent.backend.exceptions import BackendError

VSC_ACCOUNTPAGE_API = "https://account.vscentrum.be/django/api"

class VscBackend:
    """Integration class for VSC group management."""

    def __init__(self, token: str, autogroup_name: str, group_prefix: str = None):
        """
        Initialize VSC backend.

        Args:
            token: VSC account page client access token
        """
        # Launch account page client
        self.client = AccountpageClient(url=VSC_ACCOUNTPAGE_API, token=token)
        logger.debug("Successfully connected to VSC account page backend")

        if group_prefix is None:
            group_prefix = ''
        self.group_prefix = group_prefix

        self.autogroup = self.get_autogroup(autogroup_name)

    def project_group_name(self, project_slug: str) -> str:
        """Return VSC group name of given project"""
        # normalize formatting with underscore separators
        project_group_name = project_slug.replace("-", "_")
        # ensure group name starts with 'b'
        group_prefix = self.group_prefix
        if group_prefix[0] != 'b':
            group_prefix = f"b{group_prefix}"

        return f"{group_prefix}{project_group_name}"

    def get_vsc_ids(self, username: str) -> tuple:
        """Return VSC IDs for given user"""
        account = self.client.get_account(username)
        return account.vsc_id, account.vsc_id_number

    def get_project_group_ids(self, project_slug: str) -> dict | None:
        """Get IDs of VSC group of given project"""
        group_name = self.project_group_name(project_slug)
        group_account = self.client.get_group(group_name)
        return group_account.vsc_id, group_account.vsc_id_number

    def get_autogroup(self, autogroup_name: str) -> str | None:
        """Get information about VSC autogroup"""
        try:
            _, autogroup = self.client.autogroup[autogroup_name].get()
        except HTTPError as err:
            raise BackendError(f"VSC autogroup {autogroup_name} not found!")

        return autogroup

    def update_autogroup_source(self, source_group: str) -> bool:
        """Update group sources of VSC autogroup"""
        logger.info(f"Adding {source_group} to autogroup {self.autogroup['vsc_id']}")
        try:
            self.client.autogroup[self.autogroup['vsc_id']].source[source_group].add.post()
        except HTTPError as err:
            if err.code == 404:
                raise BackendError(
                    f"Failed to add VSC group {source_group} to autogroup {self.autogroup['vsc_id']},"
                    "group does not exist"
                )

        return True

    def make_project_group(self, project_slug: str, project_name: str, members: list, moderators: list) -> str:
        """
        Ensure VSC group exists for a project.

        Args:
            project_slug: Project slug (used as group name)
            project_name: Project name (used for description)
            members: List of users in the group
            moderators: List of moderators for this group

        Returns:
            The group name that was created/updated
        """
        group_name = self.project_group_name(project_slug)
        description = f"Group for sofia project: {project_name}"

        if not members and not moderators:
            raise BackendError(f"Cannot create new VSC group {group_name} without any members or moderators")

        # New VSC groups require a moderator
        try:
            exist_project_group = self.client.get_group(group_name)
        except HTTPError as err:
            # move on for 404, group does not exist yet
            if err.code != 404:
                raise BackendError(
                    f"Failed to retrieve information of VSC group {group_name} due to HTTP error:"
                    f"{err.reason} ({err.code})"
                )
            exist_project_group = None
            logger.debug(f"VSC group {group_name} does not exist, will be created")
        else:
            logger.debug(f"VSC group {group_name} already exists, will be updated")

        if exist_project_group is None and not moderators:
            raise BackendError(f"Cannot create new VSC group {group_name} without moderators")

        # Update/Create VSC group for project 
        try:
            logger.info(f"Creating/updating VSC group {group_name}: {members} (mods: {moderators})")
            self.client.create_or_update_group(
                groupname=group_name,
                moderators=moderators,
                members=members,
                info=description,
                dry_run=False,
            )
        except HTTPError as err:
            raise BackendError(f"Failed to create VSC group {group_name} due to HTTP error: {err.reason} ({err.code})")

        # Update autogroup
        self.update_autogroup_source(group_name)

        return self.get_project_group_ids(project_slug)
