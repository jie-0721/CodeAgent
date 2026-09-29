"""简易 Web 界面（标准库 http.server 实现，无第三方依赖）。

接口
----
``GET  /``              → 单页界面
``GET  /api/tools``     → 工具清单（JSON）
``GET  /api/info``      → 当前配置（API Key 已脱敏）
``POST /api/chat``      → 以 SSE 流式返回 Agent 事件
``POST /api/reset``     → 清空指定会话的上下文

事件通过 Server-Sent Events 推送给前端，与 CLI 共用同一套 Agent 事件协议，
因此界面和命令行看到的行为完全一致。
"""

from __future__ import annotations

import json
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

from .. import __version__
from ..agent import Agent
from ..config import Config
from ..errors import AgentError, ConfigError
from ..llm import build_llm
from ..memory import Memory
from ..prompts import build_system_prompt
from ..tools import Workspace, build_default_registry

STATIC_DIR = Path(__file__).resolve().parent / "static"


class Session:
    """一个浏览器会话对应一个 Agent 实例（含独立上下文与锁）。"""

    def __init__(self, config: Config) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.config = config
        self.lock = threading.Lock()
        self.workspace = Workspace(config.workspace)
        self.tools = build_default_registry(self.workspace)
        self.memory = Memory(
            system_prompt=build_system_prompt(config.workspace),
            token_budget=config.memory_token_budget,
        )
        self.llm = build_llm(config)
        self.last_result: Optional[Dict[str, Any]] = None

    def reset(self) -> None:
        self.memory.clear()
        self.last_result = None

    def make_agent(self, on_event: Any, auto_approve: bool) -> Agent:
        config = self.config.with_overrides(auto_approve=auto_approve)
        return Agent(
            llm=self.llm,
            tools=self.tools,
            config=config,
            memory=self.memory,
            on_event=on_event,
            approver=lambda tool, arguments: auto_approve,
        )


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: Any, handler: Any, config: Config) -> None:
        super().__init__(address, handler)
        self.config = config
        self.sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

    def get_session(self, session_id: Optional[str]) -> Session:
        with self._lock:
            if session_id and session_id in self.sessions:
                return self.sessions[session_id]
            session = Session(self.config)
            self.sessions[session.id] = session
            return session


class RequestHandler(BaseHTTPRequestHandler):
    server_version = f"CodeAgent/{__version__}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ 基础
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 - 覆盖父类
        if getattr(self.server, "config", None) and getattr(self.server.config, "verbose", False):
            super().log_message(fmt, *args)

    @property
    def app(self) -> AgentHTTPServer:
        return self.server  # type: ignore[return-value]

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _send_json(self, payload: Dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str = "text/plain; charset=utf-8", status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -------------------------------------------------------------------- GET
    def do_GET(self) -> None:  # noqa: N802 - 父类接口
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                index = STATIC_DIR / "index.html"
                if not index.is_file():
                    self._send_text("index.html 缺失", status=500)
                    return
                self._send_text(index.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            elif path == "/api/tools":
                session = self.app.get_session(None)
                self._send_json(
                    {
                        "tools": [
                            {
                                "name": tool.name,
                                "description": tool.description,
                                "mutating": tool.mutating,
                                "requires_approval": tool.requires_approval,
                            }
                            for tool in session.tools.all()
                        ]
                    }
                )
            elif path == "/api/info":
                config = self.app.config
                self._send_json(
                    {
                        "version": __version__,
                        "provider": config.provider,
                        "model": config.model,
                        "base_url": config.base_url,
                        "workspace": str(config.workspace),
                        "max_steps": config.max_steps,
                        "api_key": config.masked_key(),
                    }
                )
            elif path == "/api/health":
                self._send_json({"ok": True})
            else:
                self._send_json({"error": "未找到该路径"}, status=404)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass

    # ------------------------------------------------------------------- POST
    def do_POST(self) -> None:  # noqa: N802 - 父类接口
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/chat":
                self._handle_chat()
            elif path == "/api/reset":
                payload = self._read_json()
                session = self.app.get_session(payload.get("session_id"))
                session.reset()
                self._send_json({"ok": True, "session_id": session.id})
            else:
                self._send_json({"error": "未找到该路径"}, status=404)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            pass
        except AgentError as exc:
            self._send_json({"error": str(exc)}, status=500)

    # --------------------------------------------------------------- SSE 流
    def _handle_chat(self) -> None:
        payload = self._read_json()
        message = (payload.get("message") or "").strip()
        if not message:
            self._send_json({"error": "message 不能为空"}, status=400)
            return
        session = self.app.get_session(payload.get("session_id"))
        auto_approve = bool(payload.get("auto_approve", False))

        # SSE 没有 Content-Length，必须让连接在事件流结束后关闭，
        # 否则浏览器端的 fetch reader 永远等不到 done（HTTP/1.1 keep-alive 会一直挂住）。
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def send_event(event: Dict[str, Any]) -> None:
            try:
                chunk = f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(chunk)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                pass

        send_event({"type": "session", "session_id": session.id})

        with session.lock:  # 同一会话串行处理，避免上下文交叉
            try:
                agent = session.make_agent(send_event, auto_approve)
                result = agent.run(message)
                session.last_result = result.to_dict()
                send_event({"type": "done", "result": result.to_dict()})
            except ConfigError as exc:
                send_event({"type": "error", "message": f"配置错误：{exc}"})
            except Exception as exc:  # noqa: BLE001 - 保证前端能收到错误
                send_event({"type": "error", "message": f"{type(exc).__name__}: {exc}"})


def serve(config: Config, host: str = "127.0.0.1", port: int = 8000, open_browser: bool = False) -> int:
    """启动 Web 服务（阻塞）。"""
    try:
        httpd = AgentHTTPServer((host, port), RequestHandler, config)
    except OSError as exc:
        print(f"启动失败：{exc}（端口 {port} 可能已被占用，可用 --port 指定其他端口）")
        return 2

    url = f"http://{host}:{port}/"
    print(f"CodeAgent Web 界面已启动：{url}")
    print(f"  模型   : {config.model} ({config.provider})")
    print(f"  工作区 : {config.workspace}")
    print("  按 Ctrl+C 停止服务")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        httpd.server_close()
    return 0
