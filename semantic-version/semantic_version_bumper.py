#!/usr/bin/env python3
"""Calculate and optionally reserve GitLab semantic versions.

Single-file, standard-library-only utility for GitLab CI and local use.

Highlights:
- Module-scoped tag parsing, default tag template: ``{module}/v{version}``.
- Patch, release-candidate, snapshot, and explicit target-version modes.
- GitLab API tag discovery without fetching full tag sets.
- Optional release reservation through GitLab Releases API using ``CI_JOB_TOKEN``.

Security notes:
- Tokens are read from environment variables only.
- Token values and raw API response bodies are not printed in normal output.
- ``CI_JOB_TOKEN`` uses the ``JOB-TOKEN`` header; PAT/project/group tokens use
  ``PRIVATE-TOKEN``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from functools import total_ordering
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.(0|[1-9]\d*))?$")
RELEASE_BRANCH_RE = re.compile(r"^(?:release|rc|release-candidate)(?:/(\d+)\.(\d+))?$")
SAFE_ENV_RE = re.compile(r"^[A-Za-z0-9_./:+@-]+$")


class SemanticVersionError(ValueError):
    """Raised for invalid semantic version input."""


class GitLabError(RuntimeError):
    """Raised for GitLab API failures."""


class GitLabConflict(GitLabError):
    """Raised for GitLab API conflict responses."""


class ConfigError(ValueError):
    """Raised for invalid CLI/configuration combinations."""


@total_ordering
class SemVer:
    __slots__ = ("major", "minor", "patch", "rc")

    def __init__(self, major: int, minor: int, patch: int, rc: int | None = None) -> None:
        self.major = major
        self.minor = minor
        self.patch = patch
        self.rc = rc

    @classmethod
    def parse(cls, value: str) -> "SemVer":
        match = VERSION_RE.match(value)
        if not match:
            raise SemanticVersionError(f"invalid SemVer value: {value}")
        major, minor, patch, rc = match.groups()
        return cls(int(major), int(minor), int(patch), int(rc) if rc is not None else None)

    @property
    def stable(self) -> bool:
        return self.rc is None

    def _cmp_key(self) -> tuple[int, int, int, int, int]:
        # Stable versions sort after prereleases of the same base.
        return (self.major, self.minor, self.patch, 1 if self.rc is None else 0, self.rc or 0)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return NotImplemented
        return self._cmp_key() < other._cmp_key()

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SemVer):
            return False
        return self._cmp_key() == other._cmp_key()

    def __str__(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        if self.rc is None:
            return base
        return f"{base}-rc.{self.rc}"

    def __repr__(self) -> str:
        return f"SemVer.parse({str(self)!r})"


class Config:
    def __init__(
        self,
        module: str,
        mode: str = "auto",
        branch: str | None = None,
        tags: list[str] | None = None,
        tag_template: str = "{module}/v{version}",
        initial_version: str = "0.0.0",
        target_version: str | None = None,
        pipeline_iid: str = "0",
        commit_short_sha: str = "0000000",
        snapshot_template: str = "{base_version}-snapshot.{pipeline_iid}.{commit_short_sha}",
        snapshot_include_branch: bool = False,
        release_scope: str = "module",
        train_tag_template: str = "release/v{version}",
    ) -> None:
        self.module = module
        self.mode = mode
        self.branch = branch or "main"
        self.tags = tags or []
        self.tag_template = tag_template
        self.initial_version = initial_version
        self.target_version = target_version
        self.pipeline_iid = pipeline_iid
        self.commit_short_sha = commit_short_sha
        self.snapshot_template = snapshot_template
        self.snapshot_include_branch = snapshot_include_branch
        self.release_scope = release_scope
        self.train_tag_template = train_tag_template


class VersionResult:
    def __init__(
        self,
        module: str,
        mode: str,
        current_version: str | None,
        next_version: str,
        tag: str | None,
        reserved: bool = False,
        reused: bool = False,
    ) -> None:
        self.module = module
        self.mode = mode
        self.current_version = current_version
        self.next_version = next_version
        self.tag = tag
        self.reserved = reserved
        self.reused = reused

    def as_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "mode": self.mode,
            "current_version": self.current_version,
            "next_version": self.next_version,
            "tag": self.tag,
            "reserved": self.reserved,
            "reused": self.reused,
        }


class ReservationResult:
    def __init__(self, created: bool, reused: bool, tag: str, ref: str) -> None:
        self.created = created
        self.reused = reused
        self.tag = tag
        self.ref = ref


class GitLabClient:
    def __init__(self, api_url: str, project_id: str, token: str, auth_mode: str = "job-token", timeout: int = 30) -> None:
        if auth_mode not in {"job-token", "private-token"}:
            raise ConfigError("auth_mode must be job-token or private-token")
        self.api_url = api_url.rstrip("/")
        self.project_id = project_id
        self.token = token
        self.auth_mode = auth_mode
        self.timeout = timeout

    @staticmethod
    def quote_segment(value: str) -> str:
        return urllib.parse.quote(str(value), safe="")

    @property
    def project_path(self) -> str:
        return f"/projects/{self.quote_segment(self.project_id)}"

    def build_request(
        self,
        path: str,
        method: str = "GET",
        query: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> urllib.request.Request:
        url = self.api_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        encoded = urllib.parse.urlencode(data or {}).encode("utf-8") if data is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.auth_mode == "job-token":
            headers["JOB-TOKEN"] = self.token
        else:
            headers["PRIVATE-TOKEN"] = self.token
        return urllib.request.Request(url, data=encoded, headers=headers, method=method)

    def request(
        self,
        path: str,
        method: str = "GET",
        query: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> Any:
        request = self.build_request(path, method=method, query=query, data=data)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code == 409:
                raise GitLabConflict("GitLab API conflict") from exc
            raise GitLabError(f"GitLab API request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise GitLabError("GitLab API request failed") from exc
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise GitLabError("GitLab API returned invalid JSON") from exc

    def list_tags(self, search_prefix: str) -> list[str]:
        tags: list[str] = []
        page = 1
        while True:
            query = {
                "search": search_prefix,
                "order_by": "version",
                "sort": "desc",
                "per_page": "100",
                "page": str(page),
            }
            request = self.build_request(f"{self.project_path}/repository/tags", query=query)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8") or "[]")
                    headers = dict(response.getheaders())
            except HTTPError as exc:
                raise GitLabError(f"GitLab tag listing failed with HTTP {exc.code}") from exc
            except (URLError, json.JSONDecodeError) as exc:
                raise GitLabError("GitLab tag listing failed") from exc
            if not isinstance(payload, list):
                raise GitLabError("GitLab tag listing returned unexpected payload")
            for item in payload:
                if isinstance(item, dict) and isinstance(item.get("name"), str):
                    tags.append(item["name"])
            next_page = headers.get("X-Next-Page") or headers.get("x-next-page") or ""
            if not next_page:
                break
            page = int(next_page)
        return tags

    def get_current_commit_tags(self, sha: str) -> list[str]:
        payload = self.request(f"{self.project_path}/repository/commits/{self.quote_segment(sha)}/refs", query={"type": "tag"})
        if payload is None:
            return []
        if not isinstance(payload, list):
            raise GitLabError("commit refs payload was not a list")
        return [item["name"] for item in payload if isinstance(item, dict) and isinstance(item.get("name"), str)]

    def get_tag(self, tag_name: str) -> dict[str, Any] | None:
        payload = self.request(f"{self.project_path}/repository/tags/{self.quote_segment(tag_name)}")
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise GitLabError("tag payload was not an object")
        return payload

    def get_release(self, tag_name: str) -> dict[str, Any] | None:
        payload = self.request(f"{self.project_path}/releases/{self.quote_segment(tag_name)}")
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise GitLabError("release payload was not an object")
        return payload

    def create_release(
        self,
        tag_name: str,
        ref: str,
        name: str,
        description: str,
        tag_message: str | None = None,
    ) -> dict[str, Any]:
        data = {"tag_name": tag_name, "ref": ref, "name": name, "description": description}
        if tag_message:
            data["tag_message"] = tag_message
        payload = self.request(f"{self.project_path}/releases", method="POST", data=data)
        if not isinstance(payload, dict):
            raise GitLabError("release creation payload was not an object")
        return payload

    @staticmethod
    def tag_target(tag: dict[str, Any] | None) -> str | None:
        if not tag:
            return None
        if isinstance(tag.get("target"), str):
            return tag["target"]
        commit = tag.get("commit")
        if isinstance(commit, dict) and isinstance(commit.get("id"), str):
            return commit["id"]
        return None

    def reserve_release_tag(self, tag_name: str, ref: str, name: str, description: str, if_exists: str = "fail") -> ReservationResult:
        current_tags = self.get_current_commit_tags(ref)
        if tag_name in current_tags:
            tag = self.get_tag(tag_name)
            target = self.tag_target(tag)
            if target in {ref, None}:
                return ReservationResult(created=False, reused=True, tag=tag_name, ref=ref)
        tag = self.get_tag(tag_name)
        target = self.tag_target(tag)
        if target == ref:
            return ReservationResult(created=False, reused=True, tag=tag_name, ref=ref)
        if target and target != ref:
            raise GitLabError(f"tag {tag_name} already exists on a different ref")
        if if_exists == "skip" and tag is not None:
            return ReservationResult(created=False, reused=True, tag=tag_name, ref=ref)
        try:
            self.create_release(tag_name, ref, name, description)
            return ReservationResult(created=True, reused=False, tag=tag_name, ref=ref)
        except GitLabConflict:
            reread = self.get_tag(tag_name)
            if self.tag_target(reread) == ref:
                return ReservationResult(created=False, reused=True, tag=tag_name, ref=ref)
            raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Calculate and optionally reserve semantic versions for GitLab CI.")
    parser.add_argument("--module", help="Logical module name used to filter module-scoped tags.")
    parser.add_argument("--mode", choices=["auto", "patch", "rc", "snapshot"], default="auto")
    parser.add_argument("--branch", default=None)
    parser.add_argument("--tag-template", default="{module}/v{version}")
    parser.add_argument("--initial-version", default="0.0.0")
    parser.add_argument("--target-version")
    parser.add_argument("--write-env")
    parser.add_argument("--snapshot-template", default="{base_version}-snapshot.{pipeline_iid}.{commit_short_sha}")
    parser.add_argument("--snapshot-include-branch", action="store_true")
    parser.add_argument("--pipeline-iid")
    parser.add_argument("--commit-short-sha")
    parser.add_argument("--release-scope", choices=["module", "train"], default="module")
    parser.add_argument("--modules-file")
    parser.add_argument("--train-tag-template", default="release/v{version}")
    parser.add_argument("--if-exists", choices=["skip", "fail"], default="fail")
    parser.add_argument("--fetch-tags", action="store_true")
    parser.add_argument("--tag-source", choices=["git", "gitlab-api", "ls-remote", "none"], default=None)
    parser.add_argument("--gitlab-token-var", default=None)
    parser.add_argument("--gitlab-auth", choices=["auto", "job-token", "private-token"], default="auto")
    parser.add_argument("--api-url", default=None)
    parser.add_argument("--project-id", default=None)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--reserve-release", action="store_true")
    parser.add_argument("--release-ref")
    parser.add_argument("--release-name-template", default="{tag}")
    parser.add_argument("--release-description-file")
    parser.add_argument("--tag-mutation", choices=["release-api", "git-push", "none"], default="release-api")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--self-test", action="store_true")
    return parser


def run_git(args: list[str], repo: str | Path = ".") -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return result.stdout


def list_git_tags(repo: str | Path = ".") -> list[str]:
    output = run_git(["tag", "--list"], repo)
    return [line.strip() for line in output.splitlines() if line.strip()]


def fetch_tags(repo: str | Path = ".") -> None:
    run_git(["fetch", "--tags"], repo)


def list_ls_remote_tags(repo: str | Path = ".") -> list[str]:
    output = run_git(["ls-remote", "--tags", "origin"], repo)
    tags = []
    for line in output.splitlines():
        if "refs/tags/" in line and not line.endswith("^{}"):
            tags.append(line.rsplit("refs/tags/", 1)[1])
    return tags


def current_branch(repo: str | Path = ".") -> str:
    for name in ("CI_COMMIT_REF_NAME", "GITHUB_REF_NAME"):
        if os.environ.get(name):
            return os.environ[name]
    with contextlib.suppress(Exception):
        return run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo).strip()
    return "main"


def current_short_sha(repo: str | Path = ".") -> str:
    if os.environ.get("CI_COMMIT_SHORT_SHA"):
        return os.environ["CI_COMMIT_SHORT_SHA"]
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"][:8]
    with contextlib.suppress(Exception):
        return run_git(["rev-parse", "--short", "HEAD"], repo).strip()
    return "0000000"


def current_ref(repo: str | Path = ".") -> str:
    if os.environ.get("CI_COMMIT_SHA"):
        return os.environ["CI_COMMIT_SHA"]
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    with contextlib.suppress(Exception):
        return run_git(["rev-parse", "HEAD"], repo).strip()
    return "HEAD"


def extract_version_from_tag(tag: str, module_name: str, tag_template: str) -> SemVer | None:
    marker = "___VERSION___"
    template = tag_template.replace("{module}", "___MODULE___").replace("{version}", marker)
    pattern = re.escape(template)
    pattern = pattern.replace(re.escape("___MODULE___"), re.escape(module_name))
    pattern = pattern.replace(re.escape(marker), r"(?P<version>\d+\.\d+\.\d+(?:-rc\.\d+)?)")
    match = re.match(f"^{pattern}$", tag)
    if not match:
        return None
    try:
        return SemVer.parse(match.group("version"))
    except SemanticVersionError:
        return None


def build_tag(module_name: str, version: SemVer | str, tag_template: str) -> str:
    return tag_template.replace("{module}", module_name).replace("{version}", str(version))


def parse_versions(tags: list[str], module_name: str, tag_template: str) -> list[SemVer]:
    versions = []
    for tag in tags:
        version = extract_version_from_tag(tag, module_name, tag_template)
        if version is not None:
            versions.append(version)
    return versions


def latest_stable(versions: list[SemVer], initial_version: SemVer) -> SemVer:
    stable_versions = [version for version in versions if version.stable]
    return max(stable_versions) if stable_versions else initial_version


def next_patch_version(versions: list[SemVer], initial_version: SemVer) -> SemVer:
    current = latest_stable(versions, initial_version)
    return SemVer(current.major, current.minor, current.patch + 1)


def extract_major_minor_from_branch(branch: str | None) -> tuple[int, int] | None:
    if not branch:
        return None
    match = RELEASE_BRANCH_RE.match(branch)
    if not match or match.group(1) is None:
        return None
    return int(match.group(1)), int(match.group(2))


def resolve_mode(mode: str, branch: str | None) -> str:
    if mode != "auto":
        return mode
    if branch and RELEASE_BRANCH_RE.match(branch):
        return "rc"
    return "patch"


def next_rc_version(versions: list[SemVer], initial_version: SemVer, branch: str | None) -> SemVer:
    branch_line = extract_major_minor_from_branch(branch)
    if branch_line:
        major, minor = branch_line
    else:
        stable = latest_stable(versions, initial_version)
        major, minor = stable.major, stable.minor + 1
    rc_versions = [v for v in versions if v.major == major and v.minor == minor and v.patch == 0 and v.rc is not None]
    next_rc = (max(v.rc for v in rc_versions if v.rc is not None) + 1) if rc_versions else 1
    return SemVer(major, minor, 0, next_rc)


def sanitize_prerelease_identifier(value: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z-]+", "-", value).strip("-").lower()
    return sanitized or "branch"


def resolve_snapshot_base(versions: list[SemVer], initial_version: SemVer, branch: str | None) -> str:
    branch_line = extract_major_minor_from_branch(branch)
    if branch_line:
        return str(SemVer(branch_line[0], branch_line[1], 0))
    return str(next_patch_version(versions, initial_version))


def next_snapshot_version(
    versions: list[SemVer],
    initial_version: SemVer,
    branch: str | None,
    pipeline_iid: str,
    commit_short_sha: str,
    include_branch: bool,
    template: str = "{base_version}-snapshot.{pipeline_iid}.{commit_short_sha}",
) -> str:
    base = resolve_snapshot_base(versions, initial_version, branch)
    branch_slug = sanitize_prerelease_identifier(branch or "branch")
    if include_branch and "{branch}" not in template:
        template = "{base_version}-snapshot.{branch}.{pipeline_iid}.{commit_short_sha}"
    return template.format(
        base_version=base,
        pipeline_iid=pipeline_iid,
        commit_short_sha=commit_short_sha,
        branch=branch_slug,
    )


def calculate_next_version(config: Config) -> VersionResult:
    tag_template = config.train_tag_template if config.release_scope == "train" else config.tag_template
    versions = parse_versions(config.tags, config.module, tag_template)
    mode = resolve_mode(config.mode, config.branch)
    initial = SemVer.parse(config.initial_version)
    current = latest_stable(versions, initial) if versions else None
    if config.target_version:
        next_version_value = str(SemVer.parse(config.target_version))
    elif mode == "patch":
        next_version_value = str(next_patch_version(versions, initial))
    elif mode == "rc":
        next_version_value = str(next_rc_version(versions, initial, config.branch))
    elif mode == "snapshot":
        next_version_value = next_snapshot_version(
            versions,
            initial,
            config.branch,
            config.pipeline_iid,
            config.commit_short_sha,
            config.snapshot_include_branch,
            config.snapshot_template,
        )
    else:
        raise ConfigError(f"unsupported mode: {mode}")
    tag = None if mode == "snapshot" else build_tag(config.module, next_version_value, tag_template)
    return VersionResult(config.module, mode, str(current) if current else None, next_version_value, tag)


def default_tag_source(args: argparse.Namespace) -> str:
    if args.tag_source:
        return args.tag_source
    if os.environ.get("CI_API_V4_URL") and os.environ.get("CI_PROJECT_ID"):
        return "gitlab-api"
    return "git"


def resolve_gitlab_token_var(args: argparse.Namespace) -> str:
    if args.gitlab_token_var:
        return args.gitlab_token_var
    if os.environ.get("CI_JOB_TOKEN"):
        return "CI_JOB_TOKEN"
    return "GITLAB_TOKEN"


def resolve_gitlab_auth(auth: str, token_var: str) -> str:
    if auth != "auto":
        return auth
    return "job-token" if token_var == "CI_JOB_TOKEN" else "private-token"


def make_gitlab_client(args: argparse.Namespace) -> GitLabClient:
    api_url = args.api_url or os.environ.get("CI_API_V4_URL") or "https://gitlab.com/api/v4"
    project_id = args.project_id or os.environ.get("CI_PROJECT_ID")
    if not project_id:
        raise ConfigError("GitLab project id is required; pass --project-id or set CI_PROJECT_ID")
    token_var = resolve_gitlab_token_var(args)
    token = os.environ.get(token_var)
    if not token:
        raise ConfigError(f"GitLab token variable {token_var} is not set")
    return GitLabClient(api_url, project_id, token, resolve_gitlab_auth(args.gitlab_auth, token_var), args.timeout_seconds)


def gitlab_search_prefix(module_name: str, tag_template: str) -> str:
    before_version = tag_template.split("{version}", 1)[0]
    return "^" + before_version.replace("{module}", module_name)


def collect_tags(args: argparse.Namespace, tag_source: str, client: GitLabClient | None = None) -> list[str]:
    if tag_source == "none":
        return []
    if tag_source == "git":
        if args.fetch_tags:
            fetch_tags(args.repo)
        return list_git_tags(args.repo)
    if tag_source == "ls-remote":
        return list_ls_remote_tags(args.repo)
    if tag_source == "gitlab-api":
        client = client or make_gitlab_client(args)
        template = args.train_tag_template if args.release_scope == "train" else args.tag_template
        return client.list_tags(gitlab_search_prefix(args.module, template))
    raise ConfigError(f"unsupported tag source: {tag_source}")


def existing_current_release_result(args: argparse.Namespace, client: GitLabClient, ref: str) -> VersionResult | None:
    """Return a reusable release result if the target ref already has one.

    This is intentionally checked before listing repository tags and calculating
    the next version. CI jobs are commonly retried after a successful release
    reservation; a retry must reuse the tag on the current commit instead of
    observing that tag in the global tag list and incrementing again.
    """
    tag_template = args.train_tag_template if args.release_scope == "train" else args.tag_template
    resolved_mode = resolve_mode(args.mode, args.branch)
    candidates: list[tuple[SemVer, str]] = []
    for tag in client.get_current_commit_tags(ref):
        version = extract_version_from_tag(tag, args.module, tag_template)
        if version is None:
            continue
        if args.target_version and str(version) != str(SemVer.parse(args.target_version)):
            continue
        if resolved_mode == "patch" and not version.stable:
            continue
        if resolved_mode == "rc" and version.stable:
            continue
        candidates.append((version, tag))
    if not candidates:
        return None
    version, tag = max(candidates, key=lambda item: item[0])
    return VersionResult(
        module=args.module,
        mode=resolved_mode,
        current_version=str(version),
        next_version=str(version),
        tag=tag,
        reserved=False,
        reused=True,
    )


def validate_args(args: argparse.Namespace) -> None:
    if args.self_test:
        return
    if not args.module:
        raise ConfigError("--module is required")
    if args.reserve_release and args.mode == "snapshot":
        raise ConfigError("--reserve-release cannot be used with snapshot mode")
    if args.tag_mutation == "git-push" and not args.reserve_release:
        raise ConfigError("--tag-mutation git-push only applies with --reserve-release")
    SemVer.parse(args.initial_version)
    if args.target_version:
        SemVer.parse(args.target_version)


def write_env_file(path: str | Path, result: VersionResult) -> None:
    values = {
        "MODULE": result.module,
        "VERSION_MODE": result.mode,
        "CURRENT_VERSION": result.current_version or "",
        "NEXT_VERSION": result.next_version,
        "NEXT_TAG": result.tag or "",
        "RELEASE_RESERVED": "true" if result.reserved else "false",
        "RELEASE_REUSED": "true" if result.reused else "false",
    }
    for key, value in values.items():
        if value and not SAFE_ENV_RE.match(value):
            raise ConfigError(f"unsafe dotenv value for {key}")
    Path(path).write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")


def print_result(result: VersionResult, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(result.as_dict(), sort_keys=True))
        return
    for key, value in result.as_dict().items():
        print(f"{key}={'' if value is None else str(value).lower() if isinstance(value, bool) else value}")


def read_description(args: argparse.Namespace, result: VersionResult) -> str:
    if args.release_description_file:
        return Path(args.release_description_file).read_text(encoding="utf-8")
    return f"Release {result.tag or result.next_version} for {result.module}"


def reserve_with_git_push(tag: str, ref: str, repo: str | Path) -> ReservationResult:
    run_git(["tag", "-a", tag, ref, "-m", f"Release {tag}"], repo)
    run_git(["push", "origin", f"refs/tags/{tag}:refs/tags/{tag}"], repo)
    return ReservationResult(created=True, reused=False, tag=tag, ref=ref)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.self_test:
        # This hook is intentionally lightweight. Repository tests live beside this
        # script and are run by CI; the flag provides a zero-dependency smoke path.
        return 0
    try:
        validate_args(args)
        args.branch = args.branch or current_branch(args.repo)
        args.pipeline_iid = args.pipeline_iid or os.environ.get("CI_PIPELINE_IID") or "0"
        args.commit_short_sha = args.commit_short_sha or current_short_sha(args.repo)
        tag_source = default_tag_source(args)
        client = make_gitlab_client(args) if tag_source == "gitlab-api" or args.reserve_release else None
        release_ref = (args.release_ref or current_ref(args.repo)) if args.reserve_release else None
        result = None
        if args.reserve_release and client is not None and release_ref is not None:
            result = existing_current_release_result(args, client, release_ref)
        if result is None:
            tags = collect_tags(args, tag_source, client)
            config = Config(
                module=args.module,
                mode=args.mode,
                branch=args.branch,
                tags=tags,
                tag_template=args.tag_template,
                initial_version=args.initial_version,
                target_version=args.target_version,
                pipeline_iid=args.pipeline_iid,
                commit_short_sha=args.commit_short_sha,
                snapshot_template=args.snapshot_template,
                snapshot_include_branch=args.snapshot_include_branch,
                release_scope=args.release_scope,
                train_tag_template=args.train_tag_template,
            )
            result = calculate_next_version(config)
        if args.reserve_release and not result.reused:
            if not result.tag:
                raise ConfigError("snapshot results do not have release tags to reserve")
            ref = release_ref or current_ref(args.repo)
            name = args.release_name_template.format(tag=result.tag, version=result.next_version, module=result.module)
            description = read_description(args, result)
            if args.tag_mutation == "none":
                tag = client.get_tag(result.tag) if client else None
                if GitLabClient.tag_target(tag) != ref:
                    raise GitLabError("release tag is missing and --tag-mutation none was selected")
                reservation = ReservationResult(False, True, result.tag, ref)
            elif args.tag_mutation == "git-push":
                reservation = reserve_with_git_push(result.tag, ref, args.repo)
            else:
                if client is None:
                    client = make_gitlab_client(args)
                reservation = client.reserve_release_tag(result.tag, ref, name, description, args.if_exists)
            result.reserved = reservation.created
            result.reused = reservation.reused
        if args.write_env:
            write_env_file(args.write_env, result)
        print_result(result, args.json_output)
        return 0
    except (ConfigError, SemanticVersionError, GitLabError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
