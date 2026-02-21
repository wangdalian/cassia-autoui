"""
Agent 终端交互入口

提供多轮对话界面，实时显示 LLM 推理过程和工具调用。

使用方式:
    python -m agent.cli                    # 使用默认配置
    python -m agent.cli --config my.json   # 使用自定义配置
"""

import argparse
import json
import logging
import os
import sys
import time

from playwright.sync_api import sync_playwright
from prompt_toolkit import PromptSession
from rich.console import Console
from rich.markdown import Markdown
from rich.theme import Theme

# Cassia 品牌色主题
# Primary: #068466 (teal)  Accent: #FC9F5B (coral)  Dark: #001E42 (navy)
_cassia_theme = Theme({
    "markdown.h1": "bold #068466",
    "markdown.h2": "bold #068466",
    "markdown.h3": "#068466",
    "markdown.h4": "#068466",
    "markdown.strong": "bold #FC9F5B",
    "markdown.emph": "italic #FC9F5B",
    "markdown.link": "#068466 underline",
    "markdown.code": "#FC9F5B",
    "markdown.item.bullet": "#068466",
    "markdown.item.number": "#068466",
    "markdown.hr": "#068466",
})

# Rich 终端 (用于渲染 LLM 输出的 Markdown)
_console = Console(theme=_cassia_theme)

# 添加项目根目录到 sys.path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.config import load_config, apply_log_level
from lib.browser import BrowserManager
from agent.core import CassiaAgent

logger = logging.getLogger("cassia")

# ============================================================
# 日志配置
# ============================================================

_log_handler = logging.StreamHandler()
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
))
logger.addHandler(_log_handler)
logger.setLevel(logging.INFO)


# ============================================================
# 终端颜色
# ============================================================

