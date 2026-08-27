import argparse

from waldur_site_agent.common.utils import load_configuration
from waldur_site_agent_sofia_storage.client import SofiaStorageClient

WALDUR_CONFIG = "/etc/waldur/waldur-site-agent-config.yaml"
WALDUR_BACKEND = "sofia_storage"

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
        raise RuntimeError(f"ERROR: {WALDUR_BACKEND} backend not found in waldur-site-agent configuration")

    storage_client = SofiaStorageClient(
        storage_config.backend_settings["storage_file_system"],
        storage_config.backend_settings["storage_path"],
        storage_config.backend_settings["home_path"],
    )

    storage_client.set_fileset_quota(fileset_name=args.project_dir, block_limit=args.block_limit)

if __name__ == "__main__":
    main()
