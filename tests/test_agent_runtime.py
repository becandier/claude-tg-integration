import unittest

from agent_runtime import (
    AgentKind,
    LaunchSpec,
    build_launch_argv,
    parse_codex_effort_picker,
    parse_codex_model_picker,
    picker_navigation_keys,
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

    def test_newcodex_alias_selects_codex(self):
        self.assertEqual(
            parse_newstart_args("/newcodex mobile"),
            (AgentKind.CODEX, "mobile"),
        )

    def test_newclaude_alias_selects_claude(self):
        self.assertEqual(
            parse_newstart_args("/newclaude mobile"),
            (AgentKind.CLAUDE, "mobile"),
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
            notify_command=("python3", "/tmp/tg_codex_notify_hook.py"),
        )

        self.assertEqual(
            build_launch_argv(spec),
            [
                "tmux", "new-session", "-d", "-s", "codex-123",
                "-c", "/tmp/project",
                "codex", "--dangerously-bypass-approvals-and-sandbox",
                "--dangerously-bypass-hook-trust",
                "--no-alt-screen", "-m", "gpt-5.6-sol",
                "-c", 'model_reasoning_effort="high"',
                "-c", 'notify=["python3","/tmp/tg_codex_notify_hook.py"]',
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


class CodexPickerTests(unittest.TestCase):
    def test_parses_models_and_current_selection(self):
        output = """
  Select Model and Effort
› 1. gpt-5.6-sol (current)  Latest frontier model.
  2. gpt-5.6-terra          Balanced model.
  3. gpt-5.6-luna           Fast model.
"""

        options = parse_codex_model_picker(output)

        self.assertEqual([option.value for option in options], [
            "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
        ])
        self.assertTrue(options[0].selected)
        self.assertTrue(options[0].current)

    def test_parses_primary_and_advanced_efforts(self):
        primary = """
  Select Reasoning Level for gpt-5.6-sol
  1. Low (default)     Fast responses
› 2. Medium (current)  Balanced
  3. High              Greater depth
  4. Extra high        Extra depth
  5. More reasoning…   Max and Ultra
"""
        advanced = """
  Advanced Reasoning
› 1. Max    For difficult problems
  2. Ultra  For demanding work
"""

        self.assertEqual(
            [option.value for option in parse_codex_effort_picker(primary)],
            ["low", "medium", "high", "xhigh", "more"],
        )
        self.assertEqual(
            [option.value for option in parse_codex_effort_picker(advanced)],
            ["max", "ultra"],
        )

    def test_builds_navigation_from_selected_option(self):
        output = """
  Select Model and Effort
  1. gpt-5.6-sol (current)
› 2. gpt-5.6-terra
  3. gpt-5.6-luna
"""
        options = parse_codex_model_picker(output)

        self.assertEqual(picker_navigation_keys(options, "gpt-5.6-luna"), ("Down",))
        self.assertEqual(picker_navigation_keys(options, "gpt-5.6-sol"), ("Up",))


if __name__ == "__main__":
    unittest.main()
