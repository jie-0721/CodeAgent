# CodeAgent 技术设计文档

> 课程作业：Homework 1 · Code Agent
> 项目定位：一个**零第三方依赖**的本地代码助手 Agent，覆盖代码审查、代码解释、测试运行与代码执行。
> 运行环境：Python ≥ 3.9（开发环境 3.10.8），Windows / Linux / macOS 均可直接运行。

---

## 1. 需求映射

作业要求的每一条都能在代码中定位，并有对应用例。

| 作业要求 | 实现位置 | 说明 |
|---|---|---|
| 基本 Agent 循环：输入 → 推理 → 工具调用 → 输出 | `codeagent/agent.py:205` `Agent.run()` | ReAct 风格循环，最多 `max_steps` 轮（默认 12） |
| 支持至少一种工具 | `codeagent/tools/` 共 **9 个**工具 | 文件读写、检索、静态分析、代码执行、测试运行 |
| 可通过命令行或简单 Web 界面交互 | `codeagent/cli.py`、`codeagent/web/server.py` | CLI 6 个子命令 + SSE 流式 Web 界面，共用同一事件协议 |
| 推荐：使用 LLM 原生 API | `codeagent/llm.py:242` `OpenAICompatibleLLM` | 用标准库 `urllib` 直连 OpenAI Chat Completions 协议 |
| 推荐：支持上下文记忆 | `codeagent/memory.py:45` `Memory` | token 预算裁剪 + 工具调用配对修复 + 会话落盘 |
| 推荐：错误处理与重试机制 | `codeagent/errors.py`、`llm.py:312` `_with_retry` | 分层异常体系 + 指数退避重试 + 工具错误回灌自愈 |
| 技术栈自由选择 | Python 3.9+ 标准库 | `requirements.txt` 中**没有**任何必装依赖 |
| 评审：功能完整性 | 9 个工具 + 边界处理 | 越界路径、二进制/超大文件、超时、非法正则均有处理 |
| 评审：Agent 架构 | 分层解耦 + 事件协议 + 权限门 | 见第 2、3、7 节 |
| 评审：代码质量 | 类型注解、模块化、docstring | 单文件职责单一，规模最大的 `tools/analysis.py` 为纯规则集合 |
| 评审：文档 | `README.md` / `Design.md` | 本文档说明设计与取舍 |

**模块规模**（行数为实际统计）：

| 模块 | 行数 | 职责 |
|---|---|---|
| `codeagent/tools/analysis.py` | 615 | 静态分析规则引擎（AST + 正则） |
| `codeagent/cli.py` | 599 | 命令行入口、事件渲染、权限交互、斜杠命令表 |
| `codeagent/llm.py` | 394 | OpenAI 兼容客户端、SSE 解析、重试 |
| `codeagent/mock.py` | 387 | 离线规则模型（演示 + 测试驱动源） |
| `codeagent/agent.py` | 361 | Agent 主循环 |
| `codeagent/tools/base.py` | 313 | 沙箱、工具契约、注册表 |
| `codeagent/tools/fs.py` | 282 | 文件系统工具（5 个） |
| `codeagent/web/server.py` | 255 | Web 服务与 SSE 推送 |
| `codeagent/tools/exec.py` | 249 | 代码执行工具（2 个） |
| `codeagent/config.py` | 203 | 配置加载与校验 |
| `codeagent/memory.py` | 188 | 上下文记忆 |
| `codeagent/prompts.py` | 59 | 系统提示词与审查模板 |
| `codeagent/errors.py` | 44 | 异常体系 |

---

## 2. 总体架构

```
┌──────────────────────────── 交互层 ────────────────────────────┐
│  CLI (cli.py)                     Web (web/server.py)          │
│  chat / ask / demo / analyze      GET  /           单页界面     │
│  tools / web 子命令               GET  /api/tools  /api/info    │
│  ConsoleRenderer 渲染事件         POST /api/chat   SSE 推送     │
└───────────────────────────┬────────────────────────────────────┘
                            │  统一的 on_event(dict) 事件协议
┌───────────────────────────▼────────────────────────────────────┐
│                      Agent 主循环 (agent.py)                    │
│  memory.build() → llm.generate() → 有 tool_calls?               │
│       ↑                                   │                     │
│       │ 观察回灌                   权限门 (approver)             │
│       │                                   ▼                     │
│   Memory (memory.py)              ToolRegistry (tools/)         │
│   · token 预算裁剪                · Tool / ToolResult 契约      │
│   · 工具配对修复                  · JSON Schema 下发            │
│   · 会话落盘                      · Workspace 路径沙箱          │
└───────────┬───────────────────────────────┬────────────────────┘
            ▼                               ▼
┌───────────────────────┐      ┌──────────────────────────────────┐
│  LLM 客户端 (llm.py)  │      │  9 个工具 (fs / analysis / exec) │
│  · OpenAI 兼容协议    │      │  read_file  write_file  edit_file│
│  · SSE 流式解析       │      │  list_files search_text          │
│  · 指数退避重试       │      │  analyze_code  list_definitions  │
│  · 流式失败降级       │      │  run_python  run_tests           │
│  BaseLLM ◄── MockLLM  │      └──────────────────────────────────┘
└───────────────────────┘
```

**为什么零第三方依赖。** LLM 调用只用 `urllib.request`，Web 服务只用 `http.server`，测试只用 `unittest`，静态分析只用标准库 `ast`。收益有三：其一，评审者克隆仓库后无需 `pip install` 即可复现全部功能，`python -m codeagent demo` 甚至不需要 API Key；其二，协议细节（SSE 分片拼接、工具参数增量、重试退避）显式可见，不被框架封装掩盖；其三，规避依赖版本冲突这一最常见的"跑不起来"原因。

