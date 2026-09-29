"""CodeAgent —— 一个零依赖的代码助手 Agent（Homework 1）。

模块划分::

    codeagent.config    配置加载（环境变量 / .env / 命令行覆盖）
    codeagent.llm       LLM 客户端（OpenAI 兼容协议 + 流式解析 + 重试）
    codeagent.mock      离线 Mock LLM（无 API Key 也能演示完整 Agent 循环）
    codeagent.memory    会话记忆（Token 预算裁剪 + 落盘）
    codeagent.prompts   系统提示词
    codeagent.agent     Agent 主循环（推理 → 工具调用 → 观察 → 输出）
    codeagent.tools     工具集（文件读写、检索、代码执行、静态分析）
    codeagent.cli       命令行入口
    codeagent.web       简易 Web 界面
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
