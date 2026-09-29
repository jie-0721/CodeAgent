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

当前结果：**118 个用例全部通过**（约 1.6 秒，无需联网）。

| 测试文件 | 覆盖重点 |
|----------|----------|
| `tests/test_llm.py` | SSE 流式解析、分片工具参数拼接、消息序列化、请求体构造 |
| `tests/test_config.py` | `.env` 解析、环境变量优先级、校验、路径归一化 |
| `tests/test_memory.py` | Token 估算、预算裁剪、**裁剪后不产生孤立 tool 消息**、落盘恢复 |
| `tests/test_tools.py` | 沙箱越界拒绝、读写/编辑/检索、静态分析规则、代码执行与超时 |
| `tests/test_agent.py` | 多步工具调用、工具报错自愈、权限门、步数上限强制收敛、循环熔断、事件流 |
| `tests/test_mock.py` | 意图识别、工具计划、答复渲染、流式增量一致性 |
| `tests/test_cli.py` | 斜杠命令派发、参数透传、端到端 `ask`/`analyze`/`tools`/`demo` |
| `tests/test_cli.py` | 斜杠命令派发、端到端 `ask`/`analyze`/`tools`/`demo` |

---

## 八、自检：用自己审查自己（dogfooding）

```bash
python scripts/selfcheck.py          # 只报 high/medium，存在 high 则退出码非 0
python scripts/selfcheck.py --all --rules unused-import,long-line
```

该脚本用本项目的 `analyze_code` 工具扫描自身 30 个源文件，作为提交前的质量门禁。
开发过程中它确实抓到了真实问题（重复的 `except Exception: pass`、未使用导入、
超长行、以及两处分析器自身的误报），修复后当前结果为 **0 个 high 级别问题**。

---

## 九、提交与打包

```bash
python scripts/package.py --name 10086张三
# → submission/10086张三.zip

python scripts/package.py --scan-only      # 只做密钥泄露检查，不打包
```

脚本会自动排除 `__pycache__`、`.git`、虚拟环境、**`.env`（含密钥）**与打包产物，
并检查是否缺少 `README.md` / `Design.md`、是否超过 200MB 上限。

### 9.1 密钥安全（务必先读）

**真实 API Key 只会待在 `.env` 里，而 `.env` 永远不会被打包，也不会被 git 跟踪。**

| 泄露路径 | 防护措施 |
|----------|----------|
| 压缩包 | `scripts/package.py` 的 `EXCLUDE_FILES` 直接排除 `.env` / `.env.local` |
| Git 仓库 | `.gitignore` 中已包含 `.env`、`.env.local` |
| 误写进源码/文档 | **打包前自动门禁**：扫描所有待打包文件，一旦发现 `.env` 里的真实 Key 出现即中止打包并指出文件与行号 |
| 形态可疑的 Key | 同一门禁还会按形态（`sk-` + 长串等）给出警告，可人工确认 |

打包输出示例：

```text
打包前检查：
  ✓ 已排除 .env，并对 40 个待打包文件做了密钥比对（1 个密钥值）
  ✓ 密钥泄露检查通过：待打包文件中没有出现 .env 里的任何密钥
打包完成：submission/10086张三.zip
```

如果检查未通过，会打印形如 `codeagent/xxx.py:12 出现了 .env 中 CODEAGENT_API_KEY 的真实值`
并**拒绝打包**（退出码 1）。请把该处改为从环境变量读取：

```python
import os
api_key = os.environ.get("CODEAGENT_API_KEY", "")
```

**万一您已经把 Key 提交进 git 过**（例如先 `git add` 了 `.env`），仅删除文件是不够的——历史里仍然存在：

```bash
git rm --cached .env            # 停止跟踪，但保留本地文件
git commit -m "chore: 停止跟踪 .env"
git log --all -- .env           # 确认历史中是否还有记录
```

只要 Key 进过任何一次提交，就应当**立刻到服务商控制台吊销并重新生成**（例如 DeepSeek 的 API Keys 页面）——
旧 Key 作废后，即使历史里还能翻到，也已经无法使用。

