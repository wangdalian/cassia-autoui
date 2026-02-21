"""
Agent 工具注册表

定义 LLM 可调用的所有工具 (OpenAI function calling schema)，
分为两类:
  1. 通用 UI 工具: click, fill, select, check, scroll, wait, screenshot, goto
  2. 领域工具: ssh_to_gateway, run_gateway_command, fetch_gateways, ac_api_call
"""

import json
import logging
import os
import tempfile

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
            "description": "通过 AC 平台 SSH 连接到指定网关。执行: 启用SSH -> 开启隧道 -> 打开Web终端 -> 等待连接 -> 切换root。完成后可用 run_gateway_command 执行命令。",
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
        self._screenshots_dir = "screenshots"
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

    def _tool_ssh_to_gateway(self, mac: str) -> str:
        max_attempts = 3
        retry_delays = [2000, 5000]  # 第1次失败后等2s, 第2次失败后等5s

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

        # 重置连接状态
        self._ssh_connected = False

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

        # Socket.IO 有 polling 回退，WS 断连不代表终端不可用
        # 所有步骤 ($, password, #) 都已成功，信任连接结果
        if self.capture and self.capture.ws_disconnected:
            logger.info(f"[SSH] WebSocket 已断连，但终端交互正常 (Socket.IO polling 回退)")

        self._ssh_connected = True
        self.snapshot.reset()  # 进入终端页面后重置 snapshot

        return f"已通过 SSH 连接到网关 {mac} (root 用户)"

    def _tool_run_gateway_command(self, command: str, timeout_ms: int | None = None) -> str:
        if not self._ssh_connected or self.capture is None:
            return "错误: 未连接到网关 SSH，请先调用 ssh_to_gateway"

        # Socket.IO 有 polling 回退，WS 断连不一定代表终端不可用，继续尝试
        if self.capture.ws_disconnected:
            logger.warning("[SSH] WebSocket 已断连，尝试继续执行 (Socket.IO 可能使用 polling)")

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
            return "错误: SSH 终端 WebSocket 断连，命令执行失败。请重新调用 ssh_to_gateway 连接"
        except TimeoutError:
            self.page.wait_for_timeout(3000)
            new_raw = self.capture.get_raw_text()
            output = extract_command_output(new_raw, baseline, command)

        return output if output else "(无输出)"

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

    # ---- 结束任务 ----

    def _tool_done(self, summary: str) -> str:
        return f"__DONE__:{summary}"
