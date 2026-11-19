"""
Mini Agent - 交互式运行时（命令行入口模块）。

这是整个程序启动时最先执行的业务代码（命令入口）。
它负责：解析命令行参数 → 加载配置 → 创建 LLM 客户端 → 装配工具 → 创建 Agent
     → 进入「交互式对话循环」或「单次任务模式」。

对 Java 开发者：本文件相当于 Spring Boot 的启动类 Application + Controller 的合体，
但这里没有框架，全靠手写。

用法：
    mini-agent [--workspace DIR] [--task TASK]

示例：
    mini-agent                              # 用当前目录作为工作区（进入交互模式）
    mini-agent --workspace /path/to/dir     # 指定工作区目录（进入交互模式）
    mini-agent --task "create a file"       # 非交互地执行一次任务后退出
"""

import argparse
import asyncio
import platform
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import List

# prompt_toolkit：第三方库，用于实现「带历史记录/自动补全/快捷键」的高级命令行输入
# 类比 Java：类似 JLine（很多 Java CLI 都用它做交互式命令行）
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style

from mini_agent import LLMClient
from mini_agent.agent import Agent
from mini_agent.config import Config
from mini_agent.schema import LLMProvider
from mini_agent.tools.base import Tool
from mini_agent.tools.bash_tool import BashKillTool, BashOutputTool, BashTool
from mini_agent.tools.file_tools import EditTool, ReadTool, WriteTool
from mini_agent.tools.mcp_loader import cleanup_mcp_connections, load_mcp_tools_async, set_mcp_timeout_config
from mini_agent.tools.note_tool import SessionNoteTool
from mini_agent.tools.skill_tool import create_skill_tools
from mini_agent.utils import calculate_display_width


# ANSI 终端颜色码（\033[3Xm 是标准转义序列）
# 类比 Java：相当于在控制台拼接颜色转义字符串，这里集中定义成常量方便复用
class Colors:
    """终端颜色定义"""

    RESET = "\033[0m"  # 重置颜色
    BOLD = "\033[1m"  # 粗体
    DIM = "\033[2m"  # 暗色/灰色

    # 前景色（字体颜色）
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # 高亮前景色
    BRIGHT_BLACK = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    BRIGHT_WHITE = "\033[97m"

    # 背景色
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"


def get_log_directory() -> Path:
    """获取日志目录路径（Windows 下通常是 C:\\Users\\<你>\\.mini-agent\\log）。"""
    # Path.home() 是当前用户的家目录，"/" 运算符是 Path 的路径拼接语法糖
    return Path.home() / ".mini-agent" / "log"


def show_log_directory(open_file_manager: bool = True) -> None:
    """显示日志目录内容，并可选择用系统文件管理器打开它。

    Args:
        open_file_manager: 是否打开系统文件管理器
    """
    log_dir = get_log_directory()

    print(f"\n{Colors.BRIGHT_CYAN}📁 Log Directory: {log_dir}{Colors.RESET}")

    # 目录不存在就直接返回
    if not log_dir.exists() or not log_dir.is_dir():
        print(f"{Colors.RED}Log directory does not exist: {log_dir}{Colors.RESET}\n")
        return

    # glob("*.log") 是通配符匹配文件，类比 Java 的 Files.newDirectoryStream(path, "*.log")
    log_files = list(log_dir.glob("*.log"))

    if not log_files:
        print(f"{Colors.YELLOW}No log files found in directory.{Colors.RESET}\n")
        return

    # 按修改时间倒序排序（最新的排最前面）
    # lambda 是匿名函数，类比 Java 的 lambda；key 指定排序依据
    log_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)

    print(f"{Colors.DIM}{'─' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BRIGHT_YELLOW}Available Log Files (newest first):{Colors.RESET}")

    # enumerate(..., 1) 从 1 开始编号，等价于 Java 的 for (int i=1; i<=n; i++)
    for i, log_file in enumerate(log_files[:10], 1):
        mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
        size = log_file.stat().st_size
        # 大于 1024 字节就显示成 KB，否则显示原始字节数（带千分位）
        size_str = f"{size:,}" if size < 1024 else f"{size / 1024:.1f}K"
        print(f"  {Colors.GREEN}{i:2d}.{Colors.RESET} {Colors.BRIGHT_WHITE}{log_file.name}{Colors.RESET}")
        print(f"      {Colors.DIM}Modified: {mtime.strftime('%Y-%m-%d %H:%M:%S')}, Size: {size_str}{Colors.RESET}")

    if len(log_files) > 10:
        print(f"  {Colors.DIM}... and {len(log_files) - 10} more files{Colors.RESET}")

    print(f"{Colors.DIM}{'─' * 60}{Colors.RESET}")

    # 打开系统文件管理器
    if open_file_manager:
        _open_directory_in_file_manager(log_dir)

    print()


