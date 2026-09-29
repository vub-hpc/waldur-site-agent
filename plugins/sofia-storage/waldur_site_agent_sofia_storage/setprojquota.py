import argparse
import sys

from waldur_site_agent.common.utils import load_configuration
from waldur_site_agent_sofia_storage.backend import (
    DEFAULT_FILESYSTEM,
    DEFAULT_HOME_PATH,
    DEFAULT_STORAGE_PATH,
)
from waldur_site_agent_sofia_storage.client import SofiaStorageClient

WALDUR_CONFIG = "/etc/waldur/waldur-site-agent-config.yaml"
WALDUR_BACKEND = "sofia_storage"

def fail(msg: str) -> None:
    """Exit with a clean one-line error (no traceback) so the agent can
    surface it as-is in its BackendError."""
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)

def main():
    """Set quota of project fileset in sofia_storage backend"""
    parser = argparse.ArgumentParser(
        description=(
            "Set quota of project fileset in the sofia_storage"
            " backend of waldur-site-agent"
        )
    )
    parser.add_argument("project_dir", type=str)
    parser.add_argument("block_limit", type=int)
    args = parser.parse_args()

    # Load storage backend
    configuration = load_configuration(WALDUR_CONFIG, user_agent_suffix="homedir")

    try:
        storage_config = [o for o in configuration.offerings if o.backend_type == WALDUR_BACKEND][0]
    except IndexError:
        fail(f"{WALDUR_BACKEND} backend not found in waldur-site-agent configuration")

    backend_settings = storage_config.backend_settings
    storage_client = SofiaStorageClient(
        backend_settings.get("storage_file_system", DEFAULT_FILESYSTEM),
        backend_settings.get("storage_path", DEFAULT_STORAGE_PATH),
        backend_settings.get("home_path", DEFAULT_HOME_PATH),
    )

    storage_client.set_fileset_quota(fileset_name=args.project_dir, block_limit=args.block_limit)

if __name__ == "__main__":
    main()
