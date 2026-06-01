#!/usr/bin/env python3
"""Fetch source repos from a local repo manifest and optional Maven API dependencies.

This setup step intentionally avoids GitLab API credentials. It first discovers
already-accessible local git checkouts under the standard source roots and
writes a TSV repo list. It then clones/fetches those remotes into a target
project directory, preserving each repo's group-relative path.

Python and flat script repositories are copied as ordinary git repos. Maven API
dependency collection is optional and only applies to Java/Maven roots.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence


DEFAULT_SOURCE_ROOTS = [
    "~/src",
    "~/work",
]

DEFAULT_GROUP_IDS = ["com.example"]
REPO_LIST_HEADER = "# group\trelative_path\tgit_url\tsource_path"


@dataclass(frozen=True)
class RepoEntry:
    group: str
    relative_path: str
    git_url: str
    source_path: str

    @property
    def destination_parts(self) -> List[str]:
        return [self.group] + [part for part in self.relative_path.split("/") if part]


def run(cmd: Sequence[str], *, cwd: Optional[Path] = None, dry_run: bool = False) -> int:
    print("+ " + " ".join(str(part) for part in cmd))
    if dry_run:
        return 0
    return subprocess.run(list(cmd), cwd=str(cwd) if cwd else None, check=False).returncode


def capture(cmd: Sequence[str], *, cwd: Optional[Path] = None) -> Optional[str]:
    result = subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def default_group_for_root(root: Path) -> str:
    return root.expanduser().resolve().name


def find_git_repos(root: Path) -> Iterator[Path]:
    root = root.expanduser().resolve()
    if not root.exists():
        return
    for git_dir in sorted(root.rglob(".git")):
        if git_dir.is_dir():
            yield git_dir.parent.resolve()


def repo_remote_url(repo_path: Path) -> str:
    remote = capture(["git", "config", "--get", "remote.origin.url"], cwd=repo_path)
    return remote or str(repo_path)


def repo_relative_path(source_root: Path, repo_path: Path) -> str:
    rel = repo_path.resolve().relative_to(source_root.expanduser().resolve())
    return rel.as_posix() or repo_path.name


def discover_repo_entries(source_roots: Sequence[str]) -> List[RepoEntry]:
    entries: List[RepoEntry] = []
    seen: set[tuple[str, str]] = set()
    for raw_root in source_roots:
        root = Path(raw_root).expanduser().resolve()
        group = default_group_for_root(root)
        for repo in find_git_repos(root):
            relative = repo_relative_path(root, repo)
            key = (group, relative)
            if key in seen:
                continue
            seen.add(key)
            entries.append(
                RepoEntry(
                    group=group,
                    relative_path=relative,
                    git_url=repo_remote_url(repo),
                    source_path=str(repo),
                )
            )
    return sorted(entries, key=lambda item: (item.group, item.relative_path))


def write_repo_list(path: Path, entries: Sequence[RepoEntry], *, dry_run: bool = False) -> None:
    lines = [REPO_LIST_HEADER]
    lines.extend(
        "\t".join([entry.group, entry.relative_path, entry.git_url, entry.source_path])
        for entry in entries
    )
    print(f"write repo list {path} ({len(entries)} repos)")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_repo_list_line(line: str, *, line_no: int) -> Optional[RepoEntry]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = stripped.split("\t")
    if len(parts) != 4:
        raise ValueError(f"Invalid repo list line {line_no}: expected 4 tab-separated fields")
    group, relative_path, git_url, source_path = [part.strip() for part in parts]
    if not group or not relative_path or not git_url:
        raise ValueError(f"Invalid repo list line {line_no}: group, relative_path and git_url are required")
    return RepoEntry(group=group, relative_path=relative_path, git_url=git_url, source_path=source_path)


def read_repo_list(path: Path) -> List[RepoEntry]:
    entries: List[RepoEntry] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        entry = parse_repo_list_line(line, line_no=line_no)
        if entry:
            entries.append(entry)
    return entries


def entry_looks_like_api_repo(entry: RepoEntry) -> bool:
    path_parts = [part.lower() for part in entry.relative_path.split("/") if part]
    repo_name = path_parts[-1] if path_parts else entry.relative_path.lower()
    return "api" in path_parts or "api" in repo_name


def repo_list_has_api_repos(entries: Sequence[RepoEntry]) -> bool:
    return any(entry_looks_like_api_repo(entry) for entry in entries)


def destination_for_entry(project_dir: Path, entry: RepoEntry) -> Path:
    destination = project_dir
    parts = entry.destination_parts
    if entry_looks_like_api_repo(entry) and len(parts) > 1 and parts[1].lower() != "api":
        parts = [parts[0], "api"] + parts[1:]
    for part in parts:
        destination /= part
    return destination


def fetch_repo(
    entry: RepoEntry,
    destination: Path,
    *,
    depth: Optional[int],
    reset_existing: bool,
    dry_run: bool,
) -> None:
    if (destination / ".git").exists():
        run(["git", "fetch", "--all", "--prune"], cwd=destination, dry_run=dry_run)
        if reset_existing:
            default_branch = capture(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=destination)
            branch = (default_branch or "origin/master").split("/", 1)[-1]
            run(["git", "checkout", branch], cwd=destination, dry_run=dry_run)
            run(["git", "reset", "--hard", f"origin/{branch}"], cwd=destination, dry_run=dry_run)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone"]
    if depth and depth > 0:
        cmd.extend(["--depth", str(depth)])
    cmd.extend([entry.git_url, str(destination)])
    run(cmd, dry_run=dry_run)


def fetch_repos(
    entries: Sequence[RepoEntry],
    *,
    project_dir: Path,
    depth: Optional[int],
    reset_existing: bool,
    dry_run: bool,
) -> List[Path]:
    destinations: List[Path] = []
    for entry in entries:
        destination = destination_for_entry(project_dir, entry)
        fetch_repo(entry, destination, depth=depth, reset_existing=reset_existing, dry_run=dry_run)
        destinations.append(destination)
    return destinations


def iter_maven_roots(search_roots: Iterable[Path]) -> Iterator[Path]:
    seen: set[Path] = set()
    for search_root in search_roots:
        if not search_root.exists():
            continue
        for pom in search_root.rglob("pom.xml"):
            root = pom.parent.resolve()
            if root in seen:
                continue
            if any(parent in seen for parent in root.parents):
                continue
            seen.add(root)
            yield root


def jar_is_internal_api(path: Path, include_group_ids: Sequence[str]) -> bool:
    name = path.name.lower()
    if "api" not in name or path.suffix != ".jar":
        return False
    text = str(path).replace("\\", "/")
    return any(group_id.replace(".", "/") in text or group_id in path.name for group_id in include_group_ids)


def copy_internal_api_jars(temp_dir: Path, api_dir: Path, include_group_ids: Sequence[str], *, dry_run: bool) -> int:
    copied = 0
    api_dir.mkdir(parents=True, exist_ok=True)
    for jar in sorted(temp_dir.rglob("*.jar")):
        if not jar_is_internal_api(jar, include_group_ids):
            continue
        target = api_dir / jar.name
        if target.exists() and target.stat().st_size == jar.stat().st_size:
            continue
        print(f"copy {jar} -> {target}")
        if not dry_run:
            shutil.copy2(jar, target)
        copied += 1
    return copied


def fetch_maven_api_jars(
    maven_roots: Iterable[Path],
    *,
    api_dir: Path,
    settings_xml: Path,
    maven_binary: str,
    maven_profiles: Sequence[str],
    include_group_ids: Sequence[str],
    dry_run: bool,
) -> None:
    api_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uta-api-deps-") as tmp:
        tmp_root = Path(tmp)
        for root in maven_roots:
            output_dir = tmp_root / root.name
            cmd = [maven_binary, "-B", "-s", str(settings_xml)]
            if maven_profiles:
                cmd.extend(["-P", ",".join(maven_profiles)])
            cmd.extend(
                [
                    "dependency:copy-dependencies",
                    "-DincludeGroupIds=" + ",".join(include_group_ids),
                    "-DexcludeTransitive=false",
                    "-DincludeScope=compile",
                    "-Dmdep.prependGroupId=true",
                    f"-DoutputDirectory={output_dir}",
                ]
            )
            code = run(cmd, cwd=root, dry_run=dry_run)
            if code != 0:
                print(f"maven dependency copy failed in {root} with exit {code}", file=sys.stderr)
                continue
            copied = copy_internal_api_jars(output_dir, api_dir, include_group_ids, dry_run=dry_run)
            print(f"{root}: copied {copied} internal API jar(s)")


def write_manifest(path: Path, payload: Dict[str, object], *, dry_run: bool) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True)
    print(f"write manifest {path}")
    if dry_run:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-root",
        action="append",
        default=[],
        help="Local source root to scan for git repos. Repeatable. Defaults to ~/src and ~/work.",
    )
    parser.add_argument("--project-dir", "--output-root", default="external-source", help="Target directory for fetched source repos.")
    parser.add_argument("--repo-list", default=None, help="TSV repo list path. Defaults to PROJECT_DIR/repo.txt.")
    parser.add_argument("--refresh-repo-list", action="store_true", help="Regenerate repo.txt from local source roots before fetching.")
    parser.add_argument("--use-existing-repo-list", action="store_true", help="Remote deploy mode: require repo.txt and do not scan local source roots.")
    parser.add_argument(
        "--generate-repo-list-only",
        "--scan-only",
        action="store_true",
        help="Development-env mode: scan local source roots and only generate repo.txt; do not fetch repos or Maven jars.",
    )
    parser.add_argument("--depth", type=int, default=1, help="git clone depth. Use 0 for full clones.")
    parser.add_argument("--reset-existing", action="store_true", help="Reset existing fetched repos to origin default branch.")
    parser.add_argument("--skip-git", action="store_true", help="Skip git clone/fetch from repo.txt.")
    parser.add_argument("--skip-maven", action="store_true", help="Skip Maven dependency jar download. Use this for Python-only or flat-script repo setup.")
    parser.add_argument("--force-maven", action="store_true", help="Run Maven dependency fetch even when repo.txt already includes API repos.")
    parser.add_argument("--api-dir", default=None, help="Directory for copied internal API jars. Defaults to PROJECT_DIR/api.")
    parser.add_argument("--mvn", default="mvn", help="Maven executable.")
    parser.add_argument("--settings", default="~/.m2/settings.xml", help="Maven settings.xml path.")
    parser.add_argument("--maven-profile", action="append", default=[], help="Maven profile to activate. Repeatable.")
    parser.add_argument(
        "--include-group-id",
        action="append",
        default=[],
        help="Maven groupId to copy. Defaults to com.example.",
    )
    parser.add_argument("--manifest", default=None, help="Write JSON manifest. Defaults to PROJECT_DIR/setup-fetchcode-manifest.json.")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing files.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    source_roots = args.source_root or DEFAULT_SOURCE_ROOTS
    include_group_ids = args.include_group_id or DEFAULT_GROUP_IDS
    project_dir = Path(args.project_dir).expanduser().resolve()
    repo_list_path = Path(args.repo_list).expanduser().resolve() if args.repo_list else project_dir / "repo.txt"
    api_dir = Path(args.api_dir).expanduser().resolve() if args.api_dir else project_dir / "api"
    manifest_path = Path(args.manifest).expanduser().resolve() if args.manifest else project_dir / "setup-fetchcode-manifest.json"
    settings_xml = Path(args.settings).expanduser().resolve()

    if args.use_existing_repo_list and (args.refresh_repo_list or args.generate_repo_list_only):
        print("--use-existing-repo-list cannot be combined with repo-list generation flags", file=sys.stderr)
        return 2

    if args.use_existing_repo_list and not repo_list_path.exists():
        print(f"repo list does not exist: {repo_list_path}", file=sys.stderr)
        return 2

    should_generate = (
        not args.use_existing_repo_list
        and (args.refresh_repo_list or args.generate_repo_list_only or not repo_list_path.exists())
    )
    if should_generate:
        entries = discover_repo_entries(source_roots)
        write_repo_list(repo_list_path, entries, dry_run=args.dry_run)
    else:
        entries = read_repo_list(repo_list_path)

    if args.generate_repo_list_only:
        return 0

    api_repos_present = repo_list_has_api_repos(entries)
    effective_skip_maven = bool(args.skip_maven or (api_repos_present and not args.force_maven))
    if api_repos_present and not args.skip_maven and not args.force_maven:
        print("repo.txt includes API repos; skipping Maven API dependency fetch (use --force-maven to override)")

    if not effective_skip_maven and not settings_xml.exists():
        print(f"Maven settings file not found: {settings_xml}", file=sys.stderr)
        return 2

    fetched_roots: List[Path]
    if args.skip_git:
        fetched_roots = [destination_for_entry(project_dir, entry) for entry in entries]
    else:
        fetched_roots = fetch_repos(
            entries,
            project_dir=project_dir,
            depth=args.depth if args.depth > 0 else None,
            reset_existing=args.reset_existing,
            dry_run=args.dry_run,
        )
    missing_repos = [
        {
            "group": entry.group,
            "relative_path": entry.relative_path,
            "git_url": entry.git_url,
            "destination": str(destination_for_entry(project_dir, entry)),
        }
        for entry in entries
        if not args.dry_run and not (destination_for_entry(project_dir, entry) / ".git").exists()
    ]

    manifest: Dict[str, object] = {
        "project_dir": str(project_dir),
        "repo_list": str(repo_list_path),
        "api_dir": str(api_dir),
        "include_group_ids": include_group_ids,
        "repo_count": len(entries),
        "api_repos_present": api_repos_present,
        "maven_dependency_fetch_skipped": effective_skip_maven,
        "missing_repo_count": len(missing_repos),
        "missing_repos": missing_repos,
        "groups": sorted({entry.group for entry in entries}),
        "repos": [
            {
                "group": entry.group,
                "relative_path": entry.relative_path,
                "git_url": entry.git_url,
                "source_path": entry.source_path,
                "destination": str(destination_for_entry(project_dir, entry)),
            }
            for entry in entries
        ],
    }

    if not effective_skip_maven:
        maven_roots = list(iter_maven_roots(fetched_roots))
        print(f"found {len(maven_roots)} Maven root(s)")
        fetch_maven_api_jars(
            maven_roots,
            api_dir=api_dir,
            settings_xml=settings_xml,
            maven_binary=args.mvn,
            maven_profiles=args.maven_profile,
            include_group_ids=include_group_ids,
            dry_run=args.dry_run,
        )
        manifest["maven_roots"] = [str(path) for path in maven_roots]

    write_manifest(manifest_path, manifest, dry_run=args.dry_run)
    if missing_repos:
        print(f"{len(missing_repos)} repo(s) missing after fetch; see {manifest_path}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
