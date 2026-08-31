import unittest

from agent_runtime import (
    AgentKind,
    LaunchSpec,
    build_launch_argv,
    is_managed_session,
    make_session_name,
    parse_newstart_args,
    project_callback_data,
    resolve_project_dir,
)


class ParseNewStartArgsTests(unittest.TestCase):
    def test_defaults_to_claude(self):
        self.assertEqual(
            parse_newstart_args("/newstart mobile"),
            (AgentKind.CLAUDE, "mobile"),
        )

    def test_selects_codex(self):
        self.assertEqual(
            parse_newstart_args("/newstart codex mobile"),
            (AgentKind.CODEX, "mobile"),
        )

    def test_codex_without_project_is_supported(self):
        self.assertEqual(
            parse_newstart_args("/newstart codex"),
            (AgentKind.CODEX, None),
        )


class BuildLaunchArgvTests(unittest.TestCase):
    def test_builds_claude_launch_without_shell(self):
        spec = LaunchSpec(
            agent=AgentKind.CLAUDE,
            session_name="claude-123",
            project_dir="/tmp/project with spaces",
        )

        self.assertEqual(
            build_launch_argv(spec),
            [
                "tmux", "new-session", "-d", "-s", "claude-123",
                "-c", "/tmp/project with spaces",
                "claude", "--dangerously-skip-permissions",
            ],
        )

    def test_builds_codex_launch_with_full_access_and_settings(self):
        spec = LaunchSpec(
            agent=AgentKind.CODEX,
            session_name="codex-123",
            project_dir="/tmp/project",
            model="gpt-5.6-sol",
            effort="high",
        )

        self.assertEqual(
            build_launch_argv(spec),
            [
                "tmux", "new-session", "-d", "-s", "codex-123",
                "-c", "/tmp/project",
                "codex", "--dangerously-bypass-approvals-and-sandbox",
                "--no-alt-screen", "-m", "gpt-5.6-sol",
                "-c", 'model_reasoning_effort="high"',
            ],
        )

    def test_rejects_unsafe_model_value(self):
        spec = LaunchSpec(
            agent=AgentKind.CODEX,
            session_name="codex-123",
            project_dir="/tmp/project",
            model="gpt; touch /tmp/pwned",
        )

        with self.assertRaisesRegex(ValueError, "model"):
            build_launch_argv(spec)


class SessionLifecycleTests(unittest.TestCase):
    def test_session_name_contains_agent_kind(self):
        self.assertEqual(make_session_name(AgentKind.CLAUDE, 123), "claude-123")
        self.assertEqual(make_session_name(AgentKind.CODEX, 123), "codex-123")

    def test_recognizes_only_managed_session_prefixes(self):
        self.assertTrue(is_managed_session("claude-123"))
        self.assertTrue(is_managed_session("codex-123"))
        self.assertFalse(is_managed_session("shell-123"))

    def test_claude_project_callback_stays_backward_compatible(self):
        self.assertEqual(
            project_callback_data(AgentKind.CLAUDE, "mobile"),
            "start:mobile",
        )

    def test_codex_project_callback_keeps_agent_kind(self):
        self.assertEqual(
            project_callback_data(AgentKind.CODEX, "mobile"),
            "start:codex:mobile",
        )

    def test_project_path_cannot_escape_projects_root(self):
        with self.assertRaisesRegex(ValueError, "project path"):
            resolve_project_dir("../private", "/Users/me/projects", "/Users/me")


if __name__ == "__main__":
    unittest.main()
