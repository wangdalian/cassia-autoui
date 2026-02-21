"""
Cassia Gateway SSH 批量自动化脚本

功能:
  针对网关 MAC 地址列表，逐个执行以下操作:
  1. 发送配置 SSH 请求（启用 SSH）
  2. 发送隧道开启请求
  3. 在浏览器中打开 SSH Web Terminal（/ssh/host）
  4. 等待终端加载，检查 blue 用户 prompt
  5. 切换到 root 用户（su）
  6. 执行指定的 shell 命令列表
  7. 关闭终端页面，处理下一个网关

配置:
  所有配置项在脚本同级目录的 config.json 中管理。

使用方式:
  1. 编辑 config.json，填入实际的 AC 地址、密码、网关 MAC 列表、命令列表等
  2. 运行脚本: python emmc_auto_check.py

  浏览器模式 (browser_mode):
    "persistent" (推荐) - Playwright Chromium + 会话持久化，不影响系统 Chrome
      首次运行自动登录，如遇 token 验证会暂停等你手动处理；后续直接复用
    "cdp"  - 连接已打开的 Chrome（需用 --remote-debugging-port=9222 启动）
    "login" - 每次都启动新浏览器并自动登录
"""

import base64
import json
import logging
import os
import re
import sys
import time
import traceback

import pyte
from playwright.sync_api import sync_playwright, Page, BrowserContext

# ============================================================
# 日志配置（默认 INFO，可通过 config.json 的 log_level 切换为 DEBUG）
# ============================================================

logger = logging.getLogger("cassia")
_log_handler = logging.StreamHandler()
_log_handler.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"
))
logger.addHandler(_log_handler)
logger.setLevel(logging.INFO)


# ============================================================
# TerminalCapture: 通过 WebSocket 拦截精确捕获终端文本
# ============================================================

