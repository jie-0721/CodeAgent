# CodeAgent —— 一个零依赖的代码助手 Agent

> **Homework 1 · Code Agent 提交物**
> 语言：Python 3.9+ ｜ 运行时依赖：**仅标准库** ｜ 交互方式：命令行（CLI）+ Web 界面
> 能力覆盖：代码审查 · 代码解释 · 测试生成与运行 · 代码执行 · 检索定位

CodeAgent 是一个可运行的代码助手智能体：它接收自然语言任务，自主决定「读取哪个文件、
调用哪个工具、下一步做什么」，把工具返回的真实结果作为观察继续推理，直到给出带
`文件:行号` 依据的结论。全部核心逻辑（Agent 循环、工具注册表、上下文记忆、
OpenAI 兼容客户端）都是自研的，**没有任何第三方依赖**，因此代码量可控、架构清晰、
在离线机房也能跑起来。

---

## 一、核心特性

| 特性 | 说明 |
|------|------|
| **ReAct 式 Agent 循环** | 输入 → 推理 → 工具调用 → 观察回灌 → … → 最终答案；单步可并发多个工具调用 |
| **9 个内置工具** | 文件读写、定向编辑、目录遍历、全文检索、静态分析、结构解析、代码执行、测试运行 |
| **确定性分析 + LLM 编排** | 代码审查先由 `analyze_code` 给出带行号的确定性缺陷清单，LLM 只负责判断与表达，避免"幻觉行号" |
| **上下文记忆** | Token 预算裁剪 + 工具调用配对完整性修复 + 会话落盘/恢复 |
| **错误即观察** | 工具报错不会中断循环，而是回传模型自我修正；重复调用会被检测并熔断 |
| **网络重试** | 指数退避 + 抖动 + 尊重 `Retry-After`；流式失败自动降级为非流式 |
| **写操作权限门** | 写文件 / 执行代码默认逐次确认，非交互环境默认拒绝，`--yes` 可放行 |
| **双入口同协议** | CLI 与 Web 共用同一套 Agent 事件流，行为完全一致 |
| **离线可演示** | 内置 Mock LLM，没有 API Key 也能完整体验多步工具调用（工具是真实执行的） |
| **可测试** | 118 个单元/集成测试，全部基于标准库 `unittest`，无需联网 |

---

## 二、快速开始

### 2.1 环境要求

* Python **3.9+**（开发环境 3.10.8）
* 无需 `pip install` 任何东西（`requirements.txt` 里是空的，只有说明）

```bash
git clone <你的仓库地址> && cd code-agent
python --version        # 应输出 3.9 及以上
```

### 2.2 30 秒上手：不需要 API Key