def _open_directory_in_file_manager(directory: Path) -> None:
    """在系统文件管理器中打开目录（跨平台：Windows/macOS/Linux 各不同）。"""
    system = platform.system()

    try:
        # 每写一个平台就调用对应的系统命令去打开目录
        if system == "Darwin":  # macOS
            subprocess.run(["open", str(directory)], check=False)
        elif system == "Windows":
            subprocess.run(["explorer", str(directory)], check=False)
        elif system == "Linux":
            subprocess.run(["xdg-open", str(directory)], check=False)
    except FileNotFoundError:
        print(f"{Colors.YELLOW}Could not open file manager. Please navigate manually.{Colors.RESET}")
    except Exception as e:
        print(f"{Colors.YELLOW}Error opening file manager: {e}{Colors.RESET}")


def read_log_file(filename: str) -> None:
    """读取并打印指定的日志文件内容。

    Args:
        filename: 要读取的日志文件名
    """
    log_dir = get_log_directory()
    log_file = log_dir / filename

    if not log_file.exists() or not log_file.is_file():
        print(f"\n{Colors.RED}❌ Log file not found: {log_file}{Colors.RESET}\n")
        return

    print(f"\n{Colors.BRIGHT_CYAN}📄 Reading: {log_file}{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * 80}{Colors.RESET}")

    try:
        # with 语句是 Python 的资源自动关闭语法（等价于 Java 的 try-with-resources）
        with open(log_file, "r", encoding="utf-8") as f:
            content = f.read()
        print(content)
        print(f"{Colors.DIM}{'─' * 80}{Colors.RESET}")
        print(f"\n{Colors.GREEN}✅ End of file{Colors.RESET}\n")
    except Exception as e:
        print(f"\n{Colors.RED}❌ Error reading file: {e}{Colors.RESET}\n")


def print_banner():
    """打印欢迎横幅（纯展示逻辑，用等宽字符对齐边框）。"""
    BOX_WIDTH = 58
    banner_text = f"{Colors.BOLD}🤖 Mini Agent - Multi-turn Interactive Session{Colors.RESET}"
    # 因为中文/emoji 在终端占 2 个宽度，所以需要专门计算「显示宽度」才能对齐
    banner_width = calculate_display_width(banner_text)

    # 计算左右留白，让文字居中
    total_padding = BOX_WIDTH - banner_width
    left_padding = total_padding // 2  # "//" 是整数除法
    right_padding = total_padding - left_padding

    print()
    print(f"{Colors.BOLD}{Colors.BRIGHT_CYAN}╔{'═' * BOX_WIDTH}╗{Colors.RESET}")
    print(
        f"{Colors.BOLD}{Colors.BRIGHT_CYAN}║{Colors.RESET}{' ' * left_padding}{banner_text}{' ' * right_padding}{Colors.BOLD}{Colors.BRIGHT_CYAN}║{Colors.RESET}"
    )
    print(f"{Colors.BOLD}{Colors.BRIGHT_CYAN}╚{'═' * BOX_WIDTH}╝{Colors.RESET}")
    print()


def print_help():
    """打印帮助信息（列出所有斜杠命令与快捷键）。"""
    # f"""...""" 是「多行 f-string」，其中的 {表达式} 会被替换成变量值
    help_text = f"""
{Colors.BOLD}{Colors.BRIGHT_YELLOW}Available Commands:{Colors.RESET}
  {Colors.BRIGHT_GREEN}/help{Colors.RESET}      - Show this help message
  {Colors.BRIGHT_GREEN}/clear{Colors.RESET}     - Clear session history (keep system prompt)
  {Colors.BRIGHT_GREEN}/history{Colors.RESET}   - Show current session message count
  {Colors.BRIGHT_GREEN}/stats{Colors.RESET}     - Show session statistics
  {Colors.BRIGHT_GREEN}/log{Colors.RESET}       - Show log directory and recent files
  {Colors.BRIGHT_GREEN}/log <file>{Colors.RESET} - Read a specific log file
  {Colors.BRIGHT_GREEN}/exit{Colors.RESET}      - Exit program (also: exit, quit, q)

{Colors.BOLD}{Colors.BRIGHT_YELLOW}Keyboard Shortcuts:{Colors.RESET}
  {Colors.BRIGHT_CYAN}Esc{Colors.RESET}        - Cancel current agent execution
  {Colors.BRIGHT_CYAN}Ctrl+C{Colors.RESET}     - Exit program
  {Colors.BRIGHT_CYAN}Ctrl+U{Colors.RESET}     - Clear current input line
  {Colors.BRIGHT_CYAN}Ctrl+L{Colors.RESET}     - Clear screen
  {Colors.BRIGHT_CYAN}Ctrl+J{Colors.RESET}     - Insert newline (also Ctrl+Enter)
  {Colors.BRIGHT_CYAN}Tab{Colors.RESET}        - Auto-complete commands
  {Colors.BRIGHT_CYAN}↑/↓{Colors.RESET}        - Browse command history
  {Colors.BRIGHT_CYAN}→{Colors.RESET}          - Accept auto-suggestion

{Colors.BOLD}{Colors.BRIGHT_YELLOW}Usage:{Colors.RESET}
  - Enter your task directly, Agent will help you complete it
  - Agent remembers all conversation content in this session
  - Use {Colors.BRIGHT_GREEN}/clear{Colors.RESET} to start a new session
  - Press {Colors.BRIGHT_CYAN}Enter{Colors.RESET} to submit your message
  - Use {Colors.BRIGHT_CYAN}Ctrl+J{Colors.RESET} to insert line breaks within your message
"""
    print(help_text)