> 给老师/助教复现时，不需要您的 Key：直接 `python -m codeagent demo`（Mock 模式）
> 或使用本地 Ollama（见 2.3），都能完整演示 Agent 循环与工具调用。

### 9.2 上传到 GitHub / Gitee

**关键认知：`git add .` 不等于「把目录复制上去」。** 它会自动跳过 `.gitignore` 中的文件，
所以 `.env` 连暂存区都进不去。实测（还包含各种改名变体）：

```text
$ git add .                        # 就是「把整个目录放上去」
$ git diff --cached --name-only
.env.example                       ← 只有模板进仓库
.gitignore
main.py

$ git check-ignore -v .env
.gitignore:18:.env              .env
$ git check-ignore -v .env.production
.gitignore:19:.env.*            .env.production
```

完整流程：

```bash
cd <项目根目录>
git init
git config user.name  "学号姓名"        # 每台机器配一次即可
git config user.email "你的邮箱"

git add .
git status --short                     # ★ 上传前自查：不应出现任何 .env*（.env.example 除外）
git commit -m "Homework 1: CodeAgent"

git branch -M main
git remote add origin https://github.com/<用户名>/<仓库名>.git
git push -u origin main
```

**在 GitHub 网页「New repository」页面怎么选：**

| 字段 | 选什么 | 说明 |
|---|---|---|
| Repository name | `code-agent-homework1` | 用英文/连字符（会进 URL），别用中文和空格 |
| Description | 可选 | 如 `Homework 1 · 零依赖代码助手 Agent（Python）` |
| **Choose visibility** | **Public** | 交链接给老师最省事；想先私密就选 Private，之后可在 `Settings` 底部改为 Public |
| **Add README** | **留空（不要开）** | 本地已有 `README.md` |
| **Add .gitignore** | **保持 `No .gitignore`** | 本地已有加固过的 `.gitignore` |
| Add license | `No license` | 课程作业足够；本仓库未声明开源许可 |

> 为什么 README / .gitignore 要留空：勾选后 GitHub 会先给**远端**造一个初始提交，
> 它与您本地的同名文件无关，于是第一次 `git push` 会被拒绝
> （`! [rejected] ... (fetch first)`），还得手动 `git pull --rebase` 解冲突。
> 留空时远端是空仓库，一次推送即可成功。
>
> 首次推送的凭据：GitHub 早已不接受账号密码。可选 ① Git for Windows 自带的
> Credential Manager（弹浏览器登录后自动记住）；② 生成 Personal Access Token
> （`Settings → Developer settings → Personal access tokens → Tokens (classic)`，勾 `repo`）
> 当密码粘贴。**Token 本身也是密钥**，不要写进文件或塞进 remote URL
> （`https://user:token@github.com/...` 会被记进 `.git/config`）。

**推送前自查三条**（复制即用）：

```bash
git status --short                # ① 暂存区里没有 .env
git check-ignore -v .env          # ② 有输出，且指向 .gitignore 规则
git log --all -- .env             # ③ 无输出 = 历史中从未提交过
```

⚠️ **唯一真正危险的做法：网页端拖拽上传。** 在 GitHub 网页上把整个文件夹拖进去时，
上传的是您选中的文件本身，不再经过本地 git 的 `.gitignore` 过滤——而前面已验证过，
**Windows 上 `.env` 不是隐藏文件**，拖拽时很容易被一起选中。
所以请用 `git push`。若只能用网页上传：先把 `.env` 及所有 `.env.*` 变体**移到项目目录外**，
上传完再移回来（`.env.example` 可以留着）。

> 万一已经推上去了：立刻去服务商控制台吊销该 Key。删文件再提交**不能**从 git 历史里抹掉它。

**提交物清单**

| 作业要求 | 对应产物 |
|----------|----------|
| ① 源代码仓库 | 本目录全部代码（可直接推到 GitHub/Gitee） |
| ② README.md | 本文件：项目说明、使用方法、配置、测试 |
| ③ Design.md | 设计文档：架构图、循环时序、工具契约、安全模型、取舍 |
| ④ 演示视频（可选） | 见下方 1 分钟脚本建议 |
| ⑤ 备份压缩包 | `python scripts/package.py --name 学号姓名` 生成 |