class TerminalCapture:
    """
    通过在浏览器端注入 JS hook（拦截 WebSocket 和 XHR），
    捕获 webssh2/Socket.IO 的终端数据流，
    配合 pyte 虚拟终端模拟器，精确提取 xterm.js Canvas 终端中的文字。

    原理:
        不依赖特定的 Socket.IO 版本或线协议格式。
        在页面加载前注入 JS，直接 hook WebSocket 构造函数和 XHR，
        在浏览器端捕获所有 socket.io 传输的原始数据，
        然后 Python 端通过 page.evaluate() 读取并解析。

    使用方法:
        capture = TerminalCapture()
        capture.attach(page)          # 在导航到 /ssh/host 之前调用
        # ... 导航到终端页面、输入命令 ...
        text = capture.get_screen_text()   # 获取当前屏幕内容
        text = capture.get_raw_text()      # 获取所有历史输出（纯文本）
        capture.reset()                    # 处理下一个网关前重置
    """

    # 注入到浏览器的 JS hook 代码
    _JS_HOOK = """
    (function() {
        // 全局数据存储
        window.__termCapture = { messages: [], debug: [], wsDisconnected: false };

        // ---- Hook WebSocket: 捕获 WebSocket 帧 ----
        var OrigWebSocket = window.WebSocket;
        window.WebSocket = function(url, protocols) {
            var wsUrl = url ? url.toString() : '';
            window.__termCapture.debug.push('[WS] new: ' + wsUrl);

            var ws = (protocols !== undefined)
                ? new OrigWebSocket(url, protocols)
                : new OrigWebSocket(url);

            // 只拦截 socket.io 的 WebSocket 连接
            if (wsUrl.indexOf('socket.io') !== -1) {
                // 重置断连标志（新连接建立）
                window.__termCapture.wsDisconnected = false;

                ws.addEventListener('message', function(event) {
                    if (typeof event.data === 'string') {
                        window.__termCapture.messages.push(event.data);
                    } else if (event.data instanceof ArrayBuffer) {
                        // 二进制帧：尝试解码为 UTF-8 字符串
                        try {
                            var text = new TextDecoder('utf-8').decode(event.data);
                            if (text) window.__termCapture.messages.push(text);
                        } catch(e) {}
                    } else if (event.data instanceof Blob) {
                        // Blob 类型：异步读取
                        var reader = new FileReader();
                        reader.onload = function() {
                            if (reader.result) window.__termCapture.messages.push(reader.result);
                        };
                        reader.readAsText(event.data);
                    }
                });

                // ---- 监听 WebSocket 断连/错误，用于 Python 侧快速感知 ----
                ws.addEventListener('close', function(event) {
                    window.__termCapture.wsDisconnected = true;
                    window.__termCapture.debug.push(
                        '[WS] close: code=' + event.code + ' reason=' + (event.reason || '(none)')
                    );
                });
                ws.addEventListener('error', function() {
                    window.__termCapture.wsDisconnected = true;
                    window.__termCapture.debug.push('[WS] error');
                });
            }

            return ws;
        };
        window.WebSocket.prototype = OrigWebSocket.prototype;
        window.WebSocket.CONNECTING = OrigWebSocket.CONNECTING;
        window.WebSocket.OPEN = OrigWebSocket.OPEN;
        window.WebSocket.CLOSING = OrigWebSocket.CLOSING;
        window.WebSocket.CLOSED = OrigWebSocket.CLOSED;

        // ---- Hook XHR: 捕获 HTTP 长轮询响应 ----
        var origXHROpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {
            this.__captureUrl = url ? url.toString() : '';
            return origXHROpen.apply(this, arguments);
        };

        var origXHRSend = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.send = function() {
            var self = this;
            if (self.__captureUrl && self.__captureUrl.indexOf('socket.io') !== -1) {
                self.addEventListener('load', function() {
                    try {
                        if (self.responseText && self.responseText !== 'ok') {
                            window.__termCapture.messages.push(self.responseText);
                        }
                    } catch(e) {}
                });
            }
            return origXHRSend.apply(this, arguments);
        };

        // ---- Hook fetch: 捕获 fetch 方式的轮询 (以防万一) ----
        var origFetch = window.fetch;
        if (origFetch) {
            window.fetch = function(input, init) {
                var url = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
                var p = origFetch.apply(this, arguments);
                if (url.indexOf('socket.io') !== -1) {
                    p.then(function(response) {
                        return response.clone().text().then(function(text) {
                            if (text) {
                                window.__termCapture.messages.push(text);
                            }
                        });
                    }).catch(function() {});
                }
                return p;
            };
        }
    })();
    """

    ANSI_ESCAPE = re.compile(
        r'\x1b\[[0-9;]*[a-zA-Z]'    # CSI 序列: ESC [ ... 字母 (含光标移动/清除等)
        r'|\x1b\][^\x07]*\x07'       # OSC 序列: ESC ] ... BEL
        r'|\x1b[()][AB012]'          # 字符集切换
        r'|\x1b[>=]'                 # 键盘模式
        r'|\x1b\[\?[0-9;]*[hl]'     # DEC 私有模式设置/重置
        r'|\r'                       # 回车符
    )

    def __init__(self, cols=80, rows=24):
        self.cols = cols
        self.rows = rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.raw_buffer = ""       # 累积所有原始终端数据
        self.ws_disconnected = False  # WebSocket 断连标志
        self._page = None
        self._attached = False

    def attach(self, page):
        """
        在浏览器中注入 JS hook 并注册监听器。
        必须在导航到 /ssh/host 之前调用。

        hook 原理: 在页面加载前拦截 WebSocket 构造函数、XHR.open/send、fetch，
        将所有 socket.io 相关的传入数据存储到 window.__termCapture.messages，
        然后 Python 端通过 page.evaluate() 读取。
        """
        if self._attached:
            return

        self._page = page

        # 注入 JS hook（在每次页面导航时自动执行）
        page.context.add_init_script(self._JS_HOOK)

        self._attached = True

    def reset(self):
        """重置虚拟终端状态，为新的网关会话做准备。"""
        self.screen.reset()
        self.raw_buffer = ""
        self.ws_disconnected = False
        # 同时清空浏览器端的缓存
        if self._page:
            try:
                self._page.evaluate("""() => {
                    if (window.__termCapture) {
                        window.__termCapture.messages = [];
                        window.__termCapture.debug = [];
                        window.__termCapture.wsDisconnected = false;
                    }
                }""")
            except Exception:
                pass

    def _pull_browser_data(self):
        """
        从浏览器端拉取 JS hook 捕获的新数据。
        将 window.__termCapture.messages 中的消息取出并解析。
        同时检测 WebSocket 断连状态。
        """
        if self._page is None:
            return

        try:
            result = self._page.evaluate("""() => {
                if (!window.__termCapture) return { messages: [], debug: [], wsDisconnected: false };
                var msgs = window.__termCapture.messages.splice(0);
                var dbg = window.__termCapture.debug.splice(0);
                return { messages: msgs, debug: dbg, wsDisconnected: !!window.__termCapture.wsDisconnected };
            }""")
        except Exception:
            return

        # 检测 WebSocket 断连
        if result.get('wsDisconnected', False) and not self.ws_disconnected:
            self.ws_disconnected = True
            logger.warning("[TermCapture] WebSocket 连接已断开")

        # 调试信息
        for dbg in result.get('debug', []):
            logger.debug(f"[TermCapture] {dbg}")

        messages = result.get('messages', [])
        if messages:
            logger.debug(f"[TermCapture] pulled {len(messages)} messages from browser")

        # 解析每条消息
        for i, msg in enumerate(messages):
            if isinstance(msg, str):
                preview = msg[:100].replace('\n', '\\n').replace('\r', '\\r')
                logger.debug(f"[TermCapture] msg[{i}] len={len(msg)}: {preview}{'...' if len(msg)>100 else ''}")
                self._parse_message(msg)

    def _parse_message(self, raw):
        """
        解析一条原始消息，支持多种 Engine.IO/Socket.IO 版本格式:
        - Engine.IO v4: 多包用 \\x1e 分隔
        - Engine.IO v3: 多包用 <length>:<data> 格式拼接
        - Socket.IO v0.x: 多包用 \\ufffd 分隔
        - 单包: 直接是 42["data","..."] 或 5:::{"name":"data","args":[...]}
        """
        if not raw:
            return

        # ---- 格式 1: Engine.IO v4 分隔符 ----
        if '\x1e' in raw:
            for packet in raw.split('\x1e'):
                packet = packet.strip()
                if packet:
                    self._parse_single_packet(packet)
            return

        # ---- 格式 2: Socket.IO v0.x 分隔符 ----
        if '\ufffd' in raw:
            parts = re.split(r'\ufffd\d*\ufffd', raw)
            for part in parts:
                part = part.strip()
                if part:
                    self._parse_single_packet(part)
            return

        # ---- 格式 3: Engine.IO v3 长度前缀格式 <length>:<data> ----
        # 检测特征: 以数字开头，包含冒号，且冒号前的数字不像是 Socket.IO 包类型
        # 例如: "96:42[\"data\",\"hello\"]" 或 "2:40"
        if raw and raw[0].isdigit() and ':' in raw:
            packets = self._parse_engineio_v3_payload(raw)
            if packets:
                for packet in packets:
                    self._parse_single_packet(packet)
                return

        # ---- 单个包 ----
        self._parse_single_packet(raw.strip())

    def _parse_engineio_v3_payload(self, body):
        """
        解析 Engine.IO v3 的 HTTP polling 响应体。
        格式: <length1>:<packet1><length2>:<packet2>...
        例如: "2:4097:42[\"data\",\"Last login...\"]"
        其中 2 是 "40" 的长度，97 是后面包的长度。
        """
        packets = []
        i = 0
        try:
            while i < len(body):
                # 跳过空白
                while i < len(body) and body[i] in ' \t\r\n':
                    i += 1
                if i >= len(body):
                    break

                # 读取长度（直到遇到 ':'）
                colon_idx = body.index(':', i)
                length_str = body[i:colon_idx]

                # 如果长度部分不是纯数字，说明不是 v3 格式
                if not length_str.isdigit():
                    return None

                length = int(length_str)
                start = colon_idx + 1
                end = start + length

                if end > len(body):
                    # 长度超出，可能不是 v3 格式，或数据不完整
                    # 尝试把剩余部分作为一个包
                    packets.append(body[start:])
                    break

                packet = body[start:end]
                packets.append(packet)
                i = end

        except (ValueError, IndexError):
            # 解析失败，说明可能不是 v3 格式
            if not packets:
                return None

        return packets if packets else None

    def _parse_single_packet(self, packet):
        """
        解析单个 Socket.IO 数据包，提取终端数据。
        兼容 Socket.IO v0.x 和 v2+ 两种格式。
        """
        if not packet:
            return

        # ---- 格式 1: Socket.IO v2+ (Engine.IO v3/v4) ----
        # EVENT 消息: 42["event_name", data]
        # Engine.IO type=4 (message) + Socket.IO type=2 (event) = 前缀 "42"
        if packet.startswith('42'):
            try:
                arr = json.loads(packet[2:])
                if isinstance(arr, list) and len(arr) >= 2:
                    self._handle_event(arr[0], arr[1])
            except (json.JSONDecodeError, IndexError, TypeError):
                pass
            return

        # ---- 格式 2: Socket.IO v0.x ----
        # EVENT 消息: 5:::{"name":"data","args":["text"]}
        # 或带 endpoint: 5:id:/endpoint:{"name":"data","args":["text"]}
        if packet.startswith('5'):
            try:
                # 格式: type:id:endpoint:data
                # 用 : 分隔，最多分4段
                parts = packet.split(':', 3)
                if len(parts) >= 4 and parts[3]:
                    obj = json.loads(parts[3])
                    event_name = obj.get('name', '')
                    args = obj.get('args', [])
                    if args and len(args) > 0:
                        self._handle_event(event_name, args[0])
            except (json.JSONDecodeError, IndexError, TypeError, KeyError):
                pass
            return

        # ---- 格式 3: 纯 JSON (某些自定义实现) ----
        if packet.startswith('{') or packet.startswith('['):
            try:
                obj = json.loads(packet)
                if isinstance(obj, dict) and 'name' in obj and 'args' in obj:
                    args = obj.get('args', [])
                    if args:
                        self._handle_event(obj['name'], args[0])
            except (json.JSONDecodeError, TypeError):
                pass

    def _handle_event(self, event_name, data):
        """处理解析出的 Socket.IO 事件"""
        if event_name == 'data':
            if isinstance(data, str):
                preview = data[:80].replace('\n', '\\n').replace('\r', '\\r')
                logger.debug(f"[TermCapture] +data ({len(data)} chars): {preview}")
                self.raw_buffer += data
                self.stream.feed(data)
        elif event_name == 'resize':
            if isinstance(data, dict):
                new_cols = data.get('cols', self.cols)
                new_rows = data.get('rows', self.rows)
                self.screen.resize(new_rows, new_cols)
                self.cols = new_cols
                self.rows = new_rows

    def get_screen_text(self) -> str:
        """
        获取当前虚拟屏幕内容（精确还原终端可见区域显示）。
        返回值等价于肉眼看到的终端画面。
        """
        self._pull_browser_data()
        return '\n'.join(line.rstrip() for line in self.screen.display)

    def get_raw_text(self) -> str:
        """
        获取累积的所有终端输出（去除 ANSI 转义码后的纯文本）。
        包含从会话开始至今的全部输出，适合全文搜索匹配。
        """
        self._pull_browser_data()
        return self.ANSI_ESCAPE.sub('', self.raw_buffer)

    def contains(self, target: str) -> bool:
        """检查累积的终端输出中是否包含指定文本"""
        return target in self.get_raw_text()

    def count(self, target: str) -> int:
        """统计目标文本在累积输出中出现的次数"""
        return self.get_raw_text().count(target)


