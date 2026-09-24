# Sofia Storage Plugin for Waldur Site Agent

A backend plugin for Waldur Site Agent managing scratch storage on the
**sofia** cluster. Each Waldur marketplace resource becomes a GPFS fileset
on the scratch filesystem, owned by a per-project VSC user group; quotas,
membership and usage are kept in sync between Waldur and GPFS.

> [!NOTE]
> **Python 3.12+** is required for this plugin.

## Overview

`SofiaStorageBackend` (backend type `sofia_storage`) implements the standard
`BaseBackend` interface on top of two integrations:

- **GPFS** — fileset creation, ownership and quotas via the VSC
  `GpfsOperations` interface (`vsc-accountpage-clients`), plus quota and
  usage queries with `mmlsquota`
- **VSC account page** — one user group per Waldur project, created and
  managed through the VSC account page API and used as the owning group of
  the project fileset

For each `sofia_storage` resource in Waldur the agent can:

- create/update the VSC user group of the resource's project
- create the resource's GPFS fileset and set its ownership and permissions
- set/update the resource's block quota
- add/remove users (VSC group membership)
- report storage usage (fileset total and per user)
- enforce a single **active** storage resource per project
- require a positive storage quota — orders and limit updates without
  one are rejected

## Naming

| Object          | Name |
| --------------- | ---- |
| VSC group       | `{vsc_group_prefix}{project_slug}` — dashes in the slug become underscores; a `b` is prepended to the prefix when it is set and does not already start with one |
| GPFS fileset    | the resource backend ID |
| Fileset path    | `{storage_path}/{resource_backend_id}` |
| Home directory  | `{home_path}/{username}` |

Example: project `my-project` with prefix `proj_` → VSC group
`bproj_my_project`; a resource with backend ID `my-res-01` → fileset
`my-res-01` at `/gpfs/my-res-01`.

## Resource Lifecycle

**Create** (`order_process` mode):

The order must carry a positive `storage` limit — a storage resource
always runs with a quota, so an order without one is rejected before
any backend action is taken.

1. The project's VSC group is created or updated with the current project
   team as members and the project `ADMIN`/`MANAGER` users as moderators,
   then added as a source of the configured VSC autogroup. A project without
   moderators is fine: the first user of the team becomes the group
   moderator. Only a project without any users or moderators at all is
   rejected.
2. The fileset is created and owned by the project's VSC group (mode `0770`,
   owner = the VSC UID of the first moderator, or of the first team user when
   the project has no moderators) through the `waldur_make_project_vsc`
   script.
3. The ordered `storage` limit is applied as the fileset block quota (inode
   soft limit 1M) through the `waldur_set_project_quota` script.
4. Home directories (mode `0700`) are created for every project member and
   moderator through the `waldur_make_homedir_vsc` script.

**Add/remove user** (`membership_sync` mode): the user is added to or
removed from the project's VSC group — access to the fileset follows group
membership. The applied storage quota is also synced to Waldur as resource
backend metadata (`storage_limit`, in the offering unit).

**Usage** (`report` mode): the fileset total and each member's usage are
read with `mmlsquota` and reported for the `storage` component:

```json
{
  "my-res-01": {
    "TOTAL_ACCOUNT_USAGE": {
      "storage": 50.0
    },
    "vsc12345": {
      "storage": 12.5
    }
  }
}
```

Values are in the offering's `measured_unit` (GPFS reports in KB; the plugin
converts to bytes and divides by `unit_factor`). `supports_decreasing_usage`
is enabled, so usage may go down between reports.

**Delete**: terminating the resource does not remove the fileset or its
data — the block quota is zeroed so the resource stops consuming quota.

**One active resource per project**: a project may have at most one
active storage resource. The project's filesets are identified by
ownership — each fileset is chowned to the project's VSC group when it
is created — and a zero block quota marks a terminated resource. Since
creation and limit updates must keep a positive quota, termination is
the only way a fileset's quota reaches zero. Consequences:

- a new order for a project that already has an **active** resource
  **fails**, naming the active resource in the order error;
- a new order for a project whose resource was **terminated**
  **re-activates the old fileset**: no new fileset is created, the
  quota is re-established to the new ordered limit, and the old
  backend ID is reported back to Waldur — whether Waldur re-uses the
  old resource row or orders a new one for the project;
- a project without any fileset gets a new one (normal create path);
- a project left with **multiple terminated** filesets (legacy state)
  fails until the unused filesets are removed manually.

## Installation

The plugin is a member of the `waldur-site-agent` uv workspace:

```bash
# from the repository root
uv sync --all-packages
```

## Configuration

Add an offering with `backend_type: sofia_storage` to the site agent
configuration:

