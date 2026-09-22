"""The Java-side quarantine port and stable repository identity."""
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from uta.enforcement.enforcement import git_output


def repository_key(repo: Path) -> str:
    remote = git_output(repo, 'remote', 'get-url', 'origin').strip()
    if not remote:
        return str(repo.resolve())
    if '://' in remote:
        parsed = urlsplit(remote)
        remote = (parsed.hostname or '') + '/' + parsed.path.lstrip('/')
    else:
        remote = remote.split('@')[-1].replace(':', '/', 1)
    return remote.removesuffix('.git').rstrip('/')


class HangingTestQuarantineStore(Protocol):
    def active(self, repo_slug: str, ttl_days: int, *, now=None) -> list[str]: ...

    def observe(
        self,
        repo_slug: str,
        test_class: str,
        module: str,
        *,
        now=None,
    ) -> None: ...
