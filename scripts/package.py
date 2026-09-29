#!/usr/bin/env python
"""打包提交物。

作业要求把代码与文档打包成「学号姓名」的压缩包（< 200MB），例如 ``10086张三.zip``。

用法::

    python scripts/package.py --name 10086张三
    python scripts/package.py --name 10086张三 --flat --outdir submission
    python scripts/package.py --scan-only          # 只做密钥泄露检查，不打包

**密钥安全**：脚本做两层防护——

1. 排除 ``.env`` / ``.env.local``（真实 API Key 只应存在于这里）；
2. 打包前扫描**所有将被打包的文件**：既检查它们是否出现了 ``.env`` 里的真实 Key
   （精确匹配，不会误报），也检查是否残留形如 ``sk-xxxx`` 的高危字符串。
   一旦命中就**拒绝打包**（可用 ``--allow-secrets`` 强制跳过）。
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent

EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".codeagent",
    "dist",
    "build",
    "submission",
    ".eggs",
}

#: 绝不打包的文件（避免泄露密钥 / 打包产物自身）
EXCLUDE_FILES = {".env", ".env.local", "package.zip"}

#: 以该前缀开头的文件一律排除（覆盖 .env.production、.env.bak 等改名变体），
#: 但下面的白名单例外——示例模板必须随包提交。
EXCLUDE_FILE_PREFIXES = (".env",)

#: 允许打包的 .env* 文件
KEEP_ENV_FILES = {".env.example"}

EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip", ".rar", ".7z"}


def is_excluded_file(name: str) -> bool:
    """判断单个文件是否应被排除在打包之外。"""
    if name.startswith("~$"):  # Office 临时锁文件
        return True
    if name in EXCLUDE_FILES:
        return True
    if any(name.startswith(prefix) for prefix in EXCLUDE_FILE_PREFIXES) and name not in KEEP_ENV_FILES:
        return True
    return False

#: 提交大小上限（作业要求 < 200MB）
SIZE_LIMIT_BYTES = 200 * 1024 * 1024


#: 形如真实 API Key 的高危字符串（OpenAI / DeepSeek / 通义千问 等）
SUSPICIOUS_KEY_PATTERN = re.compile(r"(?<![A-Za-z0-9])(sk|ak|api)[-_][A-Za-z0-9][A-Za-z0-9_-]{18,}")

#: 出现这些词的行视为示例/测试数据，不算泄露
SAFE_LINE_MARKERS = (
    "test", "fake", "dummy", "example", "sample", "placeholder", "your", "xxx",
    "你的", "填入", "替换", "示例", "占位", "noqa: secret", "<", ">",
)

#: 只扫描文本类文件，避免在二进制里误报
TEXT_SUFFIXES = {".py", ".md", ".txt", ".json", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".html", ".js", ".css", ".env", ""}


def iter_project_files(root: Path):
    """遍历需要打包的文件。"""
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in relative.parts):
            continue
        if is_excluded_file(path.name):
            continue
        if path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        yield path, relative


# --------------------------------------------------------------------------- #
# 密钥泄露检查
# --------------------------------------------------------------------------- #
def read_env_secrets(env_path: Path) -> Dict[str, str]:
    """从 .env 中取出所有像密钥的键值（用于精确比对）。"""
    secrets: Dict[str, str] = {}
    try:
        content = env_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return secrets
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not value or len(value) < 8:
            continue
        if any(marker in value.lower() for marker in ("你的", "填入", "your", "xxxx")):
            continue  # 占位符值不算密钥
        if key.upper().endswith(("KEY", "TOKEN", "SECRET")) or "KEY" in key.upper():
            secrets[key] = value
    return secrets


def scan_for_secrets(
    files: Sequence[Tuple[Path, Path]],
    secrets: Dict[str, str],
) -> Tuple[List[str], List[str]]:
    """扫描将被打包的文件。

    返回 ``(致命问题, 警告)``：致命问题指真实 .env 密钥出现在待打包内容里；
    警告指残留的可疑 Key 形态字符串。
    """
    fatal: List[str] = []
    warnings: List[str] = []

    for path, relative in files:
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            # 1) 精确匹配 .env 中的真实密钥
            for name, value in secrets.items():
                if value and value in line:
                    fatal.append(f"{relative}:{lineno} 出现了 .env 中 {name} 的真实值")
            # 2) 形态可疑的 Key 字符串
            if any(marker in line.lower() for marker in SAFE_LINE_MARKERS):
                continue
            for match in SUSPICIOUS_KEY_PATTERN.finditer(line):
                warnings.append(f"{relative}:{lineno} 疑似 API Key 字符串：{match.group(0)[:8]}...")

    return fatal, sorted(set(warnings))


def report_secrets(files: Sequence[Tuple[Path, Path]], allow: bool = False) -> int:
    """执行检查并打印结果；返回 0 表示通过，1 表示发现致命问题。"""
    env_path = PROJECT_ROOT / ".env"
    secrets = read_env_secrets(env_path) if env_path.is_file() else {}
    fatal, warnings = scan_for_secrets(files, secrets)

    if secrets:
        print(f"  ✓ 已排除 .env，并对 {len(files)} 个待打包文件做了密钥比对（{len(secrets)} 个密钥值）")
    else:
        print(f"  ✓ 未发现 .env（或其中没有密钥），仍对 {len(files)} 个待打包文件做了形态扫描")

    for item in warnings:
        print(f"  ⚠ {item}（若确为示例文本可忽略）")

    if fatal and not allow:
        print("  ✗ 密钥泄露检查未通过，已中止打包：")
        for item in fatal:
            print(f"      {item}")
        print("      请从上述文件中删除密钥，改为从环境变量或 .env 读取；")
        print("      确认无误后可加 --allow-secrets 强制打包。")
        return 1
    if fatal:
        print("  ⚠ 检测到密钥泄露但被 --allow-secrets 忽略，请确认这是你有意为之：")
        for item in fatal:
            print(f"      {item}")
    if not fatal:
        print("  ✓ 密钥泄露检查通过：待打包文件中没有出现 .env 里的任何密钥")
    return 0


def build_archive(name: str, outdir: Path, flat: bool = False) -> Tuple[Path, list]:
    outdir.mkdir(parents=True, exist_ok=True)
    archive = outdir / f"{name}.zip"
    prefix = "" if flat else f"{name}/"

    files = list(iter_project_files(PROJECT_ROOT))
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path, relative in files:
            zf.write(path, arcname=f"{prefix}{relative.as_posix()}")
    return archive, files


def main() -> int:
    parser = argparse.ArgumentParser(description="把项目打包成「学号姓名.zip」（含密钥泄露检查）")
    parser.add_argument("--name", default="学号姓名", help="压缩包名称，通常为「学号姓名」，如 10086张三")
    parser.add_argument("--outdir", default="submission", help="输出目录，默认 submission/")
    parser.add_argument("--flat", action="store_true", help="不在压缩包内再套一层同名目录")
    parser.add_argument("--scan-only", action="store_true", help="只做密钥泄露检查，不生成压缩包")
    parser.add_argument("--allow-secrets", action="store_true", help="忽略密钥检查结果，强制打包（危险）")
    args = parser.parse_args()

    files = list(iter_project_files(PROJECT_ROOT))

    if args.scan_only:
        print(f"密钥泄露检查（待打包文件 {len(files)} 个）：")
        return report_secrets(files, allow=args.allow_secrets)

    print("打包前检查：")
    if report_secrets(files, allow=args.allow_secrets) != 0:
        return 1

    archive, files = build_archive(args.name, Path(args.outdir), flat=args.flat)
    size = archive.stat().st_size

    print(f"\n打包完成：{archive}")
    print(f"  文件数：{len(files)}")
    print(f"  大小  ：{size / 1024:.1f} KB ({size / 1024 / 1024:.2f} MB)")
    if size > SIZE_LIMIT_BYTES:
        print("  ⚠ 超过作业要求的 200MB 上限，请检查是否误打包了数据集或虚拟环境。")
    else:
        print("  ✓ 未超过 200MB 上限")

    missing = [name for name in ("README.md", "Design.md") if not (PROJECT_ROOT / name).is_file()]
    if missing:
        print(f"  ⚠ 缺少文档：{', '.join(missing)}（作业要求提交 README.md / Design.md）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
