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
    """Sofia Storage backend."""

    def __init__(self, backend_settings: dict, backend_components: dict[str, dict]) -> None:
        super().__init__(backend_settings, backend_components)
        self.backend_type = "sofia_storage"

        self.storage_fs = backend_settings.get("storage_file_system", DEFAULT_FILESYSTEM)
        self.storage_path = backend_settings.get("storage_path", DEFAULT_STORAGE_PATH)
        self.home_path = backend_settings.get("home_path", DEFAULT_HOME_PATH)
        self.client = SofiaStorageClient(self.storage_fs, self.storage_path, self.home_path)

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

    def _pre_create_resource(
        self, waldur_resource: WaldurResource, user_context: Optional[dict] = None
    ) -> None:
        """Perform actions prior to resource creation.

        This backend overrides create_resource entirely, but we must implement
        this abstract method from BaseBackend to allow instantiation.
        """
        pass

    def create_resource_with_id(
        self,
        waldur_resource: WaldurResource,
        resource_backend_id: str,
        user_context: Optional[dict] = None,
    ) -> BackendResourceInfo:
        """Create resource with a specific backend ID.

        This method creates a resource with a predetermined backend ID,
        without any retry logic or uniqueness checking. The calling code
        (typically the processor) is responsible for ensuring the ID is unique.

        Args:
            waldur_resource: Resource data from Waldur marketplace
            resource_backend_id: The specific backend ID to use for the resource
            user_context: Optional user context including team members and offering users

        Returns:
            Created backend resource information

        Raises:
            BackendError: If resource creation fails
        """
        logger.info("Creating GPFS storage resource: %s (id: %s)", waldur_resource.name, resource_backend_id)

        # Actions prior to resource creation
        self._pre_create_resource(waldur_resource, user_context)

        # Create resource with specific ID
        project_backend_id = self._get_project_backend_id(waldur_resource.project_slug)
        if not self._create_backend_resource(
            resource_backend_id, waldur_resource.name, project_backend_id, project_backend_id
        ):
            raise BackendError(f"Failed to create backend resource with ID: {resource_backend_id}")

        # Setup limits
        resource_limits = self._setup_resource_limits(resource_backend_id, waldur_resource)
        backend_resource_info = BackendResourceInfo(
            backend_id=resource_backend_id,
            limits=resource_limits,
        )

        # In GPFS, use the project slug as the fileset name
        project_slug = waldur_resource.project_slug
        project_name = waldur_resource.project_name
        project_members = [user.username for user in user_context['team']]
        project_mods = [
            user.username for user in user_context['team']
            if user.role in ['PROJECT.ADMIN', 'PROJECT.MANAGER']
        ]
        logger.info(f"Sofia Storage Backend User context: {project_members} -- {project_mods}")

        # Create VSC group for this project
        if not self.vsc_client:
            raise BackendError("Cannot create storage resource without VSC account page integration")

        project_group, project_gid = self.vsc_client.make_project_group(
            project_slug=project_slug,
            project_name=project_name,
            members=project_members,
            moderators=project_mods,
        )

        # Create fileset for project
        limits, waldur_limits = self._collect_resource_limits(waldur_resource)
        storage_limit = limits.get(OFFERING_COMPONENT, 0)

        _, project_owner_uid = self.vsc_client.get_vsc_ids(project_mods[0])

        try:
            self.client.sudo_waldur_make_project_vsc(
                project=project_slug,
                block_limit=storage_limit,
                owner_uid=project_owner_uid,
                owner_gid=project_gid,
            )
        except Exception as err:
            raise BackendError(f"Failed to make project directory for project {project_slug}: {err}")

        # Home directories of project members
        for user in project_members + project_mods:
            try:
                self.client.sudo_waldur_make_homedir_vsc(user)
            except Exception as err:
                raise BackendError(f"Failed to make home directory for user {user}: {err}")

        # Actions after resource creation
        self.post_create_resource(backend_resource_info, waldur_resource, user_context)
        return backend_resource_info

    def update_resource(
        self,
        waldur_resource: WaldurResource,
        user_context: Optional[dict] = None,
    ) -> BackendResourceInfo:
        """Update storage resource limits."""
        return self.create_resource(waldur_resource, user_context)

    def delete_resource(self, waldur_resource: WaldurResource, project_slug: str) -> None:
        """Delete storage resource (usually just zero the quota)."""
        project_slug = waldur_resource.project_slug
        logger.info("Disabling storage resource (zeroing quota): %s", project_slug)
        try:
            self.client.set_quota(fileset_name=project_slug, block_limit=0)
        except Exception as err:
            logger.error("Failed to disable storage quota for %s: %s", project_slug, err)

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
            fileset_name = rbi[:rbi.rfind("-")]
            res_usage, res_quota = self.client.get_quota(fileset_name)

            report[rbi] = {
                "TOTAL_ACCOUNT_USAGE": {
                    "storage": res_usage
                }
            }

        logger.info(f"Sofia Storage Usage Report: {report}")
        return report

    def _collect_resource_limits(
        self, waldur_resource: WaldurResource
    ) -> tuple[dict[str, int], dict[str, int]]:
        """Collect and convert limits."""
        backend_limits = {}
        waldur_limits = {}

        resource_limits = waldur_resource.limits.to_dict() if waldur_resource.limits else {}

        for component_key, data in self.backend_components.items():
            if component_key in resource_limits:
                limit_value = resource_limits[component_key]
                # Convert to GPFS units (e.g. if Waldur is GB and GPFS expects MB)
                unit_factor = data.get("unit_factor", 1)
                backend_limits[component_key] = int(limit_value * unit_factor)
                waldur_limits[component_key] = limit_value
            else:
                # Default limit if not set in Waldur
                backend_limits[component_key] = data.get("limit", 0)
                waldur_limits[component_key] = data.get("limit", 0)

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
