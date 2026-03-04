"""
Cassia AC AI Agent — Textual TUI 界面

使用 Textual 构建分区布局：固定顶部标题栏、可滚动对话区、固定底部输入栏。
运行在 alternate screen buffer 中，不污染终端 scrollback。

使用方式:
    python -m agent.tui                    # 使用默认配置
    python -m agent.tui --config my.json   # 使用自定义配置
"""

import argparse
import atexit
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback

from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.geometry import Size
from textual.suggester import Suggester
from textual.widgets import Footer, Input, RichLog, Static
from textual.worker import get_current_worker

# ============================================================
# 项目路径与依赖
# ============================================================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.config import load_config, apply_log_level
from lib.browser import BrowserManager
from agent.core import CassiaAgent
from agent.utils import fix_emoji_spacing, CASSIA_THEME

logger = logging.getLogger("cassia")

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_DEFAULT_PLACEHOLDER = "输入指令… (/ 查看命令)"


# ============================================================
# SlashCommandSuggester — 输入 / 时补全命令
# ============================================================

class SlashCommandSuggester(Suggester):
    """当用户输入 / 开头时，提供斜杠命令补全建议。"""

    def __init__(self, commands: list[str]):
        super().__init__(use_cache=False, case_sensitive=False)
        self._commands = sorted(commands)

    async def get_suggestion(self, value: str) -> str | None:
        if not value.startswith("/"):
            return None
        val = value.lower()
        for cmd in self._commands:
            if cmd.startswith(val) and cmd != val:
                return cmd
        return None


# ============================================================
# StreamingRichLog — 支持流式 Markdown 原地更新
# ============================================================

class StreamingRichLog(RichLog):
    """扩展 RichLog，支持流式 Markdown 内容的原地刷新。

    在 begin_stream/stream_chunk/end_stream 生命周期中，新到达的 chunk
    会不断重新渲染累积文本，替换上一帧的输出，实现类似 Rich Live 的效果
    但不会污染 scrollback（因为 Textual 运行在 alternate screen buffer）。

    reasoning 内容（kimi/deepseek 内部推理）使用 dim italic 样式渲染，
    与正式 content 的 Markdown 渲染视觉分离。
    """

    _stream_start_idx: int = 0
    _stream_buffer: str = ""
    _streaming: bool = False
    _reasoning_buffer: str = ""
    _content_started: bool = False
    _last_streamed_content: str = ""

    def begin_stream(self) -> None:
        self._stream_start_idx = len(self.lines)
        self._stream_buffer = ""
        self._reasoning_buffer = ""
        self._content_started = False
        self._streaming = True

    def _format_reasoning(self, text: str) -> Text:
        """将 reasoning 文本格式化为 │ 前缀的引用块"""
        text = fix_emoji_spacing(text)
        lines = text.split("\n")
        formatted = "\n".join(f"│ {line}" for line in lines)
        return Text(formatted, style="dim italic #6B7B8D")

    def stream_reasoning_chunk(self, chunk: str) -> None:
        """reasoning 内容 — │ 引用块样式渲染，不进入 Markdown 流式区域"""
        if not self._streaming:
            return
        self._reasoning_buffer += chunk
        del self.lines[self._stream_start_idx:]
        self._line_cache.clear()
        self.virtual_size = Size(self.virtual_size.width, len(self.lines))
        self.write(Text(""), scroll_end=False)
        self.write(self._format_reasoning(self._reasoning_buffer), scroll_end=True)

    def stream_chunk(self, chunk: str) -> None:
        if not self._streaming:
            return
        if self._reasoning_buffer and not self._content_started:
            self.write(Text(""), scroll_end=False)
            self.write(Text("─" * 40, style="dim #3B4261"), scroll_end=False)
            self.write(Text(""), scroll_end=False)
            self.write(Text(""), scroll_end=False)
            self._stream_start_idx = len(self.lines)
            self._content_started = True
        self._stream_buffer += chunk
        self._rerender_stream()

    def end_stream(self) -> None:
        self._last_streamed_content = self._stream_buffer
        self._streaming = False
        self._stream_buffer = ""
        self._reasoning_buffer = ""
        self._content_started = False

    def _rerender_stream(self) -> None:
        del self.lines[self._stream_start_idx:]
        self._line_cache.clear()
        self.virtual_size = Size(self.virtual_size.width, len(self.lines))
        md = Markdown(fix_emoji_spacing(self._stream_buffer))
        self.write(md, scroll_end=True)


