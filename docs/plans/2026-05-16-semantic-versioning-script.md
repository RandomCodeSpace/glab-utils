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

### GitLab CI_JOB_TOKEN release and tag mutation strategy

The script should support a mutation mode that uses GitLab's built-in `CI_JOB_TOKEN` so a release job can reserve a version without storing a long-lived project access token just for tagging.

Important GitLab permission reality:

- `CI_JOB_TOKEN` authenticates REST calls with the `JOB-TOKEN` header.
- GitLab's job token can read the Tags API endpoints needed for discovery: `GET /projects/:id/repository/tags` and `GET /projects/:id/repository/tags/:tag_name`.
- GitLab's Releases API accepts `CI_JOB_TOKEN` with the `JOB-TOKEN` header and supports release creation.
- Creating a tag directly through `POST /projects/:id/repository/tags` may require a normal API token because the documented job-token Tags API access is read-only. Do not depend on that endpoint for job-token tag creation.

Recommended job-token reservation model:

1. Calculate the next version and tag name.
2. Check current-commit refs with `GET /projects/:id/repository/commits/:sha/refs?type=tag`.
3. Check the exact tag with `GET /projects/:id/repository/tags/:tag_name`.
4. If the tag already exists on the current commit, reuse it and exit successfully.
5. If the tag exists on a different commit, fail hard; never move release tags automatically.
6. If the tag does not exist, create a GitLab release with `POST /projects/:id/releases` using `JOB-TOKEN: $CI_JOB_TOKEN`, `tag_name=<tag>`, and `ref=$CI_COMMIT_SHA`. GitLab creates the tag as part of release creation when the tag is missing.
7. If release creation returns a conflict, re-read the release/tag and verify it points at the same commit before treating the run as idempotent success.

This means the first mutation feature should be named around release reservation, not generic tag pushing:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module token-rotate \
  --mode rc \
  --tag-source gitlab-api \
  --reserve-release \
  --gitlab-auth job-token
```

For release trains from `main`/`master`:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --mode rc \
  --release-scope train \
  --modules-file modules.json \
  --tag-source gitlab-api \
  --reserve-release \
  --gitlab-auth job-token
```

If a team needs tag-only mutation without creating a GitLab release, support it as an explicit advanced mode only when prerequisites are met:

1. `--tag-mutation git-push` uses `git push` over HTTPS authenticated as `gitlab-ci-token:$CI_JOB_TOKEN`.
2. The GitLab project must enable **Allow Git push requests to the repository** for job tokens.
3. The user who started the pipeline must have sufficient project permission.
4. Job-token git pushes do not trigger CI pipelines, so release jobs must not rely on a tag pipeline being triggered by that push.

For maximum portability, the default mutation strategy should be:

```text
--reserve-release --gitlab-auth job-token --tag-mutation release-api
```

Use a project/group/personal access token only when a GitLab instance does not allow job-token Releases API calls or when the business explicitly requires direct Tags API creation.

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

### Main/master all-module release train strategy

If every release from `main`/`master` intentionally creates a new release candidate and bumps the minor line for all modules, treat that as a **release train**, not as N independent module releases.

Problem to avoid:

```text
module-a/v1.5.0-rc.1
module-b/v4.2.0-rc.1
module-c/v0.9.0-rc.1
```

calculated separately for every module on every release job. That causes unnecessary API calls, version drift, and accidental all-module bumps when the job is retried.

Recommended release-train model:

```text
release/v1.5.0-rc.1       # single train reservation tag
release/v1.5.0            # final train release tag
```

Then each module artifact derives its version from the train.

If the organization allows all modules to share the same release version, use the train version directly:

```text
module-a -> 1.5.0-rc.1
module-b -> 1.5.0-rc.1
module-c -> 1.5.0-rc.1
```

If modules must keep independent version lines, keep the train as the release event id and store per-module versions in the release manifest. The first RC for a new train bumps each module's minor version once; job retries and later reruns reuse the manifest instead of bumping minor again:

```json
{
  "train": "2026.05-rc.1",
  "commit": "<CI_COMMIT_SHA>",
  "modules": {
    "module-a": "1.5.0-rc.1",
    "module-b": "4.3.0-rc.1",
    "module-c": "0.10.0-rc.1"
  }
}
```

