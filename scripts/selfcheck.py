#!/usr/bin/env python
"""自检脚本：用本项目自己的静态分析工具审查本项目源码（dogfooding）。

用途
----
1. 作为 CI 门禁：存在 high 级别问题时退出码非 0；
2. 作为演示素材：证明 ``analyze_code`` 工具在真实代码上可用。

用法::

    python scripts/selfcheck.py                 # 只报告 high/medium
    python scripts/selfcheck.py --all           # 报告全部级别
    python scripts/selfcheck.py --path codeagent
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from codeagent.tools.analysis import SEVERITY_ORDER, analyze_source, format_findings  # noqa: E402

SKIP_PARTS = {"__pycache__", ".venv", "venv", ".git", "dist", "build", ".codeagent"}

#: 演示用的「故意有缺陷」文件，不纳入自检门禁
INTENTIONALLY_BUGGY = {"examples/buggy_code.py"}


def main() -> int:
    parser = argparse.ArgumentParser(description="用 analyze_code 工具审查本项目源码")
    parser.add_argument("--path", default=".", help="要审查的目录（相对项目根目录）")
    parser.add_argument("--all", action="store_true", help="报告全部级别（默认只报 high/medium）")
    parser.add_argument("--quiet", action="store_true", help="只输出汇总统计")
    parser.add_argument("--rules", help="只关注这些规则（逗号分隔），如 unused-import,long-line")
    parser.add_argument("--include-examples", action="store_true", help="连故意有缺陷的示例文件一起检查")
    args = parser.parse_args()

    root = (PROJECT_ROOT / args.path).resolve()
    threshold = SEVERITY_ORDER["info"] if args.all else SEVERITY_ORDER["medium"]
    only_rules = {rule.strip() for rule in args.rules.split(",")} if args.rules else None

    files = [p for p in sorted(root.rglob("*.py")) if not any(part in SKIP_PARTS for part in p.parts)]
    rule_counter: dict = {}
    high_total = 0
    medium_total = 0

    for path in files:
        relative = path.relative_to(PROJECT_ROOT).as_posix()
        if relative in INTENTIONALLY_BUGGY and not args.include_examples:
            continue
        findings, summary = analyze_source(path.read_text(encoding="utf-8"), relative)
        if only_rules is not None:
            findings = [f for f in findings if f.rule in only_rules]
        for finding in findings:
            rule_counter[finding.rule] = rule_counter.get(finding.rule, 0) + 1
        high_total += sum(1 for f in findings if f.severity == "high")
        medium_total += sum(1 for f in findings if f.severity == "medium")

        shown = [f for f in findings if SEVERITY_ORDER.get(f.severity, 9) <= threshold]
        if shown and not args.quiet:
            print(format_findings(shown, summary, relative))
            print()

    print(f"扫描文件数: {len(files)}（已跳过 {len(INTENTIONALLY_BUGGY)} 个演示用缺陷文件）")
    print(f"high: {high_total}   medium: {medium_total}")
    if rule_counter:
        print("规则命中统计:")
        for rule, count in sorted(rule_counter.items(), key=lambda kv: (-kv[1], kv[0])):
            print(f"  {rule:<20} {count}")
    print("结果: " + ("存在 high 级别问题，请修复" if high_total else "通过（无 high 级别问题）"))
    return 1 if high_total else 0


if __name__ == "__main__":
    raise SystemExit(main())
