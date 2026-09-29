"""LLM 客户端。

只依赖标准库 ``urllib`` 实现 OpenAI Chat Completions 协议，
因此可以对接任何兼容端点：DeepSeek、通义千问(DashScope 兼容模式)、Moonshot、
本地 Ollama（``http://localhost:11434/v1``）等。

关键设计：

* :class:`Message` / :class:`ToolCall` 是 Agent 与模型之间的统一数据结构；
* :func:`parse_stream` 是纯函数，把 SSE 行流解析成 :class:`Message`，
  因此可以脱离网络做单元测试；
* :meth:`OpenAICompatibleLLM.generate` 内置指数退避重试，并且当服务端不支持
  流式时自动降级为非流式。
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from .config import Config
from .errors import LLMConnectionError, LLMError

logger = logging.getLogger("codeagent.llm")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class ToolCall:
    """模型发起的一次工具调用。"""

    id: str
    name: str
    arguments: str = "{}"

    def parsed_arguments(self) -> Dict[str, Any]:
        """把 arguments 解析成 dict；解析失败时抛出 :class:`LLMError`。"""
        raw = (self.arguments or "").strip() or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMError(f"工具 {self.name} 的参数不是合法 JSON: {raw[:200]!r}") from exc
        if not isinstance(data, dict):
            raise LLMError(f"工具 {self.name} 的参数必须是 JSON 对象，实际为 {type(data).__name__}")
        return data

    def to_api(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments or "{}"},
        }


@dataclass
class Message:
    """统一消息结构（同时兼容 OpenAI 的请求/响应格式）。"""

    role: str
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None

    # -- 序列化 ------------------------------------------------------------
    def to_api(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {"role": self.role}
        if self.role == "assistant" and self.tool_calls:
            # 有工具调用时部分服务端要求 content 为 null
            data["content"] = self.content or None
        else:
            data["content"] = self.content
        if self.tool_calls:
            data["tool_calls"] = [call.to_api() for call in self.tool_calls]
        if self.tool_call_id:
            data["tool_call_id"] = self.tool_call_id
        if self.name:
            data["name"] = self.name
        return data

    @classmethod
    def from_api(cls, data: Dict[str, Any]) -> "Message":
        calls: List[ToolCall] = []
        for idx, raw in enumerate(data.get("tool_calls") or []):
            func = raw.get("function") or {}
            calls.append(
                ToolCall(
                    id=raw.get("id") or f"call_{idx}",
                    name=func.get("name") or "",
                    arguments=func.get("arguments") or "{}",
                )
            )
        content = data.get("content") or ""
        return cls(
            role=data.get("role", "assistant"),
            content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            tool_calls=calls,
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            usage=data.get("usage"),
        )

    # -- 便捷构造 ----------------------------------------------------------
    @classmethod
    def system(cls, content: str) -> "Message":
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> "Message":
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str, tool_calls: Optional[List[ToolCall]] = None) -> "Message":
        return cls(role="assistant", content=content, tool_calls=tool_calls or [])

    @classmethod
    def tool(cls, tool_call_id: str, name: str, content: str) -> "Message":
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)


# --------------------------------------------------------------------------- #
# SSE 解析（纯函数，便于测试）
# --------------------------------------------------------------------------- #
class StreamAccumulator:
    """把流式返回的分片拼装成完整 :class:`Message`。"""

    def __init__(self) -> None:
        self._content: List[str] = []
        self._calls: Dict[int, Dict[str, Any]] = {}
        self.finish_reason: Optional[str] = None
        self.usage: Optional[Dict[str, Any]] = None

    def feed(self, chunk: Dict[str, Any]) -> List[str]:
        """喂入一个 SSE JSON 分片，返回本次新增的正文文本增量。"""
        if chunk.get("usage"):
            self.usage = chunk["usage"]
        choices = chunk.get("choices") or []
        if not choices:
            return []
        choice = choices[0] or {}
        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
        delta = choice.get("delta") or {}
        deltas: List[str] = []

        text = delta.get("content")
        if isinstance(text, str) and text:
            self._content.append(text)
            deltas.append(text)

        for raw in delta.get("tool_calls") or []:
            index = int(raw.get("index", 0))
            slot = self._calls.setdefault(index, {"id": "", "name": "", "arguments": []})
            if raw.get("id"):
                slot["id"] = raw["id"]
            func = raw.get("function") or {}
            if func.get("name"):
                slot["name"] = func["name"]
            if func.get("arguments"):
                slot["arguments"].append(func["arguments"])

        return deltas

    def message(self) -> Message:
        calls = [
            ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"],
                arguments="".join(slot["arguments"]) or "{}",
            )
            for index, slot in sorted(self._calls.items())
        ]
        return Message(
            role="assistant",
            content="".join(self._content),
            tool_calls=calls,
            usage=self.usage,
        )


def iter_sse_chunks(lines: Iterable[Any]) -> Iterator[Dict[str, Any]]:
    """把 SSE 原始行流转换成 JSON 分片流（忽略心跳与 ``[DONE]``）。"""
    for raw in lines:
        if isinstance(raw, bytes):
            line = raw.decode("utf-8", errors="replace")
        else:
            line = str(raw)
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        if not line or line == "[DONE]":
            if line == "[DONE]":
                return
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue  # 容忍非 JSON 的心跳/注释行
        if isinstance(chunk, dict):
            yield chunk


def parse_stream(lines: Iterable[Any], on_delta: Optional[Any] = None) -> Message:
    """解析流式响应，返回聚合后的消息；``on_delta`` 用于实时回调文本增量。"""
    accumulator = StreamAccumulator()
    for chunk in iter_sse_chunks(lines):
        for delta in accumulator.feed(chunk):
            if on_delta is not None:
                on_delta(delta)
    return accumulator.message()


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class BaseLLM:
    """LLM 抽象基类（:class:`codeagent.mock.MockLLM` 也实现该接口）。"""

    name = "base"

    def generate(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_delta: Optional[Any] = None,
        stream: bool = True,
    ) -> Message:  # pragma: no cover - 抽象方法
        raise NotImplementedError


class OpenAICompatibleLLM(BaseLLM):
    """OpenAI Chat Completions 兼容客户端。"""

    name = "openai-compatible"

    def __init__(self, config: Config) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.endpoint = f"{self.base_url}/chat/completions"

    # ------------------------------------------------------------- 请求构造
    def build_payload(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]],
        stream: bool,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": self.config.model,
            "messages": [m.to_api() for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if stream:
            # 部分服务端支持该字段以在流末尾返回用量统计
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if self.config.stream else "application/json",
            "User-Agent": "codeagent/1.0 (+homework1)",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _open(self, payload: Dict[str, Any]) -> Any:
        """发起一次 HTTP 请求，返回已打开、可迭代的响应对象。"""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.endpoint, data=body, headers=self._headers(), method="POST")
        try:
            return urllib.request.urlopen(request, timeout=self.config.timeout)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # pragma: no cover - 读取失败不影响主流程
                detail = "<无法读取响应体>"
            if exc.code in (408, 409, 429) or exc.code >= 500:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                error = LLMConnectionError(f"HTTP {exc.code} {exc.reason}: {detail}")
                if retry_after:
                    try:
                        error.retry_after = float(retry_after)  # type: ignore[attr-defined]
                    except ValueError:
                        pass
                raise error from exc
            raise LLMError(f"HTTP {exc.code} {exc.reason}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMConnectionError(f"网络错误: {exc.reason}") from exc
        except TimeoutError as exc:
            raise LLMConnectionError(f"请求超时（{self.config.timeout}s）") from exc

    # ------------------------------------------------------------- 重试包装
    def _with_retry(self, operation: Any, description: str) -> Any:
        attempts = max(1, self.config.max_retries + 1)
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                return operation()
            except LLMConnectionError as exc:
                last_error = exc
                if attempt == attempts - 1:
                    break
                delay = getattr(exc, "retry_after", None)
                if delay is None:
                    delay = min(30.0, 1.0 * (2**attempt)) + random.uniform(0, 0.4)
                time.sleep(float(delay))
        raise LLMConnectionError(
            f"{description} 连续失败 {attempts} 次，已放弃。最后一次错误：{last_error}"
        ) from last_error

    # --------------------------------------------------------------- 主流程
    def generate(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]] = None,
        on_delta: Optional[Any] = None,
        stream: bool = True,
    ) -> Message:
        use_stream = bool(stream)
        try:
            return self._generate_once(messages, tools, on_delta, use_stream)
        except LLMError as exc:
            # 服务端不支持 stream_options / 流式时降级重试一次（仅针对 4xx 参数类错误）
            if use_stream and not isinstance(exc, LLMConnectionError):
                return self._generate_once(messages, tools, on_delta, False)
            raise

    def _generate_once(
        self,
        messages: Sequence[Message],
        tools: Optional[List[Dict[str, Any]]],
        on_delta: Optional[Any],
        stream: bool,
    ) -> Message:
        payload = self.build_payload(messages, tools, stream)

        def call() -> Message:
            response = self._open(payload)
            try:
                if stream:
                    return parse_stream(response, on_delta=on_delta)
                return self._parse_json_response(response)
            finally:
                try:
                    response.close()
                except Exception:  # pragma: no cover - 关闭失败无补救手段，仅记录
                    logger.debug("关闭 HTTP 响应失败", exc_info=True)

        return self._with_retry(call, f"调用 {self.config.model}")

    @staticmethod
    def _parse_json_response(response: Any) -> Message:
        raw = response.read()
        text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"响应不是合法 JSON: {text[:300]!r}") from exc
        if isinstance(data, dict) and data.get("error"):
            raise LLMError(f"服务端返回错误: {data['error']}")
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"响应缺少 choices 字段: {text[:300]!r}")
        message = Message.from_api(choices[0].get("message") or {})
        message.usage = data.get("usage")
        return message


def build_llm(config: Config) -> BaseLLM:
    """按配置创建 LLM 实例。"""
    if config.provider == "mock":
        from .mock import MockLLM  # 延迟导入，避免循环依赖

        return MockLLM(config)
    return OpenAICompatibleLLM(config)