For the final release of the same train, the script removes the RC suffix from the manifest versions:

```json
{
  "train": "2026.05",
  "commit": "<CI_COMMIT_SHA>",
  "modules": {
    "module-a": "1.5.0",
    "module-b": "4.3.0",
    "module-c": "0.10.0"
  }
}
```

If module-specific tags are still required for downstream systems, create them as secondary aliases after the train version is reserved:

```text
module-a/v1.5.0-rc.1
module-b/v1.5.0-rc.1
module-c/v1.5.0-rc.1
```

But the source of truth should remain the train tag or a release manifest, not per-module version calculation.

Efficient algorithm for all-module release from `main`/`master`:

1. Check whether the current commit already has a `release/v*-rc.*` or `release/v*` train tag with `GET /projects/:id/repository/commits/:sha/refs?type=tag`.
2. If the train tag exists on this commit, reuse it for all modules. This makes release job retries idempotent.
3. If no train tag exists, query only train tags with `search=^release/v&order_by=version&sort=desc&per_page=100`.
4. Calculate the next train version once.
5. Create the train tag once.
6. Generate or reuse a release manifest. If modules share one version, every module maps to the train version. If modules keep independent version lines, bump each module's minor once only when the manifest for that train is first created.
7. Optionally create module alias tags only if required.

Release manifest example:

```json
{
  "train": "1.5.0-rc.1",
  "commit": "<CI_COMMIT_SHA>",
  "modules": {
    "module-a": "1.5.0-rc.1",
    "module-b": "1.5.0-rc.1",
    "module-c": "1.5.0-rc.1"
  }
}
```

This avoids scanning module tags for every module. For a monorepo with many modules, it changes release discovery from `O(number_of_modules)` tag queries to one train-tag query plus one commit-refs idempotency check.

Support two release scopes:

| Scope | Behavior | Use when |
| --- | --- | --- |
| `module` | Calculate a version for one module from module tags | modules version independently |
| `train` | Calculate one version and apply it to all modules | every release bumps all modules together |

For `main`/`master`, prefer explicit release jobs over auto-RC on every push. In other words, normal `main` builds can produce snapshots, while a manual/scheduled release job uses `--release-scope train --mode rc` or `--release-scope train --mode patch`.

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

  --release-scope module|train
      Versioning scope. Default: module. Use train when a main/master release
      intentionally applies the same release/RC version to all modules.

  --modules-file PATH
      Optional newline, JSON, or simple manifest file listing modules for train mode.
      Used to write a release manifest and optional module alias tags.

  --train-tag-template TEMPLATE
      Tag format for release train reservations. Default: release/v{version}.

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

  --gitlab-auth auto|job-token|private-token
      Authentication header strategy for GitLab API calls. Default: auto.
      job-token sends JOB-TOKEN and is intended for CI_JOB_TOKEN.
      private-token sends PRIVATE-TOKEN and is intended for GITLAB_TOKEN or PATs.

  --reserve-release
      After calculating the immutable release/RC version, reserve it in GitLab by
      creating or reusing a release/tag. Intended only for patch/rc release jobs,
      not snapshot mode.

  --release-ref REF
      Commit SHA, branch, or tag to release. Default: CI_COMMIT_SHA, then HEAD.

  --release-name-template TEMPLATE
      GitLab release name template. Default: {tag}.

  --release-description-file PATH
      Optional Markdown file used as the GitLab release description.

  --tag-mutation release-api|git-push|none
      How to create a missing tag when --reserve-release is set. Default:
      release-api. release-api uses POST /projects/:id/releases, which works
      with CI_JOB_TOKEN on supported GitLab versions. git-push requires the
      project setting that allows CI_JOB_TOKEN pushes.

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

### Rule 6: No prior tags / first version

The default no-tag behavior treats `--initial-version` as a logical baseline, not as a previously published tag. The default baseline is `0.0.0`.

Patch mode with no prior tags:

```text
0.0.1
```

RC mode with no prior tags and no explicit release line:

```text
0.1.0-rc.1
```

RC mode with an explicit branch release line should use that line even when no tags exist:

