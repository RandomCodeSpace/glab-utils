# Semantic Versioning Script Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Build one native-Python script that calculates the next semantic version for single-module and multi-module projects, with normal patch bumps and automatic minor release-candidate bumps when an RC/release branch is cut.

**Architecture:** Keep the runtime as a single executable Python file with only standard-library imports. The script will read Git tags, scope them by module name, parse SemVer values, decide the next version from the current branch/mode, and print/write CI-friendly output. Tests will use `unittest`, `tempfile`, and real temporary Git repositories to validate behavior without external packages.

**Tech Stack:** Python 3.10+, standard library only, Git CLI via `subprocess`, `unittest` tests, existing GitHub Actions quality workflow.

---

## Brainstormed Requirements and Decisions

### Primary behavior

1. The caller passes a module name:

   ```bash
   python3 semantic-version/semantic_version_bumper.py --module token-rotate
   ```

2. For normal branches, the script generates the next patch version:

   - latest module version: `1.4.7`
   - next generated version: `1.4.8`

3. When a release-candidate branch is cut, the script bumps the minor version and starts an RC line:

   - latest stable module version: `1.4.7`
   - branch: `release/1.5`, `rc/1.5`, or `release-candidate/1.5`
   - next generated version: `1.5.0-rc.1`

4. If the RC branch already has RC versions, increment the RC number:

   - existing module versions: `1.4.7`, `1.5.0-rc.1`, `1.5.0-rc.2`
   - branch: `release/1.5`
   - next generated version: `1.5.0-rc.3`

5. If the RC branch name does not include an explicit minor version, bump from the latest stable minor:

   - latest stable module version: `1.4.7`
   - branch: `release-candidate`
   - next generated version: `1.5.0-rc.1`

### Important semantics

- Stable versions use SemVer: `MAJOR.MINOR.PATCH`.
- RC versions use SemVer prerelease format: `MAJOR.MINOR.PATCH-rc.N`.
- Normal patch generation ignores prerelease versions unless explicitly told otherwise.
- RC generation is based on the next minor line and patch `0`.
- Major bumps are intentionally out of scope for the first implementation. Add `--bump major` later if needed.

### Multi-module tag strategy

Use module-scoped Git tags by default:

```text
<module>/v<version>
```

Examples:

```text
token-rotate/v1.4.7
api/v2.3.0
worker/v0.8.0-rc.2
```

Reasoning:

- Git tags are naturally global inside a repository.
- Multi-module projects need a namespace so each module can advance independently.
- Slashed tag names are valid Git refs and easy to filter with `git tag --list 'token-rotate/v*'`.

For single-module projects, support a flag to use plain tags:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module root \
  --tag-template 'v{version}'
```

Default remains `'{module}/v{version}'` so the behavior is predictable for monorepos.

### Large tag repository strategy

A repository with hundreds of thousands or millions of tags must not run `git fetch --tags` in every pipeline. The script should support a GitLab API tag source so CI can query only relevant tag names instead of downloading all tag refs and tag objects.

Recommended source priority:

1. `gitlab-api` in GitLab CI for large repositories.
2. `ls-remote` for generic remote-only tag discovery when the GitLab API is not available.
3. `git` local tags only for small repositories or local developer runs.

GitLab API query shape:

```text
GET /projects/:id/repository/tags?search=^<module>/v&order_by=version&sort=desc&per_page=100
```

Important notes:

- URL-encode the project id/path and the `search` value.
- GitLab's tag API supports `search` with `^term` for prefix matching and supports `order_by=version` for semantic-version ordering.
- Still parse and validate versions client-side because tags may include invalid names, non-SemVer tags, or tags from older conventions.
- For RC generation, query the narrowest prefix possible, for example `^token-rotate/v1.5.0-rc.`.
- If a module has more than one page of matching tags, follow pagination headers or keyset pagination. Do not assume the first page is sufficient unless the query prefix is narrow enough to prove it.

GitLab CI should keep checkout shallow and prevent runner auto-fetching tags:

```yaml
variables:
  GIT_DEPTH: "1"
  GIT_FETCH_EXTRA_FLAGS: "--no-tags"
```

Then calculate versions from the GitLab API:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module token-rotate \
  --tag-source gitlab-api \
  --branch "$CI_COMMIT_REF_NAME" \
  --write-env version.env
```

