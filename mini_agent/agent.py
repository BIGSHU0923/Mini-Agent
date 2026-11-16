"""Agent 核心实现模块（可以理解为整个 Mini-Agent 的「大脑」）。

本模块实现了 Agent 的「推理-行动」主循环（业界常叫 ReAct loop / Agent Loop）：
    1. 把「系统提示词 + 历史消息 + 可用工具列表」一起发给大模型（LLM）
    2. 模型返回两部分内容：文本输出（content）和/或工具调用请求（tool_calls）
    3. 如果模型请求调用工具，Agent 就真的去执行这些工具，并把执行结果
       作为一条 role="tool" 的消息追加进历史消息
    4. 回到第 1 步继续循环，直到模型不再请求任何工具（认为任务已完成）
       或达到最大步数 max_steps 为止

对 Java 后端开发者的类比（帮助你快速建立心智模型）：
    - Message            ≈ 一个 DTO / POJO（定义见 schema/schema.py）
    - Tool               ≈ 一个 interface，每个具体工具（读文件、执行 bash 等）是它的实现类
    - Agent              ≈ 一个编排器 / Orchestrator（类似 Spring 里负责调度多个 Service 的类）
    - self.messages      ≈ 整个会话的上下文（context），相当于一个持续 append 的 List<Message>
    - 一次 run()         ≈ 一次完整的「请求-响应-调用工具-再请求」编排流程

注意：这里用到的所有 LLM 调用都是 async（异步）的，对应 Python 的 asyncio，
可以类比成 Java 的 CompletableFuture / 协程，作用是「等待网络 IO 时不阻塞线程」。
"""

import asyncio
import json
from pathlib import Path
from time import perf_counter
from typing import Optional

import tiktoken  # 用于精确统计 token 数量，判断上下文是否超限需要压缩

from .llm import LLMClient  # LLM 客户端封装（底层支持 Anthropic / OpenAI 两种协议）
from .logger import AgentLogger  # 运行日志记录器（把每步请求/响应写文件）
from .schema import Message  # 消息数据结构（role / content / tool_calls 等字段）
from .tools.base import Tool, ToolResult  # 工具基类 + 工具执行结果结构
from .utils import calculate_display_width  # 计算终端显示宽度（中文字符占 2 个宽度）


# ANSI 终端颜色码：用于在命令行里给输出着色（\033[3Xm 是标准转义序列）
# 类比 Java：类似在控制台打印时拼接颜色转义字符串，只是这里集中定义成常量
class Colors:
    """终端颜色定义（ANSI 转义序列）"""

    RESET = "\033[0m"  # 重置为默认颜色
    BOLD = "\033[1m"  # 粗体
    DIM = "\033[2m"  # 暗色/灰色（用于次要信息）

    # 前景色（即字体颜色）
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"

    # 高亮前景色
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"


