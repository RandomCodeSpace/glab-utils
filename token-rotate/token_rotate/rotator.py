"""
GitLab project access token self-rotator.

Detailed implementation plan
============================

Goal
----
Run inside GitLab CI/CD, read the current project access token from a CI/CD
variable, check whether that token expires within a configurable number of days,
self-rotate it when required, then write the newly returned token back into the
same GitLab CI/CD project variable.

Constraints
-----------
1. Importable OOP package plus a thin CLI wrapper.
2. Python standard library only; no pip packages.
3. Token value is read from an environment variable, never hard-coded.
4. New token value is never printed to stdout/stderr.
5. The old token is expected to be a GitLab project access token with enough
   scope to:
   - read its expiry, usually via api scope;
   - self-rotate, via api or self_rotate scope;
   - update project CI/CD variables, via api scope.

Runtime flow
------------
1. Load configuration from CLI flags and CI environment variables.
2. Read the token from the configured token variable name.
3. Read token metadata from GitLab:
   a. first try GET /projects/:id/access_tokens/:token_id;
   b. when token_id is "self" and that endpoint is unavailable, fall back to
      GET /personal_access_tokens/self for expiry inspection.
4. Parse expires_at as YYYY-MM-DD.
5. Compute days until expiry using UTC date.
6. If the token expires after the configured threshold, exit successfully with no
   changes.
7. If the token expires inside the threshold:
   a. compute a new expires_at as today + --new-expires-in-days;
   b. call POST /projects/:id/access_tokens/:token_id/rotate using the old token;
   c. extract the one-time new token from the rotate response;
   d. call PUT /projects/:id/variables/:variable-key using the NEW token;
   e. update only the variable value by default, preserving existing GitLab
      variable attributes unless optional flags are provided.
8. Exit non-zero on API, validation, or configuration errors.

Recommended GitLab CI usage
---------------------------
Store the project access token in a masked CI/CD variable named
GITLAB_PROJECT_ACCESS_TOKEN, then run:

  python3 token-rotate/gitlab_project_token_rotator.py --threshold-days 30

Optional variables/flags:
- CI_PROJECT_ID: GitLab project id; automatically present in GitLab CI.
- CI_API_V4_URL: GitLab API URL; automatically present in GitLab CI.
- GITLAB_PROJECT_ACCESS_TOKEN: default token variable read and updated.
- GITLAB_PROJECT_ACCESS_TOKEN_ID: optional token id; defaults to "self".
- TOKEN_ROTATE_BEFORE_DAYS: default threshold when --threshold-days is omitted.
- TOKEN_NEW_EXPIRES_IN_DAYS: default new lifetime when omitted; default 365.

Important operational note
--------------------------
GitLab returns the new token only once. This script intentionally does not print
that token. If rotation succeeds but CI variable update fails, the new token may
not be recoverable from logs. Keep api scope on the token so the variable update
can be completed by the script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

DEFAULT_API_URL = "https://gitlab.com/api/v4"
DEFAULT_TOKEN_VAR_NAME = "GITLAB_PROJECT_ACCESS_TOKEN"
DEFAULT_TOKEN_ID = "self"
DEFAULT_THRESHOLD_DAYS = 30
DEFAULT_NEW_EXPIRES_IN_DAYS = 365


class ConfigError(Exception):
    """Raised when required runtime configuration is missing or invalid."""


class GitLabAPIError(Exception):
    """Raised for non-2xx GitLab API responses."""

    def __init__(self, method: str, url: str, status: int, body: str):
        self.method = method
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"GitLab API {method} {url} failed with HTTP {status}")


@dataclass(frozen=True)
class Config:
    api_url: str
    project_id: str
    token_var_name: str
    variable_key: str
    token_id: str
    threshold_days: int
    new_expires_in_days: int
    timeout_seconds: int
    dry_run: bool
    variable_environment_scope: Optional[str]
    set_masked: Optional[str]
    set_protected: Optional[str]
    set_raw: Optional[str]
    set_variable_type: Optional[str]


def parse_yyyy_mm_dd(value: str) -> date:
    try:
        year, month, day = value.split("-", 2)
        return date(int(year), int(month), int(day))
    except Exception as exc:
        raise ConfigError(f"Invalid date {value!r}; expected YYYY-MM-DD") from exc


def days_until(expiry: date, today: date) -> int:
    return (expiry - today).days


def should_rotate(expires_at: str, threshold_days: int, today: date) -> bool:
    return days_until(parse_yyyy_mm_dd(expires_at), today) <= threshold_days


def calculate_new_expires_at(today: date, lifetime_days: int) -> str:
    return (today + timedelta(days=lifetime_days)).isoformat()


def env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    return default


def int_from_text(value: str, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{field_name} must be an integer, got {value!r}") from exc
    if parsed < 0:
        raise ConfigError(f"{field_name} must be zero or greater, got {parsed}")
    return parsed


def bool_to_gitlab(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return "true"
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return "false"
    raise ConfigError(f"Expected boolean value, got {value!r}")


def quote_path_segment(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def normalize_api_url(value: str) -> str:
    return value.rstrip("/")


def build_variable_update_payload(
    new_token: str,
    environment_scope: Optional[str] = None,
    masked: Optional[str] = None,
    protected: Optional[str] = None,
    raw: Optional[str] = None,
    variable_type: Optional[str] = None,
) -> Dict[str, str]:
    payload: Dict[str, str] = {"value": new_token}
    if environment_scope:
        payload["environment_scope"] = environment_scope
    if masked is not None:
        payload["masked"] = bool_to_gitlab(masked)
    if protected is not None:
        payload["protected"] = bool_to_gitlab(protected)
    if raw is not None:
        payload["raw"] = bool_to_gitlab(raw)
    if variable_type:
        if variable_type not in {"env_var", "file"}:
            raise ConfigError("variable_type must be 'env_var' or 'file'")
        payload["variable_type"] = variable_type
    return payload


class GitLabClient:
    def __init__(self, api_url: str, token: str, timeout_seconds: int):
        self.api_url = normalize_api_url(api_url)
        self.token = token
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        path: str,
        *,
        token: Optional[str] = None,
        data: Optional[Mapping[str, str]] = None,
        query: Optional[Mapping[str, str]] = None,
    ) -> Any:
        url = self.api_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)

        body: Optional[bytes] = None
        headers = {
            "PRIVATE-TOKEN": token if token is not None else self.token,
            "Accept": "application/json",
            "User-Agent": "gitlab-project-token-rotator/1.0",
        }
        if data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if not raw:
                    return {}
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise GitLabAPIError(method, url, response.status, raw) from exc
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise GitLabAPIError(method, url, exc.code, raw) from exc
        except urllib.error.URLError as exc:
            raise ConfigError(f"Failed to connect to GitLab API at {url}: {exc}") from exc

    def project_token_details(self, project_id: str, token_id: str) -> Dict[str, Any]:
        path = f"/projects/{quote_path_segment(project_id)}/access_tokens/{quote_path_segment(token_id)}"
        details = self.request("GET", path)
        if not isinstance(details, dict):
            raise ConfigError("GitLab token details response was not a JSON object")
        return details

    def current_token_details_fallback(self) -> Dict[str, Any]:
        details = self.request("GET", "/personal_access_tokens/self")
        if not isinstance(details, dict):
            raise ConfigError("GitLab self token details response was not a JSON object")
        return details

    def rotate_project_token(self, project_id: str, token_id: str, expires_at: str) -> Dict[str, Any]:
        path = f"/projects/{quote_path_segment(project_id)}/access_tokens/{quote_path_segment(token_id)}/rotate"
        rotated = self.request("POST", path, data={"expires_at": expires_at})
        if not isinstance(rotated, dict):
            raise ConfigError("GitLab rotate response was not a JSON object")
        return rotated

    def update_project_variable(
        self,
        project_id: str,
        variable_key: str,
        payload: Mapping[str, str],
        *,
        token: str,
        environment_scope_filter: Optional[str] = None,
    ) -> Dict[str, Any]:
        path = f"/projects/{quote_path_segment(project_id)}/variables/{quote_path_segment(variable_key)}"
        query = None
        if environment_scope_filter:
            query = {"filter[environment_scope]": environment_scope_filter}
        updated = self.request("PUT", path, token=token, data=payload, query=query)
        if not isinstance(updated, dict):
            raise ConfigError("GitLab variable update response was not a JSON object")
        return updated


def parse_args(argv: Optional[list[str]] = None) -> Config:
    parser = argparse.ArgumentParser(
        description="Self-rotate a GitLab project access token stored in a CI/CD variable."
    )
    parser.add_argument("--api-url", default=env_first("CI_API_V4_URL", default=DEFAULT_API_URL))
    parser.add_argument("--project-id", default=env_first("CI_PROJECT_ID"))
    parser.add_argument(
        "--token-var-name",
        default=env_first("GITLAB_PROJECT_TOKEN_VARIABLE", default=DEFAULT_TOKEN_VAR_NAME),
        help="Environment variable name that contains the current project access token.",
    )
    parser.add_argument(
        "--variable-key",
        default=None,
        help="GitLab CI/CD variable key to update. Defaults to --token-var-name.",
    )
    parser.add_argument(
        "--token-id",
        default=env_first("GITLAB_PROJECT_ACCESS_TOKEN_ID", default=DEFAULT_TOKEN_ID),
        help="Project access token id, or 'self' when supported by your GitLab version.",
    )
    parser.add_argument(
        "--threshold-days",
        default=env_first("TOKEN_ROTATE_BEFORE_DAYS", "ROTATE_BEFORE_DAYS", default=str(DEFAULT_THRESHOLD_DAYS)),
        help="Rotate when expires_at is this many days away or less.",
    )
    parser.add_argument(
        "--new-expires-in-days",
        default=env_first("TOKEN_NEW_EXPIRES_IN_DAYS", "NEW_TOKEN_EXPIRES_IN_DAYS", default=str(DEFAULT_NEW_EXPIRES_IN_DAYS)),
        help="Lifetime of the replacement token, counted from today's UTC date.",
    )
    parser.add_argument(
        "--timeout-seconds",
        default=env_first("GITLAB_API_TIMEOUT_SECONDS", default="30"),
    )
    parser.add_argument("--dry-run", action="store_true", help="Check expiry but do not rotate or update variables.")
    parser.add_argument(
        "--variable-environment-scope",
        default=env_first("GITLAB_VARIABLE_ENVIRONMENT_SCOPE"),
        help="Environment scope for projects with duplicate variable keys. Also sent as update field.",
    )
    parser.add_argument("--set-masked", default=env_first("GITLAB_VARIABLE_MASKED"), help="Optional true/false.")
    parser.add_argument("--set-protected", default=env_first("GITLAB_VARIABLE_PROTECTED"), help="Optional true/false.")
    parser.add_argument("--set-raw", default=env_first("GITLAB_VARIABLE_RAW"), help="Optional true/false.")
    parser.add_argument(
        "--set-variable-type",
        default=env_first("GITLAB_VARIABLE_TYPE"),
        help="Optional GitLab variable type: env_var or file.",
    )

    args = parser.parse_args(argv)
    if not args.project_id:
        raise ConfigError("Missing project id. Set CI_PROJECT_ID or pass --project-id.")

    token_var_name = args.token_var_name.strip()
    if not token_var_name:
        raise ConfigError("--token-var-name cannot be empty")

    variable_key = args.variable_key or token_var_name
    threshold_days = int_from_text(str(args.threshold_days), "threshold_days")
    new_expires_in_days = int_from_text(str(args.new_expires_in_days), "new_expires_in_days")
    timeout_seconds = int_from_text(str(args.timeout_seconds), "timeout_seconds")
    if timeout_seconds == 0:
        raise ConfigError("timeout_seconds must be greater than zero")

    return Config(
        api_url=normalize_api_url(args.api_url),
        project_id=str(args.project_id),
        token_var_name=token_var_name,
        variable_key=str(variable_key),
        token_id=str(args.token_id),
        threshold_days=threshold_days,
        new_expires_in_days=new_expires_in_days,
        timeout_seconds=timeout_seconds,
        dry_run=bool(args.dry_run),
        variable_environment_scope=args.variable_environment_scope,
        set_masked=args.set_masked,
        set_protected=args.set_protected,
        set_raw=args.set_raw,
        set_variable_type=args.set_variable_type,
    )


def get_token_details(client: GitLabClient, config: Config) -> Tuple[Dict[str, Any], str]:
    try:
        return client.project_token_details(config.project_id, config.token_id), "project_access_tokens"
    except GitLabAPIError as exc:
        if config.token_id == "self" and exc.status in {400, 401, 403, 404, 405}:
            try:
                return client.current_token_details_fallback(), "personal_access_tokens_self_fallback"
            except GitLabAPIError:
                raise exc
        raise


ClientFactory = Callable[[str, str, int], GitLabClient]
TodayProvider = Callable[[], date]
Printer = Callable[[str], None]


class GitLabProjectTokenRotator:
    """Coordinates token inspection, rotation, and CI/CD variable update.

    Runtime dependencies are injected so tests can import the package normally
    and exercise behavior without loading modules by filesystem path or touching
    the real process environment, clock, network, or stdout.
    """

    def __init__(
        self,
        config: Config,
        *,
        client_factory: Optional[ClientFactory] = None,
        environ: Optional[Mapping[str, str]] = None,
        today_provider: Optional[TodayProvider] = None,
        printer: Optional[Printer] = None,
    ):
        self.config = config
        self.client_factory = client_factory or GitLabClient
        self.environ = environ if environ is not None else os.environ
        self.today_provider = today_provider or date.today
        self.printer = printer or print

    def get_token_details(self, client: GitLabClient) -> Tuple[Dict[str, Any], str]:
        return get_token_details(client, self.config)

    def run(self) -> int:
        config = self.config
        token = self.environ.get(config.token_var_name)
        if not token:
            raise ConfigError(
                f"Missing token environment variable {config.token_var_name!r}. "
                "Create a masked GitLab CI/CD variable with this name."
            )

        client = self.client_factory(config.api_url, token, config.timeout_seconds)
        today = self.today_provider()

        details, details_source = self.get_token_details(client)
        expires_at = details.get("expires_at")
        token_id_report = details.get("id", config.token_id)

        if not expires_at:
            self.printer(
                f"Token {token_id_report} has no expires_at according to {details_source}; "
                "nothing to rotate."
            )
            return 0

        expiry_date = parse_yyyy_mm_dd(str(expires_at))
        remaining_days = days_until(expiry_date, today)
        self.printer(
            f"Token {token_id_report} expires_at={expires_at}; "
            f"days_until_expiry={remaining_days}; threshold_days={config.threshold_days}."
        )

        if remaining_days > config.threshold_days:
            self.printer("No rotation needed.")
            return 0

        new_expires_at = calculate_new_expires_at(today, config.new_expires_in_days)
        if config.dry_run:
            self.printer(
                "Dry run: token would be rotated and "
                f"{config.variable_key!r} would be updated; new_expires_at={new_expires_at}."
            )
            return 0

        self.printer(f"Rotating token {token_id_report}; replacement expires_at={new_expires_at}.")
        rotated = client.rotate_project_token(config.project_id, config.token_id, new_expires_at)
        new_token = rotated.get("token")
        if not isinstance(new_token, str) or not new_token:
            raise ConfigError("Rotate response did not contain a new token value")

        payload = build_variable_update_payload(
            new_token,
            environment_scope=config.variable_environment_scope,
            masked=config.set_masked,
            protected=config.set_protected,
            raw=config.set_raw,
            variable_type=config.set_variable_type,
        )
        client.update_project_variable(
            config.project_id,
            config.variable_key,
            payload,
            token=new_token,
            environment_scope_filter=config.variable_environment_scope,
        )

        rotated_id = rotated.get("id", token_id_report)
        rotated_expiry = rotated.get("expires_at", new_expires_at)
        self.printer(
            f"Rotation complete. Updated CI/CD variable {config.variable_key!r}. "
            f"new_token_id={rotated_id}; new_expires_at={rotated_expiry}; "
            "token_value=not_printed."
        )
        return 0


def run(config: Config) -> int:
    return GitLabProjectTokenRotator(config).run()


def main(argv: Optional[list[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    try:
        config = parse_args(argv)
        return run(config)
    except (ConfigError, GitLabAPIError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
