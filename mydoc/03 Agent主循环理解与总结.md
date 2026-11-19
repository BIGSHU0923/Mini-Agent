# Agent 主循环：我的理解与总结

> 学习方式：从 [cli.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py) 的 `main()` 开始，单步 debug 到实际提问，逐行走读代码。
> 本文档归档这次的理解与结论，作为学习笔记。

***

## 一、我的理解（原始版本）

> 我觉得我跟着 mini-agent 启动时从 cli.py 的 main 方法开始 debug，到实际提问，代码一行行的 debug，已经大概了解了 agent 的整个执行链路。本质是 while 循环，内部等待 llm 回复，llm 的回复包含很多信息，包括需要调用的工具等。内部在循环对话时，直到 llm 不再需要调用工具时，即认为回答完毕了。

**结论：核心骨架理解正确。** ✅

***

## 二、确认正确的部分

| 序号  | 我的理解                  | 对应的源码位置                                                                                                                                            |
| --- | --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| 2.1 | 本质是 while 循环          | [agent.py#L400](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L400) `while step < self.max_steps:`                                  |
| 2.2 | 内部等待 LLM 回复           | [agent.py#L430](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L430) `response = await self.llm.generate(...)`                       |
| 2.3 | LLM 回复包含多种信息（含要调用的工具） | [agent.py#L458-L463](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L458-L463)：`content` / `thinking` / `tool_calls`                 |
| 2.4 | LLM 不再需要调用工具 = 回答完毕   | [agent.py#L478-L482](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L478-L482) `if not response.tool_calls: return response.content` |

***

## 三、需要再抠精确的 5 点

### ① 退出条件不是 1 个，是 3 个

> 「不再调用工具」只是其中一种退出方式，不是唯一。

### ② 每次发给 LLM 的是「完整历史 + 工具清单」

[L427-L430](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L427-L430)：把整个 `self.messages` 和 `tool_list` 一起发过去。
模型能「记住」上下文，不是它有记忆，而是**每轮都把历史重发一遍**。

### ③ 不是「循环对话」，而是「一次提问 → 内部多轮 tool-use 循环」

中间没有新的用户输入。每轮追加的是 `assistant`（[L464](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L464)）和 `tool`（[L567](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L567)）消息，不再追加 user 消息。

### ④ 一次回复可能请求调用多个工具

执行工具用 `for tool_call in response.tool_calls:`（[L493](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L493)）；结果靠 `tool_call_id` 与请求配对（[L560-L566](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L560-L566)）——类似 Java 里用「请求 ID 关联 response」。

### ⑤ 工具报错不中断循环，而是「喂回模型让它自纠」

工具抛异常会被捕获并转成 `success=False` 的结果追加进历史（[L528-L539](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L528-L539)）；模型「幻觉」出不存在的工具也是同样处理（[L515-L521](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L515-L521)）。
**容错靠循环自纠，而不是外层 try/catch 崩掉。**

***

## 四、一句话总结（精确版）

> `run()` 是一个**有上限的 while 循环**。每一轮都把「完整历史 + 工具清单」发给 LLM，把回复追加进历史；若回复要调工具就执行、把结果追加进历史后继续循环，若不要调工具就返回文本、循环自然结束。此外还有「超出 max\_steps」和「被取消 / 调用失败」两个兜底退出。

补充：因为每轮历史都在增长，所以循环开头会调用 [\_summarize\_messages()](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L410) 在上下文过长时自动压缩。

***

## 五、接下来的学习计划

> 原则不变：**先读源码 → 再打断点跑一遍 → 最后动手改一点**。每一项都给出「检验标准」，能答上来/做出来才算过关。

1 自己做一个命令行启动的功能。

2 了解skill是如何加载，被使用的，后面自己写一个skill，并观察和应用。

3 了解tool是如何加载，被使用的，后面自己写一个tool，并观察和应用。

4 了解mcp是如何加载，被使用的，后面自己写一个mcp，并观察和应用。

### 阶段 A：把主循环的「配套机制」看透（最优先）

| 序号 | 学什么           | 看哪里                                                                                             | 检验标准                    |
| -- | ------------- | ----------------------------------------------------------------------------------------------- | ----------------------- |
| A1 | 上下文压缩：历史超长怎么办 | [\_summarize\_messages](file:///d:/project/mini-agent/Mini-Agent/mini_agent/agent.py#L231-L372) | 说清「什么条件触发压缩」「压缩后保留哪些消息」 |
| A2 | token 统计怎么来   | `api_total_tokens` / `response.usage`                                                           | 能解释 `usage` 是谁提供的       |
| A3 | 取消机制怎么实现      | `_check_cancelled` / `_cleanup_incomplete_messages`                                             | 说清为什么取消要在「安全检查点」才生效     |

### 阶段 B：搞懂「工具」这一层（Agent 的手脚）

| 序号 | 学什么            | 看哪里                                                                                                                                                                               | 检验标准                                       |
| -- | -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------ |
| B1 | 工具抽象基类         | [tools/base.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/tools/base.py)                                                                                                | 说清 `parameters` 和 `to_openai_schema()` 的作用 |
| B2 | 具体工具实现         | [file\_tools.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/tools/file_tools.py)、[bash\_tool.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/tools/bash_tool.py) | 对照 `execute(**arguments)` 看参数怎么进来的         |
| B3 | 工具怎么被注册进 Agent | [cli.py#add\_workspace\_tools](file:///d:/project/mini-agent/Mini-Agent/mini_agent/cli.py#L464)                                                                                   | 能说出 `self.tools` 这个 dict 的 key/value 是什么   |

### 阶段 C：搞懂「模型通信」这一层

| 序号 | 学什么      | 看哪里                                                                                                                       | 检验标准                       |
| -- | -------- | ------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| C1 | LLM 抽象接口 | [llm/base.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/llm/base.py)                                            | 说清 `generate()` 的入参出参      |
| C2 | 消息格式转换   | [anthropic\_client.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/llm/anthropic_client.py) 的 `_convert_messages` | 说清内部 Message 怎么变成厂商 API 格式 |
| C3 | 多厂商如何切换  | [llm\_wrapper.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/llm/llm_wrapper.py)                                 | 能画出「工厂模式」选择流程              |
| C4 | 重试机制     | [retry.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/retry.py)                                                  | 说清 `on_retry` 回调何时被调用      |

### 阶段 D：进阶能力

| 序号 | 学什么              | 看哪里                                                                                                                                                                                       |
| -- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1 | 配置加载与查找优先级       | [config.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/config.py)                                                                                                                |
| D2 | 跨会话记忆            | [note\_tool.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/tools/note_tool.py)                                                                                                   |
| D3 | Skills 机制（渐进式披露） | [skill\_tool.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/tools/skill_tool.py) / [skill\_loader.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/tools/skill_loader.py) |
| D4 | MCP 外部工具协议       | [mcp\_loader.py](file:///d:/project/mini-agent/Mini-Agent/mini_agent/tools/mcp_loader.py)                                                                                                 |

### 阶段 E：动手改造（真正检验）

| 序号 | 练习                                            | 目标                   |
| -- | --------------------------------------------- | -------------------- |
| E1 | 仿照 file\_tools 写一个新工具（如 `HttpRequestTool`）并注册 | 理解「给 Agent 加能力」的完整流程 |
| E2 | 给 `run()` 加「工具调用次数」统计并打印                      | 理解状态在循环中如何累积         |
| E3 | 把 `max_steps` 调到 3，观察未完成时的表现                  | 验证「兜底退出条件」           |

### 建议节奏

***

## 附：本次会话产出的学习文档
