"""Sofia Storage backend plugin for Waldur Site Agent."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth: the version in pyproject.toml, exposed through
    # the installed distribution's metadata.
    __version__ = version("waldur-site-agent-sofia-storage")
except PackageNotFoundError:
    # Source checkout without installation (e.g. tests run from the tree)
    __version__ = "0.0.0"