<details>
<summary>1 分钟演示视频脚本建议</summary>

1. （0:00-0:10）`python -m codeagent demo` 展示离线多步工具调用与审查报告；
2. （0:10-0:25）`python -m codeagent analyze examples/buggy_code.py` 展示确定性缺陷清单；
3. （0:25-0:45）`python -m codeagent chat` 里用真实模型提一个需求，展示 `/review`、
   权限确认、`edit_file` 改代码、`run_tests` 回归验证的闭环；
4. （0:45-0:60）`python -m codeagent web` 展示 Web 界面的流式输出与工具卡片。

</details>

---

## 十、常见问题

**Q0：报 `No module named codeagent`？**
说明当前目录不是项目根目录。三种解法任选其一：
① `cd` 到包含 `codeagent/` 文件夹的项目根目录再运行；
② 用 `python <项目路径>\run.py demo`（`run.py` 会自动处理导入路径，不依赖当前目录）；
③ 执行 `pip install -e .` 安装后直接用 `codeagent demo`（见 2.4）。

**Q1：没有 API Key 能用吗？**
能。`demo` 与 `--provider mock` 走离线规则模型，完整跑通 Agent 循环与工具调用；
也可以用本地 Ollama（见 2.3 表格），完全离线且免费。

**Q2：报 `400 Bad Request` 或 `tool_calls` 相关错误？**
模型必须支持 Function Calling。若服务端不支持流式，客户端会自动降级为非流式；
若仍失败，请确认 `.env` 的 `BASE_URL`/`MODEL` 与服务商文档一致，或加 `--no-stream`。

**Q2.5：报 `HTTP 401 Unauthorized`？**
Key 无效或仍是占位符。本项目会在 Key 未填/仍是 `sk-你的key` 这类占位符时提前拦截并提示，
若已填了真实 Key 仍 401，请检查 Key 是否过期、是否与 `CODEAGENT_BASE_URL` 对应的服务商匹配。

**Q3：中文在 Windows 控制台显示乱码？**
CLI 启动时会把标准输出切换为 UTF-8。若仍异常，可先执行 `chcp 65001`，
或设置环境变量 `PYTHONUTF8=1`。

**Q4：为什么写文件/执行代码要确认？**
Agent 能修改你的工作目录，默认逐次确认是安全底线；非交互场景默认拒绝，
需要放行时显式加 `--yes`。

**Q5：端口被占用怎么办？**
`python -m codeagent web --port 8010` 换端口即可。

**Q6：会不会读到工作目录之外的文件？**
不会。所有文件操作都经过沙箱校验，`../` 或绝对路径越界会被直接拒绝并给出提示。

**Q7：为什么要自己写而不用 LangChain？**
作业允许自由选择。自研循环让 Agent 架构（循环、工具契约、记忆、权限、错误处理）
完全可见可讲，且零依赖、易审阅；代价是需要自己处理流式解析与重试，
这部分在 `codeagent/llm.py` 中集中实现。

---

## 十一、作业要求对照

| 作业要求 | 实现位置 |
|----------|----------|
| 基本的 Agent 循环（输入→推理→工具调用→输出） | `codeagent/agent.py` `Agent.run` |
| 支持至少一种工具（代码执行、文件读取、搜索等） | 9 个工具，见 `codeagent/tools/` |
| 可通过命令行或简单 Web 界面交互 | `codeagent/cli.py` + `codeagent/web/`（两者都有） |
| 使用主流框架或 LLM 原生 API | LLM 原生 API（OpenAI 兼容，标准库 `urllib` 实现） |
| 支持上下文记忆 | `codeagent/memory.py`（预算裁剪 + 落盘） |
| 错误处理与重试机制 | `errors.py` + `llm.py` 指数退避重试 + 工具错误回灌自愈 |
| README / Design 文档 | `README.md` / `Design.md` |

---

## 十二、许可

本项目仅用于课程作业与学习交流，可自由修改使用。
