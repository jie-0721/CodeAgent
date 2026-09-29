"""LLM 层测试：SSE 流式解析、消息序列化、请求体构造。"""

from __future__ import annotations

import json
import unittest

from codeagent.config import Config
from codeagent.errors import LLMError
from codeagent.llm import (
    Message,
    OpenAICompatibleLLM,
    StreamAccumulator,
    ToolCall,
    iter_sse_chunks,
    parse_stream,
)


def sse(*chunks: dict) -> list:
    """把若干 JSON 分片伪装成 SSE 行。"""
    lines = [f"data: {json.dumps(chunk, ensure_ascii=False)}" for chunk in chunks]
    lines.append("data: [DONE]")
    return lines


class TestStreamParsing(unittest.TestCase):
    def test_parse_content_deltas(self) -> None:
        lines = sse(
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {"content": "，世界"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        )
        deltas: list = []
        message = parse_stream(lines, on_delta=deltas.append)
        self.assertEqual(message.content, "你好，世界")
        self.assertEqual(deltas, ["你好", "，世界"])
        self.assertEqual(message.tool_calls, [])

    def test_parse_fragmented_tool_call(self) -> None:
        """工具参数是分片到达的，必须正确拼接。"""
        lines = sse(
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read_file", "arguments": '{"pa'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.py"'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "}"}}]}}]},
        )
        message = parse_stream(lines)
        self.assertEqual(len(message.tool_calls), 1)
        call = message.tool_calls[0]
        self.assertEqual(call.name, "read_file")
        self.assertEqual(call.parsed_arguments(), {"path": "a.py"})

    def test_multiple_tool_calls_are_ordered(self) -> None:
        lines = sse(
            {"choices": [{"delta": {"tool_calls": [{"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}}]}}]},
        )
        message = parse_stream(lines)
        self.assertEqual([call.name for call in message.tool_calls], ["one", "two"])

    def test_iter_sse_chunks_skips_noise(self) -> None:
        lines = [": 心跳", "", "data: {\"choices\": []}", "data: not-json", "data: [DONE]"]
        chunks = list(iter_sse_chunks(lines))
        self.assertEqual(chunks, [{"choices": []}])

    def test_usage_is_captured(self) -> None:
        accumulator = StreamAccumulator()
        accumulator.feed({"usage": {"total_tokens": 42}})
        self.assertEqual(accumulator.message().usage, {"total_tokens": 42})


class TestMessage(unittest.TestCase):
    def test_tool_call_bad_json_raises(self) -> None:
        with self.assertRaises(LLMError):
            ToolCall(id="x", name="t", arguments="{not json").parsed_arguments()

    def test_tool_call_non_object_json_raises(self) -> None:
        with self.assertRaises(LLMError):
            ToolCall(id="x", name="t", arguments="[1,2]").parsed_arguments()

    def test_assistant_with_tool_calls_serialises_content_null(self) -> None:
        message = Message.assistant("", [ToolCall(id="1", name="read_file", arguments="{}")])
        payload = message.to_api()
        self.assertIsNone(payload["content"])
        self.assertEqual(payload["tool_calls"][0]["function"]["name"], "read_file")

    def test_message_from_api_handles_missing_fields(self) -> None:
        message = Message.from_api({"role": "assistant", "content": None})
        self.assertEqual(message.content, "")
        self.assertEqual(message.tool_calls, [])

    def test_tool_message_roundtrip(self) -> None:
        message = Message.tool("call_1", "read_file", "内容")
        payload = message.to_api()
        self.assertEqual(payload["tool_call_id"], "call_1")
        self.assertEqual(payload["role"], "tool")


class TestPayload(unittest.TestCase):
    def setUp(self) -> None:
        self.config = Config(api_key="k", model="m", base_url="https://example.com/v1/")
        self.llm = OpenAICompatibleLLM(self.config)

    def test_endpoint_has_no_double_slash(self) -> None:
        self.assertEqual(self.llm.endpoint, "https://example.com/v1/chat/completions")

    def test_build_payload_includes_tools(self) -> None:
        payload = self.llm.build_payload([Message.user("hi")], [{"type": "function"}], stream=True)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["tool_choice"], "auto")
        self.assertEqual(payload["messages"][0]["content"], "hi")

    def test_headers_use_bearer_token(self) -> None:
        self.assertEqual(self.llm._headers()["Authorization"], "Bearer k")


if __name__ == "__main__":
    unittest.main()
