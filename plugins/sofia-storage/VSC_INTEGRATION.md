# VSC Integration for Waldur Site Agent Storage Plugin

This document describes the integration between the `waldur-vub-storage-plugin` and VSC (Vlaams Supercomputing Centre) account management system.

## Overview

The storage plugin automatically creates VSC user groups for Waldur projects when storage resources are created. This ensures that:

1. A VSC user group exists for each project (named using the project slug)
2. Storage resources are created in the context of the corresponding VSC group
3. The integration follows the principle of "create if not exists"

## Configuration

### Basic Configuration

```yaml
backend_settings:
  storage_path: "/gpfs/storage1"
  vsc_token: "your-oauth-token-here"  # Required for VSC integration
  vsc_group_prefix: "proj_"           # Optional: prefix for VSC group names
```

### Environment Variables

For security, use environment variables:

```yaml
backend_settings:
  storage_path: "/gpfs/storage1"
  vsc_token: "{{ lookup('env', 'VSC_OAUTH_TOKEN') }}"
  vsc_group_prefix: "proj_"
```

## Integration Workflow

### 1. Backend Initialization

The `VubGpfsBackend` automatically initializes the VSC client when a token is provided:

```python
backend = VubGpfsBackend(
    backend_settings={
        "storage_path": "/gpfs/storage1",
        "vsc_token": "your-oauth-token",
        "vsc_group_prefix": "proj_"
    },
    backend_components={
        "storage": {
            "measured_unit": "GB",
            "accounting_type": "limit",
            "label": "Storage",
            "unit_factor": 1024
        }
    }
)
```

### 2. Resource Creation

When creating a storage resource:

```python
waldur_resource = WaldurResource(
    name="My Project",
    slug="my-project",
    backend_id="my-project"
)

# This automatically:
# 1. Creates VSC group "proj_my-project"
# 2. Creates GPFS fileset "my-project"
# 3. Sets storage quotas
result = backend.create_resource(waldur_resource)
```

### 3. VSC Group Naming

VSC group names follow the pattern:
`{vsc_group_prefix}{project_slug}`

Default: `proj_{project_slug}`

Example: Project with slug "data-science" → VSC group "proj_data-science"

## VSC Backend Module

The `VscBackend` class handles all VSC operations:

### Key Methods

- `make_project_group(project_slug, project_name)`: Create/update VSC group
- `get_project_group(project_slug)`: Get VSC group information
- `project_group_name(project_slug)`: Generate VSC group name

### Example Usage

```python
# Initialize VSC backend
vsc_backend = VscBackend(token="your-oauth-token")

# Create VSC group for project
vsc_backend.make_project_group("my-project", "My Project")

# Get VSC group information
group_info = vsc_backend.get_project_group("my-project")
```

## Error Handling

The integration includes comprehensive error handling:

1. **Missing VSC Token**: Backend fails gracefully if no VSC token is configured
2. **VSC API Errors**: Proper logging and exception handling for VSC API calls
3. **Resource Creation**: Storage creation continues even if VSC group creation fails (with logging)

## Testing

### Test Structure

Tests are organized in `tests/test_vsc_integration.py` and cover:

- VSC client initialization
- VSC group creation workflow
- Resource creation with VSC integration
- Error cases (missing VSC token, API failures)

### Running Tests

```bash
cd src/waldur-vub-storage-plugin
uv run pytest tests/test_vsc_integration.py -v
```

## Deployment Configuration

### Ansible Configuration

In your Ansible role:

```yaml
# group_vars/all/main.yml
waldur_vub_storage_plugin_config:
  storage_path: "/gpfs/storage1"
  vsc_group_prefix: "proj_"

# roles/containers/templates/waldur-vub-storage-plugin-config.yaml.j2
backend_settings:
  storage_path: "{{ waldur_vub_storage_plugin_config.storage_path }}"
  vsc_token: "{{ vsc_oauth_token }}"
  vsc_group_prefix: "{{ waldur_vub_storage_plugin_config.vsc_group_prefix }}"
```

### Environment Variables

Set VSC token as environment variable:

```bash
export VSC_OAUTH_TOKEN="your-oauth-token-here"
```

## Troubleshooting

### Common Issues

1. **Missing VSC Token**:
   - Error: "Cannot create storage resource without VSC account page integration"
   - Solution: Add `vsc_token` to backend settings

2. **VSC API Errors**:
   - Error: "Failed to create VSC group"
   - Solution: Check OAuth token, VSC API availability, and token permissions

3. **Group Naming Conflicts**:
   - Error: "Group already exists"
   - Solution: Ensure project slugs are unique and don't conflict with existing VSC groups

### Debugging

Enable debug logging:

```python
import logging
logging.getLogger("gpfs.vsc").setLevel(logging.DEBUG)
logging.getLogger("gpfs.backend").setLevel(logging.DEBUG)
```

## Security Considerations

1. **Token Storage**: Always use environment variables or secure vaults for OAuth tokens
2. **Token Permissions**: Ensure the OAuth token has only the necessary permissions
3. **Logging**: Avoid logging sensitive information (tokens, group details)

## Best Practices

1. **Group Naming**: Use consistent naming conventions (e.g., `proj_` prefix)
2. **Error Handling**: Handle VSC failures gracefully to avoid breaking storage creation
3. **Testing**: Test VSC integration separately before deploying to production
4. **Monitoring**: Monitor VSC API usage and quota consumption

## Integration with VSC Account Page Clients

The plugin uses the `vsc-accountpage-clients` library for VSC API interactions. Ensure you have the required dependency:

```toml
dependencies = [
    "vsc-accountpage-clients>=3.0.0"
]
```

## Configuration Examples

### Minimal Configuration

```yaml
backend_settings:
  storage_path: "/gpfs/storage1"
  vsc_token: "your-token"
```

### Production Configuration

```yaml
backend_settings:
  storage_path: "/gpfs/storage1"
  vsc_token: "{{ lookup('env', 'VSC_OAUTH_TOKEN') }}"
  vsc_group_prefix: "proj_"
  storage:
    measured_unit: "GB"
    accounting_type: "limit"
    label: "Storage"
    unit_factor: 1024
```

## Development

### Adding New VSC Features

To extend VSC integration:

1. Add new methods to `VscBackend` class
2. Update `backend.py` to use new methods
3. Add tests in `test_vsc_integration.py`

### Testing VSC Integration

Use mock VSC client for testing:

```python
from unittest.mock import Mock

# Create mock VSC client
mock_vsc_client = Mock()
mock_vsc_client.create_or_update_group.return_value = {"success": True}

# Initialize backend with mock client
vsc_backend = VscBackend("test-token")
vsc_backend.client = mock_vsc_client
```

This documentation provides a complete guide to integrating VSC account management with the Waldur Site Agent storage plugin. The integration ensures that storage resources are properly associated with VSC user groups for seamless access control and management.