"""离线 Mock LLM。

作用有二：

1. **演示**：没有 API Key 也能完整跑通「推理 → 工具调用 → 观察 → 输出」的 Agent 循环，
   方便录制演示视频或课堂上直接展示；
2. **测试**：让 Agent 主循环的测试不依赖网络与外部服务（确定性、可重复）。

它并不是随机胡说，而是一个**规则规划器**：识别用户意图 → 生成工具调用计划 →
根据真实工具输出渲染最终答复。因此即使离线，工具结果也是真实的。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .config import Config
from .llm import BaseLLM, Message, ToolCall

FILE_PATTERN = re.compile(
    r"([\w./\\-]+\.(?:py|js|jsx|ts|tsx|java|go|rs|c|cc|cpp|h|rb|php|md|json|yaml|yml|toml|txt))",
    re.IGNORECASE,
)
FINDING_PATTERN = re.compile(r"^\[(高|中|低|提示)\]\s+L(\d+)\s+([\w-]+):\s*(.*)$")
SUGGESTION_PATTERN = re.compile(r"^\s*建议:\s*(.*)$")

INTENT_KEYWORDS = {
    "review": ("审查", "评审", "检查", "问题", "bug", "缺陷", "质量", "优化建议", "review"),
    "explain": ("解释", "讲解", "说明", "什么意思", "explain", "理解", "怎么工作", "什么用"),
    "test": ("测试", "用例", "test", "单测", "覆盖率"),
    "run": ("运行", "执行", "跑一下", "run", "execute", "输出结果"),
    "search": ("查找", "搜索", "检索", "在哪里", "哪里", "search", "grep", "定位"),
    "list": ("有哪些文件", "列出", "目录结构", "项目结构", "list"),
}

#: 英文关键词必须按词匹配，否则 "buggy.py" 会被误判为 review（含 "bug"）
_ASCII_INTENT_PATTERNS = {
    intent: re.compile(
        r"\b(?:" + "|".join(re.escape(word) + r"s?" for word in keywords if word.isascii()) + r")\b",
        re.IGNORECASE,
    )
    for intent, keywords in INTENT_KEYWORDS.items()
    if any(word.isascii() for word in keywords)
}

#: 中文关键词按子串匹配（中文没有词边界）
_CJK_INTENT_KEYWORDS = {
    intent: tuple(word for word in keywords if not word.isascii())
    for intent, keywords in INTENT_KEYWORDS.items()
}

MOCK_NOTICE = "> 说明：本回答由离线 Mock 模型根据工具的真实输出渲染而成（未调用外部 LLM）。"


class MockLLM(BaseLLM):
    """规则驱动的离线模型。"""

    name = "mock"

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config
        self.call_count = 0
        self.delay = 0.0  # 供演示时模拟打字延迟

    # ------------------------------------------------------------------ 主入口
    def generate(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_delta: Optional[Any] = None,
        stream: bool = True,
    ) -> Message:
        self.call_count += 1
        self._emit_start(on_delta)

        available = {
            (tool.get("function") or {}).get("name")
            for tool in (tools or [])
            if isinstance(tool, dict)
        }
        available.discard(None)

        plan = self._build_plan(messages, available)
        completed = self._completed_calls(messages)
        for name, arguments in plan:
            fingerprint = (name, json.dumps(arguments, sort_keys=True, ensure_ascii=False))
            if fingerprint in completed or name not in available:
                continue
            thought = self._thought_for(name, arguments)
            if thought:
                # 流式增量必须与消息正文完全一致，否则前端会把思考文本重复渲染
                self._stream(thought, on_delta)
            return Message.assistant(
                thought,
                [
                    ToolCall(
                        id=f"mock_{self.call_count}_{name}",
                        name=name,
                        arguments=json.dumps(arguments, ensure_ascii=False),
                    )
                ],
            )

        answer = self._render_final(messages)
        self._stream(answer, on_delta)
        return Message.assistant(answer)

    # ------------------------------------------------------------------ 规划
    def _build_plan(self, messages: Sequence[Message], available: set) -> List[Tuple[str, Dict[str, Any]]]:
        last_user = self._last_user_message(messages)
        if last_user is None:
            return []

        intent = self._detect_intent(last_user)
        target = self._extract_target(last_user)
        plan: List[Tuple[str, Dict[str, Any]]] = []

        if intent == "review":
            if target and target.endswith(".py"):
                plan.append(("analyze_code", {"path": target}))
                plan.append(("read_file", {"path": target, "max_lines": 200}))
            elif target:
                plan.append(("read_file", {"path": target, "max_lines": 200}))
            else:
                plan.append(("list_files", {"pattern": "**/*.py"}))
        elif intent == "explain":
            if target:
                plan.append(("list_definitions", {"path": target}))
                plan.append(("read_file", {"path": target, "max_lines": 200}))
            else:
                plan.append(("list_files", {"pattern": "**/*.py"}))
        elif intent == "test":
            if target:
                plan.append(("read_file", {"path": target, "max_lines": 200}))
            plan.append(("run_tests", {"path": "tests"}))
        elif intent == "run":
            if target:
                plan.append(("run_python", {"path": target}))
            else:
                plan.append(("list_files", {"pattern": "**/*.py"}))
        elif intent == "search":
            keyword = self._extract_search_term(last_user)
            plan.append(("search_text", {"pattern": keyword, "include": "*.py"}))
        else:
            plan.append(("list_files", {"pattern": "**/*.py"}))

        return [item for item in plan if item[0] in available]

    @staticmethod
    def _detect_intent(text: str) -> str:
        for intent, keywords in _CJK_INTENT_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                return intent
        for intent, pattern in _ASCII_INTENT_PATTERNS.items():
            if pattern.search(text):
                return intent
        return "generic"

    @staticmethod
    def _extract_target(text: str) -> Optional[str]:
        match = FILE_PATTERN.search(text)
        return match.group(1) if match else None

    @staticmethod
    def _extract_search_term(text: str) -> str:
        quoted = re.findall(r"[\"'`]([^\"'`]{2,40})[\"'`]", text)
        if quoted:
            return quoted[0]
        for token in re.split(r"[\s,，。:：]+", text):
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,}", token):
                return token
        return "TODO"

    @staticmethod
    def _last_user_message(messages: Sequence[Message]) -> Optional[str]:
        for message in reversed(messages):
            if message.role == "user" and not message.content.startswith("[系统]"):
                return message.content
        return None

    @staticmethod
    def _completed_calls(messages: Sequence[Message]) -> set:
        done = set()
        for message in messages:
            for call in message.tool_calls:
                try:
                    parsed = json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    parsed = {}
                done.add((call.name, json.dumps(parsed, sort_keys=True, ensure_ascii=False)))
        return done

    @staticmethod
    def _thought_for(name: str, arguments: Dict[str, Any]) -> str:
        target = arguments.get("path") or arguments.get("pattern") or ""
        thoughts = {
            "analyze_code": f"先用静态分析拿到带行号的确定性结论：{target}",
            "read_file": f"读取 {target} 的关键片段，确认问题上下文。",
            "list_definitions": f"先梳理 {target} 的结构（类/函数/导入）。",
            "list_files": "先看看工作目录里有哪些文件。",
            "search_text": f"在代码库中检索 {arguments.get('pattern', '')}。",
            "run_tests": "跑一遍测试，确认当前基线状态。",
            "run_python": f"运行 {target} 观察实际输出。",
        }
        return thoughts.get(name, f"调用工具 {name}。")

    # ------------------------------------------------------------- 最终答复
    def _render_final(self, messages: Sequence[Message]) -> str:
        last_user = self._last_user_message(messages) or ""
        intent = self._detect_intent(last_user)
        target = self._extract_target(last_user) or "目标文件"
        observations = self._tool_observations(messages)
        if not observations:
            return (
                "我已经检查过当前上下文，但没有可用的工具输出可供总结。\n"
                "请告诉我具体要处理的文件，例如：`审查 examples/buggy_code.py`。\n\n" + MOCK_NOTICE
            )

        if intent == "review":
            return self._render_review(target, observations)
        if intent == "explain":
            return self._render_explain(target, observations)
        if intent == "test":
            return self._render_tests(observations)
        if intent == "run":
            return self._render_run(target, observations)
        if intent == "search":
            return self._render_search(last_user, observations)
        return self._render_generic(observations)

    # ------------------------------------------------------------------ 渲染
    @staticmethod
    def _tool_observations(messages: Sequence[Message]) -> List[Tuple[str, str]]:
        pairs: List[Tuple[str, str]] = []
        for message in messages:
            if message.role == "tool":
                pairs.append((message.name or "tool", message.content))
        return pairs

    def _render_review(self, target: str, observations: List[Tuple[str, str]]) -> str:
        analysis = next((text for name, text in observations if name == "analyze_code"), "")
        findings = self._parse_findings(analysis)
        header = next((line for line in analysis.splitlines() if line.startswith("静态分析:")), "")

        lines = [f"## 代码审查报告：`{target}`", ""]
        if header:
            lines.append(f"**{header.split('|', 1)[1].strip() if '|' in header else header}**")
            lines.append("")
        if not findings:
            lines.append("静态分析未发现规则内的明显问题；仍建议结合单元测试验证边界情况。")
        else:
            grouped: Dict[str, List[Dict[str, str]]] = {}
            for item in findings:
                grouped.setdefault(item["severity"], []).append(item)
            titles = {"高": "严重（建议立即修复）", "中": "中等（建议近期处理）", "低": "轻微（可读性/规范）", "提示": "提示"}
            for severity in ("高", "中", "低", "提示"):
                items = grouped.get(severity)
                if not items:
                    continue
                lines.append(f"### {titles[severity]}")
                for item in items[:6]:
                    lines.append(f"- `{target}:{item['line']}` **{item['rule']}** — {item['message']}")
                    if item["suggestion"]:
                        lines.append(f"  - 修复建议：{item['suggestion']}")
                lines.append("")

            priority = (grouped.get("高") or grouped.get("中") or findings)[:2]
            lines.append("### 建议的修复顺序")
            for rank, item in enumerate(priority, start=1):
                lines.append(f"{rank}. `{target}:{item['line']}` {item['message']}")
            lines.append("")

        if any(name == "read_file" for name, _ in observations):
            lines.append("（已结合 read_file 的原文确认上述位置。）")
        lines.append("")
        lines.append(MOCK_NOTICE)
        return "\n".join(lines)

    @staticmethod
    def _parse_findings(analysis: str) -> List[Dict[str, str]]:
        findings: List[Dict[str, str]] = []
        current: Optional[Dict[str, str]] = None
        for line in analysis.splitlines():
            match = FINDING_PATTERN.match(line.strip())
            if match:
                current = {
                    "severity": match.group(1),
                    "line": match.group(2),
                    "rule": match.group(3),
                    "message": match.group(4).strip(),
                    "suggestion": "",
                }
                findings.append(current)
                continue
            suggestion = SUGGESTION_PATTERN.match(line)
            if suggestion and current is not None and not current["suggestion"]:
                current["suggestion"] = suggestion.group(1).strip()
        return findings

    def _render_explain(self, target: str, observations: List[Tuple[str, str]]) -> str:
        structure = next((text for name, text in observations if name == "list_definitions"), "")
        body = next((text for name, text in observations if name == "read_file"), "")
        lines = [f"## 代码说明：`{target}`", ""]
        if structure:
            rows = [line for line in structure.splitlines()[1:] if line.strip()]
            lines.append("### 结构概览")
            lines.append("```")
            lines.extend(rows[:20])
            lines.append("```")
            lines.append("")
        if body:
            lines.append("### 关键实现")
            numbered = [line for line in body.splitlines()[1:6]]
            lines.append("```python")
            lines.extend(numbered)
            lines.append("```")
            lines.append("")
        lines.append("### 说明")
        lines.append("该文件按“顶层结构 → 关键函数 → 调用关系”的顺序阅读即可理解；")
        lines.append("如需深入某个函数，请指出函数名，我可以继续展开逐行解释。")
        lines.append("")
        lines.append(MOCK_NOTICE)
        return "\n".join(lines)

    @staticmethod
    def _render_tests(observations: List[Tuple[str, str]]) -> str:
        tests = next((text for name, text in observations if name == "run_tests"), "")
        lines = ["## 测试基线", ""]
        if tests:
            summary = tests.split("--- 摘要 ---")[-1].strip()
            lines.append("```")
            lines.extend(summary.splitlines()[:15])
            lines.append("```")
        else:
            lines.append("未能执行测试；请确认测试目录存在（如 `tests/`）。")
        lines.append("")
        lines.append("下一步可以基于失败用例补充断言或修复被测函数，然后再次运行 `run_tests` 验证。")
        lines.append("")
        lines.append(MOCK_NOTICE)
        return "\n".join(lines)

    @staticmethod
    def _render_run(target: str, observations: List[Tuple[str, str]]) -> str:
        run = next((text for name, text in observations if name in {"run_python", "run_tests"}), "")
        body = run.splitlines()[:20] if run else ["(无输出)"]
        lines = [f"## 执行结果：`{target}`", "", "```", *body, "```", "", MOCK_NOTICE]
        return "\n".join(lines)

    @staticmethod
    def _render_search(query: str, observations: List[Tuple[str, str]]) -> str:
        search = next((text for name, text in observations if name == "search_text"), "")
        body = search.splitlines()[:20] if search else ["(无匹配)"]
        lines = ["## 检索结果", "", f"查询：`{query}`", "", "```", *body, "```", "", MOCK_NOTICE]
        return "\n".join(lines)

    @staticmethod
    def _render_generic(observations: List[Tuple[str, str]]) -> str:
        listing = next((text for name, text in observations if name == "list_files"), "")
        other = next(((name, text) for name, text in observations if name != "list_files"), ("", ""))
        lines = ["## 工作目录概览", ""]
        if listing:
            lines.append("```")
            lines.extend(listing.splitlines()[:20])
            lines.append("```")
            lines.append("")
        lines.append(f"已收集 {len(observations)} 条工具输出。请告诉我具体目标（例如“审查 examples/buggy_code.py”），我可以继续深入。")
        lines.append("")
        lines.append(MOCK_NOTICE)
        return "\n".join(lines)

    # ------------------------------------------------------------------ 流式
    def _emit_start(self, on_delta: Optional[Any]) -> None:
        if on_delta is not None and self.delay <= 0:
            return

    def _stream(self, text: str, on_delta: Optional[Any]) -> None:
        """把文本按块回调，模拟流式输出。"""
        if on_delta is None:
            return
        chunk_size = 40
        for start in range(0, len(text), chunk_size):
            on_delta(text[start : start + chunk_size])
            if self.delay > 0:
                time.sleep(self.delay)