class Agent:
    """单个 Agent：拥有基础工具与 MCP（外部工具协议）支持。

    一个 Agent 实例 = 一个带记忆的会话（session）。
    它持有三样核心状态：
        1. self.messages —— 完整的对话历史（上下文）
        2. self.tools    —— 该 Agent 可调用的工具表（name -> Tool 实例的字典，类似 Map<String, Tool>）
        3. self.llm      —— 用来跟大模型通信的客户端
    """

    def __init__(
        self,
        llm_client: LLMClient,
        system_prompt: str,
        tools: list[Tool],
        max_steps: int = 50,  # Agent 循环的最大步数，防止无限循环
        workspace_dir: str = "./workspace",  # 工作目录（Agent 读写文件的根目录）
        token_limit: int = 80000,  # 当 token 超过该值时触发历史摘要（压缩上下文）
    ):
        # 保存 LLM 客户端引用
        self.llm = llm_client

        # 把工具列表转成「名字 -> 工具实例」的字典，方便按名字直接查找
        # 类比 Java：Map<String, Tool> tools = list.stream().collect(toMap(Tool::name, t -> t))
        self.tools = {tool.name: tool for tool in tools}

        self.max_steps = max_steps
        self.token_limit = token_limit
        # Path 是 Python 的路径对象（类比 java.nio.file.Path），比字符串拼接更安全
        self.workspace_dir = Path(workspace_dir)

        # 取消事件：用于从外部中断 Agent 执行（例如用户按 Esc 键）
        # asyncio.Event 类似 Java 的 CountDownLatch / 一个可以被 set 的信号量标志位
        self.cancel_event: Optional[asyncio.Event] = None

        # 确保工作目录存在（parents=True 会递归创建父目录，exist_ok=True 表示已存在不报错）
        # 类比 Java：Files.createDirectories(path)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)

        # 如果系统提示词里还没有工作目录信息，就把工作目录注入进去，
        # 这样模型就知道「自己现在在哪个目录下工作」，相对路径都基于它解析
        if "Current Workspace" not in system_prompt:
            workspace_info = f"\n\n## Current Workspace\nYou are currently working in: `{self.workspace_dir.absolute()}`\nAll relative paths will be resolved relative to this directory."
            system_prompt = system_prompt + workspace_info

        self.system_prompt = system_prompt

        # 初始化消息历史：第一条永远是 system 消息（设定 Agent 的角色与规则）
        # 类比 Java：List<Message> messages = new ArrayList<>(); messages.add(systemMessage);
        self.messages: list[Message] = [Message(role="system", content=system_prompt)]

        # 初始化日志记录器（把每一步请求/响应落到日志文件，便于复盘）
        self.logger = AgentLogger()

        # 上一次 LLM API 返回的 token 用量（每次调用后更新），由服务端统计
        self.api_total_tokens: int = 0
        # 标记位：刚做完摘要后跳过一次 token 检查，避免连续触发摘要
        self._skip_next_token_check: bool = False

    def add_user_message(self, content: str):
        """向历史中追加一条用户消息（相当于用户输入一句话）。

        类比 Java：messages.add(new Message("user", content))。
        """
        self.messages.append(Message(role="user", content=content))

    def _check_cancelled(self) -> bool:
        """检查 Agent 执行是否已被取消（例如用户按了 Esc）。

        Returns:
            已取消返回 True，否则返回 False。
        """
        # cancel_event 不为空且已被 set，说明外部请求了取消
        if self.cancel_event is not None and self.cancel_event.is_set():
            return True
        return False

    def _cleanup_incomplete_messages(self):
        """清理掉「不完整」的 assistant 消息及其后续的部分工具结果。

        为什么要清理：消息历史必须保持「成对」的完整性——
        一条 assistant 消息如果声明了要调用工具，就应当有对应的 tool 结果消息。
        如果中途被取消，就可能出现「有 assistant 调用、没有工具结果」的残缺状态，
        再发给大模型会报错。因此这里只回滚「当前这一步」的残缺消息，保留已完成的步骤。
        """
        # 从后往前找最后一条 assistant 消息的下标
        last_assistant_idx = -1
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].role == "assistant":
                last_assistant_idx = i
                break

        if last_assistant_idx == -1:
            # 没找到 assistant 消息，无需清理
            return

        # 删除最后一条 assistant 消息以及它之后的所有消息（即残缺的部分）
        removed_count = len(self.messages) - last_assistant_idx
        if removed_count > 0:
            # 切片语法 messages[:n] 表示「取前 n 个元素」，起到截断/回滚的效果
            self.messages = self.messages[:last_assistant_idx]
            print(f"{Colors.DIM}   Cleaned up {removed_count} incomplete message(s){Colors.RESET}")

    def _estimate_tokens(self) -> int:
        """用 tiktoken 精确估算整个消息历史占用的 token 数。

        token 是大模型处理文本的最小单位（1 个 token 约等于 0.75 个英文单词）。
        上下文超出模型上限会导致请求失败，所以需要提前预估并在超限时压缩历史。

        使用 cl100k_base 编码器（GPT-4、Claude、M2 等主流模型兼容的编码方式）。
        """
        try:
            # 获取 cl100k_base 编码器（GPT-4 及大多数现代模型使用的编码）
            encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            # 兜底：如果 tiktoken 初始化失败（例如未安装/无网络下载词表），改用粗略估算
            return self._estimate_tokens_fallback()

        total_tokens = 0

        for msg in self.messages:
            # 累计文本内容的 token 数（content 可能是字符串，也可能是「内容块」列表）
            if isinstance(msg.content, str):
                total_tokens += len(encoding.encode(msg.content))
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        # 把字典转成字符串再编码，用于粗略计数
                        total_tokens += len(encoding.encode(str(block)))

            # 统计思维链（thinking）的 token
            if msg.thinking:
                total_tokens += len(encoding.encode(msg.thinking))

            # 统计工具调用（tool_calls）的 token
            if msg.tool_calls:
                total_tokens += len(encoding.encode(str(msg.tool_calls)))

            # 每条消息本身还有一些固定的格式开销，粗略算 4 个 token
            total_tokens += 4

        return total_tokens

    def _estimate_tokens_fallback(self) -> int:
        """兜底的 token 估算方法（当 tiktoken 不可用时使用）。

        经验值：平均约 2.5 个字符 ≈ 1 个 token。
        """
        total_chars = 0
        for msg in self.messages:
            if isinstance(msg.content, str):
                total_chars += len(msg.content)
            elif isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, dict):
                        total_chars += len(str(block))

            if msg.thinking:
                total_chars += len(msg.thinking)

            if msg.tool_calls:
                total_chars += len(str(msg.tool_calls))

        # 粗略估算：平均 2.5 个字符 = 1 个 token
        return int(total_chars / 2.5)

    async def _summarize_messages(self):
        """消息历史摘要（上下文压缩）：当 token 超限时，把「执行过程」压缩成摘要。

        核心思想（Agent 模式）：
        - 保留所有 user 消息（它们是用户的真实意图，不能丢）
        - 把每两条 user 消息之间（即一轮 Agent 执行过程）的历史压缩成一段摘要
        - 如果最后还有一轮仍在执行（有 agent/tool 消息但后面没有新的 user），也一并摘要
        - 最终消息结构变成：system -> user1 -> summary1 -> user2 -> summary2 -> user3 -> summary3(如仍在执行)

        触发条件（满足其一即可）：
        - 本地估算的 token 超过限制
        - API 服务端返回的 total_tokens 超过限制
        """
        # 如果刚做完摘要，就跳过本次检查（等下一次 LLM 调用返回后再用最新 token 判断），
        # 避免连续触发摘要造成抖动
        if self._skip_next_token_check:
            self._skip_next_token_check = False
            return

        estimated_tokens = self._estimate_tokens()

        # 本地估算与服务端上报，任意一个超限就认为需要摘要
        should_summarize = estimated_tokens > self.token_limit or self.api_total_tokens > self.token_limit

        # 两个都没超限，不需要摘要，直接返回
        if not should_summarize:
            return

        print(
            f"\n{Colors.BRIGHT_YELLOW}📊 Token usage - Local estimate: {estimated_tokens}, API reported: {self.api_total_tokens}, Limit: {self.token_limit}{Colors.RESET}"
        )
        print(f"{Colors.BRIGHT_YELLOW}🔄 Triggering message history summarization...{Colors.RESET}")

        # 找出所有 user 消息的下标（跳过下标 0 的 system 消息）
        user_indices = [i for i, msg in enumerate(self.messages) if msg.role == "user" and i > 0]

        # 至少要有 1 条 user 消息才能做摘要
        if len(user_indices) < 1:
            print(f"{Colors.BRIGHT_YELLOW}⚠️  Insufficient messages, cannot summarize{Colors.RESET}")
            return

        # 构建新的消息列表，先保留系统提示词（第 0 条）
        new_messages = [self.messages[0]]  # 保留 system prompt
        summary_count = 0

        # 逐条遍历 user 消息，对「它之后到下一个 user 之前」的执行过程做一段摘要
        for i, user_idx in enumerate(user_indices):
            # 先把当前这条 user 消息原样放进去
            new_messages.append(self.messages[user_idx])

            # 确定本轮要摘要的消息区间终点：最后一个 user 到列表末尾，否则到下一个 user 之前
            if i < len(user_indices) - 1:
                next_user_idx = user_indices[i + 1]
            else:
                next_user_idx = len(self.messages)

            # 取出本轮的执行消息（user 之后、下一个 user 之前的那些 assistant/tool 消息）
            execution_messages = self.messages[user_idx + 1 : next_user_idx]

            # 如果本轮确实有执行消息，就把它们压缩成一段摘要
            if execution_messages:
                summary_text = await self._create_summary(execution_messages, i + 1)
                if summary_text:
                    summary_message = Message(
                        role="user",
                        # 用 user 角色承载摘要：这样既保留了信息，又能被后续模型当作「已知上下文」
                        content=f"[Assistant Execution Summary]\n\n{summary_text}",
                    )
                    new_messages.append(summary_message)
                    summary_count += 1

        # 用压缩后的新列表替换旧的历史
        self.messages = new_messages

        # 跳过下一次 token 检查，避免连续触发摘要
        # （api_total_tokens 会在下一次 LLM 调用后刷新）
        self._skip_next_token_check = True

        new_tokens = self._estimate_tokens()
        print(f"{Colors.BRIGHT_GREEN}✓ Summary completed, local tokens: {estimated_tokens} → {new_tokens}{Colors.RESET}")
        print(f"{Colors.DIM}  Structure: system + {len(user_indices)} user messages + {summary_count} summaries{Colors.RESET}")
        print(f"{Colors.DIM}  Note: API token count will update on next LLM call{Colors.RESET}")

    async def _create_summary(self, messages: list[Message], round_num: int) -> str:
        """为一轮执行过程生成摘要文本（内部会再调用一次 LLM 来做「总结」）。

        Args:
            messages: 需要被摘要的消息列表
            round_num: 轮次编号（仅用于打印提示）

        Returns:
            摘要文本
        """
        if not messages:
            return ""

        # 先按「Assistant: ... / 调用了哪些工具 / 工具返回了...」的格式拼出一段原始素材
        summary_content = f"Round {round_num} execution process:\n\n"
        for msg in messages:
            if msg.role == "assistant":
                content_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                summary_content += f"Assistant: {content_text}\n"
                if msg.tool_calls:
                    tool_names = [tc.function.name for tc in msg.tool_calls]
                    summary_content += f"  → Called tools: {', '.join(tool_names)}\n"
            elif msg.role == "tool":
                result_preview = msg.content if isinstance(msg.content, str) else str(msg.content)
                summary_content += f"  ← Tool returned: {result_preview}...\n"

        # 调用 LLM 生成一段更精炼的摘要
        try:
            summary_prompt = f"""Please provide a concise summary of the following Agent execution process:

{summary_content}

Requirements:
1. Focus on what tasks were completed and which tools were called
2. Keep key execution results and important findings
3. Be concise and clear, within 1000 words
4. Use English
5. Do not include "user" related content, only summarize the Agent's execution process"""

            summary_msg = Message(role="user", content=summary_prompt)
            # 注意：这里是「套娃」——用 LLM 来压缩 LLM 的对话历史
            response = await self.llm.generate(
                messages=[
                    Message(
                        role="system",
                        content="You are an assistant skilled at summarizing Agent execution processes.",
                    ),
                    summary_msg,
                ]
            )

            summary_text = response.content
            print(f"{Colors.BRIGHT_GREEN}✓ Summary for round {round_num} generated successfully{Colors.RESET}")
            return summary_text

        except Exception as e:
            print(f"{Colors.BRIGHT_RED}✗ Summary generation failed for round {round_num}: {e}{Colors.RESET}")
            # 摘要失败也不要中断主流程：退化为直接返回原始拼接文本
            return summary_content

    async def run(self, cancel_event: Optional[asyncio.Event] = None) -> str:
        """执行 Agent 主循环，直到任务完成或达到最大步数。

        这是整个 Agent 的入口方法，强烈建议重点阅读 while 循环部分。

        Args:
            cancel_event: 可选的取消事件，可用于中断执行。
                          被 set 后，Agent 会在下一个「安全检查点」停止
                          （会在当前步骤处理完后再停，以保持消息历史一致）。

        Returns:
            最终回复内容，或错误信息（包含「被取消」的提示）。
        """
        # 设置取消事件（也可以在调用 run() 之前先设置 self.cancel_event）
        if cancel_event is not None:
            self.cancel_event = cancel_event

        # 开始一次新的运行：初始化日志文件
        self.logger.start_new_run()
        print(f"{Colors.DIM}📝 Log file: {self.logger.get_log_file_path()}{Colors.RESET}")

        step = 0  # 当前步数
        # perf_counter 是「高精度计时器」，用于统计耗时（类比 System.nanoTime()）
        run_start_time = perf_counter()

        # ============ Agent 主循环开始 ============
        while step < self.max_steps:
            # 每一步开始时先检查是否被取消
            if self._check_cancelled():
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                return cancel_msg

            step_start_time = perf_counter()
            # 检查并在必要时压缩消息历史，防止上下文溢出（token 超限）
            await self._summarize_messages()

            # 打印这一步的标题分隔框（纯展示逻辑，可略读）
            BOX_WIDTH = 58
            step_text = f"{Colors.BOLD}{Colors.BRIGHT_CYAN}💭 Step {step + 1}/{self.max_steps}{Colors.RESET}"
            step_display_width = calculate_display_width(step_text)
            padding = max(0, BOX_WIDTH - 1 - step_display_width)  # -1 是留给前面的一个空格

            print(f"\n{Colors.DIM}╭{'─' * BOX_WIDTH}╮{Colors.RESET}")
            print(f"{Colors.DIM}│{Colors.RESET} {step_text}{' ' * padding}{Colors.DIM}│{Colors.RESET}")
            print(f"{Colors.DIM}╰{'─' * BOX_WIDTH}╯{Colors.RESET}")

            # 取出本次要提供给模型的工具列表
            tool_list = list(self.tools.values())

            # 记录 LLM 请求日志，然后正式调用 LLM
            # 这一步相当于「把问题交给大模型」：历史上文 + 工具清单一起发过去
            self.logger.log_request(messages=self.messages, tools=tool_list)

            try:
                response = await self.llm.generate(messages=self.messages, tools=tool_list)
            except Exception as e:
                # 检查是否为「重试次数耗尽」错误
                from .retry import RetryExhaustedError

                if isinstance(e, RetryExhaustedError):
                    error_msg = f"LLM call failed after {e.attempts} retries\nLast error: {str(e.last_exception)}"
                    print(f"\n{Colors.BRIGHT_RED}❌ Retry failed:{Colors.RESET} {error_msg}")
                else:
                    error_msg = f"LLM call failed: {str(e)}"
                    print(f"\n{Colors.BRIGHT_RED}❌ Error:{Colors.RESET} {error_msg}")
                # LLM 调用失败直接返回错误信息，结束本次 run
                return error_msg

            # 累加服务端上报的 token 用量（用于下一步的摘要判断）
            if response.usage:
                self.api_total_tokens = response.usage.total_tokens

            # 记录 LLM 响应日志
            self.logger.log_response(
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
                finish_reason=response.finish_reason,
            )

            # 把「模型的回复」作为一条 assistant 消息追加进历史
            # 注意：无论它是否要调用工具，这条消息都要入历史
            assistant_msg = Message(
                role="assistant",
                content=response.content,
                thinking=response.thinking,
                tool_calls=response.tool_calls,
            )
            self.messages.append(assistant_msg)

            # 如果有思维链（模型内部的思考过程），打印出来
            if response.thinking:
                print(f"\n{Colors.BOLD}{Colors.MAGENTA}🧠 Thinking:{Colors.RESET}")
                print(f"{Colors.DIM}{response.thinking}{Colors.RESET}")

            # 打印模型的文本回复
            if response.content:
                print(f"\n{Colors.BOLD}{Colors.BRIGHT_BLUE}🤖 Assistant:{Colors.RESET}")
                print(f"{response.content}")

            # 关键判断：如果模型「没有请求调用任何工具」，就认为任务已完成，结束循环
            # 这也是 Agent 循环的自然退出条件
            if not response.tool_calls:
                step_elapsed = perf_counter() - step_start_time
                total_elapsed = perf_counter() - run_start_time
                print(f"\n{Colors.DIM}⏱️  Step {step + 1} completed in {step_elapsed:.2f}s (total: {total_elapsed:.2f}s){Colors.RESET}")
                return response.content

            # 执行工具前再检查一次取消
            if self._check_cancelled():
                self._cleanup_incomplete_messages()
                cancel_msg = "Task cancelled by user."
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                return cancel_msg

            # ============ 执行模型请求的所有工具调用 ============
            # 一条 assistant 消息里可能同时请求调用多个工具，所以这里是 for 循环
            for tool_call in response.tool_calls:
                tool_call_id = tool_call.id  # 工具调用 ID：用于把「结果」和「请求」对应起来
                function_name = tool_call.function.name  # 工具名
                arguments = tool_call.function.arguments  # 工具参数（已是 dict）

                # 打印工具调用标题
                print(f"\n{Colors.BRIGHT_YELLOW}🔧 Tool Call:{Colors.RESET} {Colors.BOLD}{Colors.CYAN}{function_name}{Colors.RESET}")

                # 打印参数（格式化展示，超长参数会截断，避免刷屏）
                print(f"{Colors.DIM}   Arguments:{Colors.RESET}")
                truncated_args = {}
                for key, value in arguments.items():
                    value_str = str(value)
                    if len(value_str) > 200:
                        truncated_args[key] = value_str[:200] + "..."
                    else:
                        truncated_args[key] = value
                args_json = json.dumps(truncated_args, indent=2, ensure_ascii=False)
                for line in args_json.split("\n"):
                    print(f"   {Colors.DIM}{line}{Colors.RESET}")

                # 执行工具
                if function_name not in self.tools:
                    # 模型「幻觉」出了一个不存在的工具：返回失败结果，让模型自己纠正
                    result = ToolResult(
                        success=False,
                        content="",
                        error=f"Unknown tool: {function_name}",
                    )
                else:
                    try:
                        tool = self.tools[function_name]
                        # 用 **arguments 把字典展开成关键字参数传给 execute()
                        # 类比 Java：类似用反射调用方法并传入一个 Map 作为参数
                        result = await tool.execute(**arguments)
                    except Exception as e:
                        # 工具执行过程中任何异常都不要让它冒泡中断主循环，
                        # 而是转成一个「失败的工具结果」喂回给模型，让模型决定下一步
                        import traceback

                        error_detail = f"{type(e).__name__}: {str(e)}"
                        error_trace = traceback.format_exc()
                        result = ToolResult(
                            success=False,
                            content="",
                            error=f"Tool execution failed: {error_detail}\n\nTraceback:\n{error_trace}",
                        )

                # 记录工具执行结果日志
                self.logger.log_tool_result(
                    tool_name=function_name,
                    arguments=arguments,
                    result_success=result.success,
                    result_content=result.content if result.success else None,
                    result_error=result.error if not result.success else None,
                )

                # 打印执行结果（成功/失败，超长截断）
                if result.success:
                    result_text = result.content
                    if len(result_text) > 300:
                        result_text = result_text[:300] + f"{Colors.DIM}...{Colors.RESET}"
                    print(f"{Colors.BRIGHT_GREEN}✓ Result:{Colors.RESET} {result_text}")
                else:
                    print(f"{Colors.BRIGHT_RED}✗ Error:{Colors.RESET} {Colors.RED}{result.error}{Colors.RESET}")

                # 把工具结果作为一条 role="tool" 的消息追加进历史
                # tool_call_id 必须和上面 assistant 的调用 ID 对应，模型才能正确配对
                tool_msg = Message(
                    role="tool",
                    content=result.content if result.success else f"Error: {result.error}",
                    tool_call_id=tool_call_id,
                    name=function_name,
                )
                self.messages.append(tool_msg)

                # 每执行完一个工具都检查一次是否被取消
                if self._check_cancelled():
                    self._cleanup_incomplete_messages()
                    cancel_msg = "Task cancelled by user."
                    print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {cancel_msg}{Colors.RESET}")
                    return cancel_msg

            # 一轮工具执行完毕，打印耗时
            step_elapsed = perf_counter() - step_start_time
            total_elapsed = perf_counter() - run_start_time
            print(f"\n{Colors.DIM}⏱️  Step {step + 1} completed in {step_elapsed:.2f}s (total: {total_elapsed:.2f}s){Colors.RESET}")

            # 步数 +1，进入下一轮循环
            step += 1
        # ============ Agent 主循环结束 ============

        # 达到最大步数仍未完成
        error_msg = f"Task couldn't be completed after {self.max_steps} steps."
        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  {error_msg}{Colors.RESET}")
        return error_msg

    def get_history(self) -> list[Message]:
        """获取消息历史。

        返回的是列表的「浅拷贝」（.copy()），外部修改这个列表不会影响内部状态，
        但列表里的 Message 对象本身是共享的（保持不变即可）。
        """
        return self.messages.copy()