Fallback if API access is not available and Git CLI must be used:

```bash
git fetch --no-tags origin '+refs/tags/token-rotate/v*:refs/tags/token-rotate/v*'
```

This fetches only the module-scoped tag namespace. For RC branches, fetch an even narrower namespace:

```bash
git fetch --no-tags origin '+refs/tags/token-rotate/v1.5.0-rc.*:refs/tags/token-rotate/v1.5.0-rc.*'
```

Avoid branch reachability checks such as `git tag --merged` in the first version because they require commit history and defeat shallow-checkout performance. Tags are global refs, not branch-local refs. If the business rule needs branch-specific versions, encode the release line into the tag prefix or branch name instead of trying to infer it from commit reachability.

### Snapshot version strategy

Snapshots should be treated as ephemeral CI artifact versions, not permanent version reservations. The script should not scan tags, create tags, or update a central counter just to produce a snapshot. For performance, generate snapshots from the next release base plus GitLab CI's already-available unique identifiers.

Recommended snapshot format for SemVer-compatible consumers:

```text
<base-version>-snapshot.<pipeline-iid>.<commit-short-sha>
```

Examples:

```text
1.4.8-snapshot.732.a1b2c3d4
1.5.0-rc.3.snapshot.733.a1b2c3d4
```

If a target package ecosystem requires Maven-style snapshots, support a template override:

```bash
--snapshot-template '{base_version}-SNAPSHOT'
```

Default behavior:

- `snapshot` mode never creates a tag.
- `snapshot` mode never increments or reserves the patch/RC counter.
- Snapshot uniqueness comes from `CI_PIPELINE_IID` plus `CI_COMMIT_SHORT_SHA`.
- Snapshot ordering is good enough for CI artifacts because `CI_PIPELINE_IID` is monotonic inside a GitLab project.
- Module name should be part of the artifact path or package coordinates, not necessarily part of the SemVer string.

Base version rules:

| Branch/mode | Base version | Snapshot output example |
| --- | --- | --- |
| `main` / `snapshot` | next patch from latest stable | `1.4.8-snapshot.732.a1b2c3d4` |
| feature branch / `snapshot` | next patch from latest stable, or MR target release line | `1.4.8-snapshot.feature-login.732.a1b2c3d4` |
| MR source branch targeting `release/1.5` | target release line from `CI_MERGE_REQUEST_TARGET_BRANCH_NAME` | `1.5.0-snapshot.feature-login.732.a1b2c3d4` |
| `release/1.5` / `snapshot` | target release line | `1.5.0-snapshot.732.a1b2c3d4` |
| after existing `1.5.0-rc.2` | next RC base without reserving it | `1.5.0-rc.3.snapshot.732.a1b2c3d4` |

Feature branch snapshot policy:

- Always include the sanitized source branch slug by default for readability and package cleanup.
- Use `CI_COMMIT_REF_SLUG` for normal branch pipelines.
- Use `CI_MERGE_REQUEST_SOURCE_BRANCH_NAME` or `CI_COMMIT_REF_SLUG` for MR source identity.
- If `CI_MERGE_REQUEST_TARGET_BRANCH_NAME` matches a release branch pattern such as `release/1.5`, use that target line as the base version.
- Otherwise, use the next patch after the latest stable module tag from the GitLab Tags API.
- Do not query branch reachability and do not fetch history; feature branch snapshots are not release lineage decisions.
- Publish to a snapshot/dev package channel keyed by module and branch slug, for example `snapshots/<module>/<branch-slug>/<version>`.
- Configure retention/cleanup on the snapshot channel; feature branch snapshots should disappear after merge or after a fixed TTL.

Important: if strict SemVer precedence matters, avoid publishing snapshots to the same channel as immutable releases/RCs. SemVer prerelease ordering can be surprising for mixed labels like `rc.3.snapshot.732`. The safest operational rule is: snapshots go to a snapshot/dev repository or package channel; releases and RCs go to release channels.

If the business requires per-module sequential snapshot numbers, do not derive that from Git tags. Use one of these explicit state stores instead:

1. GitLab project/group variable per module, updated only by a serialized release/snapshot job.
2. GitLab Generic Package Registry state file such as `version-state/<module>/state.json`.
3. A small external version service.

