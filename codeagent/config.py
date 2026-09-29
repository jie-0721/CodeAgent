"""配置加载。

优先级（从低到高）：

1. 代码内置默认值
2. ``.env`` 文件（当前目录 / 项目根目录）
3. 系统环境变量
4. 命令行参数（由 :mod:`codeagent.cli` 传入 overrides）

之所以自己做 ``.env`` 解析，是为了保持整个项目 **零第三方依赖**。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .errors import ConfigError

#: 默认使用 DeepSeek 的 OpenAI 兼容端点（也可换成通义千问 / Ollama 等）
DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: 按顺序探测 API Key 环境变量，命中第一个非空值
API_KEY_ENV_ORDER = (
    "CODEAGENT_API_KEY",
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",  # 阿里云通义千问
    "MOONSHOT_API_KEY",
    "ARK_API_KEY",
)

#: 占位符特征：.env.example 被直接复制而忘记替换真实 Key 时给出明确提示，
#: 而不是让用户面对一个莫名其妙的 HTTP 401
PLACEHOLDER_MARKERS = ("你的", "填入", "替换", "your", "xxxx", "placeholder", "changeme", "<", ">")


def is_placeholder_key(api_key: str) -> bool:
    """判断 API Key 是否仍是示例占位符（空值也算）。"""
    lowered = (api_key or "").strip().lower()
    if not lowered:
        return True
    return any(marker in lowered for marker in PLACEHOLDER_MARKERS)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def parse_env_file(path: Path) -> Dict[str, str]:
    """解析 ``KEY=VALUE`` 形式的 .env 文件（支持 # 注释与引号）。

    使用 ``utf-8-sig`` 读取：Windows 记事本或部分工具保存 .env 时会写入 BOM，
    若不做处理，第一个键名会变成 ``\\ufeffCODEAGENT_API_KEY`` 而被静默忽略——
    这是最容易被忽略的一类"配置没生效"故障。
    """
    result: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return result
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            result[key] = value
    return result


def load_env_file(path: Path, override: bool = False) -> Dict[str, str]:
    """把 .env 里的键值写入 ``os.environ``（默认不覆盖已有变量）。"""
    loaded = parse_env_file(path)
    for key, value in loaded.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def load_default_env_files() -> None:
    """依次加载项目根目录与当前工作目录下的 .env。"""
    for candidate in (PROJECT_ROOT / ".env", Path.cwd() / ".env"):
        if candidate.is_file():
            load_env_file(candidate)


def _env_str(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name)
    return default if value is None or value == "" else value


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"环境变量 {name} 需要是数字，当前为 {raw!r}") from exc


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    return int(_env_float(env, name, float(default)))


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def detect_api_key(env: Mapping[str, str]) -> str:
    """按 :data:`API_KEY_ENV_ORDER` 顺序找到第一个可用的 API Key。"""
    for name in API_KEY_ENV_ORDER:
        value = env.get(name, "").strip()
        if value:
            return value
    return ""


@dataclass
class Config:
    """Agent 运行时配置。"""

    provider: str = "openai"
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    max_tokens: int = 2048
    timeout: float = 60.0
    max_retries: int = 3
    max_steps: int = 12
    workspace: Path = Path(".")
    memory_token_budget: int = 6000
    auto_approve: bool = False
    stream: bool = True
    verbose: bool = False
    session_dir: Path = Path(".codeagent/sessions")

    # ---------------------------------------------------------------- 工厂
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None, **overrides: Any) -> "Config":
        """从环境变量构造配置，``overrides`` 中值为 ``None`` 的项会被忽略。"""
        env = dict(os.environ if env is None else env)
        config = cls(
            provider=_env_str(env, "CODEAGENT_PROVIDER", "openai"),
            base_url=_env_str(env, "CODEAGENT_BASE_URL", _env_str(env, "OPENAI_BASE_URL", DEFAULT_BASE_URL)),
            api_key=_env_str(env, "CODEAGENT_API_KEY", detect_api_key(env)),
            model=_env_str(env, "CODEAGENT_MODEL", DEFAULT_MODEL),
            temperature=_env_float(env, "CODEAGENT_TEMPERATURE", 0.2),
            max_tokens=_env_int(env, "CODEAGENT_MAX_TOKENS", 2048),
            timeout=_env_float(env, "CODEAGENT_TIMEOUT", 60.0),
            max_retries=_env_int(env, "CODEAGENT_MAX_RETRIES", 3),
            max_steps=_env_int(env, "CODEAGENT_MAX_STEPS", 12),
            workspace=Path(_env_str(env, "CODEAGENT_WORKSPACE", ".")),
            memory_token_budget=_env_int(env, "CODEAGENT_MEMORY_TOKENS", 6000),
            auto_approve=_env_bool(env, "CODEAGENT_AUTO_APPROVE", False),
            stream=_env_bool(env, "CODEAGENT_STREAM", True),
            verbose=_env_bool(env, "CODEAGENT_VERBOSE", False),
            session_dir=Path(_env_str(env, "CODEAGENT_SESSION_DIR", ".codeagent/sessions")),
        )
        return config.with_overrides(**overrides)

    def with_overrides(self, **overrides: Any) -> "Config":
        """返回应用了非空覆盖项的新配置（不修改原对象）。"""
        clean = {k: v for k, v in overrides.items() if v is not None}
        unknown = set(clean) - {f.name for f in self.__dataclass_fields__.values()}
        if unknown:
            raise ConfigError(f"未知配置项: {', '.join(sorted(unknown))}")
        return replace(self, **clean) if clean else replace(self)

    # ------------------------------------------------------------ 派生属性
    def normalized(self) -> "Config":
        """把相对路径转成绝对路径，便于工具沙箱做边界判断。"""
        workspace = Path(self.workspace).expanduser().resolve()
        session_dir = Path(self.session_dir).expanduser()
        if not session_dir.is_absolute():
            session_dir = workspace / session_dir
        return replace(self, workspace=workspace, session_dir=session_dir.resolve())

    def validate(self) -> None:
        """基础合法性校验（不检查网络）。"""
        if self.max_steps < 1:
            raise ConfigError("max_steps 至少为 1")
        if self.max_tokens < 16:
            raise ConfigError("max_tokens 太小")
        if self.timeout <= 0:
            raise ConfigError("timeout 必须为正数")
        if self.provider not in {"openai", "mock"}:
            raise ConfigError(f"不支持的 provider: {self.provider}（可选 openai / mock）")
        if self.provider == "openai" and not self.api_key:
            raise ConfigError(
                "未找到 API Key。请设置环境变量 CODEAGENT_API_KEY（或 OPENAI_API_KEY / DEEPSEEK_API_KEY），"
                "或在项目根目录创建 .env 文件（可参考 .env.example）。\n"
                "提示：若只想离线体验 Agent 循环，可加 --provider mock 参数。"
            )
        if self.provider == "openai" and is_placeholder_key(self.api_key):
            raise ConfigError(
                f"检测到 API Key 仍是示例占位符（{self.masked_key()}）。\n"
                f"请打开项目根目录的 .env 文件，把 CODEAGENT_API_KEY 替换成真实 Key；\n"
                "提示：若只想离线体验 Agent 循环，可加 --provider mock 参数（无需任何 Key）。"
            )

    def masked_key(self) -> str:
        if not self.api_key:
            return "(未设置)"
        if len(self.api_key) <= 8:
            return "*" * len(self.api_key)
        return f"{self.api_key[:4]}...{self.api_key[-4:]}"

    def describe(self) -> str:
        return (
            f"provider={self.provider} model={self.model} base_url={self.base_url} "
            f"api_key={self.masked_key()} workspace={self.workspace} "
            f"max_steps={self.max_steps} auto_approve={self.auto_approve} stream={self.stream}"
        )