class Colors:
    """ANSI 终端颜色"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"

    @staticmethod
    def enabled() -> bool:
        """检查终端是否支持颜色"""
        return hasattr(sys.stdout, 'isatty') and sys.stdout.isatty()


def cprint(text: str, color: str = "", bold: bool = False):
    """打印带颜色的文本"""
    if Colors.enabled():
        prefix = ""
        if bold:
            prefix += Colors.BOLD
        if color:
            prefix += color
        suffix = Colors.RESET if (prefix) else ""
        print(f"{prefix}{text}{suffix}")
    else:
        print(text)


# ============================================================
# 回调函数
# ============================================================

def on_thinking(text: str):
    """LLM 思考完成回调 (非流式 fallback 时使用) — 渲染完整 Markdown"""
    print()
    _console.print(Markdown(text))


_streamed_content = False  # 标记本轮是否已有流式输出


def on_thinking_chunk(text: str):
    """LLM 流式思考回调 — 逐字输出到终端 (原始文本，不渲染 Markdown)"""
    global _streamed_content
    _streamed_content = True
    _console.file.flush()  # 先刷新 Rich 的缓冲，防止与 stdout 交错
    sys.stdout.write(text)
    sys.stdout.flush()


def on_tool_call(tool_name: str, args: dict, result: str):
    """工具调用回调"""
    # done 工具的结果由主流程渲染为最终输出，不在这里重复
    if tool_name == "done":
        return

    args_str = json.dumps(args, ensure_ascii=False)
    if len(args_str) > 120:
        args_str = args_str[:117] + "..."

    cprint(f"  -> {tool_name}({args_str})", Colors.CYAN)

    # 智能摘要: JSON 数组显示条数，其他截断
    result_stripped = result.strip()
    if result_stripped.startswith("["):
        try:
            count = len(json.loads(result_stripped))
            result_preview = f"(返回 {count} 条数据)"
        except (json.JSONDecodeError, ValueError):
            result_preview = result[:200] + "..." if len(result) > 200 else result
    elif len(result) > 300:
        result_preview = result[:297] + "..."
    else:
        result_preview = result
    cprint(f"  <- {result_preview}", Colors.DIM)


# ============================================================
# 主流程
# ============================================================

def get_default_config_path() -> str:
    """获取默认配置文件路径"""
    return os.path.join(PROJECT_ROOT, "agent", "config.json")


def main():
    parser = argparse.ArgumentParser(
        description="Cassia AC 管理平台 AI Agent (终端交互模式)",
    )
    parser.add_argument(
        "--config", "-c",
        type=str,
        default=get_default_config_path(),
        help="配置文件路径 (默认: agent/config.json)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="启用调试日志",
    )

    args = parser.parse_args()

    # 加载配置
    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        cprint(f"错误: {e}", Colors.RED)
        cprint(f"请编辑 agent/config.json 并填入实际的 AC 地址和 LLM API key", Colors.YELLOW)
        sys.exit(1)
    except json.JSONDecodeError as e:
        cprint(f"配置文件 JSON 格式错误: {e}", Colors.RED)
        sys.exit(1)

    if args.debug:
        config["log_level"] = "DEBUG"
    apply_log_level(config)

    # 打印配置摘要 (Cassia 品牌色)
    _console.print("=" * 60, style="#068466")
    _console.print("  Cassia AC AI Agent", style="bold #068466")
    _console.print("=" * 60, style="#068466")
    _console.print(f"  AC 地址: {config.get('base_url', '未配置')}", style="dim")
    llm_config = config.get("llm", {})
    _console.print(f"  LLM: {llm_config.get('model', '未配置')} @ {llm_config.get('base_url', '未配置')}", style="dim")
    _console.print(f"  最大步数: {config.get('agent', {}).get('max_steps', 30)}", style="dim")
    _console.print("=" * 60, style="#068466")
    print()

    # 启动浏览器
    cprint("正在启动浏览器...", Colors.YELLOW)

    with sync_playwright() as pw:
        browser_mgr = BrowserManager(config)

        profile_dir = os.path.join(PROJECT_ROOT, ".browser_profile")
        browser_mgr.launch(pw, profile_dir)

        page = browser_mgr.page
        cprint(f"浏览器已就绪，当前页面: {page.url}", Colors.GREEN)
        cprint("")

        # 创建 Agent
        agent = CassiaAgent(
            page=page,
            config=config,
            on_thinking=on_thinking,
            on_thinking_chunk=on_thinking_chunk,
            on_tool_call=on_tool_call,
        )

        # 交互循环
        cprint("输入你的指令 (输入 quit/exit 退出, reset 重置对话):", Colors.YELLOW)
        cprint("")

        # 使用 PromptSession 支持 CJK 宽字符 + in_thread 避免与 Playwright 事件循环冲突
        pt_session = PromptSession()

        while True:
            try:
                user_input = pt_session.prompt("> ", in_thread=True).strip()
            except (EOFError, KeyboardInterrupt):
                cprint("\n再见!", Colors.YELLOW)
                break

            if not user_input:
                continue

            if user_input.lower() in ("quit", "exit", "q"):
                cprint("再见!", Colors.YELLOW)
                break

            if user_input.lower() == "reset":
                agent.reset()
                cprint("对话已重置", Colors.YELLOW)
                continue

            if user_input.lower() == "snapshot":
                # 调试命令: 显示当前页面快照
                text = agent.snapshot.get_full_snapshot(page)
                cprint(text, Colors.DIM)
                continue

            if user_input.lower() == "url":
                cprint(f"当前 URL: {page.url}", Colors.DIM)
                continue

            # 执行 Agent 任务
            global _streamed_content
            _streamed_content = False  # 重置流式输出标记

            cprint(f"\n{'─' * 40}", Colors.DIM)
            start_time = time.time()

            try:
                result = agent.run(user_input)
            except Exception as e:
                result = f"执行出错: {e}"
                logger.error(f"Agent 执行异常: {e}", exc_info=True)

            elapsed = time.time() - start_time
            cprint(f"{'─' * 40}", Colors.DIM)
            print()
            # 如果内容已通过流式逐字输出且没有经过 done() 工具，跳过重复渲染
            # done() 返回的是 summary，与流式输出的 thinking 不同，需要渲染
            if not _streamed_content or result != agent.last_streamed_content:
                _console.print(Markdown(result))
            cprint(f"(耗时 {elapsed:.1f}s)\n", Colors.DIM)

        # 清理
        agent.executor.cleanup()  # 清理临时缓存文件
        browser_mgr.close()


if __name__ == "__main__":
    main()