For the first implementation, avoid stateful snapshot counters. Use `CI_PIPELINE_IID` for unique snapshots and keep state only for immutable releases/RCs.

### Release rerun and idempotency strategy

Release jobs are different from snapshots: releases and RCs are immutable version reservations. Running the same release job multiple times must not accidentally advance `1.4.8` to `1.4.9` or `1.5.0-rc.1` to `1.5.0-rc.2` just because the job was retried.

Recommended behavior:

1. Determine whether the current commit already has a module-scoped release tag before calculating a new version.
2. If the expected release tag already exists and points to the same commit, treat the rerun as idempotent success and reuse that version.
3. If the tag exists but points to a different commit, fail hard. Never move release tags automatically.
4. If the package/version already exists, verify it belongs to the same commit/build metadata; then either skip publishing or fail depending on `--if-exists skip|fail`.
5. Only calculate the next version when no release tag already exists for the current commit and release mode.

Efficient GitLab API checks:

- Use `GET /projects/:id/repository/commits/:sha/refs?type=tag` to check tags that point at the current commit.
- Filter those refs by the module tag template, for example `payment-service/v*`.
- Use the Tags API with a module prefix only when no current-commit tag is found and the script needs to calculate the next release/RC.

Release job policy matrix:

| Situation | Behavior |
| --- | --- |
| Manual job retry in the same pipeline after tag was created | Reuse existing tag/version and exit success |
| Whole pipeline rerun on the same commit after tag was created | Reuse existing tag/version and exit success |
| Release tag exists for same version but different commit | Fail; human must resolve |
| Artifact/package version already exists for same commit | Skip publish or exit success based on policy |
| Artifact/package version exists but commit/digest differs | Fail; immutable artifact conflict |
| No release tag exists for current commit | Calculate next release/RC from filtered GitLab tags |

For even stronger safety, support an explicit target version:

```bash
--target-version 1.4.8
```

When `--target-version` is provided, the script should validate and use that version instead of calculating the next one. This is useful for manual release jobs, approvals, and reruns.

### Branch/mode strategy

Support both auto-detection and explicit mode:

```bash
# Auto-detect branch from git or CI env
python3 semantic-version/semantic_version_bumper.py --module token-rotate

# Explicit branch, useful in CI
python3 semantic-version/semantic_version_bumper.py \
  --module token-rotate \
  --branch "$CI_COMMIT_REF_NAME"

# Explicit mode, useful for deterministic pipelines
python3 semantic-version/semantic_version_bumper.py \
  --module token-rotate \
  --mode rc
```

Modes:

| Mode | Meaning | Example output |
| --- | --- | --- |
| `auto` | Detect from branch pattern | `1.4.8` or `1.5.0-rc.1` |
| `patch` | Always bump patch | `1.4.8` |
| `rc` | Bump/start/increment release candidate | `1.5.0-rc.1` |
| `snapshot` | Generate an ephemeral CI artifact version without reserving it | `1.4.8-snapshot.732.a1b2c3d4` |

Default RC branch patterns:

```text
release/*
rc/*
release-candidate/*
```

The script should also accept custom patterns later, but the first version can keep this hard-coded and documented.

### Output strategy

Always print human-readable output plus machine-readable key/value lines:

```text
module=token-rotate
mode=patch
current_version=1.4.7
next_version=1.4.8
tag=token-rotate/v1.4.8
```

Support writing a dotenv file for CI:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module token-rotate \
  --write-env version.env
```

`version.env`:

```text
MODULE=token-rotate
VERSION_MODE=patch
CURRENT_VERSION=1.4.7
NEXT_VERSION=1.4.8
NEXT_TAG=token-rotate/v1.4.8
```

Do not create or push tags in the first implementation. Version calculation should be safe and side-effect free by default. Add optional `--create-tag` only after the calculation behavior is stable.

---

## Proposed CLI Contract

```text
usage: semantic_version_bumper.py --module MODULE [options]

Required:
  --module MODULE
      Logical module name. Used to filter module-scoped tags.