def print_session_info(agent: Agent, workspace_dir: Path, model: str):
    """打印会话信息框（模型、工作区、消息数、可用工具数）。"""

    # 这是一个「嵌套函数」（定义在函数内部的函数），类比 Java 的局部 lambda/私有方法
    BOX_WIDTH = 58

    def print_info_line(text: str):
        """打印单行信息并补齐右侧空格使其对齐"""
        # -1 是留给行首的一个空格
        text_width = calculate_display_width(text)
        padding = max(0, BOX_WIDTH - 1 - text_width)
        print(f"{Colors.DIM}│{Colors.RESET} {text}{' ' * padding}{Colors.DIM}│{Colors.RESET}")

    # 上边框
    print(f"{Colors.DIM}┌{'─' * BOX_WIDTH}┐{Colors.RESET}")

    # 居中标题
    header_text = f"{Colors.BRIGHT_CYAN}Session Info{Colors.RESET}"
    header_width = calculate_display_width(header_text)
    header_padding_total = BOX_WIDTH - 1 - header_width  # -1 是行首空格
    header_padding_left = header_padding_total // 2
    header_padding_right = header_padding_total - header_padding_left
    print(f"{Colors.DIM}│{Colors.RESET} {' ' * header_padding_left}{header_text}{' ' * header_padding_right}{Colors.DIM}│{Colors.RESET}")

    # 分隔线
    print(f"{Colors.DIM}├{'─' * BOX_WIDTH}┤{Colors.RESET}")

    # 信息行：注意 agent.messages / agent.tools 都是 Agent 对象的属性（类似 getter）
    print_info_line(f"Model: {model}")
    print_info_line(f"Workspace: {workspace_dir}")
    print_info_line(f"Message History: {len(agent.messages)} messages")
    print_info_line(f"Available Tools: {len(agent.tools)} tools")

    # 下边框
    print(f"{Colors.DIM}└{'─' * BOX_WIDTH}┘{Colors.RESET}")
    print()
    print(f"{Colors.DIM}Type {Colors.BRIGHT_GREEN}/help{Colors.DIM} for help, {Colors.BRIGHT_GREEN}/exit{Colors.DIM} to quit{Colors.RESET}")
    print()


def print_stats(agent: Agent, session_start: datetime):
    """打印会话统计信息（时长、各角色消息数、可用工具数、token 用量）。"""
    duration = datetime.now() - session_start
    # divmod 同时返回「商和余数」，这里用来把总秒数拆成 时:分:秒
    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    # 分别统计不同角色的消息条数（生成器表达式求和，类比 Java Stream 的 filter+count）
    user_msgs = sum(1 for m in agent.messages if m.role == "user")
    assistant_msgs = sum(1 for m in agent.messages if m.role == "assistant")
    tool_msgs = sum(1 for m in agent.messages if m.role == "tool")

    print(f"\n{Colors.BOLD}{Colors.BRIGHT_CYAN}Session Statistics:{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * 40}{Colors.RESET}")
    print(f"  Session Duration: {hours:02d}:{minutes:02d}:{seconds:02d}")
    print(f"  Total Messages: {len(agent.messages)}")
    print(f"    - User Messages: {Colors.BRIGHT_GREEN}{user_msgs}{Colors.RESET}")
    print(f"    - Assistant Replies: {Colors.BRIGHT_BLUE}{assistant_msgs}{Colors.RESET}")
    print(f"    - Tool Calls: {Colors.BRIGHT_YELLOW}{tool_msgs}{Colors.RESET}")
    print(f"  Available Tools: {len(agent.tools)}")
    # "{:,}" 表示千分位格式化，例如 12345 -> 12,345
    if agent.api_total_tokens > 0:
        print(f"  API Tokens Used: {Colors.BRIGHT_MAGENTA}{agent.api_total_tokens:,}{Colors.RESET}")
    print(f"{Colors.DIM}{'─' * 40}{Colors.RESET}\n")