**为什么自研轻量循环而不是 LangChain。** 本作业的核心考查点是"是否体现 Agent 设计模式"（架构占 30%）。框架会把 ReAct 循环、工具抽象、记忆管理变成配置项，反而难以展示对循环控制、错误恢复、上下文预算的真实理解。自研的代价是代码量更大，换来两个可验证的好处：主循环集中在 `agent.py:205-285`，每步可控可测；`BaseLLM` 抽象只有 `generate()` 一个方法（`llm.py:227-239`），因此 `MockLLM` 可无缝替换真实模型，使主循环测试完全脱离网络。

**交互层的斜杠命令派发（表驱动）。** CLI 的 REPL 最初用一条长 if/elif 链处理 `/help`、`/tools`、`/reset` 等斜杠命令，该函数膨胀到约 80 行、圈复杂度 29。现已重构为字典派发：`_build_slash_commands()`（`cli.py:255`）把每个命令注册成独立小函数，并返回「命令名 → 处理函数」映射（`cli.py:327-341`，同时支持 `/exit`、`/quit`、`/q` 三个别名指向同一处理函数）；`_handle_slash_command()`（`cli.py:344`）只负责解析 `"/命令 参数"`、查表、调用，并以返回值表示是否退出 REPL（`cli.py:346-351`）。这个改动带来三点收益：每个处理函数职责单一、圈复杂度都落在低位；新增命令只需在字典里加一行；命令表本身可直接作为参数传入测试，因此 `tests/test_cli.py` 能对 `/reset`、`/save`、未知命令等分支逐个断言，而不必驱动整个交互循环。

---

## 3. Agent 主循环

### 3.1 时序

```
用户输入
   │
   ├─► Memory.add_user(输入)                        agent.py:207
   │
   └─► for step in 1..max_steps:                    agent.py:216
         │
         ├─► emit(step_start)
         ├─► messages = Memory.build()              memory.py:76
         │      └─ system + token 预算内的历史（含配对修复）
         ├─► msg = LLM.generate(messages, tools=tools.schemas())
         │      └─ 流式：每个文本增量 → emit(assistant_delta)
         │
         ├── 没有 tool_calls ──► Memory.add_assistant(msg.content)
         │                       emit(final) → 返回 AgentResult   agent.py:221
         │
         └── 有 tool_calls:
               ├─ emit(assistant_message)（模型中间思考）
               ├─ Memory.add_assistant(content, tool_calls)       agent.py:230
               └─ for call in tool_calls:                         agent.py:232
                     ├─ emit(tool_call)
                     ├─ 重复调用计数（熔断用）                       agent.py:234
                     ├─ 权限门：requires_approval 且未 auto_approve
                     │     └─ 拒绝 → ToolResult.failure(...)        agent.py:168
                     ├─ registry.run(name, args) → ToolResult
                     ├─ emit(tool_result)
                     └─ Memory.add_tool_result(...) 观察回灌         agent.py:244
   （循环耗尽）→ emit(warning) → 强制收敛 _force_finalize()          agent.py:268
```

### 3.2 关键设计

**ReAct 思想。** 每一轮"推理"产出的 `tool_calls` 即 Act，工具返回值作为 Observation 追加进上下文，下一轮推理即可见，形成 Thought → Action → Observation 闭环。与"一次性生成答案"的本质区别是：模型可以读到真实文件内容后再下判断，从而避免幻觉行号与臆测代码。

**一次可并行多个工具。** `message.tool_calls` 是列表（`agent.py:232`），一轮内请求多个工具时按序执行并逐个回灌；每个 `tool_call_id` 都被完整保留（`agent.py:244`），配对关系不会错乱。`tests/test_agent.py:123` 覆盖"一次请求两个工具"。

**最大步数。** `max_steps` 默认 12（`config.py:125`），可用 `--max-steps` 或 `CODEAGENT_MAX_STEPS` 调整，防止模型无限调用工具耗尽预算。

**强制收敛。** 步数耗尽不直接报错，而是向上下文追加系统指令（`agent.py:310-315`），说明"工具阶段已结束，请基于已有信息直接给结论"，然后**以 `tools=None` 再调用一次模型**（`agent.py:317`）。请求中不再包含 `tools` 字段，模型在协议层面无法再发起调用，只能输出文本——这保证"即使没收敛，用户也一定拿到答复"。`tests/test_agent.py:225` 验证此时 `stopped_reason == "max_steps"` 且答复非空。

**循环熔断。** 以 `(工具名, 规范化参数 JSON)` 为指纹计数（`agent.py:234`，参数 `sort_keys=True` 保证顺序无关）：

| 触发次数 | 动作 | 代码位置 |
|---|---|---|
| ≥ 3（`REPEAT_LIMIT`） | 发 `warning` 事件，并注入 user 消息提醒"相同参数重复调用结果不会改变" | `agent.py:246-254` |
| ≥ 5（`REPEAT_LIMIT + 2`） | 立即强制收敛，`stopped_reason = "loop_detected"` 并返回 | `agent.py:255-267` |

`tests/test_agent.py:238` 用"永远返回同一次 `read_file` 调用"的脚本模型验证熔断路径；`tests/test_agent.py:91-164` 覆盖正常路径。

**结果对象。** `AgentResult`（`agent.py:74-98`）携带 `answer / steps / stopped_reason / elapsed / usage / error`，其中每步记录工具名、参数、是否成功、观察文本、耗时与当时的思考文本，可直接序列化为 JSON 作为运行报告（`ask --json` 与 Web 的 `done` 事件均使用）。

