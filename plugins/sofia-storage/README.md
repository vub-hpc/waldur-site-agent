# VUB GPFS Storage Plugin for Waldur Site Agent

A backend plugin for Waldur Site Agent that manages GPFS storage directly via CLI commands.

> [!NOTE]
> **Python 3.12+** is required for this plugin.

## Overview

This plugin enables the Waldur Site Agent to manage GPFS storage resources. It implements the standard `BaseBackend` interface, allowing Waldur to:
- Set and update GPFS quotas (blocks and inodes) for projects and users.
- Report real-time usage metrics back to Waldur.
- Perform health checks and diagnostics on the GPFS filesystem.

Unlike previous iterations, this plugin is designed to run directly as part of the Waldur Site Agent and interacts with GPFS using system commands (`mmsetquota`, `mmrepquota`, etc.), removing the need for a separate API proxy.

## Features

- **Direct GPFS CLI Interaction**: Uses `mmsetquota`, `mmlsquota`, and `mmrepquota` for management.
- **Quota Management**: Supports both block and inode quotas.
- **Usage Reporting**: Automatically collects and reports usage per project/fileset.
- **Zero-Proxy Architecture**: Simplified deployment as a standard Waldur Site Agent plugin.
- **Custom Entrypoint**: Provides `waldur-vub-storage-plugin` command (alias to `waldur_site_agent`).

## Installation

This plugin is designed to be used within a `uv` workspace alongside `waldur-site-agent`.

```toml
# In your main project's pyproject.toml or similar
[tool.uv.sources]
waldur-site-agent-vub-gpfs-storage = { path = "src/waldur-vub-storage-plugin" }
```

## Configuration

In your `waldur-vub-storage-plugin-config.yaml`, configure the offering to use the `vub_gpfs` backend:

```yaml
offerings:
  - name: "VUB GPFS Storage"
    backend_type: "vub_gpfs"
    backend_settings:
      storage_path: "/gpfs/storage1"
    backend_components:
      storage:
        measured_unit: "GB"
        accounting_type: "limit"
        label: "Storage"
        unit_factor: 1024  # If Waldur uses GB and GPFS expects MB
      inodes:
        measured_unit: "count"
        accounting_type: "limit"
        label: "Inodes"
        unit_factor: 1
```

## Deployment

Build and deploy using the Makefile from the repository root:

```bash
# Build Docker image
make build-storage IMAGE_TAG=development

# Deploy to target host
# Deploy to target host
make deploy-storage slurmdb11 IMAGE_TAG=development
```

> [!IMPORTANT]
> **GPFS Access**: The Docker Swarm service definition (`waldur-vub-storage-plugin.yml`) has volume mounts for GPFS binaries (`/usr/lpp/mmfs/bin`) commented out by default. 
> To enable actual GPFS interaction in production, you must **uncomment these lines** in the compose file before deploying.

The service runs as `waldur-vub-storage-plugin_waldur-vub-storage-plugin` in Docker Swarm.

## GPFS Commands Used

- `mmlsfileset`: Used for health checks and listing.
- `mmsetquota`: Used to set block and inode limits.
- `mmlsquota`: Used to retrieve individual project/user limits and usage.
- `mmrepquota`: Used for bulk usage reporting.

## Development

### Package Structure

```
src/waldur-vub-storage-plugin/
├── gpfs/                    # Main Python package
│   ├── __init__.py
│   ├── backend.py           # waldur-site-agent backend implementation
│   └── client.py            # Wrapper for GPFS CLI commands
├── tests/
├── pyproject.toml
└── README.md
```

### Verify Installation

```bash
uv run python -c "from gpfs.backend import VubGpfsBackend; print('Import successful')"
```

### Run Tests

```bash
cd src/waldur-vub-storage-plugin
uv run pytest
```
