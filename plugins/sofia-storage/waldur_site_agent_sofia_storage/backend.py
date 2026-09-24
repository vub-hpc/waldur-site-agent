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
      group name = {vsc_group_prefix}_{project_slug}; moderators are the
      project moderators, or the first user of the team if the project
      has none
    - create fileset in GPFS for this resource:
      fileset name = {resource_backend_id}
    - mount fileset in local storage:
      mount path = {storage_path}/{resource_backend_id}
    - enforce a single active storage resource per project: a new order
      for a project with an active resource fails, while an order for a
      terminated project re-activates the existing fileset
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

    def _get_group_moderators(self, user_context: dict) -> list[str]:
        """Return the moderators of the project's VSC group.

        Projects may exist without moderators; in that case the first
        user of the project team becomes the VSC group moderator.
        """
        project_mods = self._get_project_moderators(user_context)
        if project_mods:
            return project_mods

        project_members = [user.username for user in user_context['team']]
        if not project_members:
            raise BackendError(
                "Cannot create storage resource: the project has neither users nor moderators"
            )
        logger.info(
            "Project has no moderators, using first user %s as VSC group moderator",
            project_members[0],
        )
        return [project_members[0]]

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
        group_mods = self._get_group_moderators(user_context)
        logger.info(f"Sofia Storage Backend User context: {project_members} -- {group_mods}")

        # Create VSC group for this project
        if not self.vsc_client:
            raise BackendError("Cannot create storage resource without VSC account page integration")

        self.vsc_client.update_project_group(
            project_slug=project_slug,
            project_name=project_name,
            members=project_members,
            moderators=group_mods,
        )

    def create_resource_with_id(
        self,
        waldur_resource: WaldurResource,
        resource_backend_id: str,
        user_context: Optional[dict] = None,
    ) -> BackendResourceInfo:
        """Create GPFS fileset for resource.

        Only one active storage resource is allowed per project:

        - a request for a project that already has an active resource fails;
        - a request for a project whose resource was terminated re-activates
          the existing fileset (termination never removes filesets);
        - a request for a project without any fileset creates a new one.
        """
        if user_context is None:
            logger.error("Cannot create storage resource without Waldur user context")
            return

        # Storage resources always run with a quota: reject orders without a
        # positive storage limit before any backend action is taken.
        order_limits = waldur_resource.limits.to_dict() if waldur_resource.limits else {}
        storage_limit = order_limits.get(OFFERING_COMPONENT)
        if storage_limit is None or float(storage_limit) <= 0:
            raise BackendError(
                f"Cannot create storage resource {waldur_resource.name}: the order has no positive "
                f"'{OFFERING_COMPONENT}' limit. Storage resources are always created with a quota."
            )

        logger.info("Creating sofia storage resource: %s (id: %s)", waldur_resource.name, resource_backend_id)

        # Actions prior to resource creation
        self._pre_create_resource(waldur_resource, user_context)

        project_group, project_gid = self.vsc_client.get_project_group_ids(waldur_resource.project_slug)

        # The project's filesets, keyed by name with their block quota. A
        # quota of 0 marks a terminated resource (see list_project_filesets).
        project_filesets = self.client.list_project_filesets(project_gid)

        if resource_backend_id in project_filesets:
            # Re-activate the fileset of this resource (e.g. after termination)
            target_backend_id = resource_backend_id
            logger.info(
                "Storage resource %s already exists, skipping fileset creation",
                target_backend_id,
            )
        elif any(quota > 0 for quota in project_filesets.values()):
            active = ", ".join(sorted(name for name, quota in project_filesets.items() if quota > 0))
            raise BackendError(
                f"Project {waldur_resource.project_slug} already has an active storage resource "
                f"({active}). Only one active storage resource per project is allowed; "
                "terminate it before ordering a new one."
            )
        elif len(project_filesets) == 1:
            # Re-activate the project's terminated fileset instead of
            # creating a new one
            target_backend_id = next(iter(project_filesets))
            logger.info(
                "Re-activating terminated storage resource %s of project %s",
                target_backend_id,
                waldur_resource.project_slug,
            )
        elif len(project_filesets) > 1:
            raise BackendError(
                f"Project {waldur_resource.project_slug} has multiple terminated storage resources "
                f"({', '.join(sorted(project_filesets))}). Remove the unused filesets manually "
                "before ordering a new one."
            )
        else:
            # New fileset for the project
            target_backend_id = resource_backend_id
            group_mods = self._get_group_moderators(user_context)
            _, project_owner_uid = self.vsc_client.get_vsc_ids(group_mods[0])

            try:
                self.client.sudo_waldur_make_project_vsc(
                    project_dir=target_backend_id,
                    owner_uid=project_owner_uid,
                    owner_gid=project_gid,
                )
            except Exception as err:
                raise BackendError(f"Failed to make storage directory for resource {target_backend_id}: {err}")

        # Set fileset limits
        resource_limits = self._setup_resource_limits(target_backend_id, waldur_resource)

        backend_resource_info = BackendResourceInfo(
            backend_id=target_backend_id,
            limits=resource_limits,
        )
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

    def delete_resource(
        self,
        waldur_resource: WaldurResource,
        **kwargs: str,
    ) -> Optional[str]:
        """Delete storage resource (usually just zero the quota)."""
        resource_backend_id = waldur_resource.backend_id

        if not resource_backend_id:
            logger.info(f"Ignoring request to delete storage resource with no backend_id")
            return

        logger.info(f"Disabling storage resource (zeroing quota): {resource_backend_id}")
        zero_block_limit = {'storage': 0}

        try:
            self.client.set_resource_limits(resource_id=resource_backend_id, limits_dict=zero_block_limit)
        except Exception as err:
            logger.error(f"Failed to disable storage quota for {resource_backend_id}: {err}")

    def add_user(self, waldur_resource: WaldurResource, username: str, **kwargs: str) -> bool:
        """Add user to VSC group of the resource"""
        del kwargs

        if self.vsc_client is None:
            raise BackendError(
                "Cannot add user to storage resource without VSC account page integration"
            )

        project_slug = waldur_resource.project_slug
        return self.vsc_client.add_user_to_project(project_slug, username)

    def remove_user(self, waldur_resource: WaldurResource, username: str, **kwargs: str) -> bool:
        """Remove user from VSC group of the resource.

        Propagates the VscBackend result: True when the user no longer holds
        a membership (removed, or already absent), BackendError when the
        removal failed -- the core membership sync relies on exactly that.
        """
        del kwargs

        if self.vsc_client is None:
            raise BackendError(
                "Cannot remove user from storage resource without VSC account page integration"
            )

        project_slug = waldur_resource.project_slug
        return self.vsc_client.remove_user_to_project(project_slug, username)

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
            try:
                res_data = self.client.collect_project_quotas(rbi)
            except Exception as err:
                # A single failing resource must not discard the rest of a
                # batched report.
                logger.error("No usage report for storage resource %s: %s", rbi, err)
                continue

            for entity, usage, _ in res_data:
                rbi_entity_usage = float(usage) / self.unit_factor

                if entity == "fileset":
                    # fileset entity has total as name
                    rbi_entity_name = "TOTAL_ACCOUNT_USAGE"
                else:
                    # user entity has username as name
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
        """Collect and convert limits.

        A positive 'storage' limit is mandatory: storage resources always
        run with a quota, so a missing or zero limit is rejected rather
        than defaulted.
        """
        resource_limits = waldur_resource.limits.to_dict() if waldur_resource.limits else {}
        limit_value = resource_limits.get(OFFERING_COMPONENT)
        if limit_value is None or float(limit_value) <= 0:
            raise BackendError(
                f"Resource {waldur_resource.name} has no positive '{OFFERING_COMPONENT}' limit; "
                "storage resources must be ordered with a storage quota."
            )

        backend_limits = {OFFERING_COMPONENT: int(limit_value * self.unit_factor)}
        waldur_limits = {OFFERING_COMPONENT: limit_value}

        return backend_limits, waldur_limits

    def set_resource_limits(
        self, resource_backend_id: str, limits: dict[str, int]
    ) -> Optional[str]:
        """Set limits for the resource.

        Limit updates must keep a positive storage quota: the only
        sanctioned zero is termination, which zeroes the quota through the
        client directly.
        """
        storage_limit = limits.get(OFFERING_COMPONENT)
        if storage_limit is None or float(storage_limit) <= 0:
            raise BackendError(
                f"Refusing to set a zero or missing storage quota on {resource_backend_id}: "
                "storage resources must keep a positive quota. Terminate the resource instead."
            )
        return super().set_resource_limits(resource_backend_id, limits)

    def get_resource_metadata(self, resource_backend_id: str) -> dict:
        """Return the applied storage quota of the resource as metadata.

        Reported in the offering unit (GiB), like the resource limits in
        Waldur. Only the limit is reported: usage changes constantly and
        would trigger a metadata rewrite on every membership sync, and it
        is already covered by the usage reports.
        """
        try:
            _, block_limit = self.client.get_fileset_quota(
                fileset_name=resource_backend_id, silent=True
            )
        except Exception:
            logger.debug(
                "No quota available for storage resource %s; returning empty metadata",
                resource_backend_id,
            )
            return {}
        return {f"{OFFERING_COMPONENT}_limit": round(block_limit / self.unit_factor, 2)}

    # Required by BaseBackend interface
    def downscale_resource(self, resource_backend_id: str) -> bool: return True
    def pause_resource(self, resource_backend_id: str) -> bool: return True
    def restore_resource(self, resource_backend_id: str) -> bool: return True