**事件回调的容错。** 展示层回调抛出的异常不会中断循环，而是记入 debug 日志（`agent.py:126-132`），保证 UI 故障不影响 Agent 运行。

---

## 4. 工具契约与工具设计

### 4.1 三件套

| 组件 | 位置 | 职责 |
|---|---|---|
| `Tool` | `tools/base.py:179` | 声明 `name / description / parameters(JSON Schema) / handler / mutating / requires_approval` |
| `ToolResult` | `tools/base.py:58` | 统一返回 `ok / content / meta`，`content` 原样成为 observation |
| `ToolRegistry` | `tools/base.py:258` | 注册、查找、批量下发 schema、统一执行入口 |

**JSON Schema 下发。** `Tool.schema()`（`tools/base.py:189`）直接产出 OpenAI function calling 结构，`ToolRegistry.schemas()`（`tools/base.py:291`）汇总后由主循环交给 `LLM.generate(tools=...)`，模型因此只被告知它真正能传的参数。

**参数校验与类型纠正。** `Tool.validate()`（`tools/base.py:201-232`）做三件事：拒绝未知参数并列出可用参数名；检查必填项缺失并回带参数说明；对模型常见的"把数字写成字符串"做轻量纠正（`integer` / `number` / `boolean`）。**错误信息是写给模型看的**——例如"缺少必填参数 ['path']；参数说明: {...}"，模型读到后能在下一轮自行修正。

**错误即观察。** `Tool.invoke()`（`tools/base.py:234-252`）把 `ToolError`、`TypeError` 与任何未预期异常统一转换为 `ToolResult.failure(...)`，绝不向上抛出。主循环把它包成 `[工具名 执行失败] ... 请根据上述错误调整参数或改用其他方式。`（`agent.py:202`）回灌，等待模型自愈。`tests/test_agent.py:138` 验证"读取不存在的文件 → 失败回灌 → 换策略 → 最终收敛"整条链路。

### 4.2 九个工具

| 工具 | 位置 | 用途 | mutating | 需确认 | 输出格式 |
|---|---|---|---|---|---|
| `read_file` | `tools/fs.py:27` | 带行号读取文本，支持 `start_line` / `max_lines` 分段 | 否 | 否 | 文件名 + 总行数 + `行号\| 内容` |
| `write_file` | `tools/fs.py:49` | 新建或整文件覆盖（已存在必须显式 `overwrite=true`） | 是 | 是 | 动作 + 字符数/行数 |
| `edit_file` | `tools/fs.py:69` | 精确字符串替换，默认要求唯一匹配 | 是 | 是 | 替换处数 |
| `list_files` | `tools/fs.py:94` | glob 列文件并显示大小 | 否 | 否 | 相对路径 + 字节数 |
| `search_text` | `tools/fs.py:122` | 全文正则检索 | 否 | 否 | `文件:行号: 内容` |
| `analyze_code` | `tools/analysis.py:526` | 静态分析，输出分级缺陷清单 | 否 | 否 | `[级别] L行号 规则: 描述` + 建议 |
| `list_definitions` | `tools/analysis.py:543` | 列顶层导入、类、方法/函数签名及行号 | 否 | 否 | `L行号 def 名字(参数)` |
| `run_python` | `tools/exec.py:108` | 执行内联代码或脚本文件（`code` / `path` 二选一） | 是 | 是 | 退出码 + 耗时 + stdout/stderr |
| `run_tests` | `tools/exec.py:152` | 跑 pytest 或 unittest 并给摘要 | 是 | 是 | 退出码 + 完整输出 + 关键行摘要 |

只读与写操作的分野决定权限门：4 个只读工具与 2 个分析工具直接执行；`write_file` / `edit_file` 标记于 `tools/fs.py:227`、`tools/fs.py:247`，`run_python` / `run_tests` 标记于 `tools/exec.py:196`、`tools/exec.py:219`，均为 `mutating=True, requires_approval=True`。

**`edit_file` 优先于 `write_file`。** 系统提示词明确要求局部修改用 `edit_file`（`prompts.py:26`）。`edit_file` 在匹配数为 0 或 >1 时都报错（`tools/fs.py:75-84`），从机制上防止模型"改错地方"；只有新建文件或整文件重写才允许 `write_file`。

**`analyze_code` 的规则清单。** 用 `ast.NodeVisitor`（`_Reviewer`，`tools/analysis.py:100`）遍历语法树，配合正则扫描补充 AST 看不到的信息（未使用导入、TODO、超长行）。共 20 条规则：

| 级别 | 规则名 | 判定逻辑 |
|---|---|---|
| 高 | `syntax-error` | `ast.parse` 抛 `SyntaxError`，直接返回该单条发现（`analysis.py:427-438`） |
| 高 | `bare-except` | `ExceptHandler.type is None` |
| 高 | `silent-except` | `except Exception: pass`（高）；`except ...: return None`（中） |
| 高 | `mutable-default` | 默认参数是 `List/Dict/Set/ListComp/DictComp` |
| 高 | `dangerous-eval` | 调用内建 `eval` / `exec` |
| 高 | `shell-true` | `subprocess.*` / `os.*` 传入 `shell=True` |
| 中 | `file-not-closed` | 内建 `open()` 不在 `with` 上下文中 |
| 中 | `type-comparison` | `type(x) == T` |
| 中 | `long-function` | 函数自身行数超过 60（`LONG_FUNCTION_LINES`） |
| 中 | `high-complexity` | 圈复杂度 > 12（`COMPLEXITY_THRESHOLD`） |
| 低 | `none-comparison` | `== None` / `!= None` |
| 低 | `bool-comparison` | 与 `True`/`False` 显式比较 |
| 低 | `range-len` | `for i in range(len(seq))` |
| 低 | `shadow-builtin` | 参数或赋值目标命中内建名集合 `SHADOWABLE`（`analysis.py:29-38`） |
| 低 | `global-statement` | 使用 `global` |
| 低 | `unused-import` | 导入名在剔除 import 行后的正文中不再出现；`import *` 时整文件跳过 |
| 提示 | `missing-docstring` | 公开函数/类无 docstring |
| 提示 | `print-statement` | 非 `if __name__ == "__main__"` 上下文中的内建 `print` |
| 提示 | `long-line` | 非注释行超过 120 字符（`LONG_LINE_CHARS`） |
| 提示 | `todo-marker` | 匹配 `TODO/FIXME/XXX/HACK` |