> ⚠️ **所有命令都必须在项目根目录（含 `codeagent/` 文件夹的那一层）执行。**
> 在别的目录运行 `python -m codeagent` 会报 `No module named codeagent`。
> 想在任何目录使用，见 [2.4 安装为全局命令](#24-可选安装为全局命令)。

```bash
python -m codeagent demo
```

它会用内置的离线 Mock 模型依次演示 4 个场景（代码审查 / 代码解释 / 检索 / 运行测试）。
注意：**模型推理是本地规则模拟的，但工具调用、文件读取、静态分析全部是真实执行的。**

实际输出节选：

```text
离线演示（Mock LLM，无需 API Key）| 工作区 D:\code-agent
场景 review ────────────────────────────────────────────────
用户 > 请审查 examples/buggy_code.py 的代码质量，列出问题并给出修复建议
先用静态分析拿到带行号的确定性结论：examples/buggy_code.py

  ⚙ 调用工具 analyze_code({"path": "examples/buggy_code.py"})
  ✓ 工具返回（0.02s）
  │ 静态分析: examples/buggy_code.py | 共 21 项发现 (高 4 / 中 1 / 低 5 / 提示 11)
  │ [高] L17 mutable-default: 函数 load_users 使用了可变对象作为默认参数…
  │ [高] L21 bare-except: 使用了裸 except，会吞掉 KeyboardInterrupt / SystemExit…
读取 examples/buggy_code.py 的关键片段，确认问题上下文。

  ⚙ 调用工具 read_file({"path": "examples/buggy_code.py", "max_lines": 200})
  ✓ 工具返回（0.0s）
## 代码审查报告：`examples/buggy_code.py`
### 严重（建议立即修复）
- `examples/buggy_code.py:17` **mutable-default** — 函数 load_users 使用了可变对象作为默认参数…
  - 修复建议：改为默认 None，在函数体内 `if arg is None: arg = []`
...
场景结束：2 次工具调用，用时 0.02s
```

### 2.3 接入真实 LLM（3 步）

```bash
# 1) 复制配置模板
copy .env.example .env        # Linux/macOS: cp .env.example .env

# 2) 编辑 .env，至少填这三行
#    CODEAGENT_BASE_URL=https://api.deepseek.com/v1
#    CODEAGENT_MODEL=deepseek-chat
#    CODEAGENT_API_KEY=sk-你的真实key

# 3) 开始对话
python -m codeagent chat
```

> 💡 `.env` 里的 `CODEAGENT_API_KEY` **必须换成真实 Key**。如果忘了换，程序会明确提示
> 「检测到 API Key 仍是示例占位符」，而不是抛出一个难懂的 `HTTP 401`。
>
> PowerShell 里设置环境变量是 `$env:CODEAGENT_API_KEY = "sk-..."`（仅当前窗口有效），
> **不是** `CODEAGENT_API_KEY=sk-...`（那是 bash/Linux 写法，PowerShell 会报「无法将…识别为 cmdlet」）。
> 推荐做法还是直接写进 `.env` 文件。

任何 **OpenAI 兼容**端点都能直接用，切换到其他服务商只需改 `.env` 里的三行：

| 服务商 | BASE_URL | MODEL 示例 |
|--------|----------|-----------|
| DeepSeek（默认） | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 阿里云通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5-coder:7b`（无需 Key） |

API Key 也可用环境变量提供（按顺序探测，命中第一个）：
`CODEAGENT_API_KEY` → `OPENAI_API_KEY` → `DEEPSEEK_API_KEY` → `DASHSCOPE_API_KEY` → `MOONSHOT_API_KEY` → `ARK_API_KEY`。

> 安全提示：`.env` 已在 `.gitignore` 与打包脚本的排除列表里，不会被提交或打包。

### 2.4 （可选）安装为全局命令

不想每次都 `cd` 到项目目录，可以安装成可执行命令（本项目运行时依赖为空，安装只需几秒）：

```bash
pip install -e .          # 在项目根目录执行；Windows 上若报错可试 python -m pip install -e .
codeagent demo            # 之后在任意目录都能直接用，等价于 python -m codeagent demo
codeagent chat
codeagent web --port 8000
```

也可以**完全不安装**，直接用 `run.py`（它会自动把项目根加入 `sys.path`，因此不依赖当前目录）：

```bash
python <项目路径>\run.py demo
```

卸载：`pip uninstall codeagent-homework1`。

---

## 三、使用指南

### 3.1 子命令一览

| 命令 | 作用 |
|------|------|
| `python -m codeagent chat` | 交互式对话（推荐，支持斜杠命令） |
| `python -m codeagent ask "任务"` | 单次提问后退出，适合脚本/CI；`--json` 输出完整轨迹 |
| `python -m codeagent demo` | 离线演示（Mock LLM，无需 Key） |
| `python -m codeagent analyze 文件` | **直接**调用静态分析工具，不经过 LLM |
| `python -m codeagent tools` | 列出所有工具及其参数说明 |
| `python -m codeagent web --port 8000` | 启动 Web 界面 |

也支持 `python run.py <子命令>`（等价写法，路径更友好）。

### 3.2 交互式对话

```bash
python -m codeagent chat --session .codeagent/sessions/demo.json
```

会话中的斜杠命令：

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/tools` | 列出全部工具与用途 |
| `/config` | 显示当前配置（API Key 已脱敏） |
| `/review <文件>` | 对指定文件发起**标准化代码审查**（分析 → 读原文 → 分级报告） |
| `/memory` | 查看上下文用量与消息统计 |
| `/history` | 打印当前会话的消息序列 |
| `/reset` | 清空对话历史（保留系统提示词） |
| `/save [路径]` / `/load <路径>` | 保存 / 恢复会话 |
| `/verbose` | 切换是否打印中间思考文本 |
| `/exit`（或 `/quit`、`/q`） | 退出（带 `--session` 时自动保存） |

典型提问示例：

```text
你 > 审查 examples/buggy_code.py，把严重问题按优先级列出来，并给出最小修复补丁
你 > 解释 examples/sample_math.py 里 is_prime 的实现思路
你 > 给 examples/sample_math.py 的 divide 函数补一组边界测试，写进 examples/tests/，然后跑一遍
你 > 搜索项目里所有 TODO，并按紧急程度排个序
```

### 3.3 单次提问（可脚本化）

```bash
python -m codeagent ask --provider mock "解释 examples/sample_math.py"
python -m codeagent ask "审查 examples/buggy_code.py" --json > report.json
```

`--json` 会输出包含 `answer`、`steps`（每次工具调用的名称/参数/耗时/结果）、
`stopped_reason`、`usage` 的完整轨迹，便于写报告或做自动化断言。

### 3.4 Web 界面

```bash
python -m codeagent web --port 8000 --open-browser
```

浏览器中可实时看到：思考文本流式输出、每个工具调用的参数与返回结果卡片、
结束原因与耗时统计；勾选「自动放行写操作/执行代码」后才会放开写权限（默认关闭）。

<details>
<summary>Web 界面说明</summary>

* 前端是**单文件、零 CDN** 的 `codeagent/web/static/index.html`，离线可用；
* 后端基于标准库 `http.server`，通过 SSE（`text/event-stream`）推送 Agent 事件；
* 每个浏览器会话对应一个独立的 Agent 实例（独立上下文，串行处理）。

</details>

### 3.5 工具授权（权限门）

默认情况下，**只读工具**（读文件、检索、静态分析）无需确认即可执行；
**有副作用的工具**（`write_file` / `edit_file` / `run_python` / `run_tests`）会先展示参数并询问：

```text
⚠ 工具 write_file 会修改工作目录内容，需要确认：
    { "path": "tests/test_new.py", "content": "..." }
  允许执行？[y/N]
```

* 非交互环境（管道、CI）默认**拒绝**，避免无人值守时被误改；
* `--yes` 或 `CODEAGENT_AUTO_APPROVE=true` 可跳过确认（演示、批处理时使用）。

---

## 四、内置工具清单

| 工具 | 作用 | 副作用 | 需确认 |
|------|------|:------:|:------:|
| `read_file` | 读取文本文件（带行号、支持分段读取大文件） | — | — |
| `list_files` | 按 glob 遍历文件（自动忽略 `.git`/`__pycache__` 等） | — | — |
| `search_text` | 全文正则/纯文本检索，返回 `文件:行号: 内容` | — | — |
| `analyze_code` | AST 静态分析：20 类缺陷规则，输出分级清单 | — | — |
| `list_definitions` | 解析类/函数签名与行号，快速理解代码结构 | — | — |
| `edit_file` | 精确字符串替换（默认要求唯一匹配） | 写 | ✅ |
| `write_file` | 新建或整文件覆盖（覆盖需显式 `overwrite=true`） | 写 | ✅ |
| `run_python` | 执行代码片段或 `.py` 文件，带超时与输出截断 | 执行 | ✅ |
| `run_tests` | 运行 pytest/unittest 并给出通过/失败摘要 | 执行 | ✅ |

`analyze_code` 的规则覆盖（示例）：`bare-except`、`silent-except`、`mutable-default`、
`dangerous-eval`、`shell-true`、`file-not-closed`、`none-comparison`、`type-comparison`、
`bool-comparison`、`range-len`、`shadow-builtin`、`unused-import`、`high-complexity`、
`long-function`、`long-line`、`missing-docstring`、`todo-marker`、`global-statement`、
`print-statement`、`syntax-error` 等。

---

## 五、配置项

优先级：**命令行参数 > 系统环境变量 > `.env` 文件 > 内置默认值**。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `CODEAGENT_PROVIDER` | `openai` | `openai`（兼容接口）或 `mock`（离线规则模型） |
| `CODEAGENT_BASE_URL` | `https://api.deepseek.com/v1` | 兼容接口地址 |
| `CODEAGENT_MODEL` | `deepseek-chat` | 模型名 |
| `CODEAGENT_API_KEY` | 空 | API Key（也可用其他常见变量名） |
| `CODEAGENT_TEMPERATURE` | `0.2` | 采样温度 |
| `CODEAGENT_MAX_TOKENS` | `2048` | 单次回复上限 |
| `CODEAGENT_TIMEOUT` | `60` | 单次请求超时（秒） |
| `CODEAGENT_MAX_RETRIES` | `3` | 网络错误重试次数 |
| `CODEAGENT_MAX_STEPS` | `12` | Agent 最大推理步数 |
| `CODEAGENT_MEMORY_TOKENS` | `6000` | 上下文 Token 预算 |
| `CODEAGENT_AUTO_APPROVE` | `false` | 是否自动放行写操作/执行代码 |
| `CODEAGENT_STREAM` | `true` | 是否流式输出 |
| `CODEAGENT_WORKSPACE` | `.` | 工具沙箱根目录 |
| `CODEAGENT_SESSION_DIR` | `.codeagent/sessions` | 会话保存目录 |

---

## 六、项目结构

```text
code-agent/
├── codeagent/                 # 主包
│   ├── agent.py               # ★ Agent 主循环（推理→工具→观察→输出）
│   ├── llm.py                 # ★ OpenAI 兼容客户端 + SSE 解析 + 重试
│   ├── memory.py              # 上下文记忆（裁剪 / 配对修复 / 落盘）
│   ├── mock.py                # 离线 Mock LLM（无 Key 演示 + 确定性测试）
│   ├── prompts.py             # 系统提示词设计
│   ├── config.py              # 配置加载（.env / 环境变量 / CLI）
│   ├── errors.py              # 统一异常体系
│   ├── cli.py                 # 命令行入口（chat/ask/demo/analyze/tools/web）
│   ├── tools/                 # ★ 工具层
│   │   ├── base.py            #   沙箱 Workspace + Tool 契约 + ToolRegistry
│   │   ├── fs.py              #   文件读写 / 检索
│   │   ├── analysis.py        #   AST 静态分析（20 类规则）
│   │   └── exec.py            #   代码执行 / 测试运行
│   └── web/                   # 简易 Web 界面（标准库 http.server + SSE）
├── examples/                  # 演示素材（故意有缺陷的代码 / 示例模块与测试）
├── tests/                     # 118 个单元与集成测试
├── scripts/
│   ├── package.py             # 打包成「学号姓名.zip」
│   └── selfcheck.py           # 用本项目自己的分析工具审查本项目（dogfooding）
├── README.md                  # 本文件：项目说明与使用指南
├── Design.md                  # 设计文档：架构、循环、工具契约、取舍
├── .env.example               # 配置模板
├── pyproject.toml             # 可选：pip install -e . 后获得全局 codeagent 命令
└── requirements.txt           # 依赖说明（本项目零依赖）
```

---

## 七、测试

```bash
python -m unittest discover -s tests -v      # 推荐：标准库，零依赖
# 或用 pytest（可选）：pytest tests -q
```

当前结果：**144 个用例全部通过**（约 1.8 秒，无需联网）。

| 测试文件 | 覆盖重点 |
|----------|----------|
| `tests/test_llm.py` | SSE 流式解析、分片工具参数拼接、消息序列化、请求体构造 |
| `tests/test_config.py` | `.env` 解析、环境变量优先级、校验、路径归一化 |
| `tests/test_memory.py` | Token 估算、预算裁剪、**裁剪后不产生孤立 tool 消息**、落盘恢复 |
| `tests/test_tools.py` | 沙箱越界拒绝、读写/编辑/检索、静态分析规则、代码执行与超时 |
| `tests/test_agent.py` | 多步工具调用、工具报错自愈、权限门、步数上限强制收敛、循环熔断、事件流 |
| `tests/test_mock.py` | 意图识别、工具计划、答复渲染、流式增量一致性 |
| `tests/test_cli.py` | 斜杠命令派发、参数透传、端到端 `ask`/`analyze`/`tools`/`demo` |
| `tests/test_package.py` | 密钥泄露门禁：真实 Key 精确比对、`.env` 改名变体、gitignore 规则完整性 |
