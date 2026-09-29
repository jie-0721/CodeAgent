"""命令行入口。

提供 5 个子命令::

    python -m codeagent chat                  # 交互式对话（推荐）
    python -m codeagent ask "审查 x.py"        # 单次提问，适合脚本/CI
    python -m codeagent demo                  # 离线演示（不需要 API Key）
    python -m codeagent analyze x.py          # 直接调用静态分析工具（不经过 LLM）
    python -m codeagent web --port 8000       # 启动简易 Web 界面

交互式会话里还支持 ``/tools``、``/review <文件>``、``/memory``、``/save`` 等斜杠命令。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import __version__
from .agent import Agent, AgentResult
from .config import Config, load_default_env_files
from .errors import AgentError, ConfigError
from .llm import build_llm
from .memory import Memory
from .mock import MockLLM
from .prompts import build_review_prompt, build_system_prompt
from .tools import Tool, ToolRegistry, Workspace, build_default_registry
from .tools.analysis import analyze_source, format_findings

# --------------------------------------------------------------------------- #
# 终端美化
# --------------------------------------------------------------------------- #
_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}


def supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.name == "nt":
        # Windows Terminal / VS Code / 新版 conhost 都支持 ANSI
        return bool(os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM") or sys.stdout.isatty())
    return sys.stdout.isatty()


class Console:
    """极简彩色输出封装。"""

    def __init__(self, color: Optional[bool] = None) -> None:
        self.color = supports_color() if color is None else color
        if self.color and os.name == "nt":
            os.system("")  # 启用 Windows 控制台的 VT 处理

    def paint(self, text: str, style: str) -> str:
        if not self.color or style not in _ANSI:
            return text
        return f"{_ANSI[style]}{text}{_ANSI['reset']}"

    def print(self, text: str = "", style: Optional[str] = None) -> None:
        if style:
            text = self.paint(text, style)
        try:
            print(text)
        except UnicodeEncodeError:  # pragma: no cover - 极端终端编码
            print(text.encode("utf-8", "replace").decode("utf-8", "replace"))

    def rule(self, title: str = "") -> None:
        line = "─" * max(10, 60 - len(title))
        self.print(f"{title} {line}" if title else "─" * 60, "dim")

    def write(self, text: str, style: Optional[str] = None) -> None:
        """不换行输出（用于流式打印）。"""
        if style:
            text = self.paint(text, style)
        sys.stdout.write(text)
        sys.stdout.flush()


def setup_stdio() -> None:
    """统一使用 UTF-8，避免 Windows 控制台默认 GBK 导致中文/符号乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass


# --------------------------------------------------------------------------- #
# 事件渲染
# --------------------------------------------------------------------------- #
class ConsoleRenderer:
    """把 Agent 事件渲染成终端输出（CLI 与 Agent 主循环解耦）。"""

    def __init__(self, console: Console, verbose: bool = False) -> None:
        self.console = console
        self.verbose = verbose
        self._streamed = False

    def __call__(self, event: Dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "llm_start":
            self._streamed = False
        elif kind == "assistant_delta":
            self._streamed = True
            self.console.write(event.get("content", ""), "dim")
        elif kind == "tool_call":
            self.console.print()
            args = json.dumps(event.get("arguments"), ensure_ascii=False)
            self.console.print(f"  ⚙ 调用工具 {event.get('name')}({args})", "cyan")
        elif kind == "tool_result":
            step = event.get("step", {})
            ok = event.get("ok")
            mark = "✓" if ok else "✗"
            style = "green" if ok else "red"
            self.console.print(f"  {mark} 工具返回（{step.get('elapsed', 0)}s）", style)
            preview = self._preview(event.get("preview", ""))
            for line in preview:
                self.console.print(f"  │ {line}", "dim")
        elif kind == "assistant_message":
            if self.verbose and not self._streamed:
                self.console.print(event.get("content", ""), "dim")
        elif kind == "warning":
            self.console.print(f"  ⚠ {event.get('message')}", "yellow")
        elif kind == "final":
            if self._streamed:
                self.console.print()
            else:
                self.console.print(event.get("content", ""))
        elif kind == "error":
            self.console.print(f"  ✗ {event.get('message')}", "red")

    @staticmethod
    def _preview(text: str, max_lines: int = 6, width: int = 110) -> List[str]:
        lines: List[str] = []
        for raw in (text or "").splitlines():
            if len(raw) > width:
                raw = raw[: width - 3] + "..."
            lines.append(raw)
            if len(lines) >= max_lines:
                lines.append("...")
                break
        return lines or ["(空输出)"]


def make_approver(console: Console, config: Config):
    """构造工具授权回调：写操作/执行代码前询问用户。"""

    def approver(tool: Tool, arguments: Dict[str, Any]) -> bool:
        console.print()
        console.print(f"  ⚠ 工具 {tool.name} 会修改工作目录内容，需要确认：", "yellow")
        payload = json.dumps(arguments, ensure_ascii=False, indent=2)
        for line in payload.splitlines()[:20]:
            console.print(f"    {line}", "dim")
        if not sys.stdin.isatty():
            console.print("  （非交互环境，默认拒绝；如需自动放行请加 --yes）", "dim")
            return False
        try:
            answer = input(console.paint("  允许执行？[y/N] ", "bold")).strip().lower()
        except EOFError:
            return False
        return answer in {"y", "yes"}

    return approver


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #
def build_agent(config: Config, console: Console, approver: Optional[Any] = None, llm: Optional[Any] = None) -> Agent:
    """按配置装配 Agent（LLM + 工具 + 记忆）。"""
    workspace = Workspace(config.workspace)
    registry = build_default_registry(workspace)
    model = llm or build_llm(config)
    memory = Memory(system_prompt=build_system_prompt(config.workspace), token_budget=config.memory_token_budget)
    renderer = ConsoleRenderer(console, verbose=config.verbose)
    return Agent(
        llm=model,
        tools=registry,
        config=config,
        memory=memory,
        on_event=renderer,
        approver=approver if approver is not None else make_approver(console, config),
    )


def build_config(args: argparse.Namespace, console: Console) -> Config:
    """把命令行参数合并进配置。"""
    load_default_env_files()
    config = Config.from_env(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key=args.api_key,
        temperature=args.temperature,
        max_steps=args.max_steps,
        timeout=args.timeout,
        max_retries=args.max_retries,
        memory_token_budget=args.memory_tokens,
        workspace=Path(args.workspace),
        auto_approve=True if args.yes else None,
        stream=False if args.no_stream else None,
        verbose=True if args.verbose else None,
        session_dir=Path(args.session_dir) if getattr(args, "session_dir", None) else None,
    ).normalized()
    return config


def print_banner(console: Console, config: Config, registry: ToolRegistry) -> None:
    console.print(f"CodeAgent v{__version__}", "bold")
    console.print(f"  模型   : {config.model} ({config.provider})", "dim")
    console.print(f"  接口   : {config.base_url}", "dim")
    console.print(f"  工作区 : {config.workspace}", "dim")
    console.print(f"  工具   : {', '.join(registry.names())}（{len(registry)} 个）", "dim")
    console.print(f"  权限   : {'自动放行写操作/执行代码' if config.auto_approve else '写操作与执行代码需确认'}", "dim")
    console.print("  输入 /help 查看命令，/exit 退出。", "dim")


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
HELP_TEXT = """\
可用命令：
  /help              显示本帮助
  /tools             列出所有工具及其用途
  /config            显示当前配置
  /review <文件>     对指定文件发起标准代码审查
  /memory            显示上下文用量与消息统计
  /history           打印当前会话的消息角色序列
  /reset             清空对话历史（保留系统提示词）
  /save [路径]       保存当前会话到 JSON
  /load <路径>       从 JSON 恢复会话
  /verbose           切换是否打印模型的中间思考文本
  /exit 或 /quit     退出
"""


def _build_slash_commands(agent: Agent, config: Config, console: Console) -> Dict[str, Any]:
    """构造 REPL 斜杠命令表。

    用字典派发代替长 if/elif 链：每个命令是独立小函数，新增命令只需加一行，
    也让每个函数的圈复杂度保持在低位（便于单测与维护）。
    """

    def show_help(rest: str) -> bool:
        console.print(HELP_TEXT)
        return False

    def show_tools(rest: str) -> bool:
        console.print(agent.tools.describe())
        return False

    def show_config(rest: str) -> bool:
        console.print(config.describe())
        return False

    def reset(rest: str) -> bool:
        agent.reset()
        console.print("已清空对话历史。", "green")
        return False

    def show_memory(rest: str) -> bool:
        console.print(json.dumps(agent.memory_stats(), ensure_ascii=False, indent=2), "dim")
        return False

    def show_history(rest: str) -> bool:
        for index, message in enumerate(agent.memory.messages, start=1):
            preview = (message.content or "").replace("\n", " ")[:60]
            extra = f" (+{len(message.tool_calls)} tool_calls)" if message.tool_calls else ""
            console.print(f"  {index:>3}. {message.role:<9}{extra} {preview}", "dim")
        return False

    def toggle_verbose(rest: str) -> bool:
        config.verbose = not config.verbose
        console.print(f"verbose = {config.verbose}")
        return False

    def save_session(rest: str) -> bool:
        path = Path(rest) if rest else Path(config.session_dir) / "session.json"
        try:
            saved = agent.save_session(path)
        except OSError as exc:
            console.print(f"保存会话失败：{exc}", "red")
            return False
        console.print(f"已保存：{saved}", "green")
        return False

    def load_session(rest: str) -> bool:
        if not rest:
            console.print("用法：/load <路径>", "yellow")
            return False
        try:
            agent.load_session(Path(rest))
        except AgentError as exc:
            console.print(f"加载失败：{exc}", "red")
            return False
        console.print(f"已加载：{rest}", "green")
        return False

    def review(rest: str) -> bool:
        if not rest:
            console.print("用法：/review <文件路径>", "yellow")
            return False
        _run_once(agent, build_review_prompt(rest), console, config)
        return False

    def quit_repl(rest: str) -> bool:
        return True

    return {
        "help": show_help,
        "tools": show_tools,
        "config": show_config,
        "reset": reset,
        "memory": show_memory,
        "history": show_history,
        "verbose": toggle_verbose,
        "save": save_session,
        "load": load_session,
        "review": review,
        "exit": quit_repl,
        "quit": quit_repl,
        "q": quit_repl,
    }


def _handle_slash_command(user_input: str, commands: Dict[str, Any], console: Console) -> bool:
    """执行一条斜杠命令，返回 True 表示应当退出 REPL。"""
    command, _, rest = user_input[1:].partition(" ")
    handler = commands.get(command.lower().strip())
    if handler is None:
        console.print(f"未知命令：/{command.strip()}（输入 /help 查看帮助）", "yellow")
        return False
    return bool(handler(rest.strip()))


def cmd_chat(args: argparse.Namespace, console: Console) -> int:
    config = build_config(args, console)
    config.validate()
    agent = build_agent(config, console)
    print_banner(console, config, agent.tools)

    if args.session and Path(args.session).is_file():
        try:
            agent.load_session(Path(args.session))
            console.print(f"已恢复会话：{args.session}", "green")
        except AgentError as exc:
            console.print(f"恢复会话失败：{exc}", "red")

    commands = _build_slash_commands(agent, config, console)
    exit_code = 0

    while True:
        try:
            user_input = input(console.paint("\n你 > ", "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见。", "dim")
            break
        if not user_input:
            continue
        if user_input.startswith("/"):
            if _handle_slash_command(user_input, commands, console):
                break
            continue

        result = _run_once(agent, user_input, console, config)
        if result.stopped_reason == "error":
            exit_code = 2

    if args.session:
        try:
            saved = agent.save_session(Path(args.session))
            console.print(f"会话已保存到 {saved}", "dim")
        except OSError as exc:
            console.print(f"保存会话失败：{exc}", "red")
    return exit_code


def _run_once(agent: Agent, prompt: str, console: Console, config: Config) -> AgentResult:
    console.rule("Agent 运行")
    result = agent.run(prompt)
    console.rule()
    console.print(
        f"完成：{result.tool_calls} 次工具调用，用时 {result.elapsed}s，"
        f"结束原因={result.stopped_reason}"
        + (f"，tokens={result.usage.get('total_tokens')}" if result.usage.get("total_tokens") else ""),
        "dim",
    )
    return result


def cmd_ask(args: argparse.Namespace, console: Console) -> int:
    config = build_config(args, console)
    config.validate()
    agent = build_agent(config, console)
    result = agent.run(args.prompt)
    if args.json:
        console.print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    return 0 if result.stopped_reason == "final" else (2 if result.stopped_reason == "error" else 1)


def cmd_tools(args: argparse.Namespace, console: Console) -> int:
    config = build_config(args, console).normalized()
    registry = build_default_registry(Workspace(config.workspace))
    console.print(f"共 {len(registry)} 个工具：", "bold")
    for tool in registry.all():
        console.print(f"\n● {tool.name}", "cyan")
        console.print(textwrap.fill(tool.description, width=96, initial_indent="    ", subsequent_indent="    "))
        params = tool.parameters.get("properties", {})
        required = set(tool.parameters.get("required", []))
        for name, spec in params.items():
            flag = "必填" if name in required else "可选"
            console.print(f"    - {name} ({spec.get('type', 'any')}, {flag}): {spec.get('description', '')}", "dim")
    return 0


def cmd_analyze(args: argparse.Namespace, console: Console) -> int:
    """不经过 LLM，直接调用静态分析工具（演示工具的确定性能力）。"""
    config = build_config(args, console).normalized()
    workspace = Workspace(config.workspace)
    target = workspace.resolve(args.path, must_exist=True)
    if target.is_dir():
        console.print(f"{args.path} 是目录，请指定具体 .py 文件", "red")
        return 2
    findings, summary = analyze_source(workspace.read_text(target), workspace.relative(target))
    from .tools.analysis import SEVERITY_ORDER

    threshold = SEVERITY_ORDER.get(args.min_severity, 3)
    filtered = [f for f in findings if SEVERITY_ORDER.get(f.severity, 9) <= threshold]
    console.print(format_findings(filtered, summary, workspace.relative(target)))
    return 0 if not any(f.severity == "high" for f in filtered) else 1


DEMO_SCENARIOS = {
    "review": "请审查 examples/buggy_code.py 的代码质量，列出问题并给出修复建议",
    "explain": "解释 examples/buggy_code.py 的结构和主要逻辑",
    "search": "搜索项目里所有的 TODO 标记在哪里",
    "tests": "运行 tests 目录下的测试，看是否全部通过",
}


def cmd_demo(args: argparse.Namespace, console: Console) -> int:
    """离线演示：强制使用 MockLLM，无需 API Key。"""
    config = build_config(args, console).normalized()
    config = config.with_overrides(provider="mock", auto_approve=True, stream=True, verbose=False)
    workspace = Workspace(config.workspace)
    registry = build_default_registry(workspace)
    renderer = ConsoleRenderer(console, verbose=False)

    names = list(DEMO_SCENARIOS) if args.scenario == "all" else [args.scenario]
    console.print(f"离线演示（Mock LLM，无需 API Key）| 工作区 {config.workspace}", "bold")
    console.print("说明：工具调用与文件读取都是真实执行的，只有模型推理是本地规则模拟。", "dim")

    for name in names:
        prompt = DEMO_SCENARIOS[name]
        console.print()
        console.rule(f"场景 {name}")
        console.print(f"用户 > {prompt}", "bold")
        memory = Memory(system_prompt=build_system_prompt(config.workspace), token_budget=config.memory_token_budget)
        agent = Agent(
            llm=MockLLM(config),
            tools=registry,
            config=config,
            memory=memory,
            on_event=renderer,
            approver=lambda tool, arguments: True,
        )
        result = agent.run(prompt)
        console.print(
            f"  场景结束：{result.tool_calls} 次工具调用，用时 {result.elapsed}s",
            "dim",
        )
    console.print()
    console.print("提示：配置真实 API Key 后运行 `python -m codeagent chat` 即可进行自由对话。", "green")
    return 0


def cmd_web(args: argparse.Namespace, console: Console) -> int:
    config = build_config(args, console).normalized()
    config.validate()
    from .web.server import serve

    return serve(config, host=args.host, port=args.port, open_browser=args.open_browser)


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #
def add_common_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("通用选项")
    group.add_argument("--provider", choices=["openai", "mock"], help="LLM 提供方，默认 openai（OpenAI 兼容接口）")
    group.add_argument("--model", help="模型名，如 deepseek-chat / qwen-plus / gpt-4o-mini")
    group.add_argument("--base-url", help="OpenAI 兼容接口地址，如 https://api.deepseek.com/v1")
    group.add_argument("--api-key", help="API Key（更推荐用环境变量或 .env，避免进入命令历史）")
    group.add_argument("--temperature", type=float, help="采样温度，默认 0.2")
    group.add_argument("--max-steps", type=int, help="Agent 最大推理步数，默认 12")
    group.add_argument("--timeout", type=float, help="单次 LLM 请求超时秒数，默认 60")
    group.add_argument("--max-retries", type=int, help="网络错误重试次数，默认 3")
    group.add_argument("--memory-tokens", type=int, help="上下文 Token 预算，默认 6000")
    group.add_argument("--workspace", default=".", help="工作目录（工具沙箱根目录），默认当前目录")
    group.add_argument("--session-dir", help="会话保存目录，默认 .codeagent/sessions")
    group.add_argument("--yes", action="store_true", help="自动放行写文件/执行代码（跳过确认，谨慎使用）")
    group.add_argument("--no-stream", action="store_true", help="关闭流式输出")
    group.add_argument("--verbose", action="store_true", help="打印更多中间过程")
    group.add_argument("--no-color", action="store_true", help="禁用彩色输出")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codeagent",
        description="CodeAgent —— 一个零依赖的代码助手 Agent（支持代码审查 / 解释 / 测试 / 执行）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            示例：
              python -m codeagent demo                       # 离线体验完整 Agent 循环
              python -m codeagent chat                       # 交互式对话
              python -m codeagent ask "审查 examples/buggy_code.py"
              python -m codeagent analyze examples/buggy_code.py
              python -m codeagent web --port 8000            # 打开 Web 界面
            """
        ),
    )
    parser.add_argument("--version", action="version", version=f"CodeAgent {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    chat = subparsers.add_parser("chat", help="交互式对话（支持 /tools、/review、/save 等命令）")
    add_common_options(chat)
    chat.add_argument("--session", help="会话文件路径：启动时自动加载，退出时自动保存")
    chat.set_defaults(func=cmd_chat)

    ask = subparsers.add_parser("ask", help="单次提问后退出")
    add_common_options(ask)
    ask.add_argument("prompt", help="要交给 Agent 的任务描述")
    ask.add_argument("--json", action="store_true", help="以 JSON 输出完整结果（含工具调用轨迹）")
    ask.set_defaults(func=cmd_ask)

    demo = subparsers.add_parser("demo", help="离线演示（Mock LLM，无需 API Key）")
    add_common_options(demo)
    demo.add_argument("--scenario", choices=["review", "explain", "search", "tests", "all"], default="all")
    demo.set_defaults(func=cmd_demo)

    analyze = subparsers.add_parser("analyze", help="直接对文件做静态分析（不调用 LLM）")
    add_common_options(analyze)
    analyze.add_argument("path", help="要分析的 .py 文件")
    analyze.add_argument("--min-severity", choices=["high", "medium", "low", "info"], default="info")
    analyze.set_defaults(func=cmd_analyze)

    tools = subparsers.add_parser("tools", help="列出所有可用工具及其参数")
    add_common_options(tools)
    tools.set_defaults(func=cmd_tools)

    web = subparsers.add_parser("web", help="启动简易 Web 界面")
    add_common_options(web)
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    web.set_defaults(func=cmd_web)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    setup_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(color=False if getattr(args, "no_color", False) else None)
    try:
        return int(args.func(args, console) or 0)
    except ConfigError as exc:
        console.print(f"配置错误：{exc}", "red")
        return 2
    except AgentError as exc:
        console.print(f"运行失败：{exc}", "red")
        return 2
    except KeyboardInterrupt:
        console.print("\n已中断。", "yellow")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