def parse_args() -> argparse.Namespace:
    """解析命令行参数。

    argparse 是 Python 标准库，类比 Java 的 picocli / commons-cli。

    Returns:
        解析后的参数对象（可用 args.workspace / args.task 等访问）
    """
    parser = argparse.ArgumentParser(
        description="Mini Agent - AI assistant with file tools and MCP support",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # epilog 是显示在帮助信息末尾的补充说明
        epilog="""
Examples:
  mini-agent                              # Use current directory as workspace
  mini-agent --workspace /path/to/dir     # Use specific workspace directory
  mini-agent log                          # Show log directory and recent files
  mini-agent log agent_run_xxx.log        # Read a specific log file
        """,
    )
    # add_argument 注册一个参数；--workspace 和 -w 是同一参数的两种写法
    parser.add_argument(
        "--workspace",
        "-w",
        type=str,
        default=None,
        help="Workspace directory (default: current directory)",
    )
    parser.add_argument(
        "--task",
        "-t",
        type=str,
        default=None,
        help="Execute a task non-interactively and exit",
    )
    parser.add_argument(
        "--version",
        "-v",
        action="version",  # 打印版本后直接退出
        version="mini-agent 0.1.0",
    )

    # 子命令（类似 git 的 status/log 这种二级命令）
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # log 子命令：mini-agent log [filename]
    log_parser = subparsers.add_parser("log", help="Show log directory or read log files")
    log_parser.add_argument(
        "filename",
        nargs="?",  # 可选的位置参数（可传可不传）
        default=None,
        help="Log filename to read (optional, shows directory if omitted)",
    )

    return parser.parse_args()


async def initialize_base_tools(config: Config):
    """初始化「与工作目录无关」的基础工具。

    这些工具从包配置加载，不依赖具体工作目录。
    注意：文件类工具依赖工作目录，因此放在 add_workspace_tools() 里初始化。

    Args:
        config: 配置对象

    Returns:
        (工具列表, skill_loader 或 None) 的元组
    """

    tools = []
    skill_loader = None

    # 1. Bash 辅助工具（用于查看后台命令输出、终止命令）
    # 注意：BashTool 本体在 add_workspace_tools() 中创建（因为它需要以工作目录作为 cwd）
    if config.tools.enable_bash:
        bash_output_tool = BashOutputTool()
        tools.append(bash_output_tool)
        print(f"{Colors.GREEN}✅ Loaded Bash Output tool{Colors.RESET}")

        bash_kill_tool = BashKillTool()
        tools.append(bash_kill_tool)
        print(f"{Colors.GREEN}✅ Loaded Bash Kill tool{Colors.RESET}")

    # 3. Claude Skills（从包目录加载）
    if config.tools.enable_skills:
        print(f"{Colors.BRIGHT_CYAN}Loading Claude Skills...{Colors.RESET}")
        try:
            # 解析 skills 目录，按优先级查找
            # expanduser() 会把路径里的 ~ 展开成用户家目录
            skills_path = Path(config.tools.skills_dir).expanduser()
            if skills_path.is_absolute():
                skills_dir = str(skills_path)
            else:
                # 按优先级依次查找：
                # 1. 当前目录（开发模式：./skills 或 ./mini_agent/skills）
                # 2. 包目录（已安装：site-packages/mini_agent/skills）
                search_paths = [
                    skills_path,  # ./skills（向后兼容）
                    Path("mini_agent") / skills_path,  # ./mini_agent/skills
                    Config.get_package_dir() / skills_path,  # site-packages/mini_agent/skills
                ]

                # 找到第一个存在的路径就停止
                skills_dir = str(skills_path)  # 默认值
                for path in search_paths:
                    if path.exists():
                        skills_dir = str(path.resolve())
                        break

            # 根据目录创建 skill 工具（返回值是两个：工具列表 + 加载器）
            skill_tools, skill_loader = create_skill_tools(skills_dir)
            if skill_tools:
                tools.extend(skill_tools)  # extend 相当于 Java 的 list.addAll()
                print(f"{Colors.GREEN}✅ Loaded Skill tool (get_skill){Colors.RESET}")
            else:
                print(f"{Colors.YELLOW}⚠️  No available Skills found{Colors.RESET}")
        except Exception as e:
            print(f"{Colors.YELLOW}⚠️  Failed to load Skills: {e}{Colors.RESET}")

    # 4. MCP 工具（按优先级查找配置并加载）
    if config.tools.enable_mcp:
        print(f"{Colors.BRIGHT_CYAN}Loading MCP tools...{Colors.RESET}")
        try:
            # 应用 config.yaml 里的 MCP 超时配置
            mcp_config = config.tools.mcp
            set_mcp_timeout_config(
                connect_timeout=mcp_config.connect_timeout,
                execute_timeout=mcp_config.execute_timeout,
                sse_read_timeout=mcp_config.sse_read_timeout,
            )
            print(
                f"{Colors.DIM}  MCP timeouts: connect={mcp_config.connect_timeout}s, "
                f"execute={mcp_config.execute_timeout}s, sse_read={mcp_config.sse_read_timeout}s{Colors.RESET}"
            )

            # 按优先级查找 mcp.json
            mcp_config_path = Config.find_config_file(config.tools.mcp_config_path)
            if mcp_config_path:
                # await 表示等待一个异步操作完成（这里要连接外部 MCP 服务，属于 IO 操作）
                mcp_tools = await load_mcp_tools_async(str(mcp_config_path))
                if mcp_tools:
                    tools.extend(mcp_tools)
                    print(f"{Colors.GREEN}✅ Loaded {len(mcp_tools)} MCP tools (from: {mcp_config_path}){Colors.RESET}")
                else:
                    print(f"{Colors.YELLOW}⚠️  No available MCP tools found{Colors.RESET}")
            else:
                print(f"{Colors.YELLOW}⚠️  MCP config file not found: {config.tools.mcp_config_path}{Colors.RESET}")
        except Exception as e:
            print(f"{Colors.YELLOW}⚠️  Failed to load MCP tools: {e}{Colors.RESET}")

    print()  # 空行分隔
    return tools, skill_loader


