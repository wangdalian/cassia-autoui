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
  2. 运行脚本: python test_ssh_gateway.py

  浏览器模式 (browser_mode):
    "persistent" (推荐) - Playwright Chromium + 会话持久化，不影响系统 Chrome
      首次运行自动登录，如遇 token 验证会暂停等你手动处理；后续直接复用
    "cdp"  - 连接已打开的 Chrome（需用 --remote-debugging-port=9222 启动）
    "login" - 每次都启动新浏览器并自动登录
"""

import base64
import json
import os
import sys
import time
import traceback

from playwright.sync_api import sync_playwright, Page, BrowserContext


# ============================================================
# 加载配置文件
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

if not os.path.isfile(CONFIG_FILE):
    print(f"[错误] 未找到配置文件: {CONFIG_FILE}")
    print(f"  请复制 config.json 到脚本同级目录并填入实际配置")
    sys.exit(1)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    try:
        _config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[错误] 配置文件 JSON 格式错误: {e}")
        sys.exit(1)

# 必填项
BASE_URL = _config.get("base_url", "http://YOUR_AC_IP")
BROWSER_MODE = _config.get("browser_mode", "persistent")
AC_USERNAME = _config.get("ac_username", "admin")
AC_PASSWORD = _config.get("ac_password", "1q2w#E$R")
BLUE_PASSWORD = _config.get("blue_password", "xxx")
SU_PASSWORD = _config.get("su_password", "xxx")
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
    print("[INFO] 正在登录 AC 管理平台...")
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
        print("[INFO] AC 管理平台登录成功")
    except Exception:
        # 可能遇到了 token 验证或其他中间页面
        print("[INFO] 登录后未直接跳转到 dashboard，可能需要 token 验证")
        print("[INFO] >>> 请在弹出的浏览器中手动完成验证 <<<")
        print("[INFO] 完成后脚本会自动继续...")
        # 等待用户手动完成验证，最多等 5 分钟
        page.wait_for_url(f"{BASE_URL}/dashboard?view", timeout=300000)
        print("[INFO] AC 管理平台登录成功（手动验证完成）")


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


def page_fetch(page: Page, url: str, method: str = "POST",
               body: dict = None, extra_headers: dict = None,
               add_csrf: bool = True, redirect: str = "follow") -> dict:
    """
    在页面内通过 fetch() 发送请求。
    自动携带浏览器的所有 cookies。
    add_csrf=True 时自动从 localStorage key 't' 读取 CSRF token 并注入 body。
    redirect: "follow"(默认，自动跟随重定向) / "manual"(不跟随，返回原始 3xx 响应)
    返回 { "ok": bool, "status": int, "text": str, "redirected": bool, "url": str }
    """
    body_js = json.dumps(body) if body is not None else "{}"
    extra_headers_js = json.dumps(extra_headers) if extra_headers else "{}"

    return page.evaluate(f"""async () => {{
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

        const resp = await fetch("{url}", {{
            method: "{method}",
            headers: headers,
            body: JSON.stringify(bodyObj),
            credentials: "same-origin",
            redirect: "{redirect}"
        }});

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


def enable_ssh(page: Page, mac: str):
    """Step 1: 启用网关 SSH"""
    print(f"  [Step 1] 启用 SSH: {mac}")
    url = f"{BASE_URL}/api2/cassia/info?mac={mac}"
    result = page_fetch(page, url, "POST", {"ssh-login": "1"})
    if not result["ok"]:
        raise RuntimeError(
            f"启用 SSH 失败: HTTP {result['status']} - {result['text']}"
        )
    print(f"  [Step 1] SSH 启用成功 (HTTP {result['status']})")


def open_tunnel(page: Page, mac: str):
    """Step 2: 开启 SSH 隧道"""
    print(f"  [Step 2] 开启 SSH 隧道: {mac}")
    url = f"{BASE_URL}/ap/remote/{mac}?ssh_port=9999&ap=1"
    result = page_fetch(page, url, "POST", {}, redirect="manual")
    # redirect="manual" 时，302 响应会变成 opaqueredirect（status=0），视为成功
    status = result["status"]
    if not result["ok"] and status != 0 and not (300 <= status < 400):
        raise RuntimeError(
            f"开启隧道失败: HTTP {status} - {result['text']}"
        )
    print(f"  [Step 2] SSH 隧道开启成功 (HTTP {status})")


def open_ssh_terminal(page: Page):
    """打开 SSH Web Terminal 页面并等待终端加载完成。
    如果页面未自动跳转到 /ssh/host，则主动导航。
    Basic Auth 由 context 的 http_credentials 自动处理。
    """
    print(f"  [Step 3] 打开 SSH Web Terminal...")
    if "/ssh/host" not in page.url:
        page.goto(f"{BASE_URL}/ssh/host", timeout=TIMEOUT_PAGE_LOAD)
    page.wait_for_selector('.xterm', state='visible', timeout=TIMEOUT_TERMINAL_READY)
    print(f"  [Step 3] SSH Web Terminal 已加载")


def read_terminal_buffer(page: Page) -> str:
    """
    读取 xterm.js 终端缓冲区内容。
    尝试多种方式获取 Terminal 实例，兼容不同版本和挂载方式。
    """
    return page.evaluate("""() => {
        // 方式 1: 常见全局变量
        let term = window.term || window.terminal || window.xterm;

        // 方式 2: xterm DOM 元素上的内部属性
        if (!term) {
            const el = document.querySelector('.xterm');
            if (el) {
                // xterm.js v4/v5 内部属性
                term = el._xterm || el.__xterm || el.terminal;
                // 遍历元素属性查找 Terminal 实例
                if (!term) {
                    for (const key of Object.keys(el)) {
                        if (el[key] && el[key].buffer && el[key].buffer.active) {
                            term = el[key];
                            break;
                        }
                    }
                }
            }
        }

        // 方式 3: 遍历 window 上的属性查找 Terminal 实例
        if (!term) {
            for (const key of Object.keys(window)) {
                try {
                    const obj = window[key];
                    if (obj && typeof obj === 'object' && obj.buffer && obj.buffer.active
                        && typeof obj.buffer.active.getLine === 'function') {
                        term = obj;
                        break;
                    }
                } catch (e) {}
            }
        }

        // 方式 4: 通过 xterm-screen 的 aria-live 区域读取（无障碍文本）
        if (!term) {
            const liveRegion = document.querySelector('.xterm-accessibility-tree [aria-live]')
                || document.querySelector('[aria-live="assertive"]');
            if (liveRegion && liveRegion.textContent) {
                return liveRegion.textContent;
            }
        }

        if (!term) return '';

        const buf = term.buffer.active;
        let lines = [];
        for (let i = 0; i < buf.length; i++) {
            const line = buf.getLine(i);
            if (line) {
                const text = line.translateToString(true);
                lines.push(text);
            }
        }
        return lines.join('\\n');
    }""")


def wait_for_terminal_text(page: Page, target_text: str, timeout: int = None):
    """
    轮询等待终端输出中包含指定文本。
    每 500ms 检查一次终端缓冲区内容。
    """
    if timeout is None:
        timeout = TIMEOUT_PROMPT_WAIT

    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        output = read_terminal_buffer(page)
        if target_text in output:
            return output
        page.wait_for_timeout(500)

    # 超时，打印当前终端内容以便调试
    current_output = read_terminal_buffer(page)
    raise TimeoutError(
        f"等待终端文本 '{target_text}' 超时 ({timeout}ms)\n"
        f"当前终端内容:\n{current_output}"
    )


def wait_for_new_terminal_text(page: Page, target_text: str,
                                baseline: str, timeout: int = None):
    """
    等待终端出现新的指定文本（排除已有的 baseline 内容）。
    用于区分命令执行前后 prompt 的变化。
    """
    if timeout is None:
        timeout = TIMEOUT_COMMAND_WAIT

    # 统计 baseline 中 target_text 出现的次数
    baseline_count = baseline.count(target_text)

    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        output = read_terminal_buffer(page)
        current_count = output.count(target_text)
        if current_count > baseline_count:
            return output
        page.wait_for_timeout(500)

    current_output = read_terminal_buffer(page)
    raise TimeoutError(
        f"等待新的终端文本 '{target_text}' 超时 ({timeout}ms)\n"
        f"当前终端内容:\n{current_output}"
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
    """Step 4: 等待终端就绪"""
    print(f"  [Step 4] 等待终端就绪...")
    page.wait_for_timeout(10000)
    type_in_terminal(page, "")
    type_in_terminal(page, "")
    print(f"  [Step 4] 终端就绪")


def switch_to_root(page: Page):
    """Step 5: 切换到 root 用户 (su)"""
    print(f"  [Step 5] 切换到 root 用户...")

    # 输入 su 命令
    type_in_terminal(page, "")
    page.wait_for_timeout(3000)
    
    type_in_terminal(page, "su")
    page.wait_for_timeout(5000)  # 等待 Password: 提示

    # 输入 root 密码
    type_password_in_terminal(page, SU_PASSWORD)
    page.wait_for_timeout(5000)  # 等待切换完成

    print(f"  [Step 5] su 切换命令已执行")


def execute_shell_commands(page: Page, mac: str):
    """Step 6: 执行 shell 命令列表"""
    if not SHELL_COMMANDS:
        print(f"  [Step 6] 无需执行的 shell 命令，跳过")
        return

    print(f"  [Step 6] 开始执行 {len(SHELL_COMMANDS)} 条 shell 命令...")

    for i, cmd in enumerate(SHELL_COMMANDS, 1):
        print(f"  [Step 6] [{i}/{len(SHELL_COMMANDS)}] 执行: {cmd}")
        type_in_terminal(page, cmd)
        page.wait_for_timeout(5000)  # 等待命令执行完毕
        print(f"  [Step 6] [{i}/{len(SHELL_COMMANDS)}] 已发送")

    # 所有命令执行完毕后截图
    images_dir = os.path.join(SCRIPT_DIR, "images")
    os.makedirs(images_dir, exist_ok=True)
    safe_mac = mac.replace(":", "-")
    screenshot_path = os.path.join(images_dir, f"{safe_mac}.png")
    page.screenshot(path=screenshot_path, full_page=True)
    print(f"  [Step 6] 截图已保存: {screenshot_path}")


def _is_session_expired_error(e: Exception) -> bool:
    """判断异常是否由会话过期引起（HTTP 401 或重定向到 session 页面）"""
    msg = str(e).lower()
    return "401" in msg or "session" in msg


def process_gateway(context: BrowserContext, page: Page, mac: str, index: int, total: int):
    """处理单个网关的完整流程（含会话过期自动重登录重试）"""
    print(f"\n{'='*60}")
    print(f"[{index}/{total}] 开始处理网关: {mac}")
    print(f"{'='*60}")
    
    page.wait_for_timeout(30000000)

    for attempt in range(2):  # 最多尝试 2 次（首次 + 重试 1 次）
        try:
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
            execute_shell_commands(page, mac)

            print(f"\n[{index}/{total}] 网关 {mac} 处理完成 ✓")
            return True  # 成功

        except Exception as e:
            if attempt == 0 and _is_session_expired_error(e):
                # 首次失败且疑似会话过期，自动重登录后重试
                print(f"\n[{index}/{total}] 网关 {mac} 疑似会话过期，自动重新登录后重试...")
                print(f"  原始错误: {e}")
                try:
                    page.goto(f"{BASE_URL}/dashboard?view", timeout=TIMEOUT_PAGE_LOAD)
                    page.wait_for_timeout(1000)
                except Exception:
                    pass
                login_ac(page)
                continue  # 重试

            # 非会话问题，或已重试过仍失败
            print(f"\n[{index}/{total}] 网关 {mac} 处理失败 ✗")
            print(f"  错误: {e}")
            traceback.print_exc()
            return False

        finally:
            # 导航回 dashboard，为下一个网关做准备
            try:
                page.goto(f"{BASE_URL}/dashboard?view", timeout=TIMEOUT_PAGE_LOAD)
                page.wait_for_timeout(1000)
            except Exception:
                pass

    return False


MODE_LABELS = {
    "persistent": "Persistent（Chromium + 会话持久化）",
    "cdp": "CDP 连接",
    "login": "自动登录",
}


def main():
    """主入口函数"""
    # 参数校验
    if BASE_URL == "http://YOUR_AC_IP":
        print("[错误] 请先修改 BASE_URL 为实际的 AC 管理平台地址")
        sys.exit(1)

    if not GATEWAY_MACS:
        print("[错误] GATEWAY_MACS 列表为空，请添加网关 MAC 地址")
        sys.exit(1)

    if BROWSER_MODE not in ("persistent", "cdp", "login"):
        print(f"[错误] 不支持的 BROWSER_MODE: {BROWSER_MODE}")
        print(f"  可选值: persistent, cdp, login")
        sys.exit(1)

    total = len(GATEWAY_MACS)
    print(f"[INFO] Cassia Gateway SSH 批量自动化脚本")
    print(f"[INFO] AC 平台地址: {BASE_URL}")
    print(f"[INFO] 待处理网关数量: {total}")
    print(f"[INFO] Shell 命令数量: {len(SHELL_COMMANDS)}")
    print(f"[INFO] 浏览器模式: {MODE_LABELS.get(BROWSER_MODE, BROWSER_MODE)}")
    print()

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
                print(f"[INFO] 首次运行，将创建浏览器 profile: {BROWSER_PROFILE_DIR}")
                print(f"[INFO] 需要登录 AC 平台（仅此一次）")
            else:
                print(f"[INFO] 加载已有浏览器 profile: {BROWSER_PROFILE_DIR}")

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
                print("[INFO] 会话无效或已过期，需要登录...")
                login_ac(page)
            else:
                print("[INFO] 会话有效，无需登录")

        elif BROWSER_MODE == "cdp":
            # 方式二: 通过 CDP 连接已运行的 Chrome（已登录 AC）
            print(f"[INFO] 正在通过 CDP 连接 Chrome: {CDP_URL}")
            try:
                browser = p.chromium.connect_over_cdp(CDP_URL)
            except Exception as e:
                print(f"[错误] 无法连接到 Chrome，请确保已启动带远程调试端口的 Chrome:")
                print(f"  macOS: /Applications/Google\\ Chrome.app/Contents/MacOS/Google\\ Chrome --remote-debugging-port=9222")
                print(f"  Windows: chrome.exe --remote-debugging-port=9222")
                print(f"  错误详情: {e}")
                sys.exit(1)

            context = browser.contexts[0]
            page = context.pages[0] if context.pages else context.new_page()
            print(f"[INFO] CDP 连接成功，复用已有登录会话")

        else:
            # 方式三: 自动启动浏览器并登录 AC
            print(f"[INFO] 正在启动浏览器...")
            launch_args = []
            if DEVTOOLS:
                launch_args.append("--auto-open-devtools-for-tabs")
            browser = p.chromium.launch(headless=False, args=launch_args)
            context = browser.new_context(
                http_credentials={"username": "blue", "password": BLUE_PASSWORD},
            )
            page = context.new_page()
            login_ac(page)

        # 监听所有 DELETE 请求，打印 URL 用于调试
        context.on("request", lambda req: print(f"[DEBUG] 请求: {req.method} {req.url}") if req.method == "DELETE" else None)

        # 拦截前端的 DELETE /session 请求，阻止 AC 会话被注销（前端 30 分钟计时到期后会主动发此请求）
        def intercept_session_delete(route):
            if route.request.method == "DELETE":
                print(f"[INFO] 已拦截 DELETE {route.request.url} 请求，保持会话有效")
                route.abort()
            else:
                route.continue_()

        context.route(lambda url: "/session" in url, intercept_session_delete)
        print("[INFO] 已注册 DELETE /session 拦截规则，会话将保持有效")

        # 逐个处理网关
        success_count = 0
        fail_count = 0

        for i, mac in enumerate(GATEWAY_MACS, 1):
            if process_gateway(context, page, mac, i, total):
                success_count += 1
            else:
                fail_count += 1

        # 输出汇总
        print(f"\n{'='*60}")
        print(f"[汇总] 处理完成")
        print(f"  总数: {total}")
        print(f"  成功: {success_count}")
        print(f"  失败: {fail_count}")
        print(f"{'='*60}")

        # 保持浏览器窗口打开
        print(f"\n[INFO] 所有网关处理完毕，浏览器保持打开")
        print(f"[INFO] 按 Ctrl+C 或关闭浏览器窗口退出")
        try:
            # 阻塞等待，直到用户手动关闭
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(f"\n[INFO] 用户退出")


if __name__ == "__main__":
    main()