# ============================================================
# 加载配置文件
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

if not os.path.isfile(CONFIG_FILE):
    logger.error(f"未找到配置文件: {CONFIG_FILE}")
    logger.error(f"  请复制 config.json 到脚本同级目录并填入实际配置")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    try:
        _config = json.load(f)
    except json.JSONDecodeError as e:
        logger.error(f"配置文件 JSON 格式错误: {e}")
        sys.exit(1)

# 必填项
BASE_URL = _config.get("base_url", "http://YOUR_AC_IP")
BROWSER_MODE = _config.get("browser_mode", "persistent")
AC_USERNAME = _config.get("ac_username", "admin")
AC_PASSWORD = _config.get("ac_password", "1q2w#E$R")
BLUE_PASSWORD = _config.get("blue_password", "xxx")
SU_PASSWORD = _config.get("su_password", "xxx")
AUTO_FETCH_GATEWAYS = _config.get("auto_fetch_gateways", False)
GATEWAY_MACS = _config.get("gateway_macs", [])
SHELL_COMMANDS = _config.get("shell_commands", [])

# 可选项（有默认值）
CDP_URL = _config.get("cdp_url", "http://localhost:9222")
TIMEOUT_PAGE_LOAD = _config.get("timeout_page_load", 30000)
TIMEOUT_TERMINAL_READY = _config.get("timeout_terminal_ready", 30000)
TIMEOUT_PROMPT_WAIT = _config.get("timeout_prompt_wait", 30000)
TIMEOUT_COMMAND_WAIT = _config.get("timeout_command_wait", 30000)
TYPE_DELAY = _config.get("type_delay", 50)
DEVTOOLS = _config.get("devtools", False)

# 日志级别（可选: DEBUG / INFO / WARNING）
_log_level_name = _config.get("log_level", "INFO").upper()
logger.setLevel(getattr(logging, _log_level_name, logging.INFO))

# 命令输出解析规则（可选）
COMMAND_PARSERS = _config.get("command_parsers", [])

# 会话持久化目录（固定在脚本目录下）
BROWSER_PROFILE_DIR = os.path.join(SCRIPT_DIR, ".browser_profile")

# ============================================================
# 配置加载完成
# ============================================================


def get_basic_auth_header(username: str, password: str) -> dict:
    """生成 Basic Auth 请求头"""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def login_ac(page: Page):
    """登录 AC 管理平台"""
    logger.info("正在登录 AC 管理平台...")
    page.goto(f"{BASE_URL}/session?view")
    page.wait_for_timeout(1000)

    page.locator('input[name="username"]').fill(AC_USERNAME)
    page.wait_for_timeout(300)

    page.locator('input[name="password"]').fill(AC_PASSWORD)
    page.wait_for_timeout(300)

    page.locator('button:has-text("Login"), button:has-text("登录")').click()

    # 等待跳转到 dashboard（如果有 token 验证，这里会超时）
    try:
        page.wait_for_url(f"{BASE_URL}/dashboard?view", timeout=10000)
        logger.info("AC 管理平台登录成功")
    except Exception:
        # 可能遇到了 token 验证或其他中间页面
        logger.info("登录后未直接跳转到 dashboard，可能需要 token 验证")
        logger.info(">>> 请在弹出的浏览器中手动完成验证 <<<")
        logger.info("完成后脚本会自动继续...")
        # 等待用户手动完成验证，最多等 5 分钟
        page.wait_for_url(f"{BASE_URL}/dashboard?view", timeout=300000)
        logger.info("AC 管理平台登录成功（手动验证完成）")


def check_session_valid(page: Page) -> bool:
    """检查当前会话是否有效（是否已登录）"""
    try:
        page.goto(f"{BASE_URL}/dashboard?view", timeout=TIMEOUT_PAGE_LOAD)
        page.wait_for_timeout(2000)
        # 如果被重定向到登录页，说明会话无效
        current_url = page.url
        if "session" in current_url or "login" in current_url:
            return False
        return True
    except Exception:
        return False