def add_workspace_tools(tools: List[Tool], config: Config, workspace_dir: Path):
    """添加「依赖工作目录」的工具。

    这些工具需要知道工作目录在哪里（例如文件操作要基于工作目录解析相对路径）。

    Args:
        tools: 已有的工具列表（会在上面追加）
        config: 配置对象
        workspace_dir: 工作目录路径
    """
    # 确保工作目录存在（parents=True 递归创建父目录，exist_ok=True 已存在不报错）
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Bash 工具 —— 需要把工作目录作为命令执行的 cwd
    if config.tools.enable_bash:
        bash_tool = BashTool(workspace_dir=str(workspace_dir))
        tools.append(bash_tool)
        print(f"{Colors.GREEN}✅ Loaded Bash tool (cwd: {workspace_dir}){Colors.RESET}")

    # 文件工具 —— 需要工作目录来解析相对路径
    if config.tools.enable_file_tools:
        tools.extend(
            [
                ReadTool(workspace_dir=str(workspace_dir)),
                WriteTool(workspace_dir=str(workspace_dir)),
                EditTool(workspace_dir=str(workspace_dir)),
            ]
        )
        print(f"{Colors.GREEN}✅ Loaded file operation tools (workspace: {workspace_dir}){Colors.RESET}")

    # 会话记忆工具 —— 需要在工作目录下保存记忆文件
    if config.tools.enable_note:
        tools.append(SessionNoteTool(memory_file=str(workspace_dir / ".agent_memory.json")))
        print(f"{Colors.GREEN}✅ Loaded session note tool{Colors.RESET}")


async def _quiet_cleanup():
    """清理 MCP 连接，并屏蔽退出时 anyio/mcp 产生的噪音堆栈信息。"""
    # 屏蔽 anyio/mcp 在 stdio_client 任务组跨任务销毁时抛出的 asyncgen 终结噪音。
    # 这里故意「不恢复」原来的异常处理器：asyncgen 终结发生在 asyncio.run() 收尾阶段
    # （即在 run_agent 返回之后），恢复处理器仍会让噪音冒出来。
    # 由于这段代码紧挨着进程退出执行，吞掉迟到的异常是安全的。
    loop = asyncio.get_event_loop()
    loop.set_exception_handler(lambda _loop, _ctx: None)
    try:
        await cleanup_mcp_connections()
    except Exception:
        pass