# ============================================================
# CassiaApp — Textual 主应用
# ============================================================

class CassiaApp(App):
    """Cassia AC AI Agent TUI"""

    TITLE = "Cassia AC AI Agent"

    CSS = """
    #header-bar {
        dock: top;
        width: 100%;
        height: auto;
        background: transparent;
        color: #82AAFF;
        text-style: bold;
        padding: 0 2;
        margin: 0 1 0 1;
        border-top: solid #3B4261;
        border-bottom: solid #3B4261;
    }
    #chat-log {
        height: 1fr;
        padding: 0 0 0 2;
        scrollbar-size-vertical: 1;
        scrollbar-background: #1E2030;
        scrollbar-color: #2E3450;
        scrollbar-color-hover: #3B4261;
        scrollbar-color-active: #82AAFF;
    }
    #user-input {
        dock: bottom;
        border: round #3B4261;
        margin: 0 1 1 1;
        padding: 0 2;
    }
    #user-input:focus {
        border: round #82AAFF;
    }
    """

    _SLASH_COMMANDS = {
        "/stop":     "中断正在运行的任务",
        "/reset":    "重置对话历史",
        "/clear":    "清屏",
        "/snapshot": "显示当前页面快照",
        "/url":      "显示当前页面 URL",
        "/help":     "显示所有可用命令",
        "/quit":     "退出程序",
    }

    BINDINGS = [
        Binding("ctrl+c", "quit", "退出"),
        Binding("ctrl+l", "clear_log", "清屏"),
        Binding("ctrl+r", "reset_chat", "重置对话"),
        Binding("ctrl+y", "copy_last", "复制回复"),
        Binding("f5", "screenshot", "截图"),
    ]

    def __init__(
        self,
        config: dict,
        debug: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._config = config
        self._debug = debug
        self._agent: CassiaAgent | None = None
        self._browser_mgr: BrowserManager | None = None
        self._pw_context = None
        self._busy = False
        self._task_start: float = 0.0
        self._last_result: str = ""
        self._header_title: str = ""
        self._header_info: str = ""
        self._status_text: str = ""
        self._status_style: str = "yellow"
        self._spinner_idx: int = 0
        self._spinner_timer = None
        # 螃蟹动画状态（输入框执行中提示）
        self._crab_pos: int = 0
        self._crab_dir: int = 1
        self._crab_timer = None
        # 确认机制状态
        self._confirm_event: threading.Event | None = None
        self._confirm_result: bool = False
        self._confirm_mode: bool = False
        # 工具进度流式状态
        self._tool_progress_active: bool = False

    # ---- Layout ----

    def compose(self) -> ComposeResult:
        model = self._config.get("llm", {}).get("model", "?")
        ac_url = self._config.get("base_url", "?")
        self._header_title = "◆ Cassia AC AI Agent"
        self._header_info = f"  |  {model}  |  {ac_url}"
        yield Static("", id="header-bar")
        yield StreamingRichLog(
            id="chat-log",
            highlight=False,
            markup=False,
            wrap=True,
            auto_scroll=True,
        )
        yield Input(
            placeholder=_DEFAULT_PLACEHOLDER,
            id="user-input",
            suggester=SlashCommandSuggester(list(self._SLASH_COMMANDS.keys())),
        )
        yield Footer()

    # ---- Header Status ----

    def _update_header(self) -> None:
        header = self.query_one("#header-bar", Static)
        rich_text = Text()
        rich_text.append(self._header_title, style="bold #C0CAF5")
        rich_text.append(self._header_info, style="#82AAFF")
        if self._status_text:
            if self._spinner_timer is not None:
                char = _SPINNER[self._spinner_idx % len(_SPINNER)]
            elif self._status_style == "green":
                char = "●"
            else:
                char = "✗"
            rich_text.append(f"    {char} ", style=self._status_style)
            rich_text.append(self._status_text, style=self._status_style)
        header.update(rich_text)

    def _set_status(self, status: str, style: str = "yellow") -> None:
        self._status_text = status
        self._status_style = style
        is_animated = status.endswith("...")
        if is_animated and self._spinner_timer is None:
            self._spinner_idx = 0
            self._spinner_timer = self.set_interval(0.08, self._tick_spinner)
        elif not is_animated and self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
        self._update_header()

    def _tick_spinner(self) -> None:
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER)
        self._update_header()

    # ---- Crab Animation (input placeholder) ----

    _CRAB_TRACK_LEN = 8

    def _start_crab(self) -> None:
        self._crab_pos = 0
        self._crab_dir = 1
        input_widget = self.query_one("#user-input", Input)
        input_widget._pause_blink(visible=False)
        if self._crab_timer is None:
            self._crab_timer = self.set_interval(0.15, self._tick_crab)
        self._tick_crab()

    def _stop_crab(self) -> None:
        if self._crab_timer is not None:
            self._crab_timer.stop()
            self._crab_timer = None
        input_widget = self.query_one("#user-input", Input)
        input_widget.placeholder = _DEFAULT_PLACEHOLDER
        input_widget._restart_blink()

    def _tick_crab(self) -> None:
        track = ["· "] * self._CRAB_TRACK_LEN
        track[self._crab_pos] = "🦀"
        self.query_one("#user-input", Input).placeholder = (
            "".join(track) + " 执行中，请稍候…"
        )
        self._crab_pos += self._crab_dir
        if self._crab_pos >= self._CRAB_TRACK_LEN - 1:
            self._crab_dir = -1
        elif self._crab_pos <= 0:
            self._crab_dir = 1

    # ---- Lifecycle ----

    def on_mount(self) -> None:
        self.console.push_theme(CASSIA_THEME)
        self._update_header()
        self.query_one("#user-input", Input).focus()
        self._start_browser()

    def _create_agent(self, page) -> CassiaAgent:
        return CassiaAgent(
            page=page,
            config=self._config,
            on_thinking=self._cb_thinking,
            on_thinking_chunk=self._cb_thinking_chunk,
            on_tool_call=self._cb_tool_call,
            on_thinking_stream_start=self._cb_stream_start,
            on_thinking_stream_end=self._cb_stream_end,
            on_reasoning_chunk=self._cb_reasoning_chunk,
            on_confirm_required=self._cb_confirm_required,
            on_tool_progress=self._cb_tool_progress,
        )

    def _recover_browser(self) -> bool:
        """重建浏览器 + Agent。在 worker 线程中调用。"""
        self.call_from_thread(self._set_status, "浏览器恢复中...", "yellow")
        try:
            if self._browser_mgr:
                self._browser_mgr.close()
            self._browser_mgr = BrowserManager(self._config)
            profile_dir = os.path.join(PROJECT_ROOT, ".browser_profile")
            self._browser_mgr.launch(self._pw_context, profile_dir)
            self._agent = self._create_agent(self._browser_mgr.page)
            logger.info("[TUI] 浏览器已自动恢复")
            self.call_from_thread(self._set_status, "就绪", "green")
            return True
        except Exception as e:
            logger.error(f"[TUI] 浏览器恢复失败: {e}", exc_info=True)
            self.call_from_thread(self._set_status, "恢复失败", "red")
            return False

    def _start_browser(self) -> None:
        self._launch_browser_worker()

    @work(thread=True, group="browser")
    def _launch_browser_worker(self) -> None:
        """在后台线程启动 Playwright + 浏览器"""
        from playwright.sync_api import sync_playwright

        self.call_from_thread(self._set_status, "启动中...", "yellow")

        try:
            pw = sync_playwright().start()
            self._pw_context = pw

            self._browser_mgr = BrowserManager(self._config)
            profile_dir = os.path.join(PROJECT_ROOT, ".browser_profile")
            self._browser_mgr.launch(pw, profile_dir)

            self._agent = self._create_agent(self._browser_mgr.page)

            self.call_from_thread(self._set_status, "就绪", "green")
        except Exception as e:
            logger.error(f"浏览器启动失败: {e}", exc_info=True)
            self.call_from_thread(self._set_status, "连接失败", "red")
            log = self.query_one("#chat-log", StreamingRichLog)
            self.call_from_thread(
                log.write,
                Text(f"浏览器启动失败: {e}\n请检查 Playwright 是否已安装 (playwright install chromium)",
                     style="dim red"),
            )
        self.call_from_thread(self.query_one("#user-input", Input).focus)

    # ---- Agent Callbacks (called from worker thread) ----

    def _cb_thinking(self, text: str) -> None:
        """非流式 fallback — 完整 Markdown 一次写入"""
        logger.debug(f"[TUI] _cb_thinking: len={len(text)}")
        log = self.query_one("#chat-log", StreamingRichLog)
        md = Markdown(fix_emoji_spacing(text))
        self.call_from_thread(log.write, md, scroll_end=True)

    def _cb_stream_start(self) -> None:
        logger.debug("[TUI] _cb_stream_start")
        log = self.query_one("#chat-log", StreamingRichLog)
        self.call_from_thread(log.begin_stream)

    def _cb_thinking_chunk(self, chunk: str) -> None:
        logger.debug(f"[TUI] _cb_thinking_chunk: {chunk[:50]!r}")
        log = self.query_one("#chat-log", StreamingRichLog)
        self.call_from_thread(log.stream_chunk, chunk)

    def _cb_reasoning_chunk(self, chunk: str) -> None:
        logger.debug(f"[TUI] _cb_reasoning_chunk: {chunk[:50]!r}")
        log = self.query_one("#chat-log", StreamingRichLog)
        self.call_from_thread(log.stream_reasoning_chunk, chunk)

    def _cb_stream_end(self, content: str) -> None:
        logger.debug(f"[TUI] _cb_stream_end: content_len={len(content)}")
        log = self.query_one("#chat-log", StreamingRichLog)
        self.call_from_thread(log.end_stream)

    @staticmethod
    def _format_tool_line(prefix: str, text: str) -> Text:
        """将 tool call 文本格式化为每行带 │ 前缀的引用块。

        首行使用 prefix（如 "│ → " 或 "│ ← "），后续行用等宽的 "│   " 对齐。
        """
        pad = "│" + " " * (len(prefix) - 1)
        lines = text.split("\n")
        formatted = "\n".join(
            [f"{prefix}{lines[0]}"]
            + [f"{pad}{line}" for line in lines[1:]]
        )
        return Text(formatted, style="dim italic #6B7B8D")

    def _cb_tool_progress(self, chunk: str) -> None:
        """工具内部进度流式回调 — 用于测试生成等耗时工具的实时输出"""
        log = self.query_one("#chat-log", StreamingRichLog)
        if not self._tool_progress_active:
            self._tool_progress_active = True
            self.call_from_thread(log.begin_stream)
        self.call_from_thread(log.stream_chunk, chunk)

    def _end_tool_progress(self) -> None:
        """结束工具进度流式输出（如果活跃）"""
        if self._tool_progress_active:
            self._tool_progress_active = False
            log = self.query_one("#chat-log", StreamingRichLog)
            self.call_from_thread(log.end_stream)

    def _cb_tool_call(self, tool_name: str, args: dict, result: str) -> None:
        logger.debug(f"[TUI] _cb_tool_call: {tool_name}, result_len={len(result)}")
        self._end_tool_progress()
        if tool_name == "done":
            return

        log = self.query_one("#chat-log", StreamingRichLog)

        args_str = json.dumps(args, ensure_ascii=False)
        if len(args_str) > 120:
            args_str = args_str[:117] + "..."
        self.call_from_thread(
            log.write,
            self._format_tool_line("│ → ", f"{tool_name}({args_str})"),
        )

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
        self.call_from_thread(
            log.write,
            self._format_tool_line("│ ← ", result_preview),
        )

    # ---- Confirm Mechanism (called from worker thread) ----

    def _cb_confirm_required(self, tool_name: str, arguments: dict, preview: str) -> bool:
        """高风险操作确认回调。在 worker 线程中被调用，阻塞等待用户输入 y/n。"""
        logger.info(f"[TUI] 确认请求: {tool_name}, preview={preview}")
        log = self.query_one("#chat-log", StreamingRichLog)

        self.call_from_thread(log.write, Text(""))
        self.call_from_thread(
            log.write,
            Text(f"⚠ {preview}", style="bold #E0AF68"),
        )

        self._confirm_event = threading.Event()
        self._confirm_result = False
        self._confirm_mode = True

        self.call_from_thread(self._enter_confirm_mode)

        self._confirm_event.wait()

        self._confirm_mode = False
        confirmed = self._confirm_result
        logger.info(f"[TUI] 用户确认结果: {confirmed}")
        return confirmed

    _CONFIRM_PROMPTS = [
        "⚠ 确认执行？ 输入 y 确认 / n 取消 ⚠",
        "   确认执行？ 输入 y 确认 / n 取消   ",
    ]

    def _enter_confirm_mode(self) -> None:
        """切换 Input 到确认模式：停止螃蟹，启动闪烁提示（主线程调用）。"""
        if self._crab_timer is not None:
            self._crab_timer.stop()
            self._crab_timer = None
        self._confirm_blink_idx = 0
        self._confirm_blink_timer = self.set_interval(0.6, self._tick_confirm_blink)
        input_widget = self.query_one("#user-input", Input)
        input_widget.placeholder = self._CONFIRM_PROMPTS[0]
        input_widget._restart_blink()
        input_widget.focus()

    def _tick_confirm_blink(self) -> None:
        """确认模式闪烁 — 交替显示两种提示文本。"""
        self._confirm_blink_idx = 1 - self._confirm_blink_idx
        self.query_one("#user-input", Input).placeholder = (
            self._CONFIRM_PROMPTS[self._confirm_blink_idx]
        )

    def _exit_confirm_mode(self) -> None:
        """退出确认模式：停止闪烁，恢复螃蟹动画（主线程调用）。"""
        if hasattr(self, "_confirm_blink_timer") and self._confirm_blink_timer is not None:
            self._confirm_blink_timer.stop()
            self._confirm_blink_timer = None
        if self._busy:
            self._start_crab()
        else:
            input_widget = self.query_one("#user-input", Input)
            input_widget.placeholder = _DEFAULT_PLACEHOLDER

    # ---- Input Handling ----

    def on_input_submitted(self, event: Input.Submitted) -> None:
        user_input = event.value.strip()
        input_widget = self.query_one("#user-input", Input)
        input_widget.value = ""

        if not user_input:
            return

        # 确认模式: 拦截 y/n 输入（/stop 可穿透）
        if self._confirm_mode and self._confirm_event is not None:
            if user_input.strip().lower() == "/stop":
                self._confirm_result = False
                self._exit_confirm_mode()
                self._confirm_event.set()
                self._handle_stop()
                return
            answer = user_input.lower()
            log = self.query_one("#chat-log", StreamingRichLog)
            if answer in ("y", "yes"):
                self._confirm_result = True
                log.write(Text("  → 已确认", style="green"))
            else:
                self._confirm_result = False
                log.write(Text("  → 已取消", style="dim red"))
            self._exit_confirm_mode()
            self._confirm_event.set()
            return

        # 斜杠命令
        if user_input.startswith("/"):
            self._handle_slash_command(user_input)
            return

        # 普通文本 → 发送给 Agent
        if self._busy:
            self.notify("Agent 正在执行中，请稍候… (输入 /stop 中断)", severity="warning")
            return

        if not self._agent:
            self.notify("浏览器尚未就绪，请稍候…", severity="warning")
            return

        log = self.query_one("#chat-log", StreamingRichLog)
        if log.lines:
            log.write(Text(""))
            chat_log = self.query_one("#chat-log")
            sep_width = max(chat_log.size.width - 4, 20)
            log.write(Text("─" * sep_width, style="#2E3450"))
        log.write(Text(""))
        log.write(Text(f"❯ {user_input}", style="bold #A9B1D6"))

        self._run_agent(user_input)

    # ---- Slash Commands ----

    def _handle_slash_command(self, raw: str) -> None:
        """分发斜杠命令。"""
        cmd = raw.strip().lower().split()[0]
        log = self.query_one("#chat-log", StreamingRichLog)
        input_widget = self.query_one("#user-input", Input)

        if cmd == "/quit":
            self._cleanup()
            self.exit()

        elif cmd == "/stop":
            self._handle_stop()

        elif cmd == "/reset":
            if self._agent:
                self._agent.reset()
            log.write(Text("对话已重置", style="yellow"))
            input_widget.focus()

        elif cmd == "/clear":
            log.clear()
            input_widget.focus()

        elif cmd == "/snapshot":
            if self._agent:
                text = self._agent.snapshot.get_full_snapshot(self._agent.page)
                log.write(Text(text, style="dim"))
            else:
                log.write(Text("浏览器尚未就绪", style="dim red"))
            input_widget.focus()

        elif cmd == "/url":
            if self._agent:
                log.write(Text(f"当前 URL: {self._agent.page.url}", style="dim"))
            else:
                log.write(Text("浏览器尚未就绪", style="dim red"))
            input_widget.focus()

        elif cmd == "/help":
            self._show_help()

        else:
            log.write(Text(f"未知命令: {cmd}  (输入 /help 查看可用命令)", style="dim red"))
            input_widget.focus()

    def _show_help(self) -> None:
        """显示所有斜杠命令及说明。"""
        log = self.query_one("#chat-log", StreamingRichLog)
        log.write(Text(""))
        log.write(Text("可用命令:", style="bold #C0CAF5"))
        max_cmd_len = max(len(c) for c in self._SLASH_COMMANDS)
        for cmd, desc in self._SLASH_COMMANDS.items():
            log.write(Text(f"  {cmd:<{max_cmd_len}}  {desc}", style="#A9B1D6"))
        log.write(Text(""))
        log.write(Text("快捷键: Ctrl+C 退出 | Ctrl+L 清屏 | Ctrl+R 重置 | Ctrl+Y 复制回复", style="dim #6B7B8D"))
        self.query_one("#user-input", Input).focus()

    def _handle_stop(self) -> None:
        """中断正在运行的 Agent worker。"""
        log = self.query_one("#chat-log", StreamingRichLog)
        if not self._busy:
            log.write(Text("当前没有正在运行的任务", style="dim #6B7B8D"))
            self.query_one("#user-input", Input).focus()
            return

        self.workers.cancel_group(self, "agent")
        self._tool_progress_active = False
        log.end_stream()
        self._busy = False
        self._stop_crab()
        self._set_status("已中断", "yellow")
        log.write(Text(""))
        log.write(Text("任务已中断", style="bold #E0AF68"))
        self.query_one("#user-input", Input).focus()

    # ---- Agent Worker ----

    @work(thread=True, group="agent", exclusive=True)
    def _run_agent(self, user_input: str) -> None:
        self._busy = True
        self._task_start = time.time()
        log = self.query_one("#chat-log", StreamingRichLog)
        self.call_from_thread(self._start_crab)
        logger.debug(f"[TUI] _run_agent START: {user_input!r}")

        try:
            if not self._browser_mgr.is_alive():
                logger.warning("[TUI] 浏览器已关闭，尝试自动恢复...")
                if not self._recover_browser():
                    result = "浏览器已关闭且恢复失败，请重启程序"
                    self.call_from_thread(log.write, Text(result, style="dim red"))
                    self._busy = False
                    self.call_from_thread(self._stop_crab)
                    self.call_from_thread(self._set_status, "恢复失败", "red")
                    return
                self.call_from_thread(
                    log.write,
                    Text("浏览器已自动恢复", style="dim #A9B1D6"),
                )

            result = self._agent.run(user_input)
            logger.debug(f"[TUI] _run_agent RESULT: len={len(result)}, preview={result[:100]!r}")
        except Exception as e:
            err_msg = str(e)
            if "has been closed" in err_msg or "Target closed" in err_msg:
                logger.warning(f"[TUI] 执行中浏览器关闭: {e}")
                self.call_from_thread(log.end_stream)
                if self._recover_browser():
                    result = "浏览器已自动恢复，请重新输入指令"
                else:
                    result = f"浏览器关闭且恢复失败: {e}"
            else:
                result = f"执行出错: {e}"
                logger.error(f"Agent 执行异常: {e}", exc_info=True)
                self.call_from_thread(log.end_stream)

        if get_current_worker().is_cancelled:
            logger.debug("[TUI] _run_agent 已被取消，跳过善后")
            return

        elapsed = time.time() - self._task_start

        self._last_result = result

        dedup = log._last_streamed_content == result
        logger.debug(
            f"[TUI] _run_agent DEDUP: last_streamed_len={len(log._last_streamed_content)}, "
            f"result_len={len(result)}, skip={dedup}"
        )
        if not log._last_streamed_content or not dedup:
            md = Markdown(fix_emoji_spacing(result))
            self.call_from_thread(log.write, md, scroll_end=True)

        self.call_from_thread(
            log.write,
            Text(f"({elapsed:.1f}s)", style="dim #5C6370"),
        )
        self._busy = False
        self.call_from_thread(self._stop_crab)
        self.call_from_thread(self._set_status, "就绪", "green")
        self.call_from_thread(self.query_one("#user-input", Input).focus)
        logger.debug("[TUI] _run_agent END")

    # ---- Actions ----

    def action_clear_log(self) -> None:
        self.query_one("#chat-log", StreamingRichLog).clear()

    def action_reset_chat(self) -> None:
        if self._agent:
            self._agent.reset()
            log = self.query_one("#chat-log", StreamingRichLog)
            log.write(Text("对话已重置", style="dim #5C6370"))
        self._set_status("就绪", "green")
        self.query_one("#user-input", Input).focus()

    def action_copy_last(self) -> None:
        if self._last_result:
            self.copy_to_clipboard(self._last_result)
            self.notify("已复制最后响应到剪贴板")
        else:
            self.notify("暂无可复制的响应", severity="warning")

    def action_screenshot(self) -> None:
        if self._agent:
            try:
                path = os.path.join(PROJECT_ROOT, "reports", "screenshot.png")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self._agent.page.screenshot(path=path)
                self.notify(f"截图已保存: {path}")
            except Exception as e:
                self.notify(f"截图失败: {e}", severity="error")

    # ---- Cleanup ----

    def _cleanup(self) -> None:
        logger.warning(
            "[BrowserLifecycle] TUI _cleanup() 被调用, 调用栈:\n%s",
            "".join(traceback.format_stack()),
        )
        if self._agent:
            try:
                self._agent.executor.cleanup()
            except Exception:
                pass
        if self._browser_mgr:
            try:
                self._browser_mgr.close()
            except Exception:
                pass
        if self._pw_context:
            try:
                logger.warning("[BrowserLifecycle] TUI 正在停止 Playwright 上下文 (pw.stop)")
                self._pw_context.stop()
            except Exception:
                pass

    def on_unmount(self) -> None:
        logger.warning("[BrowserLifecycle] TUI on_unmount() 触发 (Textual 框架卸载组件)")
        self._cleanup()