def fetch_online_gateways(page: Page) -> list:
    """
    从 AC 平台获取在线网关列表。
    调用 GET /ap?status=online，返回网关对象数组。
    """
    logger.info("正在从 AC 获取在线网关列表...")
    fetch_timeout = TIMEOUT_PAGE_LOAD
    try:
        data = page.evaluate(f"""async () => {{
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), {fetch_timeout});
            let resp;
            try {{
                resp = await fetch("{BASE_URL}/ap?status=online", {{
                    credentials: "same-origin",
                    headers: {{ "X-Requested-With": "XMLHttpRequest" }},
                    signal: controller.signal
                }});
            }} catch (e) {{
                clearTimeout(timer);
                if (e.name === 'AbortError') {{
                    throw new Error("获取在线网关列表超时 ({fetch_timeout}ms)");
                }}
                throw e;
            }}
            clearTimeout(timer);
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            return await resp.json();
        }}""")
        if isinstance(data, list):
            logger.info(f"获取到 {len(data)} 个在线网关")
            return data
        logger.warning(f"AC 返回的数据格式异常（非数组）: {str(data)[:200]}")
        return []
    except Exception as e:
        logger.error(f"获取在线网关列表失败: {e}")
        return []


def extract_gateway_info(gw: dict) -> dict:
    """
    从 AC API 返回的网关对象中提取关键元数据字段。
    这些字段会合并到最终的结果 JSON 中。
    """
    container = gw.get("container") or {}
    apps = container.get("apps", [])
    app_version = ""
    if isinstance(apps, list) and apps:
        app = apps[0]
        app_version = f"{app.get('name', '')}.{app.get('version', '')}"

    return {
        "mac": gw.get("mac", ""),
        "name": gw.get("name", ""),
        "sn": gw.get("reserved3", ""),
        "status": gw.get("status", ""),
        "uplink": (gw.get("ap") or {}).get("uplink", ""),
        "version": gw.get("version", ""),
        "containerVersion": container.get("version", ""),
        "appVersion": app_version,
    }


def page_fetch(page: Page, url: str, method: str = "POST",
               body: dict = None, extra_headers: dict = None,
               add_csrf: bool = True, redirect: str = "follow",
               timeout: int = None) -> dict:
    """
    在页面内通过 fetch() 发送请求。
    自动携带浏览器的所有 cookies。
    add_csrf=True 时自动从 localStorage key 't' 读取 CSRF token 并注入 body。
    redirect: "follow"(默认，自动跟随重定向) / "manual"(不跟随，返回原始 3xx 响应)
    timeout: 请求超时毫秒数，默认使用 TIMEOUT_PAGE_LOAD（30s）
    返回 { "ok": bool, "status": int, "text": str, "redirected": bool, "url": str }
    """
    if timeout is None:
        timeout = TIMEOUT_PAGE_LOAD
    body_js = json.dumps(body) if body is not None else "{}"
    extra_headers_js = json.dumps(extra_headers) if extra_headers else "{}"

    result = page.evaluate(f"""async () => {{
        // 构建请求体
        let bodyObj = {body_js};

        // 从 localStorage 读取 CSRF token (key='t')，注入 body
        const addCsrf = {'true' if add_csrf else 'false'};
        if (addCsrf) {{
            const csrfToken = localStorage.getItem('t');
            if (csrfToken) {{
                bodyObj.csrf = csrfToken;
            }}
        }}

        const headers = {{
            "Content-Type": "application/json",
            ...{extra_headers_js}
        }};

        // 超时控制: 使用 AbortController 避免网络异常时永久挂起
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), {timeout});
        let resp;
        try {{
            resp = await fetch("{url}", {{
                method: "{method}",
                headers: headers,
                body: JSON.stringify(bodyObj),
                credentials: "same-origin",
                redirect: "{redirect}",
                signal: controller.signal
            }});
        }} catch (e) {{
            clearTimeout(timer);
            if (e.name === 'AbortError') {{
                throw new Error("fetch 超时 ({timeout}ms): {url}");
            }}
            throw e;
        }}
        clearTimeout(timer);

        // redirect: "manual" 时 resp.type 为 "opaqueredirect"，无法读取 body
        let text = '';
        if (resp.type !== 'opaqueredirect') {{
            text = await resp.text();
        }}
        return {{
            ok: resp.ok,
            status: resp.status,
            text: text,
            redirected: resp.redirected,
            url: resp.url
        }};
    }}""")

    # 检测 302 重定向到登录页的情况（session 过期时 AC 会返回 302 → /session?view）
    # fetch redirect:"follow" 会跟随到登录页，返回 200 OK，表面看起来成功
    if result.get("redirected") and any(
        kw in result.get("url", "").lower()
        for kw in ("session", "login")
    ):
        raise RuntimeError(
            f"请求被重定向到登录页 ({result['url']})，会话已过期"
        )

    return result


def enable_ssh(page: Page, mac: str):
    """Step 1: 启用网关 SSH"""
    logger.info(f"[Step 1] 启用 SSH: {mac}")
    url = f"{BASE_URL}/api2/cassia/info?mac={mac}"
    result = page_fetch(page, url, "POST", {"ssh-login": "1"})
    if not result["ok"]:
        raise RuntimeError(
            f"启用 SSH 失败: HTTP {result['status']} - {result['text']}"
        )
    logger.info(f"[Step 1] SSH 启用成功 (HTTP {result['status']})")


def open_tunnel(page: Page, mac: str):
    """Step 2: 开启 SSH 隧道"""
    logger.info(f"[Step 2] 开启 SSH 隧道: {mac}")
    url = f"{BASE_URL}/ap/remote/{mac}?ssh_port=9999&ap=1"
    result = page_fetch(page, url, "POST", {}, redirect="manual")
    # redirect="manual" 时，302 响应会变成 opaqueredirect（status=0），视为成功
    status = result["status"]
    if not result["ok"] and status != 0 and not (300 <= status < 400):
        raise RuntimeError(
            f"开启隧道失败: HTTP {status} - {result['text']}"
        )
    logger.info(f"[Step 2] SSH 隧道开启成功 (HTTP {status})")


def open_ssh_terminal(page: Page):
    """打开 SSH Web Terminal 页面并等待终端加载完成。
    如果页面未自动跳转到 /ssh/host，则主动导航。
    Basic Auth 由 context 的 http_credentials 自动处理。
    """
    logger.info("[Step 3] 打开 SSH Web Terminal...")
    if "/ssh/host" not in page.url:
        page.goto(f"{BASE_URL}/ssh/host", timeout=TIMEOUT_PAGE_LOAD)
    # 校验导航结果：如果被 302 重定向到登录页，说明会话已过期
    current_url = page.url
    if "session" in current_url or "login" in current_url:
        raise RuntimeError(
            f"SSH 终端页面被重定向到登录页 ({current_url})，会话已过期"
        )
    page.wait_for_selector('.xterm', state='visible', timeout=TIMEOUT_TERMINAL_READY)
    logger.info("[Step 3] SSH Web Terminal 已加载")


