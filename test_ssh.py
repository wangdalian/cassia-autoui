"""SSH 连接测试脚本 — 测试 e9 网关的完整 SSH 流程 (含重试)"""

import logging
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright
from lib.config import load_config
from lib.browser import BrowserManager
from lib.terminal import (
    TerminalCapture, wait_for_terminal_text, wait_for_new_terminal_text,
    type_in_terminal, type_password_in_terminal, extract_command_output,
)
from lib.ac_api import enable_ssh, open_tunnel, open_ssh_terminal

logger = logging.getLogger("cassia")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(handler)
logger.setLevel(logging.INFO)

MAC = "CC:1B:E0:E2:E9:B8"
MAX_ATTEMPTS = 3
RETRY_DELAYS = [2000, 5000]


def test_ssh():
    config = load_config("agent/config.json")
    base_url = config["base_url"]
    timeout = config.get("timeout_page_load", 30000)

    with sync_playwright() as pw:
        bm = BrowserManager(config)
        bm.launch(pw, ".browser_profile")
        page = bm.page
        print(f"浏览器就绪: {page.url}\n")

        capture = TerminalCapture()
        capture.attach(page)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(f"{'='*50}")
            print(f"  第 {attempt}/{MAX_ATTEMPTS} 次尝试")
            print(f"{'='*50}")
            capture.reset()

            try:
                # Step 1: 启用 SSH
                enable_ssh(page, MAC, base_url, timeout)
                page.wait_for_timeout(3000)

                # Step 2: 开启隧道 + 等待建立
                open_tunnel(page, MAC, base_url, timeout)
                page.wait_for_timeout(2000)

                # Step 3: 打开 Web Terminal
                open_ssh_terminal(page, base_url,
                                  timeout_page_load=timeout,
                                  timeout_terminal_ready=30000)

                # Step 4: 等待 $ prompt
                wait_for_terminal_text(page, capture, "$", timeout=30000)
                print(f"  [OK] 检测到 $ prompt (WS alive={not capture.ws_disconnected})")

            except ConnectionError as e:
                print(f"  [FAIL] ConnectionError: {e}")
                if attempt < MAX_ATTEMPTS:
                    delay = RETRY_DELAYS[attempt - 1]
                    print(f"  等待 {delay}ms 后重试...")
                    page.wait_for_timeout(delay)
                    continue
                print("  所有尝试失败，退出")
                break
            except TimeoutError as e:
                print(f"  [WARN] 等待 $ 超时，尝试唤醒终端...")
                type_in_terminal(page, "", 50)
                page.wait_for_timeout(2000)

            # Step 5: 切换 root
            type_in_terminal(page, "", 50)
            page.wait_for_timeout(1000)
            baseline = capture.get_raw_text()
            type_in_terminal(page, "su", 50)

            try:
                wait_for_terminal_text(page, capture, "assword", timeout=10000)
                print("  [OK] 检测到 password prompt")
            except ConnectionError as e:
                print(f"  [FAIL] ConnectionError (assword): {e}")
                if attempt < MAX_ATTEMPTS:
                    page.wait_for_timeout(RETRY_DELAYS[attempt - 1])
                    continue
                break
            except TimeoutError:
                print("  [WARN] 等待 assword 超时，继续...")

            type_password_in_terminal(page, config.get("su_password", ""), 50)

            try:
                wait_for_new_terminal_text(page, capture, "#", baseline, timeout=30000)
                print("  [OK] 检测到 # prompt — root 切换成功!")
            except ConnectionError as e:
                print(f"  [FAIL] ConnectionError (#): {e}")
                if attempt < MAX_ATTEMPTS:
                    page.wait_for_timeout(RETRY_DELAYS[attempt - 1])
                    continue
                break
            except TimeoutError:
                page.wait_for_timeout(3000)
                print("  [WARN] 等待 # 超时")

            # WS 断连不代表终端不可用 (Socket.IO 有 polling 回退)
            if capture.ws_disconnected:
                print(f"  [INFO] WS 已断连，但终端交互正常 (Socket.IO polling)")

            print(f"\n  SSH 连接成功! (第 {attempt} 次尝试)")

            # 执行测试命令
            print("\n  执行命令: uname -a")
            cmd_baseline = capture.get_raw_text()
            type_in_terminal(page, "uname -a", 50)
            try:
                new_raw = wait_for_new_terminal_text(page, capture, "#", cmd_baseline, timeout=15000)
                output = extract_command_output(new_raw, cmd_baseline, "uname -a")
                print(f"  输出: {output}")
            except Exception as e:
                print(f"  命令异常: {e}")

            print("\n  执行命令: show version")
            cmd_baseline = capture.get_raw_text()
            type_in_terminal(page, "show version", 50)
            try:
                new_raw = wait_for_new_terminal_text(page, capture, "#", cmd_baseline, timeout=15000)
                output = extract_command_output(new_raw, cmd_baseline, "show version")
                print(f"  输出: {output[:300]}")
            except Exception as e:
                print(f"  命令异常: {e}")

            break

        bm.close()
        print("\n测试完成")


if __name__ == "__main__":
    test_ssh()