# ============================================================
# 入口
# ============================================================

def get_default_config_path() -> str:
    return os.path.join(PROJECT_ROOT, "agent", "config.json")


def main():
    parser = argparse.ArgumentParser(
        description="Cassia AC 管理平台 AI Agent (TUI 模式)",
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

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"错误: {e}")
        print("请编辑 agent/config.json 并填入实际的 AC 地址和 LLM API key")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"配置文件 JSON 格式错误: {e}")
        sys.exit(1)

    if args.debug:
        config["log_level"] = "DEBUG"
    apply_log_level(config)

    log_file = os.path.join(PROJECT_ROOT, "agent_debug.log")
    file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S",
    ))
    file_handler.setLevel(logging.DEBUG)
    logging.getLogger("cassia").addHandler(file_handler)

    def _atexit_handler():
        logger.warning("[BrowserLifecycle] Python 进程正在退出 (atexit)")

    atexit.register(_atexit_handler)

    def _signal_handler(signum, frame):
        sig_name = signal.Signals(signum).name
        logger.error(
            "[BrowserLifecycle] 收到信号 %s, 进程即将退出。调用栈:\n%s",
            sig_name,
            "".join(traceback.format_stack(frame)),
        )

    signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _signal_handler)

    app = CassiaApp(config=config, debug=args.debug)
    app.run()


if __name__ == "__main__":
    main()
