#!/usr/bin/env python3
"""Unit tests for gitlab_project_token_rotator.py.

Tests use only the Python standard library and mock GitLab API/client behavior;
no real GitLab API calls are made.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).with_name("gitlab_project_token_rotator.py")
SPEC = importlib.util.spec_from_file_location("gitlab_project_token_rotator", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
rotator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = rotator
SPEC.loader.exec_module(rotator)


class RotatorUnitTests(unittest.TestCase):
    def make_config(self, **overrides):
        values = {
            "api_url": "https://gitlab.example.com/api/v4",
            "project_id": "123",
            "token_var_name": "PROJECT_TOKEN",
            "variable_key": "PROJECT_TOKEN",
            "token_id": "self",
            "threshold_days": 30,
            "new_expires_in_days": 90,
            "timeout_seconds": 30,
            "dry_run": False,
            "variable_environment_scope": None,
            "set_masked": None,
            "set_protected": None,
            "set_raw": None,
            "set_variable_type": None,
        }
        values.update(overrides)
        return rotator.Config(**values)

    def test_should_rotate_when_expiry_is_inside_threshold(self) -> None:
        self.assertTrue(rotator.should_rotate("2026-05-20", 5, date(2026, 5, 16)))

    def test_should_rotate_when_expiry_is_today(self) -> None:
        self.assertTrue(rotator.should_rotate("2026-05-16", 0, date(2026, 5, 16)))

    def test_should_rotate_when_expiry_has_passed(self) -> None:
        self.assertTrue(rotator.should_rotate("2026-05-15", 0, date(2026, 5, 16)))

    def test_should_not_rotate_when_expiry_is_after_threshold(self) -> None:
        self.assertFalse(rotator.should_rotate("2026-05-22", 5, date(2026, 5, 16)))

    def test_invalid_expiry_date_raises_config_error(self) -> None:
        with self.assertRaises(rotator.ConfigError):
            rotator.should_rotate("16-05-2026", 5, date(2026, 5, 16))

    def test_bool_to_gitlab_normalizes_truthy_and_falsey_values(self) -> None:
        self.assertEqual(rotator.bool_to_gitlab("yes"), "true")
        self.assertEqual(rotator.bool_to_gitlab("TRUE"), "true")
        self.assertEqual(rotator.bool_to_gitlab("0"), "false")
        self.assertEqual(rotator.bool_to_gitlab("off"), "false")

    def test_invalid_bool_raises_config_error(self) -> None:
        with self.assertRaises(rotator.ConfigError):
            rotator.bool_to_gitlab("maybe")

    def test_build_variable_update_payload_includes_only_requested_attributes(self) -> None:
        self.assertEqual(rotator.build_variable_update_payload("secret"), {"value": "secret"})
        self.assertEqual(
            rotator.build_variable_update_payload(
                "secret",
                environment_scope="production",
                masked="true",
                protected="0",
                raw="yes",
                variable_type="env_var",
            ),
            {
                "value": "secret",
                "environment_scope": "production",
                "masked": "true",
                "protected": "false",
                "raw": "true",
                "variable_type": "env_var",
            },
        )

    def test_invalid_variable_type_raises_config_error(self) -> None:
        with self.assertRaises(rotator.ConfigError):
            rotator.build_variable_update_payload("secret", variable_type="dotenv")

    def test_quote_path_segment_encodes_namespaced_project_path(self) -> None:
        self.assertEqual(rotator.quote_path_segment("group/sub/project"), "group%2Fsub%2Fproject")

    def test_calculate_new_expires_at_uses_today_plus_lifetime_days(self) -> None:
        self.assertEqual(rotator.calculate_new_expires_at(date(2026, 5, 16), 90), "2026-08-14")

    def test_parse_args_reads_required_values(self) -> None:
        config = rotator.parse_args([
            "--project-id", "group/sub/project",
            "--token-var-name", "PROJECT_TOKEN",
            "--threshold-days", "15",
            "--new-expires-in-days", "180",
            "--timeout-seconds", "10",
            "--set-masked", "true",
        ])
        self.assertEqual(config.project_id, "group/sub/project")
        self.assertEqual(config.variable_key, "PROJECT_TOKEN")
        self.assertEqual(config.threshold_days, 15)
        self.assertEqual(config.new_expires_in_days, 180)
        self.assertEqual(config.timeout_seconds, 10)
        self.assertEqual(config.set_masked, "true")

    def test_parse_args_rejects_missing_project_id(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(rotator.ConfigError):
                rotator.parse_args([])

    def test_parse_args_rejects_negative_threshold(self) -> None:
        with self.assertRaises(rotator.ConfigError):
            rotator.parse_args(["--project-id", "1", "--threshold-days", "-1"])

    def test_get_token_details_falls_back_for_self_when_project_endpoint_fails(self) -> None:
        client = mock.Mock()
        client.project_token_details.side_effect = rotator.GitLabAPIError("GET", "url", 404, "not found")
        client.current_token_details_fallback.return_value = {"id": 99, "expires_at": "2026-05-16"}

        details, source = rotator.get_token_details(client, self.make_config(token_id="self"))

        self.assertEqual(details["id"], 99)
        self.assertEqual(source, "personal_access_tokens_self_fallback")
        client.current_token_details_fallback.assert_called_once_with()

    def test_get_token_details_does_not_fallback_for_explicit_token_id(self) -> None:
        client = mock.Mock()
        client.project_token_details.side_effect = rotator.GitLabAPIError("GET", "url", 404, "not found")

        with self.assertRaises(rotator.GitLabAPIError):
            rotator.get_token_details(client, self.make_config(token_id="42"))

        client.current_token_details_fallback.assert_not_called()

    def test_run_raises_when_token_env_var_is_missing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(rotator.ConfigError):
                rotator.run(self.make_config())

    def test_run_does_not_rotate_when_token_expiry_is_outside_threshold(self) -> None:
        config = self.make_config(threshold_days=1)
        with mock.patch.dict(os.environ, {"PROJECT_TOKEN": "old-token"}), mock.patch(
            "gitlab_project_token_rotator.GitLabClient"
        ) as client_class, mock.patch("builtins.print"):
            client = client_class.return_value
            client.project_token_details.return_value = {"id": 42, "expires_at": "2099-01-01"}

            self.assertEqual(rotator.run(config), 0)

            client.rotate_project_token.assert_not_called()
            client.update_project_variable.assert_not_called()

    def test_run_dry_run_does_not_mutate_gitlab(self) -> None:
        config = self.make_config(dry_run=True, threshold_days=30)
        with mock.patch.dict(os.environ, {"PROJECT_TOKEN": "old-token"}), mock.patch(
            "gitlab_project_token_rotator.GitLabClient"
        ) as client_class, mock.patch("builtins.print"):
            client = client_class.return_value
            client.project_token_details.return_value = {"id": 42, "expires_at": date.today().isoformat()}

            self.assertEqual(rotator.run(config), 0)

            client.rotate_project_token.assert_not_called()
            client.update_project_variable.assert_not_called()

    def test_run_rotates_and_updates_variable_with_new_token(self) -> None:
        today_text = date.today().isoformat()
        config = self.make_config(
            project_id="group/sub/project",
            threshold_days=30,
            variable_environment_scope="production",
            set_masked="true",
        )
        with mock.patch.dict(os.environ, {"PROJECT_TOKEN": "old-token"}), mock.patch(
            "gitlab_project_token_rotator.GitLabClient"
        ) as client_class, mock.patch("builtins.print"):
            client = client_class.return_value
            client.project_token_details.return_value = {"id": 42, "expires_at": today_text}
            client.rotate_project_token.return_value = {
                "id": 43,
                "expires_at": rotator.calculate_new_expires_at(date.today(), 90),
                "token": "new-token",
            }

            self.assertEqual(rotator.run(config), 0)

            client.rotate_project_token.assert_called_once_with(
                "group/sub/project", "self", rotator.calculate_new_expires_at(date.today(), 90)
            )
            client.update_project_variable.assert_called_once_with(
                "group/sub/project",
                "PROJECT_TOKEN",
                {"value": "new-token", "environment_scope": "production", "masked": "true"},
                token="new-token",
                environment_scope_filter="production",
            )

    def test_run_rejects_rotate_response_without_new_token(self) -> None:
        config = self.make_config(threshold_days=30)
        with mock.patch.dict(os.environ, {"PROJECT_TOKEN": "old-token"}), mock.patch(
            "gitlab_project_token_rotator.GitLabClient"
        ) as client_class, mock.patch("builtins.print"):
            client = client_class.return_value
            client.project_token_details.return_value = {"id": 42, "expires_at": date.today().isoformat()}
            client.rotate_project_token.return_value = {"id": 43, "expires_at": "2099-01-01"}

            with self.assertRaises(rotator.ConfigError):
                rotator.run(config)

            client.update_project_variable.assert_not_called()

    def test_gitlab_client_builds_encoded_request_for_variable_update(self) -> None:
        client = rotator.GitLabClient("https://gitlab.example.com/api/v4/", "old-token", 30)
        with mock.patch.object(client, "request", return_value={"key": "PROJECT_TOKEN"}) as request:
            result = client.update_project_variable(
                "group/sub/project",
                "PROJECT_TOKEN",
                {"value": "new-token"},
                token="new-token",
                environment_scope_filter="production",
            )

        self.assertEqual(result, {"key": "PROJECT_TOKEN"})
        request.assert_called_once_with(
            "PUT",
            "/projects/group%2Fsub%2Fproject/variables/PROJECT_TOKEN",
            token="new-token",
            data={"value": "new-token"},
            query={"filter[environment_scope]": "production"},
        )

    def test_gitlab_client_request_posts_form_and_parses_json(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b'{"ok": true}'

        client = rotator.GitLabClient("https://gitlab.example.com/api/v4/", "default-token", 30)
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()) as urlopen:
            result = client.request(
                "POST",
                "/projects/123/access_tokens/self/rotate",
                token="override",
                data={"expires_at": "2026-08-14"},
                query={"dry": "false"},
            )

        self.assertEqual(result, {"ok": True})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://gitlab.example.com/api/v4/projects/123/access_tokens/self/rotate?dry=false")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.data, b"expires_at=2026-08-14")
        self.assertEqual(request.headers["Private-token"], "override")
        self.assertEqual(request.headers["Content-type"], "application/x-www-form-urlencoded")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 30)

    def test_gitlab_client_request_returns_empty_object_for_empty_body(self) -> None:
        class FakeResponse:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b""

        client = rotator.GitLabClient("https://gitlab.example.com/api/v4", "default-token", 30)
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            self.assertEqual(client.request("GET", "/empty"), {})

    def test_gitlab_client_request_wraps_invalid_json(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return b"not-json"

        client = rotator.GitLabClient("https://gitlab.example.com/api/v4", "default-token", 30)
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse()):
            with self.assertRaises(rotator.GitLabAPIError):
                client.request("GET", "/bad-json")

    def test_gitlab_client_request_wraps_http_error(self) -> None:
        error = rotator.urllib.error.HTTPError(
            "https://gitlab.example.com/api/v4/fail",
            403,
            "forbidden",
            hdrs=None,
            fp=mock.Mock(read=lambda: b'{"message":"forbidden"}'),
        )
        client = rotator.GitLabClient("https://gitlab.example.com/api/v4", "default-token", 30)
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(rotator.GitLabAPIError) as raised:
                client.request("GET", "/fail")
        self.assertEqual(raised.exception.status, 403)

    def test_gitlab_client_request_wraps_url_error(self) -> None:
        client = rotator.GitLabClient("https://gitlab.example.com/api/v4", "default-token", 30)
        with mock.patch("urllib.request.urlopen", side_effect=rotator.urllib.error.URLError("offline")):
            with self.assertRaises(rotator.ConfigError):
                client.request("GET", "/offline")

    def test_gitlab_client_wrappers_reject_non_object_responses(self) -> None:
        client = rotator.GitLabClient("https://gitlab.example.com/api/v4", "default-token", 30)
        with mock.patch.object(client, "request", return_value=[]):
            with self.assertRaises(rotator.ConfigError):
                client.project_token_details("123", "self")
            with self.assertRaises(rotator.ConfigError):
                client.current_token_details_fallback()
            with self.assertRaises(rotator.ConfigError):
                client.rotate_project_token("123", "self", "2026-08-14")
            with self.assertRaises(rotator.ConfigError):
                client.update_project_variable("123", "PROJECT_TOKEN", {"value": "new"}, token="new")

    def test_gitlab_client_wrappers_return_object_responses(self) -> None:
        client = rotator.GitLabClient("https://gitlab.example.com/api/v4", "default-token", 30)
        with mock.patch.object(client, "request", return_value={"id": 1}) as request:
            self.assertEqual(client.project_token_details("group/sub/project", "self"), {"id": 1})
            self.assertEqual(client.current_token_details_fallback(), {"id": 1})
            self.assertEqual(client.rotate_project_token("group/sub/project", "self", "2026-08-14"), {"id": 1})

        called_paths = [call.args[1] for call in request.call_args_list]
        self.assertIn("/projects/group%2Fsub%2Fproject/access_tokens/self", called_paths)
        self.assertIn("/personal_access_tokens/self", called_paths)
        self.assertIn("/projects/group%2Fsub%2Fproject/access_tokens/self/rotate", called_paths)

    def test_get_token_details_raises_original_error_when_fallback_fails(self) -> None:
        original = rotator.GitLabAPIError("GET", "project-url", 404, "not found")
        fallback = rotator.GitLabAPIError("GET", "self-url", 404, "not found")
        client = mock.Mock()
        client.project_token_details.side_effect = original
        client.current_token_details_fallback.side_effect = fallback

        with self.assertRaises(rotator.GitLabAPIError) as raised:
            rotator.get_token_details(client, self.make_config(token_id="self"))

        self.assertIs(raised.exception, original)

    def test_parse_args_rejects_empty_token_variable_name(self) -> None:
        with self.assertRaises(rotator.ConfigError):
            rotator.parse_args(["--project-id", "1", "--token-var-name", ""])

    def test_parse_args_rejects_zero_timeout(self) -> None:
        with self.assertRaises(rotator.ConfigError):
            rotator.parse_args(["--project-id", "1", "--timeout-seconds", "0"])

    def test_parse_args_rejects_non_integer_threshold(self) -> None:
        with self.assertRaises(rotator.ConfigError):
            rotator.parse_args(["--project-id", "1", "--threshold-days", "soon"])

    def test_run_exits_without_rotation_when_token_has_no_expiry(self) -> None:
        config = self.make_config()
        with mock.patch.dict(os.environ, {"PROJECT_TOKEN": "old-token"}), mock.patch(
            "gitlab_project_token_rotator.GitLabClient"
        ) as client_class, mock.patch("builtins.print"):
            client = client_class.return_value
            client.project_token_details.return_value = {"id": 42, "expires_at": None}

            self.assertEqual(rotator.run(config), 0)

            client.rotate_project_token.assert_not_called()
            client.update_project_variable.assert_not_called()

    def test_main_returns_zero_when_run_succeeds(self) -> None:
        with mock.patch("gitlab_project_token_rotator.parse_args", return_value=self.make_config()) as parse_args, mock.patch(
            "gitlab_project_token_rotator.run", return_value=0
        ) as run:
            self.assertEqual(rotator.main(["--project-id", "1"]), 0)
        parse_args.assert_called_once_with(["--project-id", "1"])
        run.assert_called_once()

    def test_main_returns_one_when_config_error_is_raised(self) -> None:
        with mock.patch("gitlab_project_token_rotator.parse_args", side_effect=rotator.ConfigError("bad config")), mock.patch(
            "sys.stderr"
        ):
            self.assertEqual(rotator.main(["--project-id", "1"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