规则按严重级别排序输出（`SEVERITY_ORDER`，`analysis.py:22`），并附带函数清单与复杂度（`format_findings`，`analysis.py:497`）；`min_severity` 参数可按级别过滤，避免把十余条"提示"一股脑塞给模型。

三处精度处理值得说明，它们直接对应静态分析工具最常见的误报来源：

1. **圈复杂度与函数长度都排除嵌套定义。** `_complexity()`（`analysis.py:64-83`）遇到内部 `FunctionDef/ClassDef/Lambda` 即停止下钻，`_own_lines()`（`analysis.py:86-97`）从函数总行数中扣除嵌套定义的行数。否则"工厂函数里定义多个 handler"会被严重高估。
2. **只在确定是内建调用时才判定。** `visit_Call()` 先看 `func` 是否为 `ast.Name`（`analysis.py:234`），因此 `obj.print()`、`obj.eval()` 不会被误报为危险调用。
3. **`shell=True` 要求接收者是 `subprocess` 或 `os`**（`analysis.py:257`），避免把某个对象的 `run(shell=True)` 参数误判为命令注入。

**这套规则的意义在于把"审查"变成可复现的确定性计算**：LLM 负责解释优先级与给出修复方案，行号与事实来自 `ast`，不会编造。

---

## 5. 上下文记忆策略

**token 估算。** 不引入 tokenizer，按字符类型加权（`memory.py:28-34`）：

```
tokens ≈ ascii_chars / 4 + wide_chars / 1.5 + 1
```

ASCII 取常见的 4 字符/token；中日韩字符按 1.5 字符/token 估算（中文通常 1 字 ≈ 1~2 token，取偏保守值）。每条消息再叠加 `MESSAGE_OVERHEAD_TOKENS = 6`，每个工具调用叠加名称与参数的估算值加 4（`memory.py:37-41`）。

**预算裁剪。** `Memory.build()`（`memory.py:76-94`）先为模型输出预留 `RESERVED_OUTPUT_TOKENS = 1024`，再用**从尾部向前累加**的方式保留最近的完整消息，直到接近预算。语义是"最近发生的事最重要"，同时保证最后一条消息（刚回灌的工具结果或用户的新问题）一定在上下文里。

**工具调用配对修复（关键）。** OpenAI 兼容接口要求 `role="tool"` 的消息紧跟在发起该调用的 `assistant` 消息之后，且 `tool_call_id` 必须能对应上。裁剪是从头部丢弃消息，一旦切点落在 `assistant(tool_calls)` 与 `tool` 之间，保留段就会以孤立的 `tool` 消息开头，服务端直接返回 400。因此 `_repair_pairing()`（`memory.py:96-102`）在裁剪后丢弃开头的连续 `tool` 消息，恢复协议合法性。`tests/test_memory.py:44` 专门构造该边界并断言首条非 system 消息不是 `tool`。

**省略提示。** 发生裁剪时，system 提示词末尾追加"已省略较早的 N 条历史消息；如需回顾请用 read_file 重新读取文件"（`memory.py:104-111`）。这既告知模型上下文不完整，也给出补偿手段——工具让模型可随时取回事实，记忆裁剪因此不会造成信息永久丢失。

**会话落盘。** `Memory.save()` / `load()`（`memory.py:147-162`）把消息序列（含工具调用与 `tool_call_id`）序列化为 JSON，默认目录 `.codeagent/sessions`。CLI 用 `--session path` 在启动时加载、退出时保存（`cli.py:360-365`、`cli.py:387-392`）；Web 端每个浏览器会话在服务进程内持有独立 `Memory`（`web/server.py:37-51`）。

---

## 6. 可靠性与错误处理

**分层异常体系**（`errors.py:11-44`）。所有异常继承 `AgentError`，CLI/Web 只需捕获基类即可给出友好提示，同时保留细分类别以便差异化处理：

| 异常 | 含义 | 处理策略 |
|---|---|---|
| `ConfigError` | 配置缺失/非法（如未提供 API Key） | 启动即失败，CLI 返回退出码 2 并给出修复提示（`cli.py:587`） |
| `LLMConnectionError` | 网络/超时/429/5xx | **可重试**，进入退避重试 |
| `LLMError` | 4xx 参数错误、响应非法 JSON、缺少 choices | 不重试；流式请求失败时降级为非流式 |
| `ToolError` 及其子类 | 工具执行失败 | 转为 observation 回灌模型自愈 |
| `ToolNotFound` | 模型请求未注册工具 | 回灌可用工具名列表 |
| `PathNotAllowed` | 路径越界 | 回灌越界说明 |
| `ToolDenied` | 用户拒绝执行 | 回灌"不要重复请求该操作" |
| `MaxStepsExceeded` | 步数超限（保留类型，实际走强制收敛路径） | — |

