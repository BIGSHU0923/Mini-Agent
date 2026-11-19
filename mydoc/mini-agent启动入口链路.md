# mini-agent 命令行启动入口链路

> 记录 `mini-agent` 命令从「敲下回车」到「Agent 真正开始工作」的完整调用链。
> 项目根目录：`d:\project\mini-agent\Mini-Agent`

---

## 0. 一句话总览

```
终端输入 mini-agent
   ↓
uv/pip 生成的启动脚本（由 pyproject.toml 定义）
   ↓
mini_agent/cli.py : main()
   ↓  parse_args() 解析参数 → 定工作目录
   ↓  asyncio.run(run_agent(...))
run_agent()
   ↓  加载配置 → 建 LLM 客户端 → 装配工具 → 建 Agent
   ↓
交互模式：while 循环等你输入  ／ 非交互模式：直接执行 --task
   ↓
Agent.run()   ← Agent 的「大脑循环」（agent.py）
```

---

## 1. 命令是怎么「注册」出来的

**位置**：[pyproject.toml](file:///d:/project/mini-agent/Mini-Agent/pyproject.toml#L27-L29)

```toml
[project.scripts]
mini-agent = "mini_agent.cli:main"
```

- 含义：定义一个名为 `mini-agent` 的可执行命令，执行它时调用 `mini_agent.cli` 模块里的 `main` 函数。
- 安装方式：`uv tool install -e .` 或 `pip install .` 时，Python 会在系统 PATH 中生成一个 `mini-agent` 启动脚本，脚本内容就是去调用那个函数。
- **Java 类比**：相当于打包插件里声明的 `Main-Class: ...MainClass`，`mini-agent` 命令 ≈ `java -jar app.jar` 里的那个 `java` 包装。

---

## 2. 第一站：main() —— 真正的业务入口

**位置**：[cli.py : main()](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L887-L913)

`main()` 只做四件事，非常薄：

| 序号 | 步骤 | 行号 |
|---|---|---|
| 2.1 | 解析命令行参数 `args = parse_args()` | [L890](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L890) |
| 2.2 | 若是 `log` 子命令 → 读日志后直接返回（不启动 Agent） | [L893-L898](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L893-L898) |
| 2.3 | 确定工作目录（`--workspace` 优先，否则用当前目录 `Path.cwd()`） | [L902-L909](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L902-L909) |
| 2.4 | `asyncio.run(run_agent(...))` 启动异步主流程 | [L913](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L913) |

> 注意 2.4 的 `asyncio.run`：它会创建一个事件循环并运行协程。**Java 类比**：相当于「启动一个异步 runtime/线程池」后再提交任务，因为后面调大模型全是 `async/await`。

---

## 3. 第二站：parse_args() —— 参数解析

**位置**：[cli.py : parse_args()](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L309-L364)

用标准库 `argparse` 定义参数（**Java 类比**：picocli / commons-cli）。

| 参数 | 作用 |
|---|---|
| `--workspace` / `-w` | 指定工作区目录 |
| `--task` / `-t` | 非交互地执行一次任务后退出 |
| `--version` / `-v` | 打印版本（`mini-agent 0.1.0`）后退出 |
| `log` 子命令 | `mini-agent log [filename]`，查看/读取日志 |

---

## 4. 第三站：run_agent() —— 装配一切

**位置**：[cli.py : run_agent()](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L514-L885)

这是启动过程中最核心的一站，按顺序做以下装配：

| 序号 | 步骤 | 关键代码位置 |
|---|---|---|
| 4.1 | 定位配置文件，并做「配置缺失」引导 | [L524](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L524) 起 |
| 4.2 | 读取解析配置 `Config.from_yaml()` | L559 附近 |
| 4.3 | 构造重试配置 `RetryConfigBase` + 回调 `on_retry` | [L588 附近](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L584) |
| 4.4 | 创建 LLM 客户端 `llm_client = LLMClient(...)` | [L594](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L594) |
| 4.5 | 加载「与工作区无关」的工具（Bash 辅助 / Skills / MCP） | [L608](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L608) |
| 4.6 | 追加「依赖工作目录」的工具（文件读写 / bash / 记忆） | [L611](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L611) |
| 4.7 | 加载系统提示词，并注入 Skills 元数据 | L614 附近 |
| 4.8 | 创建 Agent 实例 `agent = Agent(...)` | [L638](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L638) |
| 4.9 | 分支：非交互模式（带 `--task`） | [L652](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L652) |
| 4.10 | 分支：交互模式 —— 配置输入会话 + `while True` 主循环 | [L706](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L706) / [L715](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L715) |

其中两个分支的差别：

- **非交互（4.9）**：`agent.add_user_message(task)` → `await agent.run()` → 打印统计 → 清理 MCP → 返回退出。
- **交互（4.10）**：进入 `while True`，用 prompt_toolkit 等你输入；处理 `/help`、`/clear`、`/stats`、`/log` 等斜杠命令；普通输入则调用 `agent.run()`，并支持按 Esc 取消。

---

## 5. 终点：Agent.run() —— 大脑循环

**位置**：[agent.py : run()](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L374)

无论哪条分支，最终都会进入这里。这是 Agent 的 ReAct 主循环：

```
取最后一条用户消息
   ↓
while 未超过 max_steps:
   把 messages + 工具定义发给大模型
   模型返回（可能带 tool_calls）
   若有工具调用 → 执行工具 → 把结果作为一条消息追加回 messages → 继续循环
   若没有工具调用 → 说明任务完成 → 退出循环
   ↓
返回最终结果
```

到这里，「命令行入口链路」就走完了。**入口在 cli.py，灵魂在 agent.py**。

---

## 6. 附：两种等价启动方式

| 启动方式 | 命令 | 走的路 |
|---|---|---|
| ① 命令方式 | `mini-agent` | 系统脚本 → [cli.py:main](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L887) |
| ② 模块方式 | `python -m mini_agent.cli` | 直接执行 cli.py → [底部的 `if __name__ == "__main__"`](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L917-L918) → `main()` |

两者最终都进入 `main()`，效果一致。

- **调试建议**：用 `mini-agent --task "..."`（或 `python -m mini_agent.cli --task "..."`）这种非交互方式打断点，比交互模式更顺。

---

## 7. 全链路速查（含行号）

```
pyproject.toml  [project.scripts]  mini-agent = "mini_agent.cli:main"   (L27-29)
        │
        ▼
cli.py  main()                          (L887)
        ├─ parse_args()                 (L309)   ← argparse 解析参数
        ├─ [log 子命令分支]              (L893)
        ├─ 确定 workspace_dir            (L902)
        └─ asyncio.run(run_agent(...))  (L913)
                │
                ▼
        run_agent()                      (L514)
                ├─ 加载/校验配置         (L524)
                ├─ 建 LLMClient          (L594)
                ├─ initialize_base_tools (L608)   ← Skills / MCP / Bash辅助
                ├─ add_workspace_tools   (L611)   ← 文件/bash/记忆工具
                ├─ 系统提示词注入         (L614)
                ├─ Agent(...)            (L638)
                ├─ [--task 非交互分支]    (L652)
                └─ [交互 while 主循环]    (L715)
                        │
                        ▼
                agent.py  Agent.run()    (L374)   ← 真正的 ReAct 大脑循环
```