# 全局终端捕获器实例（在 main() 中初始化）
_terminal_capture: TerminalCapture = None


def read_terminal_buffer(page: Page) -> str:
    """
    读取终端缓冲区内容。
    通过 WebSocket 拦截的数据，使用 pyte 虚拟终端精确还原屏幕文本。
    """
    if _terminal_capture is None:
        return ''
    return _terminal_capture.get_screen_text()


def read_terminal_raw(page: Page) -> str:
    """
    读取终端的全部原始输出（去除 ANSI 转义码的纯文本）。
    包含从会话开始至今的全部输出，适合搜索匹配。
    """
    if _terminal_capture is None:
        return ''
    return _terminal_capture.get_raw_text()


def wait_for_terminal_text(page: Page, target_text: str, timeout: int = None):
    """
    轮询等待终端输出中包含指定文本。
    使用 WebSocket 拦截的原始数据进行匹配（去除 ANSI 转义码后的全文搜索）。
    每 500ms 检查一次。
    WebSocket 断连后有 5 秒宽限期（Socket.IO 可能回退到 HTTP 轮询传输数据）。
    """
    if timeout is None:
        timeout = TIMEOUT_PROMPT_WAIT

    ws_disconnect_time = None  # WebSocket 断连首次检测到的时间
    WS_DISCONNECT_GRACE = 5.0  # 断连后的宽限期（秒）

    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        raw_text = read_terminal_raw(page)
        if target_text in raw_text:
            return raw_text
        # WebSocket 断连检测（带宽限期，避免 Socket.IO 传输切换时误判）
        if _terminal_capture and _terminal_capture.ws_disconnected:
            if ws_disconnect_time is None:
                ws_disconnect_time = time.time()
                logger.debug(f"[等待] WebSocket 已断开，进入 {WS_DISCONNECT_GRACE} 秒宽限期")
            elif time.time() - ws_disconnect_time > WS_DISCONNECT_GRACE:
                raise ConnectionError(
                    f"WebSocket 连接已断开超过 {WS_DISCONNECT_GRACE} 秒，无法继续等待终端文本 '{target_text}'"
                )
        page.wait_for_timeout(500)

    # 超时，打印当前终端内容以便调试
    current_screen = read_terminal_buffer(page)
    current_raw = read_terminal_raw(page)
    raise TimeoutError(
        f"等待终端文本 '{target_text}' 超时 ({timeout}ms)\n"
        f"当前屏幕内容:\n{current_screen}\n"
        f"原始输出 (最后500字符):\n{current_raw[-500:]}"
    )


def wait_for_new_terminal_text(page: Page, target_text: str,
                                baseline: str, timeout: int = None):
    """
    等待终端出现新的指定文本（排除已有的 baseline 内容）。
    用于区分命令执行前后 prompt 的变化。
    使用原始文本进行匹配。
    WebSocket 断连后有 5 秒宽限期（Socket.IO 可能回退到 HTTP 轮询传输数据）。
    """
    if timeout is None:
        timeout = TIMEOUT_COMMAND_WAIT

    ws_disconnect_time = None  # WebSocket 断连首次检测到的时间
    WS_DISCONNECT_GRACE = 5.0  # 断连后的宽限期（秒）

    # 统计 baseline 中 target_text 出现的次数
    baseline_count = baseline.count(target_text)

    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        raw_text = read_terminal_raw(page)
        current_count = raw_text.count(target_text)
        if current_count > baseline_count:
            return raw_text
        # WebSocket 断连检测（带宽限期，避免 Socket.IO 传输切换时误判）
        if _terminal_capture and _terminal_capture.ws_disconnected:
            if ws_disconnect_time is None:
                ws_disconnect_time = time.time()
                logger.debug(f"[等待] WebSocket 已断开，进入 {WS_DISCONNECT_GRACE} 秒宽限期")
            elif time.time() - ws_disconnect_time > WS_DISCONNECT_GRACE:
                raise ConnectionError(
                    f"WebSocket 连接已断开超过 {WS_DISCONNECT_GRACE} 秒，无法继续等待终端文本 '{target_text}'"
                )
        page.wait_for_timeout(500)

    current_screen = read_terminal_buffer(page)
    current_raw = read_terminal_raw(page)
    raise TimeoutError(
        f"等待新的终端文本 '{target_text}' 超时 ({timeout}ms)\n"
        f"当前屏幕内容:\n{current_screen}\n"
        f"原始输出 (最后500字符):\n{current_raw[-500:]}"
    )


def type_in_terminal(page: Page, text: str):
    """
    在 xterm.js 终端中输入文本并按回车。
    xterm.js 使用隐藏的 textarea (.xterm-helper-textarea) 接收键盘输入。
    """
    page.locator('.xterm-helper-textarea').focus()
    page.keyboard.type(text, delay=TYPE_DELAY)
    page.keyboard.press('Enter')


def type_password_in_terminal(page: Page, password: str):
    """
    在终端中输入密码（不按回车前先等待一下，确保 prompt 已就绪）。
    密码输入后按回车确认。
    """
    page.wait_for_timeout(300)
    page.locator('.xterm-helper-textarea').focus()
    page.keyboard.type(password, delay=TYPE_DELAY)
    page.keyboard.press('Enter')


def check_blue_user_prompt(page: Page):
    """Step 4: 等待终端就绪（检测 shell prompt 出现）"""
    logger.info("[Step 4] 等待终端就绪...")
    try:
        # 等待 SSH 连接建立并出现 shell prompt（$ 或 # 或 >）
        wait_for_terminal_text(page, "$", timeout=TIMEOUT_PROMPT_WAIT)
        logger.info("[Step 4] 终端就绪（检测到 prompt）")
    except TimeoutError:
        # 如果超时，尝试发送空行唤醒终端
        logger.warning("[Step 4] 未检测到 prompt，尝试发送回车唤醒...")
        type_in_terminal(page, "")
        page.wait_for_timeout(2000)
        type_in_terminal(page, "")
        page.wait_for_timeout(3000)
        logger.info("[Step 4] 终端就绪（已发送回车）")


def switch_to_root(page: Page):
    """Step 5: 切换到 root 用户 (su)，使用智能等待"""
    logger.info("[Step 5] 切换到 root 用户...")

    # 发送空行确保终端就绪
    type_in_terminal(page, "")
    page.wait_for_timeout(1000)

    # 记录当前输出作为 baseline
    baseline = read_terminal_raw(page)

    # 输入 su 命令
    type_in_terminal(page, "su")

    # 等待 Password 提示出现
    try:
        wait_for_terminal_text(page, "assword", timeout=TIMEOUT_COMMAND_WAIT)
        logger.info("[Step 5] 检测到密码提示")
    except TimeoutError:
        logger.warning("[Step 5] 未检测到密码提示，继续尝试输入密码...")

    # 输入 root 密码
    type_password_in_terminal(page, SU_PASSWORD)

    # 等待 root prompt (#) 出现
    try:
        wait_for_new_terminal_text(page, "#", baseline, timeout=TIMEOUT_COMMAND_WAIT)
        logger.info("[Step 5] 已切换到 root（检测到 # prompt）")
    except TimeoutError:
        # 等待一段时间作为 fallback
        page.wait_for_timeout(3000)
        logger.warning("[Step 5] su 切换命令已执行（超时 fallback）")


