import argparse
import sys

from vsc.config.base import VSC

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
    """Create home dir for given VSC user"""
    parser = argparse.ArgumentParser(
        description=(
            "Create home directory for given VSC user in the sofia_storage"
            " backend of waldur-site-agent"
        )
    )
    parser.add_argument("username", type=str)
    args = parser.parse_args()

    if not args.username.startswith("vsc"):
        fail(f"username {args.username} does not correspond to a VSC user")

    # Load storage backend
    configuration = load_configuration(WALDUR_CONFIG, user_agent_suffix="homedir")

    try:
        storage_config = [o for o in configuration.offerings if o.backend_type == WALDUR_BACKEND][0]
    except IndexError:
        fail(f"{WALDUR_BACKEND} backend not found in waldur-site-agent configuration")

    # Convert to UID
    vsc = VSC()
    vsc.get_vsc_options()
    vsc_user = args.username
    vsc_uid = vsc.uid_to_uid_number(vsc_user)

    backend_settings = storage_config.backend_settings
    storage_client = SofiaStorageClient(
        backend_settings.get("storage_file_system", DEFAULT_FILESYSTEM),
        backend_settings.get("storage_path", DEFAULT_STORAGE_PATH),
        backend_settings.get("home_path", DEFAULT_HOME_PATH),
    )

    # Make home dir
    storage_client.make_home_dir(vsc_user, vsc_uid)

if __name__ == "__main__":
    main()
