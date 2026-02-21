"""
SSH Web Terminal 捕获与操作

核心类 TerminalCapture: 通过在浏览器端注入 JS hook (拦截 WebSocket/XHR/fetch)，
捕获 webssh2/Socket.IO 的终端数据流，配合 pyte 虚拟终端模拟器，
精确提取 xterm.js Canvas 终端中的文字。

另外提供终端输入、等待、输出提取等辅助函数。
"""

import json
import logging
import re
import time

import pyte
from playwright.sync_api import Page

logger = logging.getLogger("cassia")


class TerminalCapture:
    """
    通过 WebSocket 拦截精确捕获终端文本。

    原理:
        在页面加载前注入 JS，拦截 WebSocket 构造函数、XHR、fetch，
        捕获所有 socket.io 传输的原始数据，
        Python 端通过 page.evaluate() 读取并解析。

    使用方法:
        capture = TerminalCapture()
        capture.attach(page)          # 在导航到 /ssh/host 之前调用
        text = capture.get_screen_text()   # 获取当前屏幕内容
        text = capture.get_raw_text()      # 获取所有历史输出 (纯文本)
        capture.reset()                    # 处理下一个网关前重置
    """

    # 注入到浏览器的 JS hook 代码
    _JS_HOOK = """
    (function() {
        window.__termCapture = { messages: [], debug: [], wsDisconnected: false };

        // Hook WebSocket
        var OrigWebSocket = window.WebSocket;
        window.WebSocket = function(url, protocols) {
            var wsUrl = url ? url.toString() : '';
            window.__termCapture.debug.push('[WS] new: ' + wsUrl);
            var ws = (protocols !== undefined)
                ? new OrigWebSocket(url, protocols)
                : new OrigWebSocket(url);
            if (wsUrl.indexOf('socket.io') !== -1) {
                window.__termCapture.wsDisconnected = false;
                ws.addEventListener('message', function(event) {
                    if (typeof event.data === 'string') {
                        window.__termCapture.messages.push(event.data);
                    } else if (event.data instanceof ArrayBuffer) {
                        try {
                            var text = new TextDecoder('utf-8').decode(event.data);
                            if (text) window.__termCapture.messages.push(text);
                        } catch(e) {}
                    } else if (event.data instanceof Blob) {
                        var reader = new FileReader();
                        reader.onload = function() {
                            if (reader.result) window.__termCapture.messages.push(reader.result);
                        };
                        reader.readAsText(event.data);
                    }
                });
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

        // Hook XHR
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

        // Hook fetch
        var origFetch = window.fetch;
        if (origFetch) {
            window.fetch = function(input, init) {
                var url = (typeof input === 'string') ? input : (input && input.url ? input.url : '');
                var p = origFetch.apply(this, arguments);
                if (url.indexOf('socket.io') !== -1) {
                    p.then(function(response) {
                        return response.clone().text().then(function(text) {
                            if (text) window.__termCapture.messages.push(text);
                        });
                    }).catch(function() {});
                }
                return p;
            };
        }
    })();
    """

    ANSI_ESCAPE = re.compile(
        r'\x1b\[[0-9;]*[a-zA-Z]'
        r'|\x1b\][^\x07]*\x07'
        r'|\x1b[()][AB012]'
        r'|\x1b[>=]'
        r'|\x1b\[\?[0-9;]*[hl]'
        r'|\r'
    )

    def __init__(self, cols=80, rows=24):
        self.cols = cols
        self.rows = rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.raw_buffer = ""
        self.ws_disconnected = False
        self._page: Page | None = None
        self._attached = False

    def attach(self, page: Page):
        """在浏览器中注入 JS hook。必须在导航到 /ssh/host 之前调用。"""
        if self._attached:
            return
        self._page = page
        page.context.add_init_script(self._JS_HOOK)
        self._attached = True

    def reset(self):
        """重置虚拟终端状态，为新的网关会话做准备。"""
        self.screen.reset()
        self.raw_buffer = ""
        self.ws_disconnected = False
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
        """从浏览器端拉取 JS hook 捕获的新数据。"""
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

        if result.get('wsDisconnected', False) and not self.ws_disconnected:
            self.ws_disconnected = True
            # 从 debug 信息中提取 WS close code/reason，输出到 WARNING
            close_info = ""
            for dbg in result.get('debug', []):
                if "[WS] close:" in dbg or "[WS] error" in dbg:
                    close_info = f" ({dbg})"
                    break
            logger.warning(f"[TermCapture] WebSocket 连接已断开{close_info}")

        for dbg in result.get('debug', []):
            logger.debug(f"[TermCapture] {dbg}")

        messages = result.get('messages', [])
        if messages:
            logger.debug(f"[TermCapture] pulled {len(messages)} messages from browser")

        for i, msg in enumerate(messages):
            if isinstance(msg, str):
                preview = msg[:100].replace('\n', '\\n').replace('\r', '\\r')
                logger.debug(f"[TermCapture] msg[{i}] len={len(msg)}: {preview}{'...' if len(msg)>100 else ''}")
                self._parse_message(msg)

    def _parse_message(self, raw):
        """解析原始消息，支持多种 Engine.IO/Socket.IO 版本格式。"""
        if not raw:
            return
        if '\x1e' in raw:
            for packet in raw.split('\x1e'):
                packet = packet.strip()
                if packet:
                    self._parse_single_packet(packet)
            return
        if '\ufffd' in raw:
            parts = re.split(r'\ufffd\d*\ufffd', raw)
            for part in parts:
                part = part.strip()
                if part:
                    self._parse_single_packet(part)
            return
        if raw and raw[0].isdigit() and ':' in raw:
            packets = self._parse_engineio_v3_payload(raw)
            if packets:
                for packet in packets:
                    self._parse_single_packet(packet)
                return
        self._parse_single_packet(raw.strip())

    def _parse_engineio_v3_payload(self, body):
        """解析 Engine.IO v3 的 HTTP polling 响应体: <length>:<data>..."""
        packets = []
        i = 0
        try:
            while i < len(body):
                while i < len(body) and body[i] in ' \t\r\n':
                    i += 1
                if i >= len(body):
                    break
                colon_idx = body.index(':', i)
                length_str = body[i:colon_idx]
                if not length_str.isdigit():
                    return None
                length = int(length_str)
                start = colon_idx + 1
                end = start + length
                if end > len(body):
                    packets.append(body[start:])
                    break
                packets.append(body[start:end])
                i = end
        except (ValueError, IndexError):
            if not packets:
                return None
        return packets if packets else None

    def _parse_single_packet(self, packet):
        """解析单个 Socket.IO 数据包。"""
        if not packet:
            return
        if packet.startswith('42'):
            try:
                arr = json.loads(packet[2:])
                if isinstance(arr, list) and len(arr) >= 2:
                    self._handle_event(arr[0], arr[1])
            except (json.JSONDecodeError, IndexError, TypeError):
                pass
            return
        if packet.startswith('5'):
            try:
                parts = packet.split(':', 3)
                if len(parts) >= 4 and parts[3]:
                    obj = json.loads(parts[3])
                    event_name = obj.get('name', '')
                    args = obj.get('args', [])
                    if args:
                        self._handle_event(event_name, args[0])
            except (json.JSONDecodeError, IndexError, TypeError, KeyError):
                pass
            return
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
        """获取当前虚拟屏幕内容 (精确还原终端可见区域)。"""
        self._pull_browser_data()
        return '\n'.join(line.rstrip() for line in self.screen.display)

    def get_raw_text(self) -> str:
        """获取累积的所有终端输出 (去除 ANSI 转义码后的纯文本)。"""
        self._pull_browser_data()
        return self.ANSI_ESCAPE.sub('', self.raw_buffer)

    def contains(self, target: str) -> bool:
        """检查累积的终端输出中是否包含指定文本"""
        return target in self.get_raw_text()

    def count(self, target: str) -> int:
        """统计目标文本在累积输出中出现的次数"""
        return self.get_raw_text().count(target)


