"""配置层测试：.env 解析、环境变量优先级、校验与路径归一化。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codeagent.config import Config, detect_api_key, is_placeholder_key, parse_env_file
from codeagent.errors import ConfigError


class TestPlaceholderKey(unittest.TestCase):
    """.env.example 被直接复制时，应给出「占位符」提示而不是让用户吃 401。"""

    def test_empty_is_placeholder(self) -> None:
        self.assertTrue(is_placeholder_key(""))
        self.assertTrue(is_placeholder_key("   "))

    def test_example_keys_are_placeholders(self) -> None:
        for value in ("sk-你的key", "在此填入真实key", "your-api-key", "sk-xxxxxxxx", "<your key>"):
            self.assertTrue(is_placeholder_key(value), msg=value)

    def test_real_looking_key_is_not_placeholder(self) -> None:
        for value in ("sk-fake-3f9a1c2b4d5e6f708192a3b4c5d6e7f8", "ollama"):
            self.assertFalse(is_placeholder_key(value), msg=value)

    def test_validate_rejects_placeholder_key(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            Config(provider="openai", api_key="sk-你的key").validate()
        self.assertIn("占位符", str(ctx.exception))

    def test_validate_accepts_real_key(self) -> None:
        Config(provider="openai", api_key="sk-fake-3f9a1c2b4d5e6f708192a3b4c5d6e7f8").validate()


class TestEnvFile(unittest.TestCase):
    def test_parse_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# 注释\nCODEAGENT_MODEL=qwen-plus\n\nCODEAGENT_API_KEY=\"secret\"\nexport FOO=bar\n非法行\n",
                encoding="utf-8",
            )
            parsed = parse_env_file(path)
        self.assertEqual(parsed["CODEAGENT_MODEL"], "qwen-plus")
        self.assertEqual(parsed["CODEAGENT_API_KEY"], "secret")
        self.assertEqual(parsed["FOO"], "bar")
        self.assertNotIn("非法行", parsed)

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(parse_env_file(Path("不存在的文件.env")), {})

    def test_utf8_bom_is_stripped(self) -> None:
        """记事本保存的 .env 常带 BOM；若不清除，第一个键会被静默忽略。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_bytes("\ufeffCODEAGENT_API_KEY=sk-bom-test\n".encode("utf-8"))
            parsed = parse_env_file(path)
        self.assertEqual(parsed.get("CODEAGENT_API_KEY"), "sk-bom-test")

    def test_crlf_line_endings(self) -> None:
        """Windows 记事本默认 CRLF，必须能正常解析。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_bytes(b"CODEAGENT_MODEL=gpt-4o-mini\r\nCODEAGENT_MAX_STEPS=7\r\n")
            parsed = parse_env_file(path)
        self.assertEqual(parsed["CODEAGENT_MODEL"], "gpt-4o-mini")
        self.assertEqual(parsed["CODEAGENT_MAX_STEPS"], "7")

    def test_spaces_around_equals_are_tolerated(self) -> None:
        """CODEAGENT_API_KEY = sk-xxx 这种手写格式也应生效。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("CODEAGENT_API_KEY = sk-spaced-value\n", encoding="utf-8")
            self.assertEqual(parse_env_file(path)["CODEAGENT_API_KEY"], "sk-spaced-value")


class TestConfig(unittest.TestCase):
    def test_detect_api_key_priority(self) -> None:
        env = {"OPENAI_API_KEY": "o", "DEEPSEEK_API_KEY": "d"}
        self.assertEqual(detect_api_key(env), "o")
        self.assertEqual(detect_api_key({"DEEPSEEK_API_KEY": "d"}), "d")
        self.assertEqual(detect_api_key({}), "")

    def test_from_env_reads_values(self) -> None:
        env = {
            "CODEAGENT_MODEL": "deepseek-chat",
            "CODEAGENT_BASE_URL": "https://api.deepseek.com/v1",
            "CODEAGENT_API_KEY": "sk-test",
            "CODEAGENT_MAX_STEPS": "5",
            "CODEAGENT_TEMPERATURE": "0.7",
            "CODEAGENT_AUTO_APPROVE": "true",
        }
        config = Config.from_env(env)
        self.assertEqual(config.model, "deepseek-chat")
        self.assertEqual(config.api_key, "sk-test")
        self.assertEqual(config.max_steps, 5)
        self.assertAlmostEqual(config.temperature, 0.7)
        self.assertTrue(config.auto_approve)

    def test_overrides_ignore_none(self) -> None:
        config = Config.from_env({"CODEAGENT_MODEL": "a"}, model=None, max_steps=3)
        self.assertEqual(config.model, "a")
        self.assertEqual(config.max_steps, 3)

    def test_unknown_override_raises(self) -> None:
        with self.assertRaises(ConfigError):
            Config().with_overrides(unknown_option=1)

    def test_validate_requires_api_key_for_openai(self) -> None:
        with self.assertRaises(ConfigError):
            Config(provider="openai", api_key="").validate()

    def test_validate_allows_mock_without_key(self) -> None:
        Config(provider="mock", api_key="").validate()

    def test_validate_rejects_bad_provider(self) -> None:
        with self.assertRaises(ConfigError):
            Config(provider="nope", api_key="x").validate()

    def test_normalized_paths_are_absolute(self) -> None:
        config = Config(workspace=Path("."), session_dir=Path(".codeagent/sessions")).normalized()
        self.assertTrue(config.workspace.is_absolute())
        self.assertTrue(config.session_dir.is_absolute())
        self.assertTrue(str(config.session_dir).startswith(str(config.workspace)))

    def test_masked_key(self) -> None:
        self.assertEqual(Config(api_key="").masked_key(), "(未设置)")
        self.assertEqual(Config(api_key="12345678").masked_key(), "********")
        self.assertEqual(Config(api_key="sk-abcdefghijkl").masked_key(), "sk-a...ijkl")

    def test_bad_numeric_env_raises(self) -> None:
        with self.assertRaises(ConfigError):
            Config.from_env({"CODEAGENT_TEMPERATURE": "热"})


if __name__ == "__main__":
    unittest.main()
