"""
浏览器生命周期管理

支持三种模式:
  - persistent: Playwright Chromium + 本地会话持久化 (推荐)
  - cdp: 连接已打开的 Chrome (--remote-debugging-port)
  - login: 每次启动新浏览器并自动登录
"""

import base64
import logging
import os

from playwright.sync_api import Page, BrowserContext, Playwright

logger = logging.getLogger("cassia")


def get_basic_auth_header(username: str, password: str) -> dict:
    """生成 Basic Auth 请求头"""
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def login_ac(page: Page, config: dict):
    """
    登录 AC 管理平台。
    如果遇到 token 验证等中间页面，会暂停等待用户手动完成。
    """
    base_url = config["base_url"]
    logger.info("正在登录 AC 管理平台...")
    page.goto(f"{base_url}/session?view")
    page.wait_for_timeout(1000)

    page.locator('input[name="username"]').fill(config["ac_username"])
    page.wait_for_timeout(300)

    page.locator('input[name="password"]').fill(config["ac_password"])
    page.wait_for_timeout(300)

    page.locator('button:has-text("Login"), button:has-text("登录")').click()

    try:
        page.wait_for_url(f"{base_url}/dashboard?view", timeout=10000)
        logger.info("AC 管理平台登录成功")
    except Exception:
        logger.info("登录后未直接跳转到 dashboard，可能需要 token 验证")
        logger.info(">>> 请在弹出的浏览器中手动完成验证 <<<")
        logger.info("完成后脚本会自动继续...")
        page.wait_for_url(f"{base_url}/dashboard?view", timeout=300000)
        logger.info("AC 管理平台登录成功（手动验证完成）")


def check_session_valid(page: Page, config: dict) -> bool:
    """检查当前会话是否有效（是否已登录）"""
    base_url = config["base_url"]
    timeout = config.get("timeout_page_load", 30000)
    try:
        page.goto(f"{base_url}/dashboard?view", timeout=timeout)
        page.wait_for_timeout(2000)
        current_url = page.url
        if "session" in current_url or "login" in current_url:
            return False
        return True
    except Exception:
        return False


def setup_interceptors(context: BrowserContext, page: Page):
    """
    注册浏览器拦截器:
    - 拦截 DELETE /session 请求，防止 AC 前端主动注销会话
    - 自动关闭 alert/confirm/prompt 弹窗
    """
    # 拦截 DELETE /session
    def intercept_session_delete(route):
        if route.request.method == "DELETE":
            logger.info(f"已拦截 DELETE {route.request.url} 请求，保持会话有效")
            route.abort()
        else:
            route.continue_()

    context.route(lambda url: "/session" in url, intercept_session_delete)
    logger.info("已注册 DELETE /session 拦截规则，会话将保持有效")

    # 自动关闭弹窗
    def handle_dialog(dialog):
        logger.warning(f"[Dialog] {dialog.type}: {dialog.message}")
        dialog.accept()

    page.on("dialog", handle_dialog)
    logger.info("已注册 dialog 处理器（自动关闭弹窗并记录）")


class BrowserManager:
    """
    浏览器生命周期管理器。

    使用方法:
        bm = BrowserManager(config)
        bm.launch(playwright)   # 启动浏览器
        page = bm.page          # 获取 Page 对象
        # ... 使用 page ...
        bm.close()              # 关闭
    """

    def __init__(self, config: dict):
        self.config = config
        self._browser = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    @property
    def page(self) -> Page:
        if self._page is None:
            raise RuntimeError("浏览器未启动，请先调用 launch()")
        return self._page

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("浏览器未启动，请先调用 launch()")
        return self._context

    def launch(self, playwright: Playwright, profile_dir: str | None = None):
        """
        根据配置启动浏览器。

        Args:
            playwright: Playwright 实例 (来自 sync_playwright())
            profile_dir: 浏览器持久化目录 (persistent 模式用)
        """
        mode = self.config.get("browser_mode", "persistent")
        logger.info(f"浏览器模式: {mode}")

        if mode == "persistent":
            self._launch_persistent(playwright, profile_dir)
        elif mode == "cdp":
            self._launch_cdp(playwright)
        elif mode == "login":
            self._launch_login(playwright)
        else:
            raise ValueError(f"不支持的 browser_mode: {mode}")

        # 注册拦截器
        setup_interceptors(self._context, self._page)

    def _launch_persistent(self, pw: Playwright, profile_dir: str | None):
        """persistent 模式: Playwright Chromium + 本地会话持久化"""
        if profile_dir is None:
            profile_dir = os.path.join(os.getcwd(), ".browser_profile")

        is_first_run = not os.path.isdir(profile_dir)
        if is_first_run:
            logger.info(f"首次运行，将创建浏览器 profile: {profile_dir}")
        else:
            logger.info(f"加载已有浏览器 profile: {profile_dir}")

        launch_args = ["--disable-blink-features=AutomationControlled"]
        if self.config.get("devtools"):
            launch_args.append("--auto-open-devtools-for-tabs")

        blue_password = self.config.get("blue_password", "")
        http_creds = {"username": "blue", "password": blue_password, "send": "always"} if blue_password else None

        self._context = pw.chromium.launch_persistent_context(
            user_data_dir=profile_dir,
            headless=False,
            no_viewport=True,
            args=launch_args,
            http_credentials=http_creds,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

        # 检查会话
        if is_first_run or not check_session_valid(self._page, self.config):
            logger.info("会话无效或已过期，需要登录...")
            login_ac(self._page, self.config)
        else:
            logger.info("会话有效，无需登录")

    def _launch_cdp(self, pw: Playwright):
        """cdp 模式: 连接已运行的 Chrome"""
        cdp_url = self.config.get("cdp_url", "http://localhost:9222")
        logger.info(f"正在通过 CDP 连接 Chrome: {cdp_url}")
        try:
            self._browser = pw.chromium.connect_over_cdp(cdp_url)
        except Exception as e:
            logger.error("无法连接到 Chrome，请确保已启动带远程调试端口的 Chrome:")
            logger.error(f"  macOS: open -a 'Google Chrome' --args --remote-debugging-port=9222")
            logger.error(f"  Windows: chrome.exe --remote-debugging-port=9222")
            raise ConnectionError(f"CDP 连接失败: {e}") from e

        self._context = self._browser.contexts[0]
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        logger.info("CDP 连接成功，复用已有登录会话")

    def _launch_login(self, pw: Playwright):
        """login 模式: 每次启动新浏览器并登录"""
        logger.info("正在启动浏览器...")
        launch_args = []
        if self.config.get("devtools"):
            launch_args.append("--auto-open-devtools-for-tabs")

        self._browser = pw.chromium.launch(headless=False, args=launch_args)

        blue_password = self.config.get("blue_password", "")
        http_creds = {"username": "blue", "password": blue_password, "send": "always"} if blue_password else None

        self._context = self._browser.new_context(http_credentials=http_creds)
        self._page = self._context.new_page()
        login_ac(self._page, self.config)

    def close(self):
        """关闭浏览器和上下文"""
        # persistent 模式下 self._browser 为 None，需要单独关闭 context
        if self._context and self._browser is None:
            try:
                self._context.close()
            except Exception as e:
                logger.debug(f"关闭 context 时异常 (可忽略): {e}")
        if self._browser:
            try:
                self._browser.close()
            except Exception as e:
                logger.debug(f"关闭浏览器时异常 (可忽略): {e}")
            self._browser = None
        self._context = None
        self._page = None