**网络重试策略**（`llm.py:284-328`）。仅以下情况进入重试：HTTP `408 / 409 / 429` 或 ≥ 500（`llm.py:296`）、`URLError`（含连接失败）、`TimeoutError`。退避公式（`llm.py:324`）：

```
delay = Retry-After                        # 服务端给了就优先用它（llm.py:297）
      = min(30.0, 1.0 * 2 ** attempt) + random.uniform(0, 0.4)   # 指数退避 + 抖动
```

抖动用于避免多客户端同时重试造成二次拥塞。尝试次数为 `max_retries + 1`（默认 3 + 1 = 4 次，`llm.py:313`）。4xx 参数类错误不重试——重试同一请求体没有意义。

**流式失败降级**（`llm.py:331-345`）。首选流式（可实时显示，并用 `stream_options.include_usage` 取用量统计）；若抛出非连接类的 `LLMError`（典型情况：服务端不支持 `stream_options` 或流式），自动以 `stream=False` 重试一次。连接类错误不降级，因为它已在重试逻辑里处理。

**SSE 解析的健壮性**（`llm.py:133-221`）。`iter_sse_chunks()`（`llm.py:190`）忽略空行、以 `:` 开头的心跳/注释行与解析失败的脏行，遇到 `[DONE]` 立即结束；`StreamAccumulator`（`llm.py:133`）按 `index` 归并分片到达的 `tool_calls`，把 `arguments` 字符串逐片拼接——这是工具参数跨多个 chunk 到达时必须处理的细节（`tests/test_llm.py:40` 用三段碎片拼出完整 `{"path": "a.py"}`）。解析逻辑与网络完全解耦：`parse_stream()`（`llm.py:214`）接收任意行可迭代对象，因此测试无需起 HTTP 服务。

**工具执行的超时与截断。** `subprocess.run(timeout=...)`（`tools/exec.py:57-66`）；超时不会丢失已产生的输出——`TimeoutExpired` 携带的 `stdout/stderr` 被单独解码后返回（`tools/exec.py:74-86`），并以 `timed_out=True` 标记为失败。输出统一截断到 `MAX_OUTPUT_CHARS = 6000`，保留首尾、中间标注省略（`tools/exec.py:31-36`）；`run_tests` 另外抽取关键行（`_summarize_tests`，`tools/exec.py:234-249`）避免把整篇测试日志灌进上下文。回灌给模型的观察文本另有一层 6000 字符截断（`agent.py:191-199`）。

**BrokenPipe 处理。** 浏览器关闭页面或客户端提前断开时，`wfile.write` 会抛 `BrokenPipeError` / `ConnectionResetError`，在 SSE 推送（`web/server.py:217`）与所有响应写出处（`web/server.py:171`、`web/server.py:187`）均被吞掉，避免服务端因客户端断开而打印堆栈。

**SSE 必须 `Connection: close`。** SSE 响应没有 `Content-Length`，也不使用 chunked 编码；HTTP/1.1 默认 keep-alive，客户端无法判断事件流是否结束，浏览器 `fetch` 的 reader 会永远等不到 `done`。因此响应头显式声明 `Connection: close` 并设置 `self.close_connection = True`（`web/server.py:202-209`）。

---

## 7. 安全模型

