"""
version.py - Single source of truth for BrainrotFilter version information.

Imported by analyzer_service.py for the /version endpoint and admin panel,
and by setup/install scripts to stamp built packages.
"""

__version__ = "1.0.0"
__version_info__ = (1, 0, 0)
__release_date__ = "2026-04-12"

# Human-readable name shown in the admin UI header
__app_name__ = "BrainrotFilter"

# Minimum supported platform
__platform__ = "Linux (Debian/Ubuntu)"


def get_version() -> str:
    """Return the full version string."""
    return __version__


def get_version_tuple() -> tuple:
    """Return version as a (major, minor, patch) tuple."""
    return __version_info__


def get_build_info() -> dict:
    """Return a dict of version metadata for the /version API endpoint."""
    return {
        "version": __version__,
        "version_info": list(__version_info__),
        "release_date": __release_date__,
        "app_name": __app_name__,
        "platform": __platform__,
    }