```text
branch=release/1.0
next=1.0.0-rc.1
```

For a real product's first public release, prefer an explicit target version instead of relying on defaults:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module api \
  --mode rc \
  --target-version 1.0.0-rc.1 \
  --reserve-release
```

or use an explicit release branch:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module api \
  --mode rc \
  --branch release/1.0 \
  --reserve-release
```

For release trains, the same rule applies to the train tag. With no existing train tags, either provide `--target-version 1.0.0-rc.1` or run from a branch/ref that clearly encodes the first train line, such as `release/1.0`.

Do not create fake bootstrap tags solely to make the calculation work. If a migration needs to preserve an existing externally published version, set `--initial-version` to the latest external stable version or pass `--target-version` for the first reserved release.

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

### Task 11: Add GitLab job-token release reservation

**Objective:** Allow a release job to reserve immutable versions using GitLab's built-in `CI_JOB_TOKEN`, without fetching tags or storing a long-lived API token for the common release path.

**Files:**
- Modify: `semantic-version/semantic_version_bumper.py`
- Modify: `semantic-version/test_semantic_version_bumper.py`

**Step 1: Add failing mocked GitLab client tests**

Use `unittest.mock` around `urllib.request.urlopen` and verify:

- `CI_JOB_TOKEN` uses the `JOB-TOKEN` header, never `PRIVATE-TOKEN`.
- `GITLAB_TOKEN` or an explicitly private token uses the `PRIVATE-TOKEN` header.
- `get_tag(tag_name)` URL-encodes slashes in tags like `module-a/v1.2.3`.
- `create_release(tag_name, ref, ...)` posts to `/projects/:id/releases` with form-encoded `tag_name`, `ref`, `name`, and `description`.
- `reserve_release_tag(...)` returns idempotent success when the tag already points at the requested commit.
- `reserve_release_tag(...)` fails when the tag exists but points at a different commit.
- A `409 Conflict` from release creation is handled by re-reading the tag/release and verifying the commit before returning success.
- `--reserve-release` is rejected in `snapshot` mode.

Example test intent:

```python
class GitLabReleaseReservationTests(unittest.TestCase):
    def test_job_token_uses_job_token_header_for_release_creation(self):
        module = load_module()
        client = module.GitLabClient(
            api_url="https://gitlab.example/api/v4",
            project_id="123",
            token="dummy",
            auth_mode="job-token",
        )
        request = client.build_request("/projects/123/releases", method="POST", data={"tag_name": "module/v1.0.0", "ref": "abc"})
        self.assertEqual(request.headers["Job-token"], "dummy")
        self.assertNotIn("Private-token", request.headers)
```

**Step 2: Implement GitLab mutation helpers**

Add or extend `GitLabClient` with standard-library-only methods:

```text
build_request(path, method="GET", query=None, data=None)
get_current_commit_tags(sha)
get_tag(tag_name)
get_release(tag_name)
create_release(tag_name, ref, name, description, tag_message=None)
reserve_release_tag(tag_name, ref, name, description, if_exists)
```

Implementation notes:

- Use `JOB-TOKEN` for `auth_mode=job-token` and `PRIVATE-TOKEN` for `auth_mode=private-token`.
- In `auth_mode=auto`, choose `job-token` when `--gitlab-token-var` resolves to `CI_JOB_TOKEN`; otherwise choose `private-token`.
- URL-encode project ids and tag names with `urllib.parse.quote(value, safe="")`.
- Treat HTTP `404` as missing tag/release.
- Treat HTTP `409` during create as a possible race; re-read and verify before failing.
- Never log raw response bodies by default because they may contain request echoes or sensitive metadata.

**Step 3: Wire CLI options**

Add:

```text
--reserve-release
--release-ref
--release-name-template
--release-description-file
--tag-mutation release-api|git-push|none
--gitlab-auth auto|job-token|private-token
```

Validation rules:

- `--reserve-release` requires `--tag-source gitlab-api` or enough GitLab CI env to construct a client.
- `--reserve-release` is invalid with `--mode snapshot`.
- `--tag-mutation release-api` uses `POST /projects/:id/releases` and is the default.
- `--tag-mutation git-push` must be opt-in and should print a prerequisite warning unless `--json` is used.
- `--tag-mutation none` validates idempotency only; it never creates a tag or release.