# ============================================================
# 终端操作辅助函数
# ============================================================

def type_in_terminal(page: Page, text: str, type_delay: int = 50):
    """在 xterm.js 终端中输入文本并按回车。"""
    page.locator('.xterm-helper-textarea').focus(timeout=5000)
    page.keyboard.type(text, delay=type_delay)
    page.keyboard.press('Enter')


def type_password_in_terminal(page: Page, password: str, type_delay: int = 50):
    """在终端中输入密码并按回车 (输入前稍等确保 prompt 就绪)。"""
    page.wait_for_timeout(300)
    page.locator('.xterm-helper-textarea').focus(timeout=5000)
    page.keyboard.type(password, delay=type_delay)
    page.keyboard.press('Enter')


def wait_for_terminal_text(
    page: Page,
    capture: TerminalCapture,
    target_text: str,
    timeout: int = 30000,
):
    """
    轮询等待终端输出中包含指定文本。
    每 500ms 检查一次。
    WebSocket 断连后，若仍有数据流入 (Socket.IO polling 回退) 则持续等待；
    若无新数据超过宽限期才判定连接死亡。
    """
    last_data_time = None
    last_raw_len = 0
    WS_DISCONNECT_GRACE = 5.0

    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        raw_text = capture.get_raw_text()
        if target_text in raw_text:
            if capture.ws_disconnected:
                logger.debug(f"[等待] 找到 '{target_text}'，但 WS 已断连 (Socket.IO 可能回退到 polling)")
            return raw_text
        if capture.ws_disconnected:
            current_len = len(raw_text)
            if current_len != last_raw_len:
                last_data_time = time.time()
                last_raw_len = current_len
            elif last_data_time is None:
                last_data_time = time.time()
                logger.debug(f"[等待] WebSocket 已断开，进入 {WS_DISCONNECT_GRACE} 秒宽限期 (有新数据会重置)")
            elif time.time() - last_data_time > WS_DISCONNECT_GRACE:
                raise ConnectionError(
                    f"WebSocket 已断开且无新数据超过 {WS_DISCONNECT_GRACE} 秒，"
                    f"无法继续等待终端文本 '{target_text}'"
                )
        page.wait_for_timeout(500)

    current_screen = capture.get_screen_text()
    current_raw = capture.get_raw_text()
    raise TimeoutError(
        f"等待终端文本 '{target_text}' 超时 ({timeout}ms)\n"
        f"当前屏幕内容:\n{current_screen}\n"
        f"原始输出 (最后500字符):\n{current_raw[-500:]}"
    )


