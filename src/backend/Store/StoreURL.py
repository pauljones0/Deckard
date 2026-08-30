"""
Author: Core447
Year: 2026

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
any later version.

This programm comes with ABSOLUTELY NO WARRANTY!

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
"""

from typing import NamedTuple

# Parse both catalog URLs and their raw-content form.
_REPO_DOMAINS = ("github.com", "raw.githubusercontent.com")


class RepoRef(NamedTuple):
    """The owner and repository identity of a store URL."""
    user: str
    repo: str


def parse_repo_url(repo_url: object) -> RepoRef | None:
    """Parse a complete GitHub or raw-content repository URL without raising."""
    if not isinstance(repo_url, str):
        return None

    segments = repo_url.split("/")
    for domain in _REPO_DOMAINS:
        if domain in segments:
            index = segments.index(domain)
            break
    else:
        return None

    if index + 2 >= len(segments):
        return None

    user = segments[index + 1]
    repo = segments[index + 2]
    if not user or not repo:
        return None

    return RepoRef(user=user, repo=repo)
