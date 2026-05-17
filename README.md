# glab-utils

[![Quality](https://img.shields.io/github/actions/workflow/status/RandomCodeSpace/glab-utils/quality.yml?branch=main&label=quality&logo=githubactions&logoColor=white&style=for-the-badge)](https://github.com/RandomCodeSpace/glab-utils/actions/workflows/quality.yml)
[![Sonar Quality Gate](https://img.shields.io/sonar/quality_gate/RandomCodeSpace_glab-utils?server=https%3A%2F%2Fsonarcloud.io&label=sonar&logo=sonarcloud&logoColor=white&style=for-the-badge)](https://sonarcloud.io/project/overview?id=RandomCodeSpace_glab-utils)
[![Coverage](https://img.shields.io/sonar/coverage/RandomCodeSpace_glab-utils?server=https%3A%2F%2Fsonarcloud.io&label=coverage&logo=sonarcloud&logoColor=white&style=for-the-badge)](https://sonarcloud.io/component_measures?id=RandomCodeSpace_glab-utils&metric=coverage)
[![Bugs](https://img.shields.io/sonar/bugs/RandomCodeSpace_glab-utils?server=https%3A%2F%2Fsonarcloud.io&label=bugs&logo=sonarcloud&logoColor=white&style=for-the-badge)](https://sonarcloud.io/project/issues?id=RandomCodeSpace_glab-utils&resolved=false&types=BUG)
[![Vulnerabilities](https://img.shields.io/sonar/vulnerabilities/RandomCodeSpace_glab-utils?server=https%3A%2F%2Fsonarcloud.io&label=vulnerabilities&logo=sonarcloud&logoColor=white&style=for-the-badge)](https://sonarcloud.io/project/issues?id=RandomCodeSpace_glab-utils&resolved=false&types=VULNERABILITY)
[![Security Hotspots](https://img.shields.io/sonar/security_hotspots/RandomCodeSpace_glab-utils?server=https%3A%2F%2Fsonarcloud.io&label=security%20hotspots&logo=sonarcloud&logoColor=white&style=for-the-badge)](https://sonarcloud.io/security_hotspots?id=RandomCodeSpace_glab-utils)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white&style=for-the-badge)](https://www.python.org/downloads/)
[![Runtime](https://img.shields.io/badge/runtime-stdlib%20only-2ea44f?logo=python&logoColor=white&style=for-the-badge)](https://docs.python.org/3/library/)

Utilities for GitLab automation.

## GitLab project token rotator

`token-rotate/gitlab_project_token_rotator.py` is a single-command Python utility that runs inside GitLab CI/CD, reads an existing project access token from a CI/CD environment variable, checks whether the token expires within a configured number of days, rotates it when needed, and stores the newly issued token back into the GitLab CI/CD variable.

The runtime implementation uses only the Python standard library. No pip dependencies are required for production use.

## Requirements

- Python 3.10 or newer.
- A GitLab project access token stored as a masked CI/CD variable.
- The token must have enough permission to inspect its expiry, rotate itself, and update the project CI/CD variable that stores the token.
- In most GitLab setups, the token needs `api` scope. If your GitLab version supports self-rotation with narrower permissions, `self_rotate` may be usable for the rotation call, but updating the CI/CD variable still requires API access.

## Default CI/CD variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `CI_API_V4_URL` | GitLab API v4 URL provided by GitLab CI | `https://gitlab.com/api/v4` |
| `CI_PROJECT_ID` | GitLab project id provided by GitLab CI | required |
| `GITLAB_PROJECT_ACCESS_TOKEN` | Variable containing the current token value | required |
| `GITLAB_PROJECT_ACCESS_TOKEN_ID` | Project access token id, or `self` when supported | `self` |
| `TOKEN_ROTATE_BEFORE_DAYS` | Rotate when the token expires in this many days or fewer | `30` |
| `TOKEN_NEW_EXPIRES_IN_DAYS` | Lifetime for the replacement token from the current UTC date | `365` |
| `GITLAB_API_TIMEOUT_SECONDS` | GitLab API request timeout | `30` |

## Basic usage

Add the script to a GitLab CI job and run:

```bash
python3 token-rotate/gitlab_project_token_rotator.py --threshold-days 30
```

Run a safe expiry check without changing the token or CI/CD variable:

```bash
python3 token-rotate/gitlab_project_token_rotator.py --dry-run
```

If the GitLab variable key is different from the environment variable that exposes the token to the job, pass both names explicitly:

```bash
python3 token-rotate/gitlab_project_token_rotator.py \
  --token-var-name GITLAB_PROJECT_ACCESS_TOKEN \
  --variable-key GITLAB_PROJECT_ACCESS_TOKEN
```

## GitLab CI example

```yaml
rotate_project_access_token:
  image: python:3.12-alpine
  rules:
    - if: '$CI_PIPELINE_SOURCE == "schedule"'
  script:
    - python3 token-rotate/gitlab_project_token_rotator.py --threshold-days 30
```

Create a scheduled pipeline for the job, for example weekly or daily. The script exits successfully without making changes when the token expires after the configured threshold.

## Optional flags

```text
--api-url API_URL
--project-id PROJECT_ID
--token-var-name TOKEN_VAR_NAME
--variable-key VARIABLE_KEY
--token-id TOKEN_ID
--threshold-days THRESHOLD_DAYS
--new-expires-in-days NEW_EXPIRES_IN_DAYS
--timeout-seconds TIMEOUT_SECONDS
--dry-run
--variable-environment-scope VARIABLE_ENVIRONMENT_SCOPE
--set-masked true|false
--set-protected true|false
--set-raw true|false
--set-variable-type env_var|file
```

Use `--variable-environment-scope` when the project has duplicate variable keys with different GitLab environment scopes.

## Safety behavior

- The token value is read from the configured environment variable and is never hard-coded.
- The replacement token is never printed to stdout or stderr.
- The script updates the GitLab CI/CD variable only after GitLab returns a replacement token.
- By default, only the variable value is changed. Optional flags can set GitLab variable attributes such as masked, protected, raw, variable type, and environment scope.

Important: GitLab returns the newly rotated token only once. If rotation succeeds but updating the CI/CD variable fails, the new token may not be recoverable from logs. Keep enough API permission on the token so the script can complete both rotation and variable update.

## Semantic version bumper

`semantic-version/semantic_version_bumper.py` is a single-file Python utility that calculates module-scoped semantic versions and can optionally reserve immutable GitLab releases/tags from CI.

The runtime uses only the Python standard library. No pip dependencies are required.

### Tag model

Default module tags use:

```text
<module>/v<version>
```

Examples:

```text
api/v1.2.3
worker/v0.9.0-rc.2
```

For a single-module repository, use plain tags with:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module root \
  --tag-template 'v{version}'
```

### Calculate versions

Next patch from existing tags:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module api \
  --mode patch
```

Release candidate from an explicit release line:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module api \
  --mode rc \
  --branch release/1.5
```

No prior tags are deterministic:

```text
patch mode -> 0.0.1
generic rc mode -> 0.1.0-rc.1
release/1.0 rc mode -> 1.0.0-rc.1
```

For the first public release, prefer an explicit target version:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module api \
  --mode rc \
  --target-version 1.0.0-rc.1
```

Snapshot versions can be customized without changing release tag behavior:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module api \
  --mode snapshot \
  --snapshot-template '{base_version}-snapshot.{pipeline_iid}.{commit_short_sha}'
```

### GitLab API tag discovery

For large repositories, avoid fetching all tags. Use GitLab API tag filtering:

```bash
python3 semantic-version/semantic_version_bumper.py \
  --module api \
  --tag-source gitlab-api \
  --branch "$CI_COMMIT_REF_NAME" \
  --write-env version.env
```

Recommended CI checkout settings:

```yaml
variables:
  GIT_DEPTH: "1"
  GIT_FETCH_EXTRA_FLAGS: "--no-tags"
```

### Reserve releases with CI_JOB_TOKEN

Release jobs can create or reuse GitLab releases/tags with the built-in job token:

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
        --module api \
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

The script uses `JOB-TOKEN: $CI_JOB_TOKEN` and the GitLab Releases API. It does not assume direct `POST /repository/tags` works with job tokens. If a release job is retried after the tag was already created on the same commit, the script reuses that current-commit tag instead of incrementing again.

### Local checks

```bash
python -m unittest discover -s semantic-version -p 'test_*.py' -v
python semantic-version/semantic_version_bumper.py --help
python semantic-version/semantic_version_bumper.py --self-test
```

## Local checks

```bash
python -m unittest discover -s token-rotate -p 'test_*.py' -v
python -m unittest discover -s semantic-version -p 'test_*.py' -v
python token-rotate/quality_gate.py --min-coverage 95
python token-rotate/gitlab_project_token_rotator.py --help
python semantic-version/semantic_version_bumper.py --help
```