def wait_for_new_terminal_text(
    page: Page,
    capture: TerminalCapture,
    target_text: str,
    baseline: str,
    timeout: int = 30000,
):
    """
    等待终端出现新的指定文本 (排除 baseline 中已有的)。
    用于区分命令执行前后的 prompt 变化。
    WebSocket 断连后，若仍有数据流入则持续等待。
    """
    last_data_time = None
    last_raw_len = 0
    WS_DISCONNECT_GRACE = 5.0
    baseline_count = baseline.count(target_text)

    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        raw_text = capture.get_raw_text()
        if raw_text.count(target_text) > baseline_count:
            if capture.ws_disconnected:
                logger.debug(f"[等待] 找到 '{target_text}'，但 WS 已断连 (Socket.IO 可能回退到 polling)")
            return raw_text
        if capture.ws_disconnected:
            current_len = len(raw_text)
            if current_len != last_raw_len:
                last_data_time = time.time()
                last_raw_len = current_len
            elif last_data_time is None:
                last_data_time = time.time()
                logger.debug(f"[等待] WebSocket 已断开，进入 {WS_DISCONNECT_GRACE} 秒宽限期 (有新数据会重置)")
            elif time.time() - last_data_time > WS_DISCONNECT_GRACE:
                raise ConnectionError(
                    f"WebSocket 已断开且无新数据超过 {WS_DISCONNECT_GRACE} 秒，"
                    f"无法继续等待终端文本 '{target_text}'"
                )
        page.wait_for_timeout(500)

    current_screen = capture.get_screen_text()
    current_raw = capture.get_raw_text()
    raise TimeoutError(
        f"等待新的终端文本 '{target_text}' 超时 ({timeout}ms)\n"
        f"当前屏幕内容:\n{current_screen}\n"
        f"原始输出 (最后500字符):\n{current_raw[-500:]}"
    )


def extract_command_output(new_raw: str, baseline: str, cmd: str) -> str:
    """
    从终端原始文本中提取某条命令的输出。
    取 new_raw 相对于 baseline 的增量部分，
    去掉首行的命令回显和末尾的 prompt 行。
    """
    diff = new_raw[len(baseline):]
    lines = diff.split('\n')

    # 去掉首行命令回显
    if lines and cmd.strip() in lines[0]:
        lines = lines[1:]

    # 去掉末尾 prompt 行
    while lines and re.match(r'^\s*\S+[@:]\S*[#$]\s*$', lines[-1].strip()):
        lines = lines[:-1]

    return '\n'.join(lines).strip()
