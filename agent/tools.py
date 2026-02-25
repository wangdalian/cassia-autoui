"""
Agent 工具注册表

定义 LLM 可调用的所有工具 (OpenAI function calling schema)，
分为两类:
  1. 通用 UI 工具: click, fill, select, check, scroll, wait, screenshot, goto
  2. 领域工具: ssh_to_gateway, run_gateway_command, fetch_gateways, ac_api_call
"""

import csv
import json
import logging
import os
import re
import tempfile
from collections import Counter
from datetime import datetime

from playwright.sync_api import Page

from lib.snapshot import SnapshotParser
from lib.terminal import (
    TerminalCapture,
    type_in_terminal,
    type_password_in_terminal,
    wait_for_terminal_text,
    wait_for_new_terminal_text,
    extract_command_output,
)
from lib.ac_api import (
    page_fetch,
    enable_ssh,
    open_tunnel,
    open_ssh_terminal,
    fetch_gateways as ac_fetch_gateways,
    extract_gateway_info,
)

logger = logging.getLogger("cassia")


# ============================================================
# OpenAI Function Calling Schema (工具定义)
# ============================================================

TOOL_DEFINITIONS = [
    # ---- 通用 UI 工具 ----
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": "点击页面上的元素。使用 ref 编号指定目标 (来自页面快照中 [N] 标记的可交互元素)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "integer",
                        "description": "元素 ref 编号 (页面快照中 [N] 的数字)",
                    },
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": "在输入框中填入文本。先清空原有内容再填入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "integer",
                        "description": "输入框的 ref 编号",
                    },
                    "value": {
                        "type": "string",
                        "description": "要填入的文本",
                    },
                },
                "required": ["ref", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_select",
            "description": "从下拉选择框中选择一个选项。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "integer",
                        "description": "下拉框的 ref 编号",
                    },
                    "value": {
                        "type": "string",
                        "description": "要选择的选项值",
                    },
                },
                "required": ["ref", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_check",
            "description": "勾选或取消勾选复选框。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "integer",
                        "description": "复选框的 ref 编号",
                    },
                    "checked": {
                        "type": "boolean",
                        "description": "true=勾选, false=取消",
                    },
                },
                "required": ["ref", "checked"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_goto",
            "description": "导航到指定 URL。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "目标 URL (可以是相对路径如 /dashboard?view)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_scroll",
            "description": "滚动页面。用于查看页面上方或下方不可见的内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down"],
                        "description": "滚动方向",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "滚动像素数 (默认 500)",
                    },
                },
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_wait",
            "description": "等待指定的毫秒数。用于等待页面加载、动画完成或异步操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ms": {
                        "type": "integer",
                        "description": "等待毫秒数 (建议 500~5000)",
                    },
                },
                "required": ["ms"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "截取当前页面的屏幕截图并保存。用于调试或记录状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "截图文件名 (不含路径，如 step1.png)",
                    },
                },
                "required": ["filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_press_key",
            "description": "按下键盘按键。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "按键名称 (如 Enter, Escape, Tab, ArrowDown 等)",
                    },
                },
                "required": ["key"],
            },
        },
    },
    # ---- 领域工具: SSH 终端 ----
    {
        "type": "function",
        "function": {
            "name": "ssh_to_gateway",
            "description": "通过 AC 平台 SSH 连接到指定网关。执行: 启用SSH -> 开启隧道 -> 打开Web终端 -> 等待连接 -> 切换root。完成后可用 run_gateway_command 执行命令。注意: M 系列和 Z 系列网关（嵌入式系统）不支持 SSH。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mac": {
                        "type": "string",
                        "description": "网关 MAC 地址 (如 CC:1B:E0:E4:3C:E0)",
                    },
                },
                "required": ["mac"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_gateway_command",
            "description": "在已连接的网关 SSH 终端中执行命令并返回输出。必须先调用 ssh_to_gateway 连接到网关。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "命令超时毫秒数 (默认 30000)",
                    },
                },
                "required": ["command"],
            },
        },
    },
    # ---- 领域工具: eMMC 健康检查 ----
    {
        "type": "function",
        "function": {
            "name": "check_emmc_health",
            "description": "检查当前已 SSH 连接网关的 eMMC 存储健康状态。需先通过 ssh_to_gateway 连接。返回芯片名称、磨损指标（EST_TYP_A/B、EOL_INFO）和健康等级。M/Z 系列不支持。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "batch_check_emmc",
            "description": "批量检查网关 eMMC 健康状态。自动获取在线网关列表，逐个 SSH 连接并检查，生成分析报告（JSON/CSV/HTML）。M/Z 系列自动跳过。不传参数则检查所有在线网关。",
            "parameters": {
                "type": "object",
                "properties": {
                    "macs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "指定要检查的 MAC 地址列表（可选）",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "模糊匹配网关名称/MAC/型号进行筛选（可选）",
                    },
                },
            },
        },
    },
    # ---- 领域工具: AC API ----
    {
        "type": "function",
        "function": {
            "name": "fetch_gateways",
            "description": "获取 AC 平台上的网关列表，返回每个网关的 MAC、名称、版本、状态等信息。可按状态过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["all", "online", "offline"],
                        "description": "过滤条件: all=所有网关, online=仅在线, offline=仅离线 (默认 all)",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ac_api_call",
            "description": "调用 AC 平台的 HTTP API。自动处理 CSRF token 和 session cookie。适用于高级操作 (如上传固件、修改设置等)。",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PUT", "DELETE"],
                        "description": "HTTP 方法",
                    },
                    "path": {
                        "type": "string",
                        "description": "API 路径 (如 /ap, /firmware, /setting, /event)",
                    },
                    "body": {
                        "type": "object",
                        "description": "请求体 (POST/PUT/DELETE 时使用)",
                    },
                    "query": {
                        "type": "string",
                        "description": "查询参数字符串 (如 status=online&pageSize=50)",
                    },
                },
                "required": ["method", "path"],
            },
        },
    },
    # ---- 数据搜索 ----
    {
        "type": "function",
        "function": {
            "name": "search_data",
            "description": "在上次 API 返回的大量数据中按关键词搜索。数据已缓存在本地，无需重新请求 API。当 ac_api_call 返回'数据量较大，已缓存'时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "搜索关键词。支持多个关键词用逗号分隔 (如 'disconnected,offline')，匹配任意一个即命中",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回条数 (默认 50)",
                    },
                },
                "required": ["keyword"],
            },
        },
    },
    # ---- 本地文件 ----
    {
        "type": "function",
        "function": {
            "name": "write_local_file",
            "description": "将内容写入本地文件。用于保存分析报告、导出数据等。文件保存在 Agent 运行目录下的 reports/ 文件夹中。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名（如 gateway_report.html、analysis.md）。不需要路径前缀。",
                    },
                    "content": {
                        "type": "string",
                        "description": "文件内容",
                    },
                },
                "required": ["filename", "content"],
            },
        },
    },
    # ---- 结束任务 ----
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "任务完成。向用户报告最终结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "任务完成的总结，包括关键结果",
                    },
                },
                "required": ["summary"],
            },
        },
    },
]