def _extract_command_output(new_raw: str, baseline: str, cmd: str) -> str:
    """
    从终端原始文本中提取某条命令的输出。
    new_raw: 命令执行后的完整终端文本
    baseline: 命令执行前的终端文本
    cmd: 输入的命令字符串

    提取逻辑: 取 new_raw 相对于 baseline 的增量部分，
    去掉首行的命令回显和末尾的 prompt 行。
    """
    diff = new_raw[len(baseline):]
    lines = diff.split('\n')

    # 去掉首行命令回显（可能包含命令本身）
    if lines and cmd.strip() in lines[0]:
        lines = lines[1:]

    # 去掉末尾 prompt 行（以 # 或 $ 结尾的行）
    while lines and re.match(r'^\s*\S+[@:]\S*[#$]\s*$', lines[-1].strip()):
        lines = lines[:-1]

    return '\n'.join(lines).strip()


def _parse_command_output(cmd: str, output_text: str) -> dict:
    """
    根据 COMMAND_PARSERS 配置，从命令输出中提取结构化字段。
    返回 { field_name: value, ... }，无匹配则返回空 dict。
    """
    result = {}
    for parser in COMMAND_PARSERS:
        if parser.get("command") == cmd:
            for field_name, pattern in parser.get("extract", {}).items():
                m = re.search(pattern, output_text, re.MULTILINE)
                if m:
                    result[field_name] = m.group(1)
            break
    return result


def _indent_text(text: str, prefix: str = "    ") -> str:
    """给多行文本添加缩进前缀"""
    return '\n'.join(prefix + line for line in text.split('\n'))


def execute_shell_commands(page: Page, mac: str) -> dict:
    """
    Step 6: 执行 shell 命令列表，使用智能等待检测命令完成。
    返回解析结果字典（由 command_parsers 配置决定提取哪些字段）。
    """
    gateway_result = {}

    if not SHELL_COMMANDS:
        logger.info("[Step 6] 无需执行的 shell 命令，跳过")
        return gateway_result

    logger.info(f"[Step 6] 开始执行 {len(SHELL_COMMANDS)} 条 shell 命令...")

    for i, cmd in enumerate(SHELL_COMMANDS, 1):
        logger.info(f"[Step 6] [{i}/{len(SHELL_COMMANDS)}] 执行: {cmd}")

        # 记录命令执行前的输出
        baseline = read_terminal_raw(page)

        type_in_terminal(page, cmd)

        # 等待命令完成（检测新的 prompt 出现）
        cmd_output = ""
        try:
            new_raw = wait_for_new_terminal_text(
                page, "#", baseline, timeout=TIMEOUT_COMMAND_WAIT
            )
            cmd_output = _extract_command_output(new_raw, baseline, cmd)
        except TimeoutError:
            # 超时 fallback: 等待固定时间，尽量提取已有输出
            page.wait_for_timeout(3000)
            new_raw = read_terminal_raw(page)
            cmd_output = _extract_command_output(new_raw, baseline, cmd)
            logger.warning(f"[Step 6] [{i}/{len(SHELL_COMMANDS)}] 等待超时，已提取部分输出")

        # INFO 级别打印命令输出
        if cmd_output:
            logger.info(f"[Step 6] [{i}/{len(SHELL_COMMANDS)}] 输出:\n{_indent_text(cmd_output)}")
        else:
            logger.info(f"[Step 6] [{i}/{len(SHELL_COMMANDS)}] 输出: (空)")

        # 结构化解析
        parsed = _parse_command_output(cmd, cmd_output)
        if parsed:
            gateway_result.update(parsed)
            logger.info(
                f"[Step 6] [{i}/{len(SHELL_COMMANDS)}] 解析: "
                f"{json.dumps(parsed, ensure_ascii=False)}"
            )

        logger.info(f"[Step 6] [{i}/{len(SHELL_COMMANDS)}] 完成")

    # 所有命令执行完毕后截图
    screenshots_dir = os.path.join(SCRIPT_DIR, "emmc_results", "screenshots")
    os.makedirs(screenshots_dir, exist_ok=True)
    safe_mac = mac.replace(":", "-")
    screenshot_path = os.path.join(screenshots_dir, f"{safe_mac}.png")
    page.screenshot(path=screenshot_path, full_page=True)
    logger.info(f"[Step 6] 截图已保存: {screenshot_path}")

    # 完整终端文本仅在 DEBUG 级别输出
    raw_text = read_terminal_raw(page)
    logger.debug(f"[Step 6] 终端完整输出:\n{'─'*40}\n"
                 f"{raw_text[-2000:] if len(raw_text) > 2000 else raw_text}\n"
                 f"{'─'*40}")

    return gateway_result


def _is_session_expired_error(e: Exception) -> bool:
    """判断异常是否由会话过期引起（HTTP 401 或重定向到 session/login 页面）"""
    msg = str(e).lower()
    return "401" in msg or "session" in msg or "login" in msg


def _is_network_error(e: Exception) -> bool:
    """判断异常是否由网络问题引起（连接拒绝/重置/超时/断开等）"""
    # ConnectionError 由 WebSocket 断连检测主动抛出
    if isinstance(e, ConnectionError):
        return True
    msg = str(e).lower()
    network_keywords = [
        "net::err_connection",      # ERR_CONNECTION_REFUSED, ERR_CONNECTION_RESET, ...
        "net::err_network",         # ERR_NETWORK_CHANGED, ...
        "net::err_internet",        # ERR_INTERNET_DISCONNECTED
        "net::err_timed_out",       # ERR_TIMED_OUT
        "net::err_name_not",        # ERR_NAME_NOT_RESOLVED
        "fetch 超时",                # page_fetch() AbortController 超时
        "获取在线网关列表超时",         # fetch_online_gateways() 超时
        "websocket 连接已断开",       # TerminalCapture 断连检测
        "target page, context or browser has been closed",  # Playwright 页面崩溃
        "connection refused",
        "connection reset",
        "econnrefused",
        "econnreset",
        "etimedout",
        "enetunreach",
    ]
    return any(kw in msg for kw in network_keywords)