Options:
  --mode auto|patch|rc|snapshot
      Version generation mode. Default: auto.

  --branch BRANCH
      Branch name used for auto mode. Defaults to Git branch, then CI env vars.

  --tag-template TEMPLATE
      Tag format. Default: {module}/v{version}.
      For single-module plain tags, use: v{version}

  --initial-version VERSION
      Baseline when no prior version exists. Default: 0.0.0.
      First patch from no tags becomes 0.0.1.
      First RC from no tags becomes 0.1.0-rc.1.

  --write-env PATH
      Write CI dotenv output.

  --snapshot-template TEMPLATE
      Format for snapshot versions. Default:
      {base_version}-snapshot.{pipeline_iid}.{commit_short_sha}

  --snapshot-include-branch
      Include the sanitized branch slug in snapshot versions.

  --target-version VERSION
      Explicit immutable release/RC version to validate and use instead of calculating.

  --if-exists skip|fail
      Behavior when release artifact/package already exists. Default: fail.

  --fetch-tags
      Run git fetch --tags before reading tags. Intended only for small repos.

  --tag-source git|gitlab-api|ls-remote
      Tag discovery source. Default: git for local runs, gitlab-api when GitLab CI
      variables are present. Large repositories should use gitlab-api.

  --gitlab-token-var NAME
      CI/CD variable that contains a GitLab API token. Default: CI_JOB_TOKEN,
      falling back to GITLAB_TOKEN. Never print this value.

  --repo PATH
      Repository path. Default: current working directory.

  --allow-dirty
      Allow running with uncommitted changes. Default: allowed for calculate-only mode.

  --json
      Print JSON output instead of key/value output.
```

---

## Version Calculation Rules

### Rule 1: Parse SemVer only

Accept:

```text
1.2.3
1.2.3-rc.1
v1.2.3 only after removing tag prefix
```

Reject/ignore:

```text
1.2
1.2.3.4
1.2.3-beta.1
foo
```

First version supports only stable and `rc.N` prerelease versions.

### Rule 2: Patch mode

Given stable versions for a module:

```text
1.2.0
1.2.1
1.3.0-rc.1
```

Patch mode chooses latest stable only:

```text
current_version=1.2.1
next_version=1.2.2
```

### Rule 3: RC mode with explicit branch minor

Branch examples:

```text
release/1.3
rc/1.3
release-candidate/1.3
```

Given:

```text
1.2.5
```

Next:

```text
1.3.0-rc.1
```

### Rule 4: RC mode with existing RCs

Given:

```text
1.2.5
1.3.0-rc.1
1.3.0-rc.2
```

Next:

```text
1.3.0-rc.3
```

### Rule 5: RC mode without explicit branch minor

Given latest stable:

```text
1.2.5
```

Branch:

```text
release-candidate
```

Next:

```text
1.3.0-rc.1
```

### Rule 6: No prior tags

Initial stable baseline defaults to `0.0.0`.

Patch mode:

```text
0.0.1
```

RC mode:

```text
0.1.0-rc.1
```

---

## Implementation Tasks

### Task 1: Create script skeleton and CLI parser

**Objective:** Add a single executable Python script with argument parsing and no business logic yet.

**Files:**
- Create: `semantic-version/semantic_version_bumper.py`
- Test: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Write failing CLI smoke test**

Create `semantic-version/test_semantic_version_bumper.py` with a test that imports the script module using `importlib.util` and checks parser defaults.

```python
import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).with_name("semantic_version_bumper.py")