```yaml
offerings:
  - name: "Sofia Scratch Storage"
    waldur_api_url: "https://waldur.example.com/api/"
    waldur_api_token: ""
    waldur_offering_uuid: "..."

    order_processing_backend: "sofia_storage"
    membership_sync_backend: "sofia_storage"
    reporting_backend: "sofia_storage"
    backend_type: "sofia_storage"

    backend_settings:
      vsc_token: "your-oauth-token"     # required for resource creation
      vsc_autogroup: "autogroup-name"   # VSC autogroup sourced from the project groups
      vsc_group_prefix: "proj_"         # optional prefix for VSC group names (default: "")
      storage_file_system: "gpfs"       # GPFS filesystem name (default: "gpfs")
      storage_path: "/gpfs"             # local mount point of the scratch filesystem (default: "/gpfs")
      home_path: "/home"                # home directory base path (default: "/home")

    backend_components:
      storage:
        limit: 100
        measured_unit: "GiB"
        accounting_type: "usage"
        label: "Storage"
        unit_factor: 1073741824  # bytes per measured unit (1073741824 for GiB, 1000000000 for GB)
```

### Backend settings

- **`vsc_token`** — VSC account page access token. Effectively required:
  without it, resource creation fails with
  `Cannot create storage resource without VSC account page integration`.
- **`vsc_autogroup`** — name of the VSC autogroup that each project group
  is added to as a source.
- **`vsc_group_prefix`** — optional prefix for VSC group names (see
  [Naming](#naming)).
- **`storage_file_system`** — GPFS filesystem name passed to the GPFS
  commands (default `gpfs`).
- **`storage_path`** — local mount point where the filesets appear
  (default `/gpfs`).
- **`home_path`** — base path for home directories (default `/home`).

### Components

The plugin manages a single component, **`storage`**, which must be present
in `backend_components`:

- **`unit_factor`** — bytes per `measured_unit`. Ordered limits are
  multiplied by it before being applied to GPFS, and usage read from GPFS
  (in KB, converted to bytes) is divided by it before being reported.
- **`limit`** — not used as a fallback by this plugin: the order itself
  must carry a positive `storage` limit, and zero-quota resources are
  not allowed (creation and limit updates without a positive limit are
  rejected).

## Standalone Scripts

Privileged operations run through three entry points of this package,
installed to `/usr/local/bin` and invoked by the agent with `sudo`. Each
script loads `/etc/waldur/waldur-site-agent-config.yaml` and picks the
offering whose `backend_type` is `sofia_storage`:

| Script | Arguments | Action |
| ------ | --------- | ------ |
| `waldur_make_homedir_vsc` | `<username>` | Create the home directory of a VSC user (username must start with `vsc`) |
| `waldur_make_project_vsc` | `<project_dir> <owner_uid> <owner_gid>` | Create the fileset and set ownership (mode `0770`) |
| `waldur_set_project_quota` | `<project_dir> <block_limit>` | Set the fileset block quota (inode soft limit 1M) |

## GPFS Commands Used

- Fileset creation, ownership and quota setting: VSC `GpfsOperations`
  interface (`vsc.filesystem.gpfs`)
- `sudo /usr/lpp/mmfs/bin/mmlsquota` — `-Y -j <fileset>` for the fileset
  quota/usage, `-Y -u <user> <fs>:<fileset>` for a user's quota/usage in a
  fileset
- `sudo getent group <group>` — members of the project's VSC group, read
  from the local name service instead of querying the VSC account page

## Requirements

The agent must run on a host that:

- has the GPFS client installed (`/usr/lpp/mmfs/bin`) and the scratch
  filesystem mounted at `storage_path`
- allows the agent user to run, without a password:
  - `sudo /usr/local/bin/waldur_make_homedir_vsc *`
  - `sudo /usr/local/bin/waldur_make_project_vsc *`
  - `sudo /usr/local/bin/waldur_set_project_quota *`
  - `sudo /usr/lpp/mmfs/bin/mmlsquota *`
  - `sudo getent group *`
- can resolve VSC identities (UID lookups for home directories)
- can reach the VSC account page (`https://account.vscentrum.be/django/api`)

## Package Structure

```
plugins/sofia-storage/
├── pyproject.toml
├── README.md
└── waldur_site_agent_sofia_storage/
    ├── __init__.py
    ├── backend.py      # SofiaStorageBackend (BaseBackend implementation)
    ├── client.py       # SofiaStorageClient (GPFS via VSC GpfsOperations + CLI)
    ├── vsc.py          # VscBackend (VSC account page group management)
    ├── mkhomedir.py    # waldur_make_homedir_vsc entry point
    ├── mkprojdir.py    # waldur_make_project_vsc entry point
    └── setprojquota.py # waldur_set_project_quota entry point
```

## Development

```bash
# from the repository root
uv sync --all-packages

# verify the import
uv run python -c "from waldur_site_agent_sofia_storage.backend import SofiaStorageBackend; print('Import OK')"
```

There is currently no test suite for this plugin.