def _save_gateway_result(mac: str, gw_info: dict, cmd_result: dict):
    """
    将网关解析结果保存为 JSON 文件。
    gw_info: 网关元数据（mac, name, sn, uplink, version 等）
    cmd_result: 命令输出解析结果（devName, EST_TYP_A 等）
    """
    # 元数据在前，命令解析结果在后
    result = {**gw_info, **cmd_result}
    if not result:
        return
    gateways_dir = os.path.join(SCRIPT_DIR, "emmc_results", "gateways")
    os.makedirs(gateways_dir, exist_ok=True)
    safe_mac = mac.replace(":", "-")
    result_path = os.path.join(gateways_dir, f"{safe_mac}.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"解析结果: {json.dumps(result, ensure_ascii=False)}")
    logger.info(f"结果已保存: {result_path}")


def process_gateway(context: BrowserContext, page: Page, gw_info: dict, index: int, total: int):
    """
    处理单个网关的完整流程（含会话过期/网络异常自动重试）。
    gw_info: 网关信息字典，至少包含 "mac" 键，
             auto_fetch 模式下还包含 name/sn/uplink/version 等元数据。
    """
    mac = gw_info["mac"]
    gw_name = gw_info.get("name", "")
    display = f"{mac} ({gw_name})" if gw_name else mac

    logger.info(f"\n{'='*60}")
    logger.info(f"[{index}/{total}] 开始处理网关: {display}")
    logger.info(f"{'='*60}")

    # 人工操作模式
    # page.wait_for_timeout(30000000)

    for attempt in range(3):  # 最多尝试 3 次（支持复合故障：如网络异常重试后又遇会话过期）
        try:
            # 每次尝试都重置终端捕获器，避免残留数据导致误匹配
            if _terminal_capture is not None:
                _terminal_capture.reset()

            page.wait_for_timeout(3000)

            # Step 1: 启用 SSH
            enable_ssh(page, mac)
            page.wait_for_timeout(3000)  # 等待 SSH 服务启动

            # Step 2: 开启 SSH 隧道（页面会自动跳转到 /ssh/host）
            open_tunnel(page, mac)

            # Step 3: 打开 SSH Web Terminal
            open_ssh_terminal(page)

            # Step 4: 检查 blue 用户 prompt
            check_blue_user_prompt(page)

            # Step 5: 切换到 root 用户
            switch_to_root(page)

            # Step 6: 执行 shell 命令
            cmd_result = execute_shell_commands(page, mac)

            # 保存解析结果（元数据 + 命令解析结果）
            _save_gateway_result(mac, gw_info, cmd_result)

            logger.info(f"\n[{index}/{total}] 网关 {display} 处理完成 ✓")
            return True  # 成功

        except Exception as e:
            if attempt < 2 and _is_session_expired_error(e):
                # 疑似会话过期，自动重登录后重试
                logger.warning(f"\n[{index}/{total}] 网关 {display} 疑似会话过期（第{attempt+1}次），自动重新登录后重试...")
                logger.warning(f"  原始错误: {e}")
                try:
                    page.goto(f"{BASE_URL}/dashboard?view", timeout=TIMEOUT_PAGE_LOAD)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                login_ac(page)
                continue  # 重试

            if attempt < 2 and _is_network_error(e):
                # 疑似网络问题，等待后重试
                logger.warning(f"\n[{index}/{total}] 网关 {display} 疑似网络异常（第{attempt+1}次），等待 5 秒后重试...")
                logger.warning(f"  原始错误: {e}")
                try:
                    page.wait_for_timeout(5000)
                    page.goto(f"{BASE_URL}/dashboard?view", timeout=TIMEOUT_PAGE_LOAD)
                    page.wait_for_timeout(1000)
                except Exception:
                    # 如果连导航都失败，再等一段时间
                    try:
                        time.sleep(5)
                    except Exception:
                        pass
                continue  # 重试

            # 已用完重试次数，或非可恢复错误
            error_msg = str(e)
            logger.error(f"\n[{index}/{total}] 网关 {display} 处理失败 ✗")
            logger.error(f"  错误: {e}")
            traceback.print_exc()
            # 即使失败也保存已有的元数据（version/containerVersion 等）
            _save_gateway_result(mac, gw_info, {"_error": error_msg})
            return error_msg  # 返回错误信息（非 True 即失败）

        finally:
            # 导航回 dashboard，为下一个网关做准备
            try:
                page.goto(f"{BASE_URL}/dashboard?view", timeout=TIMEOUT_PAGE_LOAD)
                page.wait_for_timeout(1000)
            except Exception:
                pass

    # 重试次数用尽仍失败，也保存已有的元数据
    _save_gateway_result(mac, gw_info, {"_error": "重试次数用尽仍失败"})
    return "重试次数用尽仍失败"


MODE_LABELS = {
    "persistent": "Persistent（Chromium + 会话持久化）",
    "cdp": "CDP 连接",
    "login": "自动登录",
}


def main():
    """主入口函数"""
    # 参数校验
    if BASE_URL == "http://YOUR_AC_IP":
        logger.error("请先修改 BASE_URL 为实际的 AC 管理平台地址")
        sys.exit(1)

    if not AUTO_FETCH_GATEWAYS and not GATEWAY_MACS:
        logger.error("gateway_macs 列表为空且 auto_fetch_gateways 未开启，无可处理的网关")
        sys.exit(1)

    if BROWSER_MODE not in ("persistent", "cdp", "login"):
        logger.error(f"不支持的 BROWSER_MODE: {BROWSER_MODE}")
        logger.error(f"  可选值: persistent, cdp, login")
        sys.exit(1)

    logger.info("Cassia Gateway SSH 批量自动化脚本")
    logger.info(f"AC 平台地址: {BASE_URL}")
    logger.info(f"网关来源: {'自动获取在线列表' if AUTO_FETCH_GATEWAYS else '配置文件 MAC 列表'}")
    logger.info(f"Shell 命令数量: {len(SHELL_COMMANDS)}")
    logger.info(f"浏览器模式: {MODE_LABELS.get(BROWSER_MODE, BROWSER_MODE)}")
    logger.info("")

    browser = None

    with sync_playwright() as p:
        if BROWSER_MODE == "persistent":
            # =====================================================
            # 方式一 (推荐): Playwright Chromium + 本地会话持久化
            # 使用 Playwright 自带的 Chromium，完全不碰系统 Chrome
            # 会话数据保存在 .browser_profile 目录中
            # =====================================================
            is_first_run = not os.path.isdir(BROWSER_PROFILE_DIR)
            if is_first_run:
                logger.info(f"首次运行，将创建浏览器 profile: {BROWSER_PROFILE_DIR}")
                logger.info("需要登录 AC 平台（仅此一次）")
            else:
                logger.info(f"加载已有浏览器 profile: {BROWSER_PROFILE_DIR}")

            # 不使用 channel="chrome"，使用 Playwright 自带 Chromium
            launch_args = ["--disable-blink-features=AutomationControlled"]
            if DEVTOOLS:
                launch_args.append("--auto-open-devtools-for-tabs")
            context = p.chromium.launch_persistent_context(
                user_data_dir=BROWSER_PROFILE_DIR,
                headless=False,
                no_viewport=True,
                args=launch_args,
                http_credentials={"username": "blue", "password": BLUE_PASSWORD},
            )

            page = context.pages[0] if context.pages else context.new_page()

            # 检查会话是否有效
            if is_first_run or not check_session_valid(page):
                logger.info("会话无效或已过期，需要登录...")
                login_ac(page)
            else:
                logger.info("会话有效，无需登录")

        elif BROWSER_MODE == "cdp":
            # 方式二: 通过 CDP 连接已运行的 Chrome（已登录 AC）
            logger.info(f"正在通过 CDP 连接 Chrome: {CDP_URL}")
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception as e:
                logger.error("无法连接到 Chrome，请确保已启动带远程调试端口的 Chrome:")
                logger.error(f"  macOS: /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222")
                logger.error(f"  Windows: chrome.exe --remote-debugging-port=9222")
                logger.error(f"  错误详情: {e}")
                sys.exit(1)

            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            logger.info("CDP 连接成功，复用已有登录会话")

        else:
            # 方式三: 自动启动浏览器并登录 AC
            logger.info("正在启动浏览器...")
            launch_args = []
            if DEVTOOLS:
                launch_args.append("--auto-open-devtools-for-tabs")
            browser = p.chromium.launch(headless=False, args=launch_args)
            context = browser.new_context(
                http_credentials={"username": "blue", "password": BLUE_PASSWORD},
            )
            page = context.new_page()
            login_ac(page)

        # 监听所有 DELETE 请求，用于调试
        context.on("request", lambda req: logger.debug(f"请求: {req.method} {req.url}") if req.method == "DELETE" else None)

        # 拦截前端的 DELETE /session 请求，阻止 AC 会话被注销（前端 30 分钟计时到期后会主动发此请求）
        def intercept_session_delete(route):
            if route.request.method == "DELETE":
                logger.info(f"已拦截 DELETE {route.request.url} 请求，保持会话有效")
                route.abort()
            else:
                route.continue_()

        context.route(lambda url: "/session" in url, intercept_session_delete)
        logger.info("已注册 DELETE /session 拦截规则，会话将保持有效")

        # 注册 dialog 处理器：自动关闭 alert/confirm/prompt 弹窗并记录内容
        # AC 会话过期时会弹出 alert，如果不处理可能干扰 Playwright 操作
        def _handle_dialog(dialog):
            logger.warning(f"[Dialog] {dialog.type}: {dialog.message}")
            dialog.accept()

        page.on("dialog", _handle_dialog)
        logger.info("已注册 dialog 处理器（自动关闭弹窗并记录）")

        # 初始化终端捕获器（通过 WebSocket 拦截终端数据）
        global _terminal_capture
        _terminal_capture = TerminalCapture()
        _terminal_capture.attach(page)
        logger.info("终端数据捕获器已初始化（WebSocket 拦截模式）")

        # 获取网关列表
        if AUTO_FETCH_GATEWAYS:
            raw_gateways = fetch_online_gateways(page)
            # 保存完整的 AP 列表到 emmc_results/（含所有详细信息）
            if raw_gateways:
                ap_list_dir = os.path.join(SCRIPT_DIR, "emmc_results")
                os.makedirs(ap_list_dir, exist_ok=True)
                ap_list_path = os.path.join(ap_list_dir, "ap_list.json")
                with open(ap_list_path, "w", encoding="utf-8") as f:
                    json.dump(raw_gateways, f, ensure_ascii=False, indent=2)
                logger.info(f"AP 列表已保存: {ap_list_path}（{len(raw_gateways)} 条）")
            # 只处理 container.status == "running" 的网关
            running_gateways = [
                gw for gw in raw_gateways
                if (gw.get("container") or {}).get("status") == "running"
            ]
            skipped = len(raw_gateways) - len(running_gateways)
            if skipped:
                logger.info(f"已过滤 {skipped} 个无 container 或 container 未运行的网关")
            gateways = [extract_gateway_info(gw) for gw in running_gateways]
            if gateways:
                for gw in gateways:
                    logger.info(f"  {gw['mac']} - {gw['name'] or '(未命名)'}")
            else:
                logger.error("未获取到符合条件的在线网关（需 container.status=running），退出")
                sys.exit(1)
        else:
            gateways = [{"mac": mac} for mac in GATEWAY_MACS]
            logger.info(f"使用配置文件中的 {len(gateways)} 个网关")

        total = len(gateways)

        # 逐个处理网关
        success_count = 0
        fail_count = 0
        failed_gateways = []  # 收集失败网关信息

        for i, gw_info in enumerate(gateways, 1):
            result = process_gateway(context, page, gw_info, i, total)
            if result is True:
                success_count += 1
            else:
                fail_count += 1
                # result 为错误信息字符串；记录完整的 gw_info 元数据
                failed_gateways.append({
                    **gw_info,  # 包含 mac, name, sn, version, containerVersion, appVersion 等
                    "error": result if isinstance(result, str) else "未知错误",
                })

        # 保存失败网关到文件
        if failed_gateways:
            fail_dir = os.path.join(SCRIPT_DIR, "emmc_results")
            os.makedirs(fail_dir, exist_ok=True)
            fail_path = os.path.join(fail_dir, "failed_gateways.json")
            with open(fail_path, "w", encoding="utf-8") as f:
                json.dump(failed_gateways, f, ensure_ascii=False, indent=2)
            logger.info(f"失败网关已保存: {fail_path}（{len(failed_gateways)} 条）")

        # 输出汇总
        logger.info(f"\n{'='*60}")
        logger.info(f"[汇总] 处理完成")
        logger.info(f"  总数: {total}")
        logger.info(f"  成功: {success_count}")
        logger.info(f"  失败: {fail_count}")
        if failed_gateways:
            logger.info(f"  失败网关列表:")
            for fg in failed_gateways:
                logger.info(f"    - {fg['mac']} ({fg['name'] or '未命名'}): {fg['error']}")
        logger.info(f"{'='*60}")

        # 保持浏览器窗口打开
        logger.info("\n所有网关处理完毕，浏览器保持打开")
        logger.info("按 Ctrl+C 或关闭浏览器窗口退出")
        try:
            # 阻塞等待，直到用户手动关闭
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("\n用户退出")


if __name__ == "__main__":
    main()
