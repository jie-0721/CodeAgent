"""打包脚本测试：密钥泄露门禁必须真的拦得住，且不误伤示例文本。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.package import (
    PROJECT_ROOT,
    is_excluded_file,
    iter_project_files,
    read_env_secrets,
    scan_for_secrets,
)

#: 形似真实 Key 的测试固定值（含 fake 字样，属于示例数据，不应触发警告）
FAKE_KEY = "sk-fake-9f8e7d6c5b4a39281706f5e4d3c2b1a0"


class PackageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def files(self):
        return list(iter_project_files(self.root))


class TestReadEnvSecrets(PackageTestCase):
    def test_reads_key_values(self) -> None:
        self.write(".env", f"CODEAGENT_API_KEY={FAKE_KEY}\nCODEAGENT_MODEL=deepseek-chat\n")
        secrets = read_env_secrets(self.root / ".env")
        self.assertEqual(secrets["CODEAGENT_API_KEY"], FAKE_KEY)
        self.assertNotIn("CODEAGENT_MODEL", secrets)

    def test_ignores_placeholder_values(self) -> None:
        self.write(".env", "CODEAGENT_API_KEY=sk-你的key\nOPENAI_API_KEY=your-key-here\n")
        self.assertEqual(read_env_secrets(self.root / ".env"), {})

    def test_ignores_short_values(self) -> None:
        self.write(".env", "CODEAGENT_API_KEY=abc\n")
        self.assertEqual(read_env_secrets(self.root / ".env"), {})

    def test_missing_file_is_safe(self) -> None:
        self.assertEqual(read_env_secrets(self.root / "不存在.env"), {})


class TestSecretScan(PackageTestCase):
    def test_detects_real_key_in_source_file(self) -> None:
        """真实密钥被写进源码 / 文档时必须拦下（致命）。"""
        self.write(".env", f"CODEAGENT_API_KEY={FAKE_KEY}\n")
        self.write("codeagent/bad.py", f'API_KEY = "{FAKE_KEY}"\n')
        fatal, _ = scan_for_secrets(self.files(), read_env_secrets(self.root / ".env"))
        self.assertTrue(any("bad.py" in item for item in fatal), msg=fatal)

    def test_detects_real_key_in_markdown(self) -> None:
        self.write(".env", f"CODEAGENT_API_KEY={FAKE_KEY}\n")
        self.write("README.md", f"配置：CODEAGENT_API_KEY={FAKE_KEY}\n")
        fatal, _ = scan_for_secrets(self.files(), read_env_secrets(self.root / ".env"))
        self.assertTrue(any("README.md" in item for item in fatal), msg=fatal)

    def test_env_itself_is_excluded_from_packaging(self) -> None:
        """.env 不在待打包文件里，因此密钥只放在 .env 是安全的。"""
        self.write(".env", f"CODEAGENT_API_KEY={FAKE_KEY}\n")
        self.write("codeagent/ok.py", "import os\nKEY = os.environ['CODEAGENT_API_KEY']\n")
        relative_names = [rel.as_posix() for _, rel in self.files()]
        self.assertNotIn(".env", relative_names)
        fatal, warnings = scan_for_secrets(self.files(), read_env_secrets(self.root / ".env"))
        self.assertEqual(fatal, [])
        self.assertEqual(warnings, [])

    def test_placeholder_text_is_not_flagged(self) -> None:
        self.write(".env.example", "CODEAGENT_API_KEY=sk-你的真实key\n")
        self.write("docs.md", "请把 api_key 替换成 your-own-key\n")
        self.write(".env", f"CODEAGENT_API_KEY={FAKE_KEY}\n")
        fatal, warnings = scan_for_secrets(self.files(), read_env_secrets(self.root / ".env"))
        self.assertEqual(fatal, [])
        self.assertEqual(warnings, [])

    def test_test_fixtures_are_not_flagged(self) -> None:
        """测试里形似 Key 的固定值只要带 fake/test 字样就不该报警。"""
        self.write("tests/test_x.py", f'KEY = "{FAKE_KEY}"  # fake\n')
        _, warnings = scan_for_secrets(self.files(), {})
        self.assertEqual(warnings, [])

    def test_suspicious_key_shape_produces_warning(self) -> None:
        """没有 .env 可比对时，仍要能凭形态给出警告（非致命）。"""
        value = "sk-" + "9f8e7d6c5b4a39281706f5e4d3c2b1a0"  # 拼接构造，避免本行自身被扫描告警
        self.write("codeagent/suspect.py", f'TOKEN = "{value}"\n')
        fatal, warnings = scan_for_secrets(self.files(), {})
        self.assertEqual(fatal, [])
        self.assertTrue(any("suspect.py" in item for item in warnings), msg=warnings)

    def test_binary_files_are_skipped(self) -> None:
        (self.root / "blob.png").write_bytes(b"\x89PNG" + FAKE_KEY.encode())
        fatal, warnings = scan_for_secrets(self.files(), {})
        self.assertEqual(fatal, [])
        self.assertEqual(warnings, [])


class TestProjectSelfScan(unittest.TestCase):
    def test_real_project_has_no_leaked_secrets(self) -> None:
        """本项目自身必须通过门禁（防止有人把 Key 写进 README / 源码）。"""
        files = list(iter_project_files(PROJECT_ROOT))
        env_path = PROJECT_ROOT / ".env"
        secrets = read_env_secrets(env_path) if env_path.is_file() else {}
        fatal, _ = scan_for_secrets(files, secrets)
        self.assertEqual(fatal, [], msg=f"待打包文件中发现密钥泄露: {fatal}")


class TestExcludedFileNames(PackageTestCase):
    """改名变体也要挡住：很多人会另存为 .env.production / .env.bak。"""

    def test_env_variants_are_excluded(self) -> None:
        for name in (".env", ".env.local", ".env.production", ".env.bak", ".env.txt", ".envrc"):
            self.assertTrue(is_excluded_file(name), msg=name)

    def test_example_template_is_kept(self) -> None:
        self.assertFalse(is_excluded_file(".env.example"))

    def test_office_lock_and_normal_files(self) -> None:
        self.assertTrue(is_excluded_file("~$Homework 1.pptx"))
        self.assertFalse(is_excluded_file("README.md"))
        self.assertFalse(is_excluded_file("agent.py"))

    def test_variants_never_reach_the_package(self) -> None:
        secret = "sk-" + "realvalue" * 4
        for name in (".env", ".env.local", ".env.production", ".env.bak"):
            self.write(name, f"CODEAGENT_API_KEY={secret}\n")
        self.write(".env.example", "CODEAGENT_API_KEY=sk-你的key\n")
        self.write("main.py", "print('hi')\n")

        packaged = [rel.as_posix() for _, rel in self.files()]
        self.assertEqual(sorted(packaged), [".env.example", "main.py"])
        fatal, _ = scan_for_secrets(self.files(), {})
        self.assertEqual(fatal, [])


class TestGitignoreCoverage(unittest.TestCase):
    """保护来自 .gitignore 规则，因此规则本身也要有测试兜底。"""

    def setUp(self) -> None:
        self.content = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")

    def test_env_patterns_present(self) -> None:
        for pattern in (".env", ".env.*"):
            self.assertIn(pattern, self.content.splitlines(), msg=pattern)

    def test_example_template_is_negated(self) -> None:
        """!.env.example 必须存在，否则助教看不到需要配置哪些变量。"""
        lines = [line.strip() for line in self.content.splitlines()]
        self.assertIn("!.env.example", lines)
        # 取反规则必须在 .env.* 之后，否则不生效
        self.assertLess(lines.index(".env.*"), lines.index("!.env.example"))


if __name__ == "__main__":
    unittest.main()