**路径沙箱。** 所有文件工具都通过 `Workspace`（`tools/base.py:80`）解析路径：相对路径拼接到工作目录根、绝对路径直接使用，随后 `Path.resolve()` 归一化（消除 `..` 与符号链接），再用 `os.path.normcase` 比较前缀（`tools/base.py:107-113`）。用 `normcase` 而非普通字符串比较，是为了在 Windows 上正确做到大小写不敏感并把 `/` 与 `\` 视为等价。目标是根目录本身或位于 `root + os.sep` 之下才放行，否则抛 `PathNotAllowed`。`tests/test_tools.py:55-69` 覆盖 `../` 逃逸、绝对路径越界与写入越界三种情形。

**读取限制。** 超过 `max_read_bytes = 200_000` 的文件拒绝整体读取并提示改用分段读取（`tools/base.py:136-140`）；`BINARY_SUFFIXES` 中的扩展名（图片、压缩包、可执行文件、字体等，`tools/base.py:48-51`）直接拒绝，避免把二进制噪声塞进上下文；编码非 UTF-8 时以 `errors="replace"` 容错而非中断。单次读取行数上限 `MAX_LINES_PER_READ = 2000`（`tools/fs.py:15`）。

**遍历忽略。** `DEFAULT_EXCLUDES`（`tools/base.py:24-45`）跳过 `.git`、`__pycache__`、`node_modules`、虚拟环境、构建产物与 `.codeagent`，既提速也避免无关内容进入上下文。

**权限门。** 带副作用的工具默认必须经用户确认（`agent.py:168-181`）。CLI 的 `make_approver()`（`cli.py:163-181`）打印工具名与完整参数，交互式终端询问 `y/N`；**非交互环境默认拒绝**（`cli.py:172-174`），并提示可加 `--yes` 自动放行。该默认值很重要：在 CI 或管道输入场景下，宁可不执行也不要静默改文件。Web 端把权限开关做成界面复选框，随每次请求以 `auto_approve` 字段传入（`web/server.py:200`），由 `Session.make_agent()` 注入为 `approver`（`web/server.py:57-66`）。

**诚实的边界：这是"作业级沙箱"，不是内核级隔离。** `Workspace` 约束的是**工具层的路径参数**；`run_python` / `run_tests` 启动的子进程仍以当前用户权限运行，被执行的代码本身可以读写进程可达的任意文件（`tools/exec.py:5-10` 的模块文档明确声明）。因此这两个工具被标记为需要确认，而不是靠沙箱保证安全。真正的隔离需要容器、受限用户或系统级沙箱，超出本作业范围（见第 12 节）。

**凭据管理：真实 API Key 只存在于 `.env`，且被三重隔离。** 提交作业时最现实的泄露风险不是被工具误发，而是 Key 被写进源码、文档或压缩包，因此本项目把它当作一条独立的工程约束来处理：

1. **排除**：`.gitignore` 忽略 `.env` / `.env.local`，`scripts/package.py` 的 `EXCLUDE_FILES` 同样排除它们——Key 只在自己机器上存在；
2. **门禁**：打包前 `report_secrets()`（`scripts/package.py:146`）会先用 `read_env_secrets()`（`scripts/package.py:90`）取出 `.env` 中长度 ≥ 8 且键名含 `KEY`/`TOKEN`/`SECRET` 的值（占位符如 `sk-你的key` 自动跳过），再对**全部待打包文件**做精确子串比对；命中即打印 `文件:行号` 并中止打包（退出码 1，可用 `--allow-secrets` 显式覆盖）；
3. **形态告警**：除精确比对外，`scan_for_secrets()`（`scripts/package.py:113`）还用 `SUSPICIOUS_KEY_PATTERN`（`scripts/package.py:60`）扫描 `sk-`/`ak-` + 长串这类高危形态，作为精确比对之外的兜底；含 `test`/`fake`/`example`/`your` 等标记的行视为示例数据，避免误伤测试固定值。

占位符 Key 还会在启动阶段被 `is_placeholder_key()`（`config.py`）拦下并给出明确提示，而不是让用户面对一个费解的 `HTTP 401`。如果 Key 曾经进入过 git 历史，正确做法是 `git rm --cached .env` 并**立即到服务商控制台吊销重建**——历史无法真正删除，吊销才是唯一有效的补救。

---

## 8. 事件协议

主循环不直接打印任何内容，而是通过 `on_event(dict)` 抛出结构化事件（`agent.py:126-132`）。CLI 的 `ConsoleRenderer`（`cli.py:109`）与 Web 的 `send_event`（`web/server.py:212`）消费同一套事件，因此命令行与浏览器里观察到的行为完全一致。

| 事件类型 | 触发时机 | 关键字段 |
|---|---|---|
| `step_start` | 每轮推理开始 | `index`、`max_steps` |
| `llm_start` | 发起模型请求前 | `index`、`stream` |
| `assistant_delta` | 流式文本增量 | `content`、`index` |
| `assistant_message` | 该轮有工具调用且模型带思考文本 | `content` |
| `tool_call` | 即将执行工具 | `name`、`arguments`、`index` |
| `tool_result` | 工具执行结束 | `step`（完整 `AgentStep`）、`ok`、`preview`（前 400 字符） |
| `warning` | 重复调用告警、达到步数上限 | `message` |
| `final` | 产出最终答复 | `content` |
| `error` | LLM 调用失败等致命错误 | `message` |
| `session` | Web 专用：返回会话 ID | `session_id` |
| `done` | Web 专用：本次运行结束 | `result`（`AgentResult.to_dict()`） |

**SSE 传输格式。** 每条事件序列化为 `data: {json}\n\n`（`web/server.py:214`），JSON 使用 `ensure_ascii=False` 保留中文可读性；首帧先发 `session` 让前端记住会话 ID，末帧发 `done` 携带完整结果与工具轨迹（`web/server.py:220-227`）。前端按 `\n\n` 切分缓冲区、取 `data:` 行解析，用 `assistant_delta` 增量渲染 Markdown，用 `tool_call` / `tool_result` 成对生成工具卡片。

---

## 9. Mock LLM 设计

`MockLLM`（`codeagent/mock.py:58`）实现与真实客户端相同的 `BaseLLM.generate()` 接口，因此可在不改动 Agent 任何代码的前提下替换（`llm.py:388-393` 的 `build_llm()` 按 `provider` 选择）。它存在的两个理由：**离线演示**（无 API Key 也能跑通完整循环，适合课堂展示与录屏）与**确定性测试**（主循环测试不依赖网络、不受模型随机性影响）。

它不是随机编造，而是**规则规划器**，分三步：

1. **意图识别**：`INTENT_KEYWORDS`（`mock.py:30-37`）定义六类意图的中英文关键词，随后拆成两条匹配路径——英文关键词必须**按词边界**匹配（`_ASCII_INTENT_PATTERNS`，`mock.py:40-48`），中文关键词**按子串**匹配（`_CJK_INTENT_KEYWORDS`，`mock.py:50-54`）。这个拆分解决了一个具体 bug：若英文也用子串匹配，文件名 `buggy.py` 会因为含 `bug` 被误判成"代码审查"意图。目标文件路径由 `FILE_PATTERN`（`mock.py:23`）抽取。
2. **生成工具计划**：`_build_plan()`（`mock.py:112-150`）按意图产出有序工具序列——review 意图对 `.py` 文件产出 `analyze_code` → `read_file`；explain 产出 `list_definitions` → `read_file`；test 产出 `run_tests`；search 产出 `search_text`。计划按模型实际获得的工具集合过滤（`mock.py:150`）。
3. **去重后逐个执行**：`_completed_calls()`（`mock.py:185-194`）从历史消息中还原已调用过的 `(工具名, 参数)` 指纹，已完成的步骤直接跳过，因此每一步都推进计划而不会死循环（`tests/test_mock.py:103` 验证）。

计划跑完后进入 `_render_final()`（`mock.py:211`），按意图渲染答复：`_render_review()`（`mock.py:243`）解析 `analyze_code` 输出里的 `[级别] L行号 规则: 描述` 与 `建议:` 行（`FINDING_PATTERN` / `SUGGESTION_PATTERN`，`mock.py:27-28`），按严重级别分组并给出修复顺序；`_render_explain()`（`:303`）/ `_render_tests()`（`:329`）/ `_render_run()`（`:346`）/ `_render_search()`（`:353`）/ `_render_generic()`（`:360`）分别渲染结构说明、测试摘要、执行输出、检索结果与目录概览。所有答复结尾都带一行显式声明"本回答由离线 Mock 模型根据工具的真实输出渲染而成（未调用外部 LLM）"（`mock.py:55`）——**工具结果是真的，只有推理是模拟的**。

`_stream()`（`mock.py:379`）按 40 字符分块回调 `on_delta`。这里有一个自测出来的细节：流式增量必须与最终消息正文**完全一致**，否则前端会把思考文本渲染两遍；因此生成工具调用时也只推送 `thought` 本身而不额外追加换行（`mock.py:94-95`）。`tests/test_mock.py:149` 对此做了断言（`"".join(chunks) == message.content`）。

---

## 10. 测试策略

全部用例基于标准库 `unittest`，`python -m unittest discover -s tests` 即可执行（不装 pytest）。测试包 `tests/__init__.py` 把项目根加入 `sys.path`，保证 discover 与 pytest 两种入口都能正确导入。

按源码中 `def test_` 统计，共 **118 个用例**：

| 测试文件 | 用例数 | 覆盖点与关键断言 |
|---|---|---|
| `tests/test_tools.py` | 33 | 沙箱越界（`../`、绝对路径、写入）必须被拒；`read_file` 行号与窗口截断；`write_file` 拒绝静默覆盖；`edit_file` 匹配数为 0 或多于 1 都报错；`search_text` 非法正则降级为纯文本；`analyze_code` 命中 `bare-except`/`mutable-default`/`dangerous-eval` 且 `high ≥ 3`；语法错误单独成一条；`run_python` 退出码与超时（`timed_out=True`）；参数校验三类错误信息与字符串→整数纠正；未注册工具返回失败而非抛异常 |
| `tests/test_agent.py` | 18 | 无工具直接作答；工具调用后收敛且消息序列为 `user→assistant→tool→assistant`；一次两个工具时 `tool_call_id` 顺序正确；工具失败可自愈；未注册工具与非法 JSON 参数被回灌；权限拒绝时**文件系统未被改动**；只读工具不触发 approver；`max_steps=2` 时走强制收敛；重复调用触发 `loop_detected`；LLM 异常产出 `stopped_reason="error"`；事件序列包含 `tool_call`/`tool_result`/`final`；关闭流式后无 `assistant_delta`；会话存取与统计 |
| `tests/test_cli.py` | 18 | 斜杠命令表注册齐全（帮助文本里列出的命令都必须可派发）、`/exit` 与 `/quit` 变体、未知命令不退出、`/reset` 清空历史、`/verbose` 切换、`/save` 落盘、`/load` 失败路径、带空格参数解析、`--timeout`/`--max-retries`/`--memory-tokens` 等参数确实透传到配置；以及端到端 `main(["ask", ...])`、`main(["analyze", ...])`（含缺少文件时的退出码）、`main(["tools"])`、`main(["demo"])` 离线演示 |
| `tests/test_llm.py` | 13 | 流式正文增量拼接；**跨 3 个分片的工具参数拼接**；多工具按 `index` 排序；SSE 噪声行（心跳、非法 JSON、`[DONE]`）被跳过；usage 捕获；非法 JSON 参数抛 `LLMError`；带工具调用时 `content` 序列化为 `null`；`from_api` 容忍 `content=None`；endpoint 不出现双斜杠；payload 含 `tools` 与 `tool_choice=auto`；`Authorization: Bearer` 头 |
| `tests/test_mock.py` | 13 | 六类意图识别；目标文件与检索词抽取；review 场景确实跑两个工具；explain / search / listing 场景；已完成的调用不重复；`tools=None` 时直接给最终答复；计划中不可用的工具被跳过；`on_delta` 分块能还原完整正文；**流式增量与消息正文一致**；Agent 流式事件能拼出最终答复 |
| `tests/test_config.py` | 12 | `.env` 解析（注释、引号、`export`、非法行）；API Key 按 `CODEAGENT_API_KEY → OPENAI_API_KEY → DEEPSEEK_API_KEY …` 顺序探测；类型转换；`None` 覆盖被忽略；未知配置项报错；`openai` 缺 Key 时校验失败而 `mock` 允许；非法 provider 报错；路径归一化为绝对路径且会话目录落在工作区内；Key 脱敏；非数字环境变量报错 |
| `tests/test_memory.py` | 11 | 中英文 token 估算非零且工具调用增加开销；`build()` 恒含 system；裁剪保留最近消息；**裁剪后首条非 system 消息不能是 `tool`**；裁剪触发省略提示；统计字段；存取往返保持工具调用与 `tool_call_id`；加载不存在的会话抛 `AgentError`；`clear()` 保留 system prompt |

**示例用例不计入上表。** `examples/tests/test_sample_math.py` 另有 4 个用例（`test_add` / `test_divide` / `test_average` / `test_is_prime`），用于演示 `run_tests` 工具与"测试生成"场景，不计入项目自身的回归统计。

**提交前自检（dogfooding）。** `scripts/selfcheck.py` 用本项目自己的 `analyze_code` 审查项目源码：遍历项目内 31 个 Python 源文件（其中 1 个是刻意注入缺陷的演示文件，被排除在门禁之外，实际参与判定 30 个），跳过 `__pycache__`、虚拟环境与构建产物，并把 `examples/buggy_code.py` 排除在门禁之外（`scripts/selfcheck.py:30`、`scripts/selfcheck.py:53`）；默认只报 high/medium，同时输出规则命中直方图，存在 high 级别问题即返回退出码 1（`scripts/selfcheck.py:75`），可直接用作 CI 门禁。本次提交前运行结果为 **0 个 high 级别问题**。

**测试替身。** `tests/test_agent.py:18` 的 `ScriptedLLM` 按脚本或回调返回预设 `Message`，并记录每次请求的 `messages` 与 `tools`，因此可以断言"第二次请求里确实带了 tool 结果与工具 schema"这类协议级行为。`BaseLLM` 只有一个方法这一设计，正是让主循环可测的关键。

---

## 11. 可扩展性

**新增一个工具只需三步**（Agent 主循环无需任何改动）：

1. 在 `codeagent/tools/` 下（或已有的 `fs.py` / `exec.py` / `analysis.py` 中）写 `build_xxx_tools(workspace) -> List[Tool]`，用 `Tool(name=..., description=..., parameters=<JSON Schema>, handler=..., mutating=..., requires_approval=...)` 声明。handler 可用 `raise ToolError("...")` 报告可恢复错误，或用 `ToolResult.failure(...)` 返回带 `meta` 的失败。
2. 在 `codeagent/tools/__init__.py:27` 的 `build_all_tools()` 中追加 `tools.extend(build_xxx_tools(workspace))`。
3. 在 `tests/test_tools.py` 补用例（沙箱越界、参数校验、典型与边界输入各一条）。若希望模型优先使用该工具，可在 `codeagent/prompts.py:37-41` 的"任务类型参考"中补一句工作流。

`Tool.validate()` 会自动处理未知参数、必填校验与类型纠正，`ToolRegistry` 会自动把 schema 下发给模型——新增工具不必改动这两处。

**其他可扩展点。** 换模型只改配置（`CODEAGENT_BASE_URL` / `CODEAGENT_MODEL` / `CODEAGENT_API_KEY`），因为协议是 OpenAI 兼容的；新增 LLM 提供方只需实现 `BaseLLM.generate()` 并在 `build_llm()`（`llm.py:388`）注册；`Memory` 的裁剪策略集中在 `build()` 与 `_repair_pairing()` 两处，便于替换为摘要式压缩。

**未来可做的改进。**
- **检索增强**：当前 `search_text` 是正则全文检索，可加向量索引与语义检索工具，让大仓库定位更准。
- **多 Agent 协作**：可把"审查者 / 修复者 / 验证者"拆成独立角色，复用现有 `Agent` 与事件协议，由协调者串联；当前 `Agent` 与 UI 已解耦，具备拆分基础。
- **进程级/容器级沙箱**：为 `run_python` 引入低权限用户、容器或作业对象（Windows Job Object）限制网络与文件系统，把"需要确认"升级为"默认安全"。
- **精确 token 计数**：接入目标模型的 tokenizer 替代字符加权估算，并改用摘要压缩而非直接丢弃早期消息。
- **流式断线续传**：SSE 增加事件 ID 与 `Last-Event-ID` 支持，前端刷新后可续接同一次运行。
- **更广的语言支持**：`analyze_code` 目前只支持 Python，可为 JS/TS/Java 增加对应解析器与规则集。

---

## 12. 设计取舍与不足

**取舍一：零依赖 vs. 生态能力。** 放弃 `requests` / `openai` SDK / Web 框架，换来"克隆即可运行"与协议细节可见；代价是自行实现 HTTP 重试、SSE 解析与 Web 路由，这部分代码必须自己承担正确性（因此 `test_llm.py` 对分片拼接做了重点覆盖）。

**取舍二：自研循环 vs. LangChain。** 详见第 2 节。自研把控制权（步数、熔断、权限、强制收敛）留在项目内，代价是失去框架自带的工具生态与记忆组件。

**取舍三：确定性规则 vs. 纯 LLM 判断。** `analyze_code` 用 AST 规则产出事实、LLM 负责解释。好处是行号不会幻觉、结论可复现；代价是规则仍有误报与漏报，且规则的"口味"需要人工维护——例如 `SHADOWABLE` 只覆盖常见内建名，`print-statement` 只跳过含 `__name__` 判断的作用域，正则类规则（`todo-marker`、`long-line`）基于文本行而非语法结构。

**已知不足。**
1. **执行类工具不是真正的隔离**：`run_python` / `run_tests` 的子进程拥有与主进程相同的权限，`Workspace` 只约束工具参数层面的路径，无法约束被执行的代码行为（`tools/exec.py:5-10` 已声明）。安全依赖用户的确认动作，而非机制强制。
2. **token 估算是近似值**：中文按 1.5 字符/token 加权是经验值，与具体模型的分词器存在偏差，极端情况下可能仍触发服务端的上下文超限错误。
3. **裁剪以整条消息为粒度**：单条超长的工具结果（上限 6000 字符）仍可能占据预算的显著比例，尚未实现"单条消息内部截断 + 摘要"。
4. **熔断基于参数指纹**：语义等价但参数不同的重复调用（例如换一种写法反复读取同一文件）无法识别，只能依赖 `max_steps` 兜底。
5. **Web 端无鉴权、无跨进程持久化**：默认只监听 `127.0.0.1`，同一会话内用锁串行处理（`web/server.py:222`），会话状态保存在进程内存中，重启即丢失，也不区分多用户权限。
6. **`analyze_code` 仅支持 Python**：对其他语言只能退化为 `read_file` + `search_text`。
7. **SSE 无断线续传**：运行中若连接断开，只能重新发起，无法从断点继续。