# ============================================================
# 工具执行器
# ============================================================

class ToolExecutor:
    """
    工具执行器: 接收 LLM 的 function call，调用实际的 Playwright/lib 函数。
    """

    def __init__(
        self,
        page: Page,
        snapshot: SnapshotParser,
        config: dict,
        capture: TerminalCapture | None = None,
    ):
        self.page = page
        self.snapshot = snapshot
        self.config = config
        self.capture = capture
        self._ssh_connected = False
        self._ws_polling_mode = False
        self._screenshots_dir = "screenshots"
        self._gateway_models: dict[str, str] = {}
        # 大数据缓存: API 返回大量数据时写入临时文件，供 search_data 工具检索
        self._last_data_file: str | None = None
        self._last_data_count: int = 0

    def execute(self, tool_name: str, arguments: dict) -> str:
        """
        执行指定工具，返回结果字符串。

        Args:
            tool_name: 工具名称
            arguments: LLM 传入的参数字典

        Returns:
            执行结果的文本描述
        """
        try:
            handler = getattr(self, f"_tool_{tool_name}", None)
            if handler is None:
                return f"错误: 未知工具 '{tool_name}'"
            return handler(**arguments)
        except Exception as e:
            logger.error(f"[Tool] {tool_name} 执行失败: {e}")
            return f"错误: {e}"

    def get_preview(self, tool_name: str, arguments: dict) -> str | None:
        """
        获取工具的预览信息（用于确认机制）。

        如果工具定义了 _preview_<tool_name> 方法，调用并返回预览文本；
        返回 None 表示该工具不需要用户确认。
        """
        handler = getattr(self, f"_preview_{tool_name}", None)
        if handler is None:
            return None
        try:
            return handler(**arguments)
        except Exception as e:
            logger.error(f"[Tool] {tool_name} preview 失败: {e}")
            return f"预览生成失败: {e}"

    # ---- 通用 UI 工具 ----

    def _tool_browser_click(self, ref: int) -> str:
        result = self.snapshot.execute_action(self.page, "click", ref)
        self.page.wait_for_timeout(500)
        return result

    def _tool_browser_fill(self, ref: int, value: str) -> str:
        result = self.snapshot.execute_action(self.page, "fill", ref, value=value)
        return result

    def _tool_browser_select(self, ref: int, value: str) -> str:
        result = self.snapshot.execute_action(self.page, "select", ref, value=value)
        self.page.wait_for_timeout(500)
        return result

    def _tool_browser_check(self, ref: int, checked: bool) -> str:
        action = "check" if checked else "uncheck"
        result = self.snapshot.execute_action(self.page, action, ref)
        return result

    def _tool_browser_goto(self, url: str) -> str:
        base_url = self.config.get("base_url", "")
        if url.startswith("/"):
            url = f"{base_url}{url}"
        self.page.goto(url, timeout=self.config.get("timeout_page_load", 30000))
        self.page.wait_for_timeout(1000)
        self.snapshot.reset()  # 页面导航后重置 snapshot
        return f"已导航到: {self.page.url}"

    def _tool_browser_scroll(self, direction: str, amount: int = 500) -> str:
        delta = -amount if direction == "up" else amount
        self.page.mouse.wheel(0, delta)
        self.page.wait_for_timeout(300)
        return f"已向{'上' if direction == 'up' else '下'}滚动 {amount} 像素"

    def _tool_browser_wait(self, ms: int) -> str:
        self.page.wait_for_timeout(ms)
        return f"已等待 {ms}ms"

    def _tool_browser_screenshot(self, filename: str) -> str:
        import os
        os.makedirs(self._screenshots_dir, exist_ok=True)
        path = os.path.join(self._screenshots_dir, filename)
        self.page.screenshot(path=path, full_page=True)
        return f"截图已保存: {path}"

    def _tool_browser_press_key(self, key: str) -> str:
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(300)
        return f"已按下: {key}"

    # ---- 领域工具: SSH ----

    _SSH_UNSUPPORTED_PREFIXES = ("M", "Z")

    def _check_gateway_ssh_support(self, mac: str) -> str | None:
        """检查网关型号是否支持 SSH，返回 None 表示支持，否则返回错误信息。"""
        mac_upper = mac.upper()
        model = self._gateway_models.get(mac_upper)

        if model is None:
            try:
                gateways = ac_fetch_gateways(
                    self.page, self.config["base_url"],
                    status="all", timeout=self.config.get("timeout_page_load", 30000),
                )
                for gw in gateways:
                    gw_mac = (gw.get("mac") or "").upper()
                    gw_model = (gw.get("model") or "").strip()
                    if gw_mac:
                        self._gateway_models[gw_mac] = gw_model
                model = self._gateway_models.get(mac_upper)
            except Exception as e:
                logger.warning(f"[SSH] 无法获取网关型号信息，跳过前置检查: {e}")
                return None

        if model and model[0].upper() in self._SSH_UNSUPPORTED_PREFIXES:
            return (
                f"错误: 网关 {mac} 型号为 {model}（{model[0].upper()} 系列），"
                f"属于嵌入式系统，不支持 SSH 连接。"
            )
        return None

    def _tool_ssh_to_gateway(self, mac: str) -> str:
        unsupported = self._check_gateway_ssh_support(mac)
        if unsupported:
            return unsupported

        max_attempts = 3
        retry_delays = [2000, 5000]

        for attempt in range(1, max_attempts + 1):
            result = self._ssh_connect_once(mac)
            if not result.startswith("错误:"):
                return result
            # 连接失败
            if attempt < max_attempts:
                delay = retry_delays[attempt - 1]
                logger.warning(
                    f"[SSH] 第 {attempt} 次连接失败，{delay/1000:.0f}s 后重试: {result}"
                )
                self.page.wait_for_timeout(delay)
            else:
                logger.error(f"[SSH] {max_attempts} 次连接均失败: {result}")
                return result

        return "错误: SSH 连接失败"

    def _ssh_connect_once(self, mac: str) -> str:
        """单次 SSH 连接尝试 (enable → tunnel → terminal → prompt → su)。"""
        base_url = self.config["base_url"]
        timeout = self.config.get("timeout_page_load", 30000)

        self._ssh_connected = False
        self._ws_polling_mode = False

        # 初始化终端捕获器
        if self.capture is None:
            self.capture = TerminalCapture()
            self.capture.attach(self.page)
        else:
            self.capture.reset()

        # Step 1: 启用 SSH
        enable_ssh(self.page, mac, base_url, timeout)
        self.page.wait_for_timeout(3000)

        # Step 2: 开启隧道
        open_tunnel(self.page, mac, base_url, timeout)
        self.page.wait_for_timeout(2000)  # 等待隧道完全建立

        # Step 3: 打开 Web Terminal
        open_ssh_terminal(
            self.page, base_url,
            timeout_page_load=timeout,
            timeout_terminal_ready=self.config.get("timeout_terminal_ready", 30000),
        )

        # Step 4: 等待 prompt
        prompt_timeout = self.config.get("timeout_prompt_wait", 30000)
        try:
            wait_for_terminal_text(self.page, self.capture, "$", timeout=prompt_timeout)
        except ConnectionError as e:
            return f"错误: SSH 终端 WebSocket 断连，无法建立连接: {e}"
        except TimeoutError:
            type_in_terminal(self.page, "", self.config.get("type_delay", 50))
            self.page.wait_for_timeout(2000)

        # Step 5: 切换 root
        type_in_terminal(self.page, "", self.config.get("type_delay", 50))
        self.page.wait_for_timeout(1000)
        baseline = self.capture.get_raw_text()
        type_in_terminal(self.page, "su", self.config.get("type_delay", 50))

        try:
            wait_for_terminal_text(self.page, self.capture, "assword", timeout=10000)
        except ConnectionError as e:
            return f"错误: SSH 终端 WebSocket 断连: {e}"
        except TimeoutError:
            pass

        su_password = self.config.get("su_password", "")
        type_password_in_terminal(self.page, su_password, self.config.get("type_delay", 50))

        try:
            wait_for_new_terminal_text(
                self.page, self.capture, "#", baseline,
                timeout=self.config.get("timeout_command_wait", 30000),
            )
        except ConnectionError as e:
            return f"错误: SSH 终端 WebSocket 断连: {e}"
        except TimeoutError:
            self.page.wait_for_timeout(3000)

        if self.capture and self.capture.ws_disconnected:
            self._ws_polling_mode = True
            logger.info("[SSH] WebSocket 已断连，终端通过 Socket.IO polling 正常工作")
        else:
            self._ws_polling_mode = False

        self._ssh_connected = True
        self.snapshot.reset()  # 进入终端页面后重置 snapshot

        return f"已通过 SSH 连接到网关 {mac} (root 用户)"

    def _tool_run_gateway_command(self, command: str, timeout_ms: int | None = None) -> str:
        if not self._ssh_connected or self.capture is None:
            return "错误: 未连接到网关 SSH，请先调用 ssh_to_gateway"

        if self.capture.ws_disconnected and not self._ws_polling_mode:
            logger.warning("[SSH] WebSocket 新断连，尝试继续执行 (Socket.IO 可能回退到 polling)")

        # 校验 timeout_ms: LLM 可能传 null/字符串/不合理值
        try:
            timeout_ms = int(timeout_ms) if timeout_ms is not None else 30000
        except (ValueError, TypeError):
            timeout_ms = 30000
        timeout_ms = max(1000, min(timeout_ms, 300000))  # 限制在 1s ~ 300s

        baseline = self.capture.get_raw_text()
        type_delay = self.config.get("type_delay", 50)
        type_in_terminal(self.page, command, type_delay)

        try:
            new_raw = wait_for_new_terminal_text(
                self.page, self.capture, "#", baseline, timeout=timeout_ms,
            )
            output = extract_command_output(new_raw, baseline, command)
        except ConnectionError:
            self._ssh_connected = False
            self._ws_polling_mode = False
            return "错误: SSH 终端 WebSocket 断连，命令执行失败。请重新调用 ssh_to_gateway 连接"
        except TimeoutError:
            self.page.wait_for_timeout(3000)
            new_raw = self.capture.get_raw_text()
            output = extract_command_output(new_raw, baseline, command)

        return output if output else "(无输出)"

    # ---- 领域工具: eMMC 健康检查 ----

    _EMMC_CMD_DEVNAME = "cat /sys/block/mmcblk0/device/name"
    _EMMC_CMD_EXTCSD = "/sbin/mmc extcsd read /dev/mmcblk0 | grep -E 'Life Time|EOL'"

    _EMMC_PATTERNS = {
        "EST_TYP_A": re.compile(r"EST_TYP_A\]:\s*(0x[0-9a-fA-F]+)"),
        "EST_TYP_B": re.compile(r"EST_TYP_B\]:\s*(0x[0-9a-fA-F]+)"),
        "EOL_INFO":  re.compile(r"EOL_INFO\]:\s*(0x[0-9a-fA-F]+)"),
    }

    _EMMC_HEALTH_LEVELS = [
        (1, 3, "健康"),
        (4, 6, "良好"),
        (7, 9, "警告"),
        (10, 11, "危险"),
    ]

    @staticmethod
    def _emmc_health_level(val: int) -> str:
        for lo, hi, label in ToolExecutor._EMMC_HEALTH_LEVELS:
            if lo <= val <= hi:
                return label
        if val == 0:
            return "未知"
        return "危险"

    def _parse_emmc_output(self, devname_output: str, extcsd_output: str) -> dict:
        """解析 eMMC 命令输出，返回结构化字段。"""
        result = {}

        devname = devname_output.strip().splitlines()
        result["devName"] = devname[-1].strip() if devname else "N/A"

        for field, pattern in self._EMMC_PATTERNS.items():
            m = pattern.search(extcsd_output)
            result[field] = m.group(1) if m else "N/A"

        typ_a_hex = result.get("EST_TYP_A", "N/A")
        try:
            typ_a_val = int(typ_a_hex, 16)
        except (ValueError, TypeError):
            typ_a_val = 0
        result["health_value"] = typ_a_val
        result["health_level"] = self._emmc_health_level(typ_a_val)

        return result

    def _tool_check_emmc_health(self) -> str:
        if not self._ssh_connected or self.capture is None:
            return "错误: 未连接到网关 SSH，请先调用 ssh_to_gateway"

        devname_output = self._tool_run_gateway_command(self._EMMC_CMD_DEVNAME)
        extcsd_output = self._tool_run_gateway_command(self._EMMC_CMD_EXTCSD)

        parsed = self._parse_emmc_output(devname_output, extcsd_output)
        return json.dumps(parsed, ensure_ascii=False, indent=2)

    def _get_emmc_target_gateways(
        self, macs: list[str] | None = None, keyword: str | None = None,
    ) -> tuple[list[dict], int]:
        """
        获取 eMMC 批量检查的目标网关列表。
        返回 (可检查列表, 被跳过的 M/Z 系列数量)。
        """
        base_url = self.config["base_url"]
        timeout = self.config.get("timeout_page_load", 30000)

        all_gateways = ac_fetch_gateways(self.page, base_url, status="online", timeout=timeout)
        if not all_gateways:
            return [], 0

        gw_list = []
        for gw in all_gateways:
            info = extract_gateway_info(gw)
            mac_upper = info.get("mac", "").upper()
            if mac_upper:
                self._gateway_models[mac_upper] = info.get("model", "")
            gw_list.append(info)

        if macs:
            mac_set = {m.upper() for m in macs}
            gw_list = [g for g in gw_list if g.get("mac", "").upper() in mac_set]

        if keyword:
            kw = keyword.lower()
            gw_list = [
                g for g in gw_list
                if kw in g.get("name", "").lower()
                or kw in g.get("mac", "").lower()
                or kw in g.get("model", "").lower()
            ]

        skipped = 0
        targets = []
        for g in gw_list:
            model = g.get("model", "")
            if model and model[0].upper() in self._SSH_UNSUPPORTED_PREFIXES:
                skipped += 1
            else:
                targets.append(g)

        return targets, skipped

    def _preview_batch_check_emmc(
        self, macs: list[str] | None = None, keyword: str | None = None,
    ) -> str:
        targets, skipped = self._get_emmc_target_gateways(macs, keyword)
        n = len(targets)
        est_minutes = max(1, n * 0.5)
        parts = [f"将检查 {n} 个在线网关的 eMMC 健康状态"]
        if skipped:
            parts.append(f"（已排除 {skipped} 个 M/Z 系列）")
        parts.append(f"，预计耗时约 {est_minutes:.0f} 分钟")
        return "".join(parts)

    def _tool_batch_check_emmc(
        self, macs: list[str] | None = None, keyword: str | None = None,
    ) -> str:
        targets, skipped = self._get_emmc_target_gateways(macs, keyword)
        if not targets:
            return "没有符合条件的在线网关可供检查"

        results = []
        total = len(targets)
        base_url = self.config["base_url"]

        for i, gw in enumerate(targets, 1):
            mac = gw.get("mac", "")
            entry = {
                "mac": mac,
                "name": gw.get("name", ""),
                "model": gw.get("model", ""),
                "status": "success",
                "error": "",
            }
            logger.info(f"[eMMC] ({i}/{total}) 开始检查: {mac} ({gw.get('name', '')})")

            try:
                ssh_result = self._tool_ssh_to_gateway(mac)
                if ssh_result.startswith("错误"):
                    entry["status"] = "ssh_failed"
                    entry["error"] = ssh_result
                    results.append(entry)
                    self._ssh_connected = False
                    logger.warning(f"[eMMC] ({i}/{total}) SSH 连接失败: {mac}")
                    continue

                devname_output = self._tool_run_gateway_command(self._EMMC_CMD_DEVNAME)
                extcsd_output = self._tool_run_gateway_command(self._EMMC_CMD_EXTCSD)
                parsed = self._parse_emmc_output(devname_output, extcsd_output)
                entry.update(parsed)
                logger.info(
                    f"[eMMC] ({i}/{total}) 检查完成: {mac} -> "
                    f"EST_TYP_A={parsed.get('EST_TYP_A')}, {parsed.get('health_level')}"
                )
            except Exception as e:
                entry["status"] = "error"
                entry["error"] = str(e)
                logger.error(f"[eMMC] ({i}/{total}) 检查异常: {mac} -> {e}")

            results.append(entry)

            # 回到网关列表页面，为下一个网关做准备
            try:
                self.page.goto(f"{base_url}/ap?view", wait_until="domcontentloaded", timeout=15000)
                self.page.wait_for_timeout(1000)
                self._ssh_connected = False
            except Exception:
                pass

        report_paths = self._generate_emmc_report(results)

        success = sum(1 for r in results if r["status"] == "success")
        failed = total - success
        risk = sum(1 for r in results if r.get("health_value", 0) >= 7 and r["status"] == "success")

        summary_lines = [
            f"eMMC 批量检查完成: {total} 个网关",
            f"  成功: {success}, 失败: {failed}",
        ]
        if skipped:
            summary_lines.append(f"  跳过 M/Z 系列: {skipped}")
        if risk:
            summary_lines.append(f"  ⚠ 风险网关 (EST_TYP_A >= 7): {risk} 个")
        summary_lines.append(f"\n报告已生成:")
        for path in report_paths:
            summary_lines.append(f"  - {path}")

        return "\n".join(summary_lines)

    def _generate_emmc_report(self, results: list[dict]) -> list[str]:
        """生成 eMMC 检查报告文件（JSON + CSV + HTML），返回文件路径列表。"""
        reports_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "reports")
        os.makedirs(reports_dir, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        paths = []

        # JSON
        json_path = os.path.join(reports_dir, f"emmc_results_{ts}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        paths.append(json_path)

        # CSV
        csv_path = os.path.join(reports_dir, f"emmc_results_{ts}.csv")
        csv_columns = ["mac", "name", "model", "devName", "EST_TYP_A", "EST_TYP_B", "EOL_INFO", "health_level", "status", "error"]
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(results)
        paths.append(csv_path)

        # HTML
        html_path = os.path.join(reports_dir, f"emmc_report_{ts}.html")
        html_content = self._build_emmc_html_report(results)
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        paths.append(html_path)

        logger.info(f"[eMMC] 报告已生成: {paths}")
        return paths

    def _build_emmc_html_report(self, results: list[dict]) -> str:
        """生成 eMMC HTML 分析报告。"""
        import html as html_mod

        _HL = [
            {"name": "健康", "min": 1, "max": 3, "color": "#22c55e", "bg": "#f0fdf4"},
            {"name": "良好", "min": 4, "max": 6, "color": "#f59e0b", "bg": "#fefce8"},
            {"name": "警告", "min": 7, "max": 9, "color": "#f97316", "bg": "#fff7ed"},
            {"name": "危险", "min": 10, "max": 11, "color": "#ef4444", "bg": "#fef2f2"},
        ]
        def _hl_info(val):
            for lv in _HL:
                if lv["min"] <= val <= lv["max"]:
                    return lv
            return _HL[-1] if val > 0 else {"name": "未知", "color": "#9ca3af", "bg": "#f9fafb"}

        success_data = [r for r in results if r.get("status") == "success"]
        total = len(results)
        success_count = len(success_data)
        failed_count = total - success_count

        level_counts = Counter()
        vendor_counts = Counter()
        risk_rows = []
        for d in success_data:
            val = d.get("health_value", 0)
            if val > 0:
                info = _hl_info(val)
                level_counts[info["name"]] += 1
                vendor_counts[d.get("devName", "N/A")] += 1
            if val >= 7:
                risk_rows.append(d)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Overview cards
        cards_html = f"""
        <div style="display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap;">
            <div style="flex:1;min-width:150px;padding:16px;border-radius:8px;background:#f0fdf4;border:1px solid #bbf7d0;">
                <div style="font-size:28px;font-weight:700;color:#22c55e;">{success_count}</div>
                <div style="color:#666;">检查成功</div>
            </div>
            <div style="flex:1;min-width:150px;padding:16px;border-radius:8px;background:#fef2f2;border:1px solid #fecaca;">
                <div style="font-size:28px;font-weight:700;color:#ef4444;">{failed_count}</div>
                <div style="color:#666;">检查失败</div>
            </div>
            <div style="flex:1;min-width:150px;padding:16px;border-radius:8px;background:#fff7ed;border:1px solid #fed7aa;">
                <div style="font-size:28px;font-weight:700;color:#f97316;">{len(risk_rows)}</div>
                <div style="color:#666;">风险网关 (≥7)</div>
            </div>
        </div>"""

        # Health level distribution
        level_html_parts = []
        for lv in _HL:
            cnt = level_counts.get(lv["name"], 0)
            level_html_parts.append(
                f'<span style="display:inline-block;padding:4px 12px;border-radius:4px;'
                f'background:{lv["bg"]};color:{lv["color"]};margin-right:8px;font-weight:600;">'
                f'{lv["name"]}: {cnt}</span>'
            )
        level_html = "".join(level_html_parts)

        # Risk table
        risk_table = ""
        if risk_rows:
            rows_html = ""
            for r in sorted(risk_rows, key=lambda x: x.get("health_value", 0), reverse=True):
                val = r.get("health_value", 0)
                info = _hl_info(val)
                rows_html += f"""<tr>
                    <td>{html_mod.escape(r.get('mac', ''))}</td>
                    <td>{html_mod.escape(r.get('name', ''))}</td>
                    <td>{html_mod.escape(r.get('model', ''))}</td>
                    <td>{html_mod.escape(r.get('devName', ''))}</td>
                    <td>{html_mod.escape(r.get('EST_TYP_A', ''))}</td>
                    <td style="color:{info['color']};font-weight:700;">{info['name']}</td>
                </tr>"""
            risk_table = f"""
            <h2 style="color:#f97316;">⚠ 风险网关清单</h2>
            <table style="width:100%;border-collapse:collapse;margin-bottom:24px;">
                <thead><tr style="background:#f8fafc;">
                    <th style="padding:8px;border:1px solid #e2e8f0;text-align:left;">MAC</th>
                    <th style="padding:8px;border:1px solid #e2e8f0;text-align:left;">名称</th>
                    <th style="padding:8px;border:1px solid #e2e8f0;text-align:left;">型号</th>
                    <th style="padding:8px;border:1px solid #e2e8f0;text-align:left;">芯片</th>
                    <th style="padding:8px;border:1px solid #e2e8f0;text-align:left;">EST_TYP_A</th>
                    <th style="padding:8px;border:1px solid #e2e8f0;text-align:left;">健康等级</th>
                </tr></thead>
                <tbody>{rows_html}</tbody>
            </table>"""

        # Full results table
        all_rows_html = ""
        for r in results:
            val = r.get("health_value", 0)
            info = _hl_info(val) if val > 0 else {"color": "#9ca3af", "name": r.get("status", "")}
            err = html_mod.escape(r.get("error", ""))
            all_rows_html += f"""<tr>
                <td>{html_mod.escape(r.get('mac', ''))}</td>
                <td>{html_mod.escape(r.get('name', ''))}</td>
                <td>{html_mod.escape(r.get('model', ''))}</td>
                <td>{html_mod.escape(r.get('devName', 'N/A'))}</td>
                <td>{html_mod.escape(r.get('EST_TYP_A', 'N/A'))}</td>
                <td>{html_mod.escape(r.get('EST_TYP_B', 'N/A'))}</td>
                <td>{html_mod.escape(r.get('EOL_INFO', 'N/A'))}</td>
                <td style="color:{info['color']};font-weight:600;">{info.get('name', '')}</td>
                <td style="color:#999;font-size:12px;">{err}</td>
            </tr>"""

        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>eMMC 健康检查报告</title>
<style>
    body {{ font-family: -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; margin:0; padding:24px; background:#fafbfc; color:#1a202c; }}
    h1 {{ color:#1e293b; border-bottom:2px solid #e2e8f0; padding-bottom:12px; }}
    h2 {{ color:#334155; margin-top:32px; }}
    table {{ width:100%; border-collapse:collapse; margin-bottom:24px; }}
    th {{ padding:10px; border:1px solid #e2e8f0; text-align:left; background:#f8fafc; font-weight:600; }}
    td {{ padding:8px 10px; border:1px solid #e2e8f0; }}
    tr:hover {{ background:#f1f5f9; }}
    .meta {{ color:#64748b; font-size:14px; margin-bottom:24px; }}
</style>
</head>
<body>
<h1>eMMC 健康检查报告</h1>
<div class="meta">生成时间: {now} &nbsp;|&nbsp; 共 {total} 个网关</div>
{cards_html}
<h2>健康等级分布</h2>
<div style="margin-bottom:24px;">{level_html}</div>
{risk_table}
<h2>全量检查结果</h2>
<table>
    <thead><tr style="background:#f8fafc;">
        <th>MAC</th><th>名称</th><th>型号</th><th>芯片</th>
        <th>EST_TYP_A</th><th>EST_TYP_B</th><th>EOL_INFO</th>
        <th>健康等级</th><th>备注</th>
    </tr></thead>
    <tbody>{all_rows_html}</tbody>
</table>
</body>
</html>"""

    # ---- 领域工具: AC API ----

    def _tool_fetch_gateways(self, status: str = "all") -> str:
        base_url = self.config["base_url"]
        timeout = self.config.get("timeout_page_load", 30000)

        # 校验 status 参数
        if status not in ("all", "online", "offline"):
            status = "all"

        gateways = ac_fetch_gateways(self.page, base_url, status=status, timeout=timeout)
        if not gateways:
            status_label = {"all": "", "online": "在线", "offline": "离线"}.get(status, "")
            return f"未找到{status_label}网关"

        result = []
        for gw in gateways:
            info = extract_gateway_info(gw)
            result.append(info)
            mac_upper = info.get("mac", "").upper()
            if mac_upper:
                self._gateway_models[mac_upper] = info.get("model", "")

        return json.dumps(result, ensure_ascii=False, indent=2)

    def _tool_ac_api_call(
        self, method: str, path: str,
        body: dict | None = None, query: str | None = None,
    ) -> str:
        base_url = self.config["base_url"]
        timeout = self.config.get("timeout_page_load", 30000)

        url = f"{base_url}{path}"
        if query:
            url = f"{url}?{query}"

        # 使用 json.dumps 转义 URL，确保 \n \r \t " \ 等均安全
        safe_url = json.dumps(url)[1:-1]  # 去掉首尾引号

        if method == "GET":
            # GET 请求用 page.evaluate(fetch(...))，带超时
            result = self.page.evaluate(f"""async () => {{
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), {timeout});
                let resp;
                try {{
                    resp = await fetch("{safe_url}", {{
                        credentials: "same-origin",
                        headers: {{ "X-Requested-With": "XMLHttpRequest" }},
                        signal: controller.signal
                    }});
                }} catch (e) {{
                    clearTimeout(timer);
                    if (e.name === 'AbortError') throw new Error("GET 请求超时 ({timeout}ms): {safe_url}");
                    throw e;
                }}
                clearTimeout(timer);
                return {{ ok: resp.ok, status: resp.status, text: await resp.text() }};
            }}""")
        else:
            result = page_fetch(self.page, url, method, body, timeout=timeout)

        if result.get("ok"):
            text = result.get("text", "")
            # 尝试格式化 JSON 输出
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                # 非 JSON: 超长文本也截断
                if len(text) > 8000:
                    return (
                        f"(文本响应过长，共 {len(text)} 字符，已截断)\n\n"
                        f"{text[:8000]}\n... (已截断)"
                    )
                return text if text else "(空响应)"

            # 大数据缓存: 数组超过阈值时写入临时文件
            max_items = (self.config.get("agent") or {}).get("max_response_items", 100)
            if isinstance(parsed, list) and len(parsed) > max_items:
                return self._cache_large_response(parsed)

            # 对于 dict 响应，检查常见的包装字段中是否有大数组
            if isinstance(parsed, dict):
                for key in ("data", "items", "list", "results", "rows"):
                    val = parsed.get(key)
                    if isinstance(val, list) and len(val) > max_items:
                        return self._cache_large_response(val)

            formatted = json.dumps(parsed, ensure_ascii=False, indent=2)
            # 最终的大小保护: 序列化后过长也截断
            if len(formatted) > 15000:
                return (
                    f"(JSON 响应过长，共 {len(formatted)} 字符，已截断)\n\n"
                    f"{formatted[:8000]}\n... (已截断)"
                )
            return formatted
        else:
            return f"API 调用失败: HTTP {result.get('status')} - {result.get('text', '')[:500]}"

    def cleanup(self):
        """清理临时文件等资源。"""
        if self._last_data_file and os.path.isfile(self._last_data_file):
            try:
                os.unlink(self._last_data_file)
            except OSError:
                pass
            self._last_data_file = None
            self._last_data_count = 0

    def _cache_large_response(self, data: list) -> str:
        """将大量数据写入临时文件，返回摘要信息给 LLM。"""
        # 清理上一次的临时文件 (防止泄漏)
        self.cleanup()

        # 写入临时文件
        fd, path = tempfile.mkstemp(suffix=".json", prefix="cassia_data_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            # 写入失败则清理已创建的文件
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        self._last_data_file = path
        self._last_data_count = len(data)
        logger.info(f"[Tool] 大数据缓存: {len(data)} 条 → {path}")

        # 取前 5 条作为样例
        sample = data[:5]
        sample_str = json.dumps(sample, ensure_ascii=False, indent=2)
        # 样例也不要太长
        if len(sample_str) > 2000:
            sample_str = sample_str[:2000] + "\n  ..."

        return (
            f"共 {len(data)} 条数据，数据量较大，已缓存到本地。\n\n"
            f"前 5 条样例:\n{sample_str}\n\n"
            f"请使用 search_data(keyword) 工具按关键词搜索缓存数据，"
            f"只返回匹配的条目。支持多关键词(逗号分隔)。"
        )

    # ---- 数据搜索 ----

    def _tool_search_data(self, keyword: str, max_results: int = 50) -> str:
        """在缓存的大数据中按关键词搜索。"""
        if not self._last_data_file or not os.path.isfile(self._last_data_file):
            return "错误: 没有缓存数据。请先调用 ac_api_call 获取数据。"

        # 校验 max_results
        try:
            max_results = int(max_results)
        except (ValueError, TypeError):
            max_results = 50
        max_results = max(1, min(max_results, 200))

        # 解析关键词 (逗号分隔)
        keywords = [k.strip().lower() for k in keyword.split(",") if k.strip()]
        if not keywords:
            return "错误: 关键词不能为空"

        # 读取缓存数据
        try:
            with open(self._last_data_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            return f"错误: 读取缓存数据失败: {e}"

        # 搜索匹配 (用索引追踪位置，避免 data.index() 值查找的重复条目问题)
        matches = []
        stop_index = len(data)
        for i, item in enumerate(data):
            item_str = json.dumps(item, ensure_ascii=False).lower() if not isinstance(item, str) else item.lower()
            if any(kw in item_str for kw in keywords):
                matches.append(item)
                if len(matches) >= max_results:
                    stop_index = i + 1
                    break

        total_matches = len(matches)
        if len(matches) >= max_results:
            # 继续计数剩余匹配项但不保存
            for item in data[stop_index:]:
                item_str = json.dumps(item, ensure_ascii=False).lower() if not isinstance(item, str) else item.lower()
                if any(kw in item_str for kw in keywords):
                    total_matches += 1

        if not matches:
            return (
                f"在 {self._last_data_count} 条缓存数据中未找到包含 "
                f"'{keyword}' 的条目。可尝试其他关键词。"
            )

        result_str = json.dumps(matches, ensure_ascii=False, indent=2)
        # 防止结果仍然过大
        if len(result_str) > 15000:
            result_str = result_str[:15000] + "\n  ... (结果过长已截断)"

        header = f"在 {self._last_data_count} 条数据中搜索 '{keyword}'，"
        if total_matches > len(matches):
            header += f"共匹配 {total_matches} 条，显示前 {len(matches)} 条:"
        else:
            header += f"匹配 {total_matches} 条:"

        return f"{header}\n\n{result_str}"

    # ---- 本地文件 ----

    def _tool_write_local_file(self, filename: str, content: str) -> str:
        reports_dir = "reports"
        os.makedirs(reports_dir, exist_ok=True)
        safe_name = os.path.basename(filename)
        path = os.path.join(reports_dir, safe_name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"文件已保存: {path} ({len(content)} 字符)"

    # ---- 结束任务 ----

    def _tool_done(self, summary: str) -> str:
        return f"__DONE__:{summary}"
