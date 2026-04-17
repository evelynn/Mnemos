"""Thin wrapper around python-gitlab.

Phase 1 only needs project search and MR creation; both land in later weeks.
The settings-driven client lives here so future modules import from a stable path.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gitlab import Gitlab


def make_client(base_url: str, token: str) -> "Gitlab":
    from gitlab import Gitlab

    return Gitlab(url=base_url, private_token=token, ssl_verify=True)
