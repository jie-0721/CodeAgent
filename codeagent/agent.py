"""Agent 主循环：输入 → 推理 → 工具调用 → 观察 → 输出。

这是整个项目的核心。循环伪代码::

    memory.add_user(输入)
    for step in 1..max_steps:
        msg = llm.generate(memory.build(), tools=schemas)      # 推理
        if msg 没有 tool_calls:                                 # 收敛
            return msg.content
        memory.add_assistant(msg.content, msg.tool_calls)
        for call in msg.tool_calls:                             # 工具调用
            result = execute(call)     # 含权限确认 / 异常兜底
            memory.add_tool_result(call.id, call.name, result)  # 观察回灌
    return 强制收敛（让模型基于已有信息直接作答）

关键工程细节：

* **事件回调**：每一步都通过 ``on_event`` 抛出结构化事件，CLI 与 Web UI 复用同一套逻辑；
* **权限门**：写文件 / 执行代码等有副作用的工具默认需要用户确认（``--yes`` 可跳过）；
* **循环熔断**：同一工具+参数重复调用会被检测并注入纠正提示，避免烧 token；
* **异常即观察**：工具错误不会中断循环，而是回传模型让其自我修正。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .errors import AgentError, LLMError, ToolNotFound
from .llm import BaseLLM, Message, ToolCall
from .memory import Memory
from .tools import Tool, ToolRegistry, ToolResult

logger = logging.getLogger("codeagent.agent")

#: 单条工具观察结果回灌给模型的最大字符数
MAX_OBSERVATION_CHARS = 6000
#: 同一 (工具, 参数) 允许重复调用的次数
REPEAT_LIMIT = 3

Approver = Callable[[Tool, Dict[str, Any]], bool]
EventHandler = Callable[[Dict[str, Any]], None]


@dataclass
class AgentStep:
    """一次工具调用的完整记录（用于展示 trace 与写报告）。"""

    index: int
    tool: str
    arguments: Dict[str, Any]
    ok: bool
    observation: str
    elapsed: float
    thought: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "tool": self.tool,
            "arguments": self.arguments,
            "ok": self.ok,
            "observation": self.observation,
            "elapsed": self.elapsed,
            "thought": self.thought,
        }


@dataclass
class AgentResult:
    """一次 ``run`` 的结果。"""

    answer: str
    steps: List[AgentStep] = field(default_factory=list)
    stopped_reason: str = "final"
    elapsed: float = 0.0
    usage: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def tool_calls(self) -> int:
        return len(self.steps)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "answer": self.answer,
            "steps": [step.to_dict() for step in self.steps],
            "stopped_reason": self.stopped_reason,
            "elapsed": self.elapsed,
            "usage": self.usage,
            "tool_calls": self.tool_calls,
            "error": self.error,
        }


class Agent:
    """把 LLM、工具、记忆组装成可运行的多步 Agent。"""

    def __init__(
        self,
        llm: BaseLLM,
        tools: ToolRegistry,
        config: Config,
        memory: Optional[Memory] = None,
        on_event: Optional[EventHandler] = None,
        approver: Optional[Approver] = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.config = config
        self.on_event = on_event
        self.approver = approver
        self.memory = memory if memory is not None else Memory(token_budget=config.memory_token_budget)
        if not self.memory.system_prompt:
            from .prompts import build_system_prompt

            self.memory.system_prompt = build_system_prompt(config.workspace)
        self.memory.token_budget = config.memory_token_budget

    # ------------------------------------------------------------------ 事件
    def _emit(self, event_type: str, **payload: Any) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event({"type": event_type, **payload})
        except Exception:  # 展示层回调异常不应中断 Agent 运行，记录到 debug 日志
            logger.debug("事件回调 %s 抛出异常", event_type, exc_info=True)

    # ------------------------------------------------------------- 工具执行
    def _execute_tool(self, call: ToolCall, thought: str, index: int) -> AgentStep:
        started = time.time()
        arguments: Dict[str, Any] = {}
        try:
            arguments = call.parsed_arguments()
        except LLMError as exc:
            result = ToolResult.failure(str(exc), error_type="BadArguments")
        else:
            try:
                tool = self.tools.get(call.name)
            except ToolNotFound as exc:
                result = ToolResult.failure(str(exc), error_type="ToolNotFound")
            else:
                result = self._run_tool_with_permission(tool, arguments)

        observation = self._render_observation(call.name, result)
        step = AgentStep(
            index=index,
            tool=call.name,
            arguments=arguments,
            ok=result.ok,
            observation=observation,
            elapsed=round(time.time() - started, 2),
            thought=thought,
        )
        self._emit(
            "tool_result",
            step=step.to_dict(),
            ok=result.ok,
            preview=result.content[:400],
        )
        return step

    def _run_tool_with_permission(self, tool: Tool, arguments: Dict[str, Any]) -> ToolResult:
        if tool.requires_approval and not self.config.auto_approve:
            approved = False
            if self.approver is not None:
                try:
                    approved = bool(self.approver(tool, arguments))
                except Exception:  # noqa: BLE001
                    approved = False
            if not approved:
                return ToolResult.failure(
                    f"用户拒绝执行 {tool.name}。请不要重复请求同一操作；"
                    "可以改为说明你打算做什么，或者采用只读方式继续分析。",
                    error_type="ToolDenied",
                )
        try:
            return self.tools.run(tool.name, arguments)
        except AgentError as exc:  # 理论上不会触发（run 内部已兜底）
            return ToolResult.failure(str(exc), error_type=type(exc).__name__)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(
                f"工具 {tool.name} 异常: {type(exc).__name__}: {exc}", error_type=type(exc).__name__
            )

    @staticmethod
    def _render_observation(tool_name: str, result: ToolResult) -> str:
        content = result.content or ""
        if len(content) > MAX_OBSERVATION_CHARS:
            content = (
                content[: MAX_OBSERVATION_CHARS // 2]
                + f"\n...（输出过长，已省略 {len(content) - MAX_OBSERVATION_CHARS} 字符）...\n"
                + content[-MAX_OBSERVATION_CHARS // 2 :]
            )
        if result.ok:
            return f"[{tool_name} 执行成功]\n{content}"
        return f"[{tool_name} 执行失败]\n{content}\n请根据上述错误调整参数或改用其他方式。"

    # ------------------------------------------------------------------ 主循环
    def run(self, user_input: str) -> AgentResult:
        """执行一次完整的 Agent 循环。"""
        self.memory.add_user(user_input)
        steps: List[AgentStep] = []
        usage_total: Dict[str, int] = {}
        repeats: Dict[str, int] = {}
        started = time.time()
        stopped_reason = "max_steps"
        answer = ""

        try:
            for index in range(1, self.config.max_steps + 1):
                self._emit("step_start", index=index, max_steps=self.config.max_steps)
                message = self._call_llm(index)
                self._accumulate_usage(usage_total, message)

                if not message.tool_calls:
                    answer = (message.content or "").strip()
                    self.memory.add_assistant(answer)
                    self._emit("final", content=answer)
                    stopped_reason = "final"
                    break

                if message.content.strip():
                    self._emit("assistant_message", content=message.content.strip())
                self.memory.add_assistant(message.content, message.tool_calls)

                for call in message.tool_calls:
                    parsed_args = _safe_json(call.arguments) or {}
                    key = f"{call.name}:{json.dumps(parsed_args, sort_keys=True, ensure_ascii=False)}"
                    repeats[key] = repeats.get(key, 0) + 1
                    self._emit(
                        "tool_call",
                        name=call.name,
                        arguments=_safe_json(call.arguments),
                        index=index,
                    )
                    step = self._execute_tool(call, message.content.strip(), index)
                    steps.append(step)
                    self.memory.add_tool_result(call.id, call.name, step.observation)

                    if repeats[key] >= REPEAT_LIMIT:
                        self._emit(
                            "warning",
                            message=f"检测到重复调用 {call.name} 达到 {repeats[key]} 次，已注入纠正提示",
                        )
                        self.memory.add_user(
                            f"[系统] 你已连续 {repeats[key]} 次用相同参数调用 {call.name}，"
                            "结果不会改变。请改用其他工具、调整参数，或直接基于现有信息给出结论。"
                        )
                    if repeats[key] >= REPEAT_LIMIT + 2:
                        answer, usage_total = self._force_finalize(
                            "loop_detected", steps, usage_total
                        )
                        stopped_reason = "loop_detected"
                        self._emit("final", content=answer)
                        return AgentResult(
                            answer=answer,
                            steps=steps,
                            stopped_reason=stopped_reason,
                            elapsed=round(time.time() - started, 2),
                            usage=usage_total,
                        )
            else:
                self._emit("warning", message=f"达到最大步数 {self.config.max_steps}，强制收敛")
                answer, usage_total = self._force_finalize("max_steps", steps, usage_total)
                stopped_reason = "max_steps"
                self._emit("final", content=answer)
        except LLMError as exc:
            stopped_reason = "error"
            answer = f"LLM 调用失败：{exc}"
            self._emit("error", message=str(exc))

        return AgentResult(
            answer=answer,
            steps=steps,
            stopped_reason=stopped_reason,
            elapsed=round(time.time() - started, 2),
            usage=usage_total,
            error=None if stopped_reason != "error" else answer,
        )

    # ------------------------------------------------------------------ 辅助
    def _call_llm(self, index: int, use_tools: bool = True) -> Message:
        schemas = self.tools.schemas() if (use_tools and len(self.tools)) else None
        if self.config.stream:
            self._emit("llm_start", index=index, stream=True)

            def on_delta(text: str) -> None:
                self._emit("assistant_delta", content=text, index=index)

        else:
            self._emit("llm_start", index=index, stream=False)
            on_delta = None
        return self.llm.generate(
            self.memory.build(),
            tools=schemas,
            on_delta=on_delta,
            stream=self.config.stream,
        )

    def _force_finalize(
        self, reason: str, steps: List[AgentStep], usage_total: Dict[str, int]
    ) -> tuple:
        """到达步数上限 / 检测到循环时，让模型基于现有信息直接作答。"""
        self.memory.add_user(
            "[系统] 工具调用阶段已结束（原因："
            + ("达到最大步数上限" if reason == "max_steps" else "检测到重复调用循环")
            + "）。请立即基于已获得的信息给出最终结论，不要再请求调用任何工具；"
            "如果信息不足，请明确说明还缺什么。"
        )
        try:
            message = self._call_llm(0, use_tools=False)
            self._accumulate_usage(usage_total, message)
            answer = (message.content or "").strip()
        except LLMError as exc:
            answer = f"（强制收敛时模型调用失败：{exc}）"
        self.memory.add_assistant(answer)
        return answer, usage_total

    @staticmethod
    def _accumulate_usage(total: Dict[str, int], message: Message) -> None:
        usage = message.usage or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = usage.get(key)
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
        total["llm_calls"] = total.get("llm_calls", 0) + 1

    # ------------------------------------------------------------------ 状态
    def reset(self) -> None:
        """清空对话历史（保留 system prompt）。"""
        self.memory.clear()

    def save_session(self, path: Path) -> Path:
        return self.memory.save(Path(path))

    def load_session(self, path: Path) -> None:
        loaded = Memory.load(Path(path))
        loaded.token_budget = self.config.memory_token_budget
        if not loaded.system_prompt:
            from .prompts import build_system_prompt

            loaded.system_prompt = build_system_prompt(self.config.workspace)
        self.memory = loaded

    def memory_stats(self) -> Dict[str, Any]:
        return self.memory.stats()


def _safe_json(raw: str) -> Optional[Dict[str, Any]]:
    """尽量解析工具参数用于展示；失败返回 None。"""
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
