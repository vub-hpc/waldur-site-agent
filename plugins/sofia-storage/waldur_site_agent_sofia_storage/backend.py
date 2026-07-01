"""Sofia Storage backend for Waldur Site Agent."""

from typing import Optional

from waldur_api_client.models.resource import Resource as WaldurResource
from waldur_site_agent.backend import logger
from waldur_site_agent.backend.backends import BaseBackend
from waldur_site_agent.backend.exceptions import BackendError
from waldur_site_agent.backend.structures import BackendResourceInfo

from .client import SofiaStorageClient
from .vsc import VscBackend

OFFERING_COMPONENT = "storage"

DEFAULT_FILESYSTEM = "gpfs"
DEFAULT_STORAGE_PATH = "/gpfs"
DEFAULT_HOME_PATH = "/home"

class SofiaStorageBackend(BaseBackend):
    """
    Sofia Storage backend

    - create VSC group for the project of the resource:
      group name = {vsc_group_prefix}_{project_slug}
    - create fileset in GPFS for this resource:
      fileset name = {resource_backend_id}
    - mount fileset in local storage:
      mount path = {storage_path}/{resource_backend_id}
    """
    supports_decreasing_usage = True

    def __init__(self, backend_settings: dict, backend_components: dict[str, dict]) -> None:
        super().__init__(backend_settings, backend_components)
        self.backend_type = "sofia_storage"

        self.vsc_client = None
        vsc_token = backend_settings.get("vsc_token")
        vsc_autogroup = backend_settings.get("vsc_autogroup")
        vsc_group_prefix = backend_settings.get("vsc_group_prefix")

        if vsc_token:
            self.vsc_client = VscBackend(
                token=vsc_token,
                autogroup_name=vsc_autogroup,
                group_prefix=vsc_group_prefix,
            )

        component_data = self.backend_components[OFFERING_COMPONENT]
        self.unit_factor = float(component_data.get("unit_factor", 1))

        self.storage_fs = backend_settings.get("storage_file_system", DEFAULT_FILESYSTEM)
        self.storage_path = backend_settings.get("storage_path", DEFAULT_STORAGE_PATH)
        self.home_path = backend_settings.get("home_path", DEFAULT_HOME_PATH)
        self.client = SofiaStorageClient(
            filesystem=self.storage_fs,
            storage_path=self.storage_path,
            home_path=self.home_path,
            unit_factor=self.unit_factor,
            vsc_group_prefix=vsc_group_prefix,
        )

    def ping(self, raise_exception: bool = False) -> bool:
        """Check if GPFS commands are accessible."""
        try:
            # Try to list filesets as a health check
            self.client.list_filesets()
        except Exception as e:
            if raise_exception:
                raise
            logger.error("GPFS ping failed: %s", e)
            return False

        return True

    def list_components(self) -> list[str]:
        """Return list of supported components."""
        return list(self.backend_components.keys())

    def diagnostics(self) -> bool:
        """Log backend diagnostics info."""
        logger.info("Sofia Storage Backend Diagnostics")
        logger.info("Storage Path: %s", self.storage_path)
        return self.ping()

    def _get_project_moderators(self, user_context: dict):
        """Return users with moderator rights in project team"""
        return [
            user.username for user in user_context['team']
            if user.role in ['PROJECT.ADMIN', 'PROJECT.MANAGER']
        ]

    def _pre_create_resource(
        self,
        waldur_resource: WaldurResource,
        user_context: Optional[dict] = None
    ) -> None:
        """Create/Update VSC group for this resource"""
        if user_context is None:
            logger.error("Cannot pre-create storage resource without Waldur user context")
            return

        project_slug = waldur_resource.project_slug
        project_name = waldur_resource.project_name
        project_members = [user.username for user in user_context['team']]
        project_mods = self._get_project_moderators(user_context)
        logger.info(f"Sofia Storage Backend User context: {project_members} -- {project_mods}")

        # Create VSC group for this project
        if not self.vsc_client:
            raise BackendError("Cannot create storage resource without VSC account page integration")

        self.vsc_client.update_project_group(
            project_slug=project_slug,
            project_name=project_name,
            members=project_members,
            moderators=project_mods,
        )

    def create_resource_with_id(
        self,
        waldur_resource: WaldurResource,
        resource_backend_id: str,
        user_context: Optional[dict] = None,
    ) -> BackendResourceInfo:
        """Create GPFS fileset for resource"""
        if user_context is None:
            logger.error("Cannot create storage resource without Waldur user context")
            return

        logger.info("Creating sofia storage resource: %s (id: %s)", waldur_resource.name, resource_backend_id)

        # Actions prior to resource creation
        self._pre_create_resource(waldur_resource, user_context)

        # Create resource with specific ID
        project_backend_id = self._get_project_backend_id(waldur_resource.project_slug)
        if not self._create_backend_resource(
            resource_backend_id,
            waldur_resource.name,
            project_backend_id,
        ):
            raise BackendError(f"Failed to create backend resource with ID: {resource_backend_id}")

        # Setup limits
        resource_limits = self._setup_resource_limits(resource_backend_id, waldur_resource)
        backend_resource_info = BackendResourceInfo(
            backend_id=resource_backend_id,
            limits=resource_limits,
        )

        # Create fileset for project
        limits, waldur_limits = self._collect_resource_limits(waldur_resource)
        storage_limit = limits.get(OFFERING_COMPONENT, 0)

        project_group, project_gid = self.vsc_client.get_project_group_ids(waldur_resource.project_slug)
        project_mods = self._get_project_moderators(user_context)
        _, project_owner_uid = self.vsc_client.get_vsc_ids(project_mods[0])

        try:
            self.client.sudo_waldur_make_project_vsc(
                project_dir=resource_backend_id,
                block_limit=storage_limit,
                owner_uid=project_owner_uid,
                owner_gid=project_gid,
            )
        except Exception as err:
            raise BackendError(f"Failed to make storage directory for resource {resource_backend_id}: {err}")

        # Actions after resource creation
        self.post_create_resource(backend_resource_info, waldur_resource, user_context)

        return backend_resource_info

    def post_create_resource(
        self,
        resource: BackendResourceInfo,
        waldur_resource: WaldurResource,
        user_context: Optional[dict] = None,
    ) -> None:
        """Post-create actions for storage resource"""
        if user_context is None:
            logger.error("Cannot post-create storage resource without Waldur user context")
            return

        # Home directories of project members
        project_members = [user.username for user in user_context['team']]
        project_mods = self._get_project_moderators(user_context)
        for user in project_members + project_mods:
            try:
                self.client.sudo_waldur_make_homedir_vsc(user)
            except Exception as err:
                raise BackendError(f"Failed to make home directory for user {user}: {err}")

    def update_resource(
        self,
        waldur_resource: WaldurResource,
        user_context: Optional[dict] = None,
    ) -> BackendResourceInfo:
        """Update storage resource limits."""
        return self.create_resource(waldur_resource, user_context)

    def delete_resource(
        self,
        waldur_resource: WaldurResource,
        **kwargs: str,
    ) -> Optional[str]:
        """Delete storage resource (usually just zero the quota)."""
        resource_backend_id = waldur_resource.backend_id
        logger.info(f"Disabling storage resource (zeroing quota): {resource_backend_id}")
        try:
            self.client.set_fileset_quota(fileset_name=resource_backend_id, block_limit=0)
        except Exception as err:
            logger.error(f"Failed to disable storage quota for {resource_backend_id}: {err}")

    def add_user(self, waldur_resource: WaldurResource, username: str, **kwargs: str) -> bool:
        """Add user to VSC group of the resource"""
        del kwargs

        project_slug = waldur_resource.project_slug
        self.vsc_client.add_user_to_project(project_slug, username)

        return True

    def remove_user(self, waldur_resource: WaldurResource, username: str, **kwargs: str) -> bool:
        """Remove user from VSC group of the resource"""
        del kwargs

        project_slug = waldur_resource.project_slug
        self.vsc_client.remove_user_to_project(project_slug, username)

        return True

    def _get_usage_report(self, resource_backend_ids: list[str]) -> dict:
        """Return usage report for the specified resources.

        Expected format:
        {
            "backend_id": {
                "TOTAL_ACCOUNT_USAGE": {
                    "component_name": usage_value
                },
                "username": {
                    "component_name": usage_value
                }
            }
        }
        """
        report = {}
        for rbi in resource_backend_ids:
            rbi_usage = {}
            res_data = self.client.collect_project_quotas(rbi)
            for entity, usage, _ in res_data:
                rbi_entity_usage = float(usage) / self.unit_factor

                if entity == "fileset":
                    rbi_entity_name = "TOTAL_ACCOUNT_USAGE"
                else:
                    # username
                    rbi_entity_name = entity

                rbi_usage.update({
                    rbi_entity_name: {
                        OFFERING_COMPONENT: rbi_entity_usage,
                    },
                })

            report[rbi] = rbi_usage

        logger.info(f"Sofia Storage Usage Report: {report}")
        return report

    def _collect_resource_limits(
        self, waldur_resource: WaldurResource
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Collect and convert limits."""
        backend_limits = {}
        waldur_limits = {}

        resource_limits = waldur_resource.limits.to_dict() if waldur_resource.limits else {}


        if OFFERING_COMPONENT in resource_limits:
            limit_value = resource_limits[OFFERING_COMPONENT]
            backend_limits[OFFERING_COMPONENT] = int(limit_value * self.unit_factor)
            waldur_limits[OFFERING_COMPONENT] = limit_value
        else:
            # Default limit if not set in Waldur
            comp_data = self.backend_components[OFFERING_COMPONENT]
            backend_limits[OFFERING_COMPONENT] = comp_data.get("limit", 0)
            waldur_limits[OFFERING_COMPONENT] = comp_data.get("limit", 0)

        return backend_limits, waldur_limits

    def get_resource_metadata(self, resource_backend_id: str) -> dict:
        """Return metadata for resource."""
        try:
            quota = self.client.get_quota(resource_backend_id)
            return quota
        except Exception:
            return {}

    # Required by BaseBackend interface
    def downscale_resource(self, resource_backend_id: str) -> bool: return True
    def pause_resource(self, resource_backend_id: str) -> bool: return True
    def restore_resource(self, resource_backend_id: str) -> bool: return True