def load_module():
    spec = importlib.util.spec_from_file_location("semantic_version_bumper", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class CliParserTests(unittest.TestCase):
    def test_parser_defaults_to_auto_mode_and_module_tag_template(self):
        module = load_module()
        args = module.parse_args(["--module", "token-rotate"])
        self.assertEqual(args.module, "token-rotate")
        self.assertEqual(args.mode, "auto")
        self.assertEqual(args.tag_template, "{module}/v{version}")
```

**Step 2: Run test to verify failure**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: FAIL because `semantic_version_bumper.py` does not exist yet.

**Step 3: Implement minimal CLI parser**

Create `semantic-version/semantic_version_bumper.py`:

```python
#!/usr/bin/env python3
"""Calculate the next semantic version for a module from Git tags."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", required=True, help="Logical module name")
    parser.add_argument("--mode", choices=("auto", "patch", "rc"), default="auto")
    parser.add_argument("--branch", help="Branch name used for auto mode")
    parser.add_argument("--tag-template", default="{module}/v{version}")
    parser.add_argument("--initial-version", default="0.0.0")
    parser.add_argument("--write-env")
    parser.add_argument("--fetch-tags", action="store_true")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def parse_args(argv=None):
    return build_parser().parse_args(argv)


def main(argv=None) -> int:
    parse_args(argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 4: Verify pass**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: add semantic version bumper CLI skeleton"
```

---

### Task 2: Add SemVer parser and ordering

**Objective:** Parse stable and RC SemVer strings into comparable values.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing tests**

```python
class SemverParserTests(unittest.TestCase):
    def test_parse_stable_version(self):
        module = load_module()
        version = module.SemVer.parse("1.2.3")
        self.assertEqual((version.major, version.minor, version.patch), (1, 2, 3))
        self.assertIsNone(version.rc)

    def test_parse_rc_version(self):
        module = load_module()
        version = module.SemVer.parse("1.2.0-rc.4")
        self.assertEqual((version.major, version.minor, version.patch, version.rc), (1, 2, 0, 4))

    def test_reject_unsupported_prerelease(self):
        module = load_module()
        with self.assertRaises(ValueError):
            module.SemVer.parse("1.2.0-beta.1")

    def test_order_stable_after_rc_for_same_base(self):
        module = load_module()
        self.assertLess(module.SemVer.parse("1.2.0-rc.3"), module.SemVer.parse("1.2.0"))
```

**Step 2: Run failure**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: FAIL because `SemVer` does not exist.

**Step 3: Implement SemVer**

Add a frozen dataclass, regex parser, `is_stable`, `bump_patch`, `next_minor_rc`, and `with_next_rc` helpers. Use only `dataclasses`, `functools.total_ordering`, and `re`.

**Step 4: Verify pass**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: parse and order semantic versions"
```

---

### Task 3: Add tag template matching

**Objective:** Convert Git tags into module versions using the configured tag template.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing tests**

Test cases:

```python
class TagTemplateTests(unittest.TestCase):
    def test_extract_module_scoped_version(self):
        module = load_module()
        self.assertEqual(
            str(module.extract_version_from_tag("token-rotate/v1.2.3", "token-rotate", "{module}/v{version}")),
            "1.2.3",
        )

    def test_ignore_other_module_tag(self):
        module = load_module()
        self.assertIsNone(module.extract_version_from_tag("api/v1.2.3", "worker", "{module}/v{version}"))

    def test_extract_single_module_plain_tag(self):
        module = load_module()
        self.assertEqual(
            str(module.extract_version_from_tag("v2.0.1", "root", "v{version}")),
            "2.0.1",
        )
```

**Step 2: Implement**

Implement `extract_version_from_tag(tag, module_name, tag_template)` by converting the template into a strict regex. Escape literal template pieces and replace `{module}` and `{version}` placeholders.

**Step 3: Verify**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 4: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: support module-scoped version tags"
```

---

### Task 4: Add Git tag discovery

**Objective:** Read tags from a repository through the Git CLI.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing integration-style test**

Use `tempfile.TemporaryDirectory`, `subprocess.run`, and a temporary Git repo:

```python
class GitTagDiscoveryTests(unittest.TestCase):
    def test_list_git_tags_from_repo(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("test\n")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "tag", "token-rotate/v1.2.3"], cwd=repo, check=True)
            self.assertEqual(module.list_git_tags(repo), ["token-rotate/v1.2.3"])
```

**Step 2: Implement**

Add `run_git(args, repo)` and `list_git_tags(repo)` using `subprocess.run(..., text=True, capture_output=True, check=False)` with friendly errors.

**Step 3: Verify**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 4: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: discover semantic version tags from git"
```

---

### Task 5: Add GitLab API tag discovery for large repositories

**Objective:** Query only module-relevant tags from GitLab without fetching all tags into the job workspace.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing tests with mocked HTTP responses**

Use `unittest.mock` to patch `urllib.request.urlopen` and verify:

- the request URL contains `/projects/<id>/repository/tags`
- the query string includes `search=%5Etoken-rotate%2Fv`
- the query string includes `order_by=version`, `sort=desc`, and `per_page=100`
- returned tag names are extracted from JSON
- pagination is followed when GitLab returns a `Link` or `X-Next-Page` header

**Step 2: Implement `list_gitlab_tags(api_url, project_id, token, search_prefix)`**

Use only standard-library modules:

- `urllib.request`
- `urllib.parse`
- `json`

Authentication behavior:

- Prefer `JOB-TOKEN` when using `CI_JOB_TOKEN`.
- Use `PRIVATE-TOKEN` when using a project/group/personal access token variable such as `GITLAB_TOKEN`.
- Never print the token or response bodies that could include sensitive data.

**Step 3: Add source selection**

Implement `--tag-source git|gitlab-api|ls-remote`.

Default logic:

```text
if CI_API_V4_URL and CI_PROJECT_ID are present:
    tag_source = gitlab-api
else:
    tag_source = git
```

**Step 4: Verify**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: query module tags through GitLab API"
```

---

### Task 6: Implement patch version calculation

**Objective:** Generate the next patch version for the selected module.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing tests**

```python
class PatchCalculationTests(unittest.TestCase):
    def test_patch_bumps_latest_stable_for_module(self):
        module = load_module()
        versions = [module.SemVer.parse(v) for v in ["1.0.0", "1.0.1", "1.1.0-rc.1"]]
        self.assertEqual(str(module.next_patch_version(versions, module.SemVer.parse("0.0.0"))), "1.0.2")

    def test_patch_from_no_tags_uses_initial_version(self):
        module = load_module()
        self.assertEqual(str(module.next_patch_version([], module.SemVer.parse("0.0.0"))), "0.0.1")
```

**Step 2: Implement**

Patch calculation:

```text
stable_versions = versions where rc is None
current = max(stable_versions) or initial_version
next = current.major.current.minor.(current.patch + 1)
```

**Step 3: Verify**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 4: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: calculate next patch version"
```

---

### Task 7: Implement RC branch detection and minor bump

**Objective:** Auto-detect release-candidate branches and calculate minor RC versions.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing tests**

```python
class RcCalculationTests(unittest.TestCase):
    def test_release_branch_with_minor_starts_rc_line(self):
        module = load_module()
        versions = [module.SemVer.parse("1.4.7")]
        self.assertEqual(str(module.next_rc_version(versions, module.SemVer.parse("0.0.0"), "release/1.5")), "1.5.0-rc.1")

    def test_release_branch_increments_existing_rc_line(self):
        module = load_module()
        versions = [module.SemVer.parse(v) for v in ["1.4.7", "1.5.0-rc.1", "1.5.0-rc.2"]]
        self.assertEqual(str(module.next_rc_version(versions, module.SemVer.parse("0.0.0"), "release/1.5")), "1.5.0-rc.3")

    def test_release_branch_without_minor_bumps_latest_stable_minor(self):
        module = load_module()
        versions = [module.SemVer.parse("1.4.7")]
        self.assertEqual(str(module.next_rc_version(versions, module.SemVer.parse("0.0.0"), "release-candidate")), "1.5.0-rc.1")

    def test_auto_mode_detects_rc_branch(self):
        module = load_module()
        self.assertEqual(module.resolve_mode("auto", "rc/2.0"), "rc")
```

**Step 2: Implement**

Implementation pieces:

- `resolve_mode(mode, branch)`
- `extract_major_minor_from_branch(branch)`
- `next_rc_version(versions, initial_version, branch)`

Branch minor regex:

```text
(?:release|rc|release-candidate)/(\d+)\.(\d+)
```

If no branch minor exists, use latest stable `major, minor + 1`.

**Step 3: Verify**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 4: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: calculate release candidate minor versions"
```

---

### Task 8: Implement snapshot version calculation

**Objective:** Generate unique ephemeral snapshot versions without fetching all tags or reserving a release number.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing tests**

Add tests for:

```python
class SnapshotCalculationTests(unittest.TestCase):
    def test_snapshot_uses_next_patch_base_and_ci_identifiers(self):
        module = load_module()
        versions = [module.SemVer.parse("1.4.7")]
        result = module.next_snapshot_version(
            versions=versions,
            initial_version=module.SemVer.parse("0.0.0"),
            branch="main",
            pipeline_iid="732",
            commit_short_sha="a1b2c3d4",
            include_branch=False,
        )
        self.assertEqual(result, "1.4.8-snapshot.732.a1b2c3d4")

    def test_release_branch_snapshot_uses_release_line_without_reserving_rc(self):
        module = load_module()
        versions = [module.SemVer.parse("1.4.7")]
        result = module.next_snapshot_version(
            versions=versions,
            initial_version=module.SemVer.parse("0.0.0"),
            branch="release/1.5",
            pipeline_iid="733",
            commit_short_sha="b2c3d4e5",
            include_branch=False,
        )
        self.assertEqual(result, "1.5.0-snapshot.733.b2c3d4e5")
```

**Step 2: Implement snapshot helpers**

Implement:

- `sanitize_prerelease_identifier(value)` for branch slugs.
- `resolve_snapshot_base(versions, initial_version, branch)`.
- `next_snapshot_version(...)`.

Use `CI_PIPELINE_IID` and `CI_COMMIT_SHORT_SHA` by default when CLI flags are not provided. If they are missing outside CI, fall back to `0` and the local Git short SHA, or fail with a clear message when Git is unavailable.

**Step 3: Add CLI options**

Add:

```text
--mode snapshot
--snapshot-template
--snapshot-include-branch
--pipeline-iid
--commit-short-sha
```

Default template:

```text
{base_version}-snapshot.{pipeline_iid}.{commit_short_sha}
```

**Step 4: Verify**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 5: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: calculate ephemeral snapshot versions"
```

---

### Task 9: Wire end-to-end calculation into CLI output

**Objective:** Make the CLI calculate and print version output from real tags.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing end-to-end tests**

Test running `main([...])` with captured stdout using `contextlib.redirect_stdout`.

Assertions:

- patch mode prints `next_version=...`
- RC mode prints `mode=rc`
- tag output uses configured tag template
- `--json` emits valid JSON

**Step 2: Implement calculation object**

Add a small dataclass:

```python
@dataclass(frozen=True)
class VersionResult:
    module: str
    mode: str
    current_version: SemVer | None
    next_version: SemVer
    tag: str
```

Add `calculate_next_version(args)` and output formatters.

**Step 3: Verify**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 4: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: expose semantic version calculation through CLI"
```

---

### Task 10: Add dotenv output for CI

**Objective:** Support CI systems that pass variables between jobs.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing test**

Use a temporary file path and verify contents:

```text
MODULE=token-rotate
VERSION_MODE=patch
CURRENT_VERSION=1.4.7
NEXT_VERSION=1.4.8
NEXT_TAG=token-rotate/v1.4.8
```

**Step 2: Implement `write_env_file(path, result)`**

Rules:

- Write UTF-8 text.
- End file with newline.
- Do not quote values unless needed. Module names/tags should be restricted to safe characters.

**Step 3: Verify**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
```

Expected: PASS.

**Step 4: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: write semantic version output for CI"
```

---

### Task 11: Add README documentation

**Objective:** Document usage for single-module and multi-module projects.

**Files:**
- Modify: `README.md`

**Step 1: Add section**

Add `## Semantic version bumper` with:

- purpose
- tag scheme
- normal patch example
- RC branch example
- GitLab CI/GitHub Actions example

Example GitLab CI:

```yaml
calculate_version:
  image: python:3.12-alpine
  script:
    - apk add --no-cache git
    - python3 semantic-version/semantic_version_bumper.py --module token-rotate --branch "$CI_COMMIT_REF_NAME" --write-env version.env
  artifacts:
    reports:
      dotenv: version.env
```

Example GitHub Actions:

```yaml
- name: Calculate version
  run: |
    python3 semantic-version/semantic_version_bumper.py \
      --module token-rotate \
      --branch "${GITHUB_REF_NAME}" \
      --write-env version.env
    cat version.env >> "$GITHUB_ENV"
```

**Step 2: Verify markdown links and commands**

```bash
python semantic-version/semantic_version_bumper.py --help
python -m unittest discover -s semantic-version -p 'test_*.py' -v
```

Expected: help prints successfully, tests pass.

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document semantic version bumper"
```

---

### Task 12: Integrate with existing quality checks

**Objective:** Ensure the new script is included in repository quality gates.

**Files:**
- Modify: `.github/workflows/quality.yml` only if current test discovery does not already include the new folder.

**Step 1: Check current workflow**

Inspect test discovery paths. If it only runs `token-rotate`, either:

1. Add a second test command for `semantic-version`, or
2. Change discovery to cover all project test files.

Prefer explicit test command to keep behavior obvious:

```bash
python -m unittest discover -s semantic-version -p 'test_*.py' -v
```

**Step 2: Run local checks**

```bash
python -m unittest discover -s token-rotate -p 'test_*.py' -v
python -m unittest discover -s semantic-version -p 'test_*.py' -v
python token-rotate/quality_gate.py --min-coverage 95
python semantic-version/semantic_version_bumper.py --help
python -m py_compile semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git diff --check
```

Expected: all pass.

**Step 3: Commit**

```bash
git add .github/workflows/quality.yml
git commit -m "ci: include semantic version bumper tests"
```

---

## Acceptance Criteria

- The implementation is a single runtime script: `semantic-version/semantic_version_bumper.py`.
- The runtime script uses only Python standard-library imports.
- The script accepts `--module` and scopes version history to that module.
- Multi-module repositories can use tags like `module-name/v1.2.3`.
- Single-module repositories can use plain tags with `--tag-template 'v{version}'`.
- Normal branches generate the next patch version.
- RC/release branches generate the next minor RC version.
- Existing RC versions increment `rc.N` instead of restarting at `rc.1`.
- No prior tags produce deterministic initial versions.
- Output is CI-friendly key/value text by default.
- Optional `--write-env` writes a dotenv file.
- Snapshot mode generates unique versions from `CI_PIPELINE_IID` and `CI_COMMIT_SHORT_SHA` without creating tags or reserving counters.
- Large repositories can use GitLab API tag filtering instead of fetching all tags/history.
- Unit tests cover SemVer parsing, tag matching, patch generation, RC generation, snapshot generation, and end-to-end CLI output.
- Existing repository quality workflow remains green.

---

## Example Expected Behavior Matrix

| Tags for module | Branch | Mode | Next version | Next tag |
| --- | --- | --- | --- | --- |
| none | `main` | `auto` | `0.0.1` | `module/v0.0.1` |
| `module/v1.2.3` | `main` | `auto` | `1.2.4` | `module/v1.2.4` |
| `module/v1.2.3` | `feature/foo` | `patch` | `1.2.4` | `module/v1.2.4` |
| `module/v1.2.3` | `release/1.3` | `auto` | `1.3.0-rc.1` | `module/v1.3.0-rc.1` |
| `module/v1.2.3`, `module/v1.3.0-rc.1` | `release/1.3` | `auto` | `1.3.0-rc.2` | `module/v1.3.0-rc.2` |
| `module/v1.2.3`, `other/v9.9.9` | `main` | `auto` | `1.2.4` | `module/v1.2.4` |
| `v2.0.0` with `--tag-template 'v{version}'` | `main` | `auto` | `2.0.1` | `v2.0.1` |
| `module/v1.4.7` | `main` | `snapshot` | `1.4.8-snapshot.732.a1b2c3d4` | none |
| `module/v1.4.7` | `release/1.5` | `snapshot` | `1.5.0-snapshot.733.b2c3d4e5` | none |

---

## Open Questions Before Implementation

These are worth confirming before coding if the script will be used across many teams:

1. Should RC branch names always include the target minor, for example `release/1.5`, or should generic `release-candidate` be enough?
2. Should final release from `1.5.0-rc.3` to `1.5.0` be supported in the first version?
3. Should the script ever create Git tags, or only calculate the next version?
4. Should module names allow slashes, for example `services/api`, or should they be simple slugs only?
5. Should version files be updated, for example `pyproject.toml`, `package.json`, or `__init__.py`, or should CI consume the generated version without editing files?
6. Should snapshots use strict SemVer prerelease style, Maven-style `-SNAPSHOT`, or an ecosystem-specific template?
7. Should snapshots be unique only per pipeline, or is a per-module sequential snapshot counter required?

Recommended first implementation: calculate only, do not tag, do not edit version files. This keeps the script safe, reusable, and easy to test. Add mutation features only after version calculation is trusted.
