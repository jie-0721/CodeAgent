"""统一异常体系。

设计原则：所有 Agent 运行时可能出现的异常都继承自 :class:`AgentError`，
调用方（CLI / Web）只需捕获一个基类即可给出友好提示，
而内部又可以通过具体类型做差异化处理（例如网络错误才重试）。
"""

from __future__ import annotations


class AgentError(Exception):
    """所有 CodeAgent 异常的基类。"""


class ConfigError(AgentError):
    """配置缺失或非法（例如没有提供 API Key）。"""


class LLMError(AgentError):
    """LLM 调用失败。"""


class LLMConnectionError(LLMError):
    """网络层错误（连接失败 / 超时 / 5xx / 429），可重试。"""


class ToolError(AgentError):
    """工具执行失败（属于可恢复错误，会被回传给 LLM 让其自我修正）。"""


class ToolNotFound(ToolError):
    """LLM 请求了未注册的工具。"""


class ToolDenied(ToolError):
    """用户拒绝执行该工具（写操作/执行代码需要授权）。"""


class PathNotAllowed(ToolError):
    """路径越界：目标不在工作区沙箱内。"""


class MaxStepsExceeded(AgentError):
    """超过最大推理步数，通常意味着 Agent 陷入循环。"""