**Step 4: Preserve idempotency**

Reservation flow:

```text
if current commit already has expected release/train tag:
    return success, reserved=false, reused=true
elif exact tag exists on same commit:
    return success, reserved=false, reused=true
elif exact tag exists on different commit:
    fail immutable-tag-conflict
elif tag_mutation == none:
    fail missing-tag
elif tag_mutation == release-api:
    create GitLab release with tag_name and ref
    return success, reserved=true, reused=false
elif tag_mutation == git-push:
    create annotated/local tag and push exactly refs/tags/<tag>
    return success, reserved=true, reused=false
```

**Step 5: Verify**

```bash
python -m unittest semantic-version/test_semantic_version_bumper.py -v
python -m py_compile semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
```

Expected: PASS.

**Step 6: Commit**

```bash
git add semantic-version/semantic_version_bumper.py semantic-version/test_semantic_version_bumper.py
git commit -m "feat: reserve GitLab releases with CI job token"
```

---

### Task 12: Add README documentation

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
- GitLab `CI_JOB_TOKEN` release reservation example
- first-version behavior and how to bootstrap `1.0.0-rc.1`

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

Example GitLab release reservation with `CI_JOB_TOKEN`:

```yaml
reserve_release:
  image: python:3.12-alpine
  rules:
    - if: '$CI_COMMIT_BRANCH == "main"'
      when: manual
  variables:
    GIT_DEPTH: "1"
    GIT_FETCH_EXTRA_FLAGS: "--no-tags"
  script:
    - python3 semantic-version/semantic_version_bumper.py \
        --module token-rotate \
        --mode rc \
        --tag-source gitlab-api \
        --reserve-release \
        --gitlab-auth job-token \
        --release-ref "$CI_COMMIT_SHA" \
        --write-env version.env
  artifacts:
    reports:
      dotenv: version.env
```

For the first public RC, either run from an explicit branch such as `release/1.0` or pass `--target-version 1.0.0-rc.1` to avoid accidentally starting at the default `0.1.0-rc.1`.

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

### Task 13: Integrate with existing quality checks

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
- Release jobs can use `CI_JOB_TOKEN` with the `JOB-TOKEN` header to create or reuse GitLab releases/tags through the Releases API.
- Direct tag creation with `CI_JOB_TOKEN` is not assumed; tag-only mutation requires explicit `--tag-mutation git-push` and project settings that allow job-token pushes.
- First-version behavior is deterministic: defaults start at `0.0.1` for patch and `0.1.0-rc.1` for generic RC, while `--target-version` or `release/1.0` bootstraps `1.0.0-rc.1`.
- Unit tests cover SemVer parsing, tag matching, patch generation, RC generation, snapshot generation, GitLab release reservation, and end-to-end CLI output.
- Existing repository quality workflow remains green.

---

## Example Expected Behavior Matrix

| Tags for module | Branch | Mode | Next version | Next tag |
| --- | --- | --- | --- | --- |
| none | `main` | `auto` | `0.0.1` | `module/v0.0.1` |
| none | `release/1.0` | `auto` | `1.0.0-rc.1` | `module/v1.0.0-rc.1` |
| none with `--target-version 1.0.0-rc.1` | `main` | `rc` | `1.0.0-rc.1` | `module/v1.0.0-rc.1` |
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
3. Should tag-only mutation be supported beyond GitLab release reservation, or is creating/reusing a GitLab Release enough for all release jobs?
4. Should module names allow slashes, for example `services/api`, or should they be simple slugs only?
5. Should version files be updated, for example `pyproject.toml`, `package.json`, or `__init__.py`, or should CI consume the generated version without editing files?
6. Should snapshots use strict SemVer prerelease style, Maven-style `-SNAPSHOT`, or an ecosystem-specific template?
7. Should snapshots be unique only per pipeline, or is a per-module sequential snapshot counter required?

Recommended first implementation: calculate versions first, then add GitLab release reservation as an explicit opt-in mutation path guarded by `--reserve-release`. Do not edit version files in the first release of the script.
