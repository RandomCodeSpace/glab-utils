import contextlib
import importlib.util
import io
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError

SCRIPT = pathlib.Path(__file__).with_name("semantic_version_bumper.py")


def load_module():
    spec = importlib.util.spec_from_file_location("semantic_version_bumper", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")

    def getheaders(self):
        return list(self.headers.items())


class ParserTests(unittest.TestCase):
    def test_parser_defaults_to_auto_mode_and_module_tag_template(self):
        module = load_module()
        args = module.build_parser().parse_args(["--module", "token-rotate"])
        self.assertEqual(args.mode, "auto")
        self.assertEqual(args.tag_template, "{module}/v{version}")
        self.assertEqual(args.initial_version, "0.0.0")
        self.assertEqual(args.gitlab_auth, "auto")
        self.assertFalse(args.reserve_release)


class SemVerTests(unittest.TestCase):
    def test_parse_stable_and_rc_versions(self):
        module = load_module()
        self.assertEqual(str(module.SemVer.parse("1.2.3")), "1.2.3")
        self.assertEqual(str(module.SemVer.parse("1.2.3-rc.4")), "1.2.3-rc.4")

    def test_reject_invalid_versions(self):
        module = load_module()
        for value in ["1.2", "1.2.3.4", "1.2.3-beta.1", "v1.2.3", "foo"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    module.SemVer.parse(value)

    def test_stable_sorts_after_rc_for_same_base(self):
        module = load_module()
        versions = [module.SemVer.parse(v) for v in ["1.0.0-rc.2", "1.0.0", "1.0.0-rc.1"]]
        self.assertEqual([str(v) for v in sorted(versions)], ["1.0.0-rc.1", "1.0.0-rc.2", "1.0.0"])


class TagTemplateTests(unittest.TestCase):
    def test_extract_module_scoped_version(self):
        module = load_module()
        version = module.extract_version_from_tag("token-rotate/v1.2.3", "token-rotate", "{module}/v{version}")
        self.assertEqual(str(version), "1.2.3")

    def test_ignore_other_module_tag(self):
        module = load_module()
        self.assertIsNone(module.extract_version_from_tag("api/v1.2.3", "worker", "{module}/v{version}"))

    def test_extract_single_module_plain_tag(self):
        module = load_module()
        version = module.extract_version_from_tag("v2.0.1", "root", "v{version}")
        self.assertEqual(str(version), "2.0.1")

    def test_build_tag_uses_template(self):
        module = load_module()
        self.assertEqual(module.build_tag("api", module.SemVer.parse("1.2.3"), "{module}/v{version}"), "api/v1.2.3")


class GitTagDiscoveryTests(unittest.TestCase):
    def test_list_git_tags_from_repo(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("test\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "tag", "-a", "token-rotate/v1.2.3", "-m", "token-rotate/v1.2.3"], cwd=repo, check=True)
            self.assertEqual(module.list_git_tags(repo), ["token-rotate/v1.2.3"])


class GitLabClientTests(unittest.TestCase):
    def test_job_token_uses_job_token_header(self):
        module = load_module()
        client = module.GitLabClient("https://gitlab.example/api/v4", "123", "jobtok", "job-token")
        request = client.build_request("/projects/123/releases", method="POST", data={"tag_name": "module/v1.0.0", "ref": "abc"})
        self.assertEqual(request.headers["Job-token"], "jobtok")
        self.assertNotIn("Private-token", request.headers)

    def test_private_token_uses_private_token_header(self):
        module = load_module()
        client = module.GitLabClient("https://gitlab.example/api/v4", "123", "priv", "private-token")
        request = client.build_request("/projects/123/repository/tags")
        self.assertEqual(request.headers["Private-token"], "priv")
        self.assertNotIn("Job-token", request.headers)

    def test_list_gitlab_tags_uses_prefix_and_pagination(self):
        module = load_module()
        responses = [
            FakeResponse([{"name": "token-rotate/v1.2.3"}], {"X-Next-Page": "2"}),
            FakeResponse([{"name": "token-rotate/v1.2.4"}], {"X-Next-Page": ""}),
        ]
        captured_urls = []

        def fake_urlopen(request, timeout=30):
            captured_urls.append(request.full_url)
            return responses.pop(0)

        client = module.GitLabClient("https://gitlab.example/api/v4", "group/project", "jobtok", "job-token")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            tags = client.list_tags("^token-rotate/v")
        self.assertEqual(tags, ["token-rotate/v1.2.3", "token-rotate/v1.2.4"])
        self.assertIn("/projects/group%2Fproject/repository/tags", captured_urls[0])
        self.assertIn("search=%5Etoken-rotate%2Fv", captured_urls[0])
        self.assertIn("page=2", captured_urls[1])

    def test_get_tag_url_encodes_slashes(self):
        module = load_module()
        captured = []

        def fake_urlopen(request, timeout=30):
            captured.append(request.full_url)
            return FakeResponse({"name": "module-a/v1.2.3", "target": "abc"})

        client = module.GitLabClient("https://gitlab.example/api/v4", "123", "jobtok", "job-token")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            tag = client.get_tag("module-a/v1.2.3")
        self.assertEqual(tag["target"], "abc")
        self.assertIn("module-a%2Fv1.2.3", captured[0])

    def test_create_release_posts_form_fields(self):
        module = load_module()
        captured = []

        def fake_urlopen(request, timeout=30):
            captured.append(request)
            return FakeResponse({"tag_name": "module/v1.0.0"})

        client = module.GitLabClient("https://gitlab.example/api/v4", "123", "jobtok", "job-token")
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            client.create_release("module/v1.0.0", "abc", "module/v1.0.0", "release notes")
        body = captured[0].data.decode("utf-8")
        self.assertIn("tag_name=module%2Fv1.0.0", body)
        self.assertIn("ref=abc", body)
        self.assertIn("description=release+notes", body)

    def test_reserve_release_reuses_existing_tag_on_same_commit(self):
        module = load_module()
        client = module.GitLabClient("https://gitlab.example/api/v4", "123", "jobtok", "job-token")
        client.get_current_commit_tags = mock.Mock(return_value=["module/v1.0.0"])
        client.get_tag = mock.Mock(return_value={"target": "abc"})
        client.create_release = mock.Mock()
        result = client.reserve_release_tag("module/v1.0.0", "abc", "name", "desc", "fail")
        self.assertTrue(result.reused)
        self.assertFalse(result.created)
        client.create_release.assert_not_called()

    def test_reserve_release_fails_existing_tag_on_other_commit(self):
        module = load_module()
        client = module.GitLabClient("https://gitlab.example/api/v4", "123", "jobtok", "job-token")
        client.get_current_commit_tags = mock.Mock(return_value=[])
        client.get_tag = mock.Mock(return_value={"target": "different"})
        with self.assertRaises(module.GitLabError):
            client.reserve_release_tag("module/v1.0.0", "abc", "name", "desc", "fail")

    def test_reserve_release_rereads_after_conflict(self):
        module = load_module()
        client = module.GitLabClient("https://gitlab.example/api/v4", "123", "jobtok", "job-token")
        client.get_current_commit_tags = mock.Mock(return_value=[])
        client.get_tag = mock.Mock(side_effect=[None, {"target": "abc"}])
        client.create_release = mock.Mock(side_effect=module.GitLabConflict("conflict"))
        result = client.reserve_release_tag("module/v1.0.0", "abc", "name", "desc", "fail")
        self.assertTrue(result.reused)
        self.assertFalse(result.created)


class CalculationTests(unittest.TestCase):
    def test_patch_bumps_latest_stable_for_module(self):
        module = load_module()
        versions = [module.SemVer.parse(v) for v in ["1.0.0", "1.0.1", "1.1.0-rc.1"]]
        self.assertEqual(str(module.next_patch_version(versions, module.SemVer.parse("0.0.0"))), "1.0.2")

    def test_patch_from_no_tags_uses_initial_version(self):
        module = load_module()
        self.assertEqual(str(module.next_patch_version([], module.SemVer.parse("0.0.0"))), "0.0.1")

    def test_release_branch_with_minor_starts_rc_line(self):
        module = load_module()
        versions = [module.SemVer.parse("1.4.7")]
        self.assertEqual(str(module.next_rc_version(versions, module.SemVer.parse("0.0.0"), "release/1.5")), "1.5.0-rc.1")

    def test_release_branch_increments_existing_rc_line(self):
        module = load_module()
        versions = [module.SemVer.parse(v) for v in ["1.4.7", "1.5.0-rc.1", "1.5.0-rc.2"]]
        self.assertEqual(str(module.next_rc_version(versions, module.SemVer.parse("0.0.0"), "release/1.5")), "1.5.0-rc.3")

    def test_generic_rc_from_no_tags_uses_default_minor(self):
        module = load_module()
        self.assertEqual(str(module.next_rc_version([], module.SemVer.parse("0.0.0"), "release-candidate")), "0.1.0-rc.1")

    def test_explicit_target_version_overrides_calculation(self):
        module = load_module()
        result = module.calculate_next_version(
            module.Config(module="api", mode="rc", branch="main", tags=[], target_version="1.0.0-rc.1")
        )
        self.assertEqual(result.next_version, "1.0.0-rc.1")

    def test_auto_mode_detects_rc_branch(self):
        module = load_module()
        self.assertEqual(module.resolve_mode("auto", "rc/2.0"), "rc")

    def test_snapshot_uses_next_patch_base_and_ci_identifiers(self):
        module = load_module()
        versions = [module.SemVer.parse("1.4.7")]
        result = module.next_snapshot_version(versions, module.SemVer.parse("0.0.0"), "main", "732", "a1b2c3d4", False)
        self.assertEqual(result, "1.4.8-snapshot.732.a1b2c3d4")

    def test_release_branch_snapshot_uses_release_line_without_reserving_rc(self):
        module = load_module()
        versions = [module.SemVer.parse("1.4.7")]
        result = module.next_snapshot_version(versions, module.SemVer.parse("0.0.0"), "release/1.5", "733", "b2c3d4e5", False)
        self.assertEqual(result, "1.5.0-snapshot.733.b2c3d4e5")


class CliEndToEndTests(unittest.TestCase):
    def test_main_prints_patch_output_and_writes_env(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            repo = pathlib.Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
            (repo / "README.md").write_text("test\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
            subprocess.run(["git", "tag", "-a", "api/v1.2.3", "-m", "api/v1.2.3"], cwd=repo, check=True)
            env_path = repo / "version.env"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = module.main(["--module", "api", "--mode", "patch", "--repo", str(repo), "--write-env", str(env_path)])
            self.assertEqual(code, 0)
            self.assertIn("next_version=1.2.4", stdout.getvalue())
            self.assertIn("NEXT_TAG=api/v1.2.4", env_path.read_text(encoding="utf-8"))

    def test_main_json_output_is_valid(self):
        module = load_module()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = module.main(["--module", "api", "--mode", "rc", "--branch", "release/1.0", "--tag-source", "none", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["next_version"], "1.0.0-rc.1")
        self.assertEqual(data["tag"], "api/v1.0.0-rc.1")

    def test_reserve_release_is_rejected_for_snapshot_mode(self):
        module = load_module()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = module.main(["--module", "api", "--mode", "snapshot", "--reserve-release", "--tag-source", "none"])
        self.assertEqual(code, 2)
        self.assertIn("snapshot", stderr.getvalue())

    def test_snapshot_template_cli_override_is_used(self):
        module = load_module()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = module.main([
                "--module", "api",
                "--mode", "snapshot",
                "--tag-source", "none",
                "--pipeline-iid", "7",
                "--commit-short-sha", "abc1234",
                "--snapshot-template", "{base_version}-SNAPSHOT",
                "--json",
            ])
        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["next_version"], "0.0.1-SNAPSHOT")

    def test_reserve_release_reuses_current_commit_tag_before_calculating_next(self):
        module = load_module()

        class FakeClient:
            def __init__(self):
                self.list_tags = mock.Mock(side_effect=AssertionError("should not query tags after current-ref hit"))
                self.get_current_commit_tags = mock.Mock(return_value=["api/v1.2.4"])
                self.get_tag = mock.Mock(return_value={"target": "abc"})
                self.reserve_release_tag = mock.Mock(side_effect=AssertionError("current-ref hit is already reserved"))

        fake_client = FakeClient()
        stdout = io.StringIO()
        with mock.patch.object(module, "make_gitlab_client", return_value=fake_client), contextlib.redirect_stdout(stdout):
            code = module.main([
                "--module", "api",
                "--mode", "patch",
                "--tag-source", "gitlab-api",
                "--reserve-release",
                "--release-ref", "abc",
                "--json",
            ])
        self.assertEqual(code, 0)
        data = json.loads(stdout.getvalue())
        self.assertEqual(data["next_version"], "1.2.4")
        self.assertEqual(data["tag"], "api/v1.2.4")
        self.assertTrue(data["reused"])
        self.assertFalse(data["reserved"])

    def test_self_test_flag_runs_embedded_tests(self):
        module = load_module()
        self.assertEqual(module.main(["--self-test"]), 0)


if __name__ == "__main__":
    unittest.main()
