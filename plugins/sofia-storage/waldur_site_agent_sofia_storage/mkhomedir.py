import argparse

from vsc.config.base import VSC

from waldur_site_agent.common.utils import load_configuration
from waldur_site_agent_sofia_storage.client import SofiaStorageClient

WALDUR_CONFIG = "/etc/waldur/waldur-site-agent-config.yaml"
WALDUR_BACKEND = "sofia_storage"

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

    # Convert to UID
    if not args.username.startswith("vsc"):
        raise ValueError(f"ERROR: username {args.username} does not correspond to a VSC user")

    vsc = VSC()
    vsc.get_vsc_options()
    vsc_user = args.username
    vsc_uid = vsc.uid_to_uid_number(vsc_user)

    # Load storage backend
    configuration = load_configuration(WALDUR_CONFIG, user_agent_suffix="homedir")

    try:
        storage_config = [o for o in configuration.offerings if o.backend_type == WALDUR_BACKEND][0]
    except IndexError:
        raise RuntimeError(f"ERROR: {WALDUR_BACKEND} backend not found in waldur-site-agent configuration")

    storage_client = SofiaStorageClient(
        storage_config.backend_settings["storage_file_system"],
        storage_config.backend_settings["storage_path"],
        storage_config.backend_settings["home_path"],
    )

    # Make home dir
    storage_client.make_home_dir(vsc_user, vsc_uid)

if __name__ == "__main__":
    main()
