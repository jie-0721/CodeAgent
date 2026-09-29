"""会话记忆（上下文管理）。

职责
----
* 维护 ``system + user + assistant + tool`` 消息序列；
* 在 token 预算内裁剪历史（多轮对话不会因上下文无限增长而崩掉）；
* 裁剪时保证 **工具调用配对完整性**：不能只保留 tool 结果而丢掉对应的 assistant 工具调用，
  否则 OpenAI 兼容接口会直接报 400；
* 支持会话落盘 / 恢复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .errors import AgentError
from .llm import Message, ToolCall

#: 每条消息的固定开销（role / 分隔符等）估算值
MESSAGE_OVERHEAD_TOKENS = 6
#: 为模型输出预留的 token（不参与历史裁剪）
RESERVED_OUTPUT_TOKENS = 1024


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数：ASCII 约 4 字符/token，中日韩字符约 1.5 字符/token。"""
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    wide_chars = len(text) - ascii_chars
    return int(ascii_chars / 4 + wide_chars / 1.5) + 1


def message_tokens(message: Message) -> int:
    total = estimate_tokens(message.content) + MESSAGE_OVERHEAD_TOKENS
    for call in message.tool_calls:
        total += estimate_tokens(call.name) + estimate_tokens(call.arguments) + 4
    return total


@dataclass
class Memory:
    """一次会话的消息历史与裁剪策略。"""

    system_prompt: str = ""
    token_budget: int = 6000
    messages: List[Message] = field(default_factory=list)
    dropped_count: int = 0

    # ------------------------------------------------------------------ 写入
    def add(self, message: Message) -> Message:
        self.messages.append(message)
        return message

    def add_user(self, content: str) -> Message:
        return self.add(Message.user(content))

    def add_assistant(self, content: str, tool_calls: Optional[List[ToolCall]] = None) -> Message:
        return self.add(Message.assistant(content, tool_calls))

    def add_tool_result(self, tool_call_id: str, name: str, content: str) -> Message:
        return self.add(Message.tool(tool_call_id, name, content))

    def extend(self, messages: Iterable[Message]) -> None:
        for message in messages:
            self.add(message)

    def clear(self) -> None:
        self.messages.clear()
        self.dropped_count = 0

    # ------------------------------------------------------------------ 构建
    def build(self, budget: Optional[int] = None) -> List[Message]:
        """返回可以直接发给模型的上下文（system + 预算内的历史）。"""
        limit = max(256, int(budget or self.token_budget) - RESERVED_OUTPUT_TOKENS)
        system_message = Message.system(self._system_with_notice())
        available = max(256, limit - message_tokens(system_message))

        kept: List[Message] = []
        used = 0
        for message in reversed(self.messages):
            cost = message_tokens(message)
            if kept and used + cost > available:
                break
            kept.append(message)
            used += cost
        kept.reverse()
        kept = self._repair_pairing(kept)

        self.dropped_count = len(self.messages) - len(kept)
        return [system_message, *kept]

    def _repair_pairing(self, kept: List[Message]) -> List[Message]:
        """保证裁剪后的首条消息不是孤立的 tool 结果。"""
        index = 0
        # 开头的 tool 消息一定缺少对应的 assistant 工具调用，直接丢弃
        while index < len(kept) and kept[index].role == "tool":
            index += 1
        return kept[index:]

    def _system_with_notice(self) -> str:
        if self.dropped_count <= 0:
            return self.system_prompt
        return (
            f"{self.system_prompt}\n\n"
            f"（注意：为控制上下文长度，本次已省略较早的 {self.dropped_count} 条历史消息；"
            "如需回顾请用 read_file 重新读取文件。）"
        )

    # ------------------------------------------------------------------ 统计
    def token_usage(self) -> int:
        return sum(message_tokens(message) for message in self.messages)

    def stats(self) -> Dict[str, Any]:
        by_role: Dict[str, int] = {}
        for message in self.messages:
            by_role[message.role] = by_role.get(message.role, 0) + 1
        return {
            "messages": len(self.messages),
            "tokens": self.token_usage(),
            "budget": self.token_budget,
            "by_role": by_role,
            "truncated": max(0, len(self.messages) - (len(self.build()) - 1)),
        }

    # ------------------------------------------------------------------ 落盘
    def to_dict(self) -> Dict[str, Any]:
        return {
            "system_prompt": self.system_prompt,
            "token_budget": self.token_budget,
            "messages": [_message_to_dict(message) for message in self.messages],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Memory":
        memory = cls(
            system_prompt=data.get("system_prompt", ""),
            token_budget=int(data.get("token_budget", 6000)),
        )
        for raw in data.get("messages", []):
            memory.add(_message_from_dict(raw))
        return memory

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Memory":
        path = Path(path)
        if not path.is_file():
            raise AgentError(f"会话文件不存在: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AgentError(f"会话文件不是合法 JSON: {path}") from exc
        return cls.from_dict(data)


def _message_to_dict(message: Message) -> Dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": call.arguments} for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
        "name": message.name,
    }


def _message_from_dict(data: Dict[str, Any]) -> Message:
    calls = [
        ToolCall(id=item.get("id", ""), name=item.get("name", ""), arguments=item.get("arguments", "{}"))
        for item in data.get("tool_calls", [])
    ]
    return Message(
        role=data.get("role", "user"),
        content=data.get("content", "") or "",
        tool_calls=calls,
        tool_call_id=data.get("tool_call_id"),
        name=data.get("name"),
    )