async def run_agent(workspace_dir: Path, task: str = None):
    """运行 Agent（交互模式或非交互模式）。

    Args:
        workspace_dir: 工作目录路径
        task: 若提供，则执行该任务后退出（非交互模式）
    """
    session_start = datetime.now()

    # 1. 从包目录加载配置
    config_path = Config.get_default_config_path()

    # 配置文件不存在时，打印一段「如何配置」的引导后直接返回
    if not config_path.exists():
        print(f"{Colors.RED}❌ Configuration file not found{Colors.RESET}")
        print()
        print(f"{Colors.BRIGHT_CYAN}📦 Configuration Search Path:{Colors.RESET}")
        print(f"  {Colors.DIM}1) mini_agent/config/config.yaml{Colors.RESET} (development)")
        print(f"  {Colors.DIM}2) ~/.mini-agent/config/config.yaml{Colors.RESET} (user)")
        print(f"  {Colors.DIM}3) <package>/config/config.yaml{Colors.RESET} (installed)")
        print()
        print(f"{Colors.BRIGHT_YELLOW}🚀 Quick Setup (Recommended):{Colors.RESET}")
        print(
            f"  {Colors.BRIGHT_GREEN}curl -fsSL https://raw.githubusercontent.com/MiniMax-AI/Mini-Agent/main/scripts/setup-config.sh | bash{Colors.RESET}"
        )
        print()
        print(f"{Colors.DIM}  This will automatically:{Colors.RESET}")
        print(f"{Colors.DIM}    • Create ~/.mini-agent/config/{Colors.RESET}")
        print(f"{Colors.DIM}    • Download configuration files{Colors.RESET}")
        print(f"{Colors.DIM}    • Guide you to add your API Key{Colors.RESET}")
        print()
        print(f"{Colors.BRIGHT_YELLOW}📝 Manual Setup:{Colors.RESET}")
        user_config_dir = Path.home() / ".mini-agent" / "config"
        example_config = Config.get_package_dir() / "config" / "config-example.yaml"
        print(f"  {Colors.DIM}mkdir -p {user_config_dir}{Colors.RESET}")
        print(f"  {Colors.DIM}cp {example_config} {user_config_dir}/config.yaml{Colors.RESET}")
        print(f"  {Colors.DIM}# Then edit {user_config_dir}/config.yaml to add your API Key{Colors.RESET}")
        print()
        return

    # 读取并解析配置；不同异常给出不同提示
    try:
        config = Config.from_yaml(config_path)
    except FileNotFoundError:
        print(f"{Colors.RED}❌ Error: Configuration file not found: {config_path}{Colors.RESET}")
        return
    except ValueError as e:
        print(f"{Colors.RED}❌ Error: {e}{Colors.RESET}")
        print(f"{Colors.YELLOW}Please check the configuration file format{Colors.RESET}")
        return
    except Exception as e:
        print(f"{Colors.RED}❌ Error: Failed to load configuration file: {e}{Colors.RESET}")
        return

    # 2. 初始化 LLM 客户端
    # 在函数内部 import 是 Python 允许的写法（延迟导入，避免循环依赖）
    from mini_agent.retry import RetryConfig as RetryConfigBase

    # 把配置文件里的重试配置转换成重试模块需要的格式
    retry_config = RetryConfigBase(
        enabled=config.llm.retry.enabled,
        max_retries=config.llm.retry.max_retries,
        initial_delay=config.llm.retry.initial_delay,
        max_delay=config.llm.retry.max_delay,
        exponential_base=config.llm.retry.exponential_base,
        # 元组里写 Exception 表示「所有异常都重试」
        retryable_exceptions=(Exception,),
    )

    # 定义重试回调函数：每次重试时在终端打印提示
    def on_retry(exception: Exception, attempt: int):
        """重试回调函数，用于在终端显示重试信息"""
        print(f"\n{Colors.BRIGHT_YELLOW}⚠️  LLM call failed (attempt {attempt}): {str(exception)}{Colors.RESET}")
        next_delay = retry_config.calculate_delay(attempt - 1)
        print(f"{Colors.DIM}   Retrying in {next_delay:.1f}s (attempt {attempt + 1})...{Colors.RESET}")

    # 把配置里的 provider 字符串转成枚举（三元表达式：A if 条件 else B）
    provider = LLMProvider.ANTHROPIC if config.llm.provider.lower() == "anthropic" else LLMProvider.OPENAI

    # 创建 LLM 客户端（这是 Agent 与模型通信的通道）
    llm_client = LLMClient(
        api_key=config.llm.api_key,
        provider=provider,
        api_base=config.llm.api_base,
        model=config.llm.model,
        retry_config=retry_config if config.llm.retry.enabled else None,
    )

    # 设置重试回调
    if config.llm.retry.enabled:
        llm_client.retry_callback = on_retry
        print(f"{Colors.GREEN}✅ LLM retry mechanism enabled (max {config.llm.retry.max_retries} retries){Colors.RESET}")

    # 3. 初始化基础工具（与工作目录无关）
    tools, skill_loader = await initialize_base_tools(config)

    # 4. 添加依赖工作目录的工具
    add_workspace_tools(tools, config, workspace_dir)

    # 5. 加载系统提示词（按优先级查找）
    system_prompt_path = Config.find_config_file(config.agent.system_prompt_path)
    if system_prompt_path and system_prompt_path.exists():
        system_prompt = system_prompt_path.read_text(encoding="utf-8")
        print(f"{Colors.GREEN}✅ Loaded system prompt (from: {system_prompt_path}){Colors.RESET}")
    else:
        # 找不到就用一个默认提示词兜底
        system_prompt = "You are Mini-Agent, an intelligent assistant powered by MiniMax M2.5 that can help users complete various tasks."
        print(f"{Colors.YELLOW}⚠️  System prompt not found, using default{Colors.RESET}")

    # 6. 把 Skills 元数据注入系统提示词（渐进式披露 - 第 1 层）
    if skill_loader:
        skills_metadata = skill_loader.get_skills_metadata_prompt()
        if skills_metadata:
            # 用真实的 skills 元数据替换提示词里的占位符
            system_prompt = system_prompt.replace("{SKILLS_METADATA}", skills_metadata)
            print(f"{Colors.GREEN}✅ Injected {len(skill_loader.loaded_skills)} skills metadata into system prompt{Colors.RESET}")
        else:
            # 没有 skills 就把占位符替换成空串
            system_prompt = system_prompt.replace("{SKILLS_METADATA}", "")
    else:
        # 未启用 skills 时同样移除占位符
        system_prompt = system_prompt.replace("{SKILLS_METADATA}", "")

    # 7. 创建 Agent（把前面准备好的 llm、提示词、工具都交给它）
    agent = Agent(
        llm_client=llm_client,
        system_prompt=system_prompt,
        tools=tools,
        max_steps=config.agent.max_steps,
        workspace_dir=str(workspace_dir),
    )

    # 8. 显示欢迎信息（只在交互模式显示）
    if not task:
        print_banner()
        print_session_info(agent, workspace_dir, config.llm.model)

    # 8.5 非交互模式：执行任务后退出
    if task:
        print(f"\n{Colors.BRIGHT_BLUE}Agent{Colors.RESET} {Colors.DIM}›{Colors.RESET} {Colors.DIM}Executing task...{Colors.RESET}\n")
        agent.add_user_message(task)
        try:
            # 这里就是调用 Agent 的核心循环（见 agent.py 的 run 方法）
            await agent.run()
        except Exception as e:
            print(f"\n{Colors.RED}❌ Error: {e}{Colors.RESET}")
        finally:
            # finally 块无论是否异常都会执行
            print_stats(agent, session_start)

        # 清理 MCP 连接
        await _quiet_cleanup()
        return

    # 9. 配置 prompt_toolkit 输入会话
    # 命令自动补全器（输入 / 开头时会提示这些命令）
    command_completer = WordCompleter(
        ["/help", "/clear", "/history", "/stats", "/log", "/exit", "/quit", "/q"],
        ignore_case=True,
        sentence=True,
    )

    # 输入提示符的自定义样式（绿色加粗）
    prompt_style = Style.from_dict(
        {
            "prompt": "#00ff00 bold",  # 绿色加粗
            "separator": "#666666",  # 灰色
        }
    )

    # 自定义快捷键绑定
    kb = KeyBindings()

    @kb.add("c-u")  # Ctrl+U：清空当前输入行
    def _(event):
        """清空当前输入行"""
        event.current_buffer.reset()

    @kb.add("c-l")  # Ctrl+L：清屏
    def _(event):
        """清屏"""
        event.app.renderer.clear()

    @kb.add("c-j")  # Ctrl+J（对应 Ctrl+Enter）
    def _(event):
        """插入换行符"""
        event.current_buffer.insert_text("\n")

    # 创建输入会话（带历史记录和自动建议）
    # 用 FileHistory 把历史持久化到用户家目录，重启后仍可 ↑/↓ 翻历史
    history_file = Path.home() / ".mini-agent" / ".history"
    history_file.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        history=FileHistory(str(history_file)),
        auto_suggest=AutoSuggestFromHistory(),
        completer=command_completer,
        style=prompt_style,
        key_bindings=kb,
    )

    # 10. 交互式主循环（程序停在这里等你不断输入）
    while True:
        try:
            # 异步等待用户输入（await session.prompt_async 就是「阻塞等待键盘输入」）
            user_input = await session.prompt_async(
                [
                    ("class:prompt", "You"),
                    ("", " › "),
                ],
                multiline=False,
                enable_history_search=True,
            )
            user_input = user_input.strip()

            # 空输入直接跳过
            if not user_input:
                continue

            # 处理斜杠命令
            if user_input.startswith("/"):
                command = user_input.lower()

                if command in ["/exit", "/quit", "/q"]:
                    print(f"\n{Colors.BRIGHT_YELLOW}👋 Goodbye! Thanks for using Mini Agent{Colors.RESET}\n")
                    print_stats(agent, session_start)
                    break

                elif command == "/help":
                    print_help()
                    continue

                elif command == "/clear":
                    # 清空消息历史，但保留系统提示词（相当于开启新会话）
                    old_count = len(agent.messages)
                    agent.messages = [agent.messages[0]]  # 只保留 system 消息
                    print(f"{Colors.GREEN}✅ Cleared {old_count - 1} messages, starting new session{Colors.RESET}\n")
                    continue

                elif command == "/history":
                    print(f"\n{Colors.BRIGHT_CYAN}Current session message count: {len(agent.messages)}{Colors.RESET}\n")
                    continue

                elif command == "/stats":
                    print_stats(agent, session_start)
                    continue

                elif command == "/log" or command.startswith("/log "):
                    # 解析 /log 命令：无参数则显示日志目录，有参数则读指定日志文件
                    parts = user_input.split(maxsplit=1)  # 最多切一刀
                    if len(parts) == 1:
                        # /log —— 显示日志目录
                        show_log_directory(open_file_manager=True)
                    else:
                        # /log <filename> —— 读取指定日志文件
                        filename = parts[1].strip("\"'")  # 去掉首尾引号
                        read_log_file(filename)
                    continue

                else:
                    print(f"{Colors.RED}❌ Unknown command: {user_input}{Colors.RESET}")
                    print(f"{Colors.DIM}Type /help to see available commands{Colors.RESET}\n")
                    continue

            # 普通对话 —— 退出检查
            if user_input.lower() in ["exit", "quit", "q"]:
                print(f"\n{Colors.BRIGHT_YELLOW}👋 Goodbye! Thanks for using Mini Agent{Colors.RESET}\n")
                print_stats(agent, session_start)
                break

            # 运行 Agent，并支持按 Esc 取消
            print(
                f"\n{Colors.BRIGHT_BLUE}Agent{Colors.RESET} {Colors.DIM}›{Colors.RESET} {Colors.DIM}Thinking... (Esc to cancel){Colors.RESET}\n"
            )
            agent.add_user_message(user_input)

            # 创建取消事件（Agent 侧通过检查它来决定是否中断）
            cancel_event = asyncio.Event()
            agent.cancel_event = cancel_event

            # Esc 按键监听线程（因为主线程要跑 Agent，所以用独立线程监听键盘）
            esc_listener_stop = threading.Event()
            esc_cancelled = [False]  # 用列表做可变容器，方便跨线程读写

            def esc_key_listener():
                """在独立线程里监听 Esc 键。"""
                if platform.system() == "Windows":
                    try:
                        # msvcrt 是 Windows 专属的标准库，用于无阻塞读键盘
                        import msvcrt

                        while not esc_listener_stop.is_set():
                            if msvcrt.kbhit():  # 有按键按下
                                char = msvcrt.getch()
                                if char == b"\x1b":  # b"\x1b" 就是 Esc 的字节码
                                    print(f"\n{Colors.BRIGHT_YELLOW}⏹️  Esc pressed, cancelling...{Colors.RESET}")
                                    esc_cancelled[0] = True
                                    cancel_event.set()
                                    break
                            esc_listener_stop.wait(0.05)  # 每 50ms 轮询一次
                    except Exception:
                        pass
                    return

                # Unix/macOS 分支
                try:
                    import select
                    import termios
                    import tty

                    fd = sys.stdin.fileno()
                    old_settings = termios.tcgetattr(fd)

                    try:
                        tty.setcbreak(fd)  # 切换到「单字符读取」模式
                        while not esc_listener_stop.is_set():
                            rlist, _, _ = select.select([sys.stdin], [], [], 0.05)
                            if rlist:
                                char = sys.stdin.read(1)
                                if char == "\x1b":  # Esc
                                    print(f"\n{Colors.BRIGHT_YELLOW}⏹️  Esc pressed, cancelling...{Colors.RESET}")
                                    esc_cancelled[0] = True
                                    cancel_event.set()
                                    break
                    finally:
                        # 无论如何都恢复原来的终端设置
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass

            # 启动 Esc 监听线程（daemon=True 表示主程序退出时该线程自动结束）
            esc_thread = threading.Thread(target=esc_key_listener, daemon=True)
            esc_thread.start()

            # 运行 Agent，并周期性检查取消
            try:
                # create_task 把协程提交到事件循环后台执行（类比 Java 的 submit 到一个 Executor）
                agent_task = asyncio.create_task(agent.run())

                # 轮询取消状态，直到 Agent 任务结束
                while not agent_task.done():
                    if esc_cancelled[0]:
                        cancel_event.set()
                    await asyncio.sleep(0.1)  # 每 100ms 检查一次

                # 取出结果（若内部抛异常，这里会重新抛出）
                _ = agent_task.result()

            except asyncio.CancelledError:
                print(f"\n{Colors.BRIGHT_YELLOW}⚠️  Agent execution cancelled{Colors.RESET}")
            finally:
                # 清理：重置取消事件、停止监听线程
                agent.cancel_event = None
                esc_listener_stop.set()
                esc_thread.join(timeout=0.2)

            # 视觉分隔线
            print(f"\n{Colors.DIM}{'─' * 60}{Colors.RESET}\n")

        except KeyboardInterrupt:
            # 捕获 Ctrl+C
            print(f"\n\n{Colors.BRIGHT_YELLOW}👋 Interrupt signal detected, exiting...{Colors.RESET}\n")
            print_stats(agent, session_start)
            break

        except Exception as e:
            # 兜底捕获：出现任何异常都打印出来，但不退出程序
            print(f"\n{Colors.RED}❌ Error: {e}{Colors.RESET}")
            print(f"{Colors.DIM}{'─' * 60}{Colors.RESET}\n")

    # 11. 退出前清理 MCP 连接
    await _quiet_cleanup()


def main():
    """CLI 主入口（mini-agent 命令最终调用这里）。"""
    # 解析命令行参数
    args = parse_args()

    # 处理 log 子命令
    if args.command == "log":
        if args.filename:
            read_log_file(args.filename)
        else:
            show_log_directory(open_file_manager=True)
        return

    # 确定工作目录
    # expanduser() 展开 ~ 为用户家目录，让路径更通用
    if args.workspace:
        workspace_dir = Path(args.workspace).expanduser().absolute()
    else:
        # 默认用当前工作目录
        workspace_dir = Path.cwd()

    # 确保工作目录存在
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # 运行 Agent（配置始终从包目录加载）
    # asyncio.run 会创建事件循环并运行协程，等价于「启动一个异步 runtime」
    asyncio.run(run_agent(workspace_dir, task=args.task))


# 仅当直接运行本文件时执行（类比 Java 的 public static void main）
if __name__ == "__main__":
    main()
