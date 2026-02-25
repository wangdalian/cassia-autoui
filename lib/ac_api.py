"""
Cassia AC 平台 API 操作

封装通过浏览器 page.evaluate(fetch(...)) 发送 AC API 请求的逻辑，
以及 SSH 启用、隧道开启、在线网关获取等高级操作。
"""

import json
import logging

from playwright.sync_api import Page

logger = logging.getLogger("cassia")


def page_fetch(
    page: Page,
    url: str,
    method: str = "POST",
    body: dict | None = None,
    extra_headers: dict | None = None,
    add_csrf: bool = True,
    redirect: str = "follow",
    timeout: int = 30000,
) -> dict:
    """
    在页面内通过 fetch() 发送请求，自动携带浏览器的所有 cookies。

    add_csrf=True 时自动从 localStorage key 't' 读取 CSRF token 并注入 body。
    redirect: "follow" (跟随重定向) / "manual" (不跟随)
    返回 { "ok": bool, "status": int, "text": str, "redirected": bool, "url": str }
    """
    body_js = json.dumps(body) if body is not None else "{}"
    extra_headers_js = json.dumps(extra_headers) if extra_headers else "{}"

    # 转义 URL 中可能破坏 JS 字符串的字符
    safe_url = url.replace("\\", "\\\\").replace('"', '\\"')

    result = page.evaluate(f"""async () => {{
        let bodyObj = {body_js};
        const addCsrf = {'true' if add_csrf else 'false'};
        if (addCsrf) {{
            const csrfToken = localStorage.getItem('t');
            if (csrfToken) bodyObj.csrf = csrfToken;
        }}
        const headers = {{
            "Content-Type": "application/json",
            ...{extra_headers_js}
        }};
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), {timeout});
        let resp;
        try {{
            resp = await fetch("{safe_url}", {{
                method: "{method}",
                headers: headers,
                body: JSON.stringify(bodyObj),
                credentials: "same-origin",
                redirect: "{redirect}",
                signal: controller.signal
            }});
        }} catch (e) {{
            clearTimeout(timer);
            if (e.name === 'AbortError') throw new Error("fetch 超时 ({timeout}ms): {safe_url}");
            throw e;
        }}
        clearTimeout(timer);
        let text = '';
        if (resp.type !== 'opaqueredirect') text = await resp.text();
        return {{
            ok: resp.ok,
            status: resp.status,
            text: text,
            redirected: resp.redirected,
            url: resp.url
        }};
    }}""")

    # 检测重定向到登录页 (会话过期)
    if result.get("redirected") and any(
        kw in result.get("url", "").lower()
        for kw in ("session", "login")
    ):
        raise RuntimeError(
            f"请求被重定向到登录页 ({result['url']})，会话已过期"
        )

    return result


def enable_ssh(page: Page, mac: str, base_url: str, timeout: int = 30000):
    """启用网关 SSH"""
    logger.info(f"启用 SSH: {mac}")
    url = f"{base_url}/api2/cassia/info?mac={mac}"
    result = page_fetch(page, url, "POST", {"ssh-login": "1"}, timeout=timeout)
    if not result["ok"]:
        raise RuntimeError(
            f"启用 SSH 失败: HTTP {result['status']} - {result['text']}"
        )
    logger.info(f"SSH 启用成功 (HTTP {result['status']})")


def open_tunnel(page: Page, mac: str, base_url: str, timeout: int = 30000):
    """开启 SSH 隧道"""
    logger.info(f"开启 SSH 隧道: {mac}")
    url = f"{base_url}/ap/remote/{mac}?ssh_port=9999&ap=1"
    result = page_fetch(page, url, "POST", {}, redirect="manual", timeout=timeout)
    status = result["status"]
    # redirect="manual" 时 302 → opaqueredirect (status=0)，视为成功
    if not result["ok"] and status != 0 and not (300 <= status < 400):
        raise RuntimeError(
            f"开启隧道失败: HTTP {status} - {result['text']}"
        )
    logger.info(f"SSH 隧道开启成功 (HTTP {status})")


def open_ssh_terminal(
    page: Page,
    base_url: str,
    timeout_page_load: int = 30000,
    timeout_terminal_ready: int = 30000,
):
    """
    打开 SSH Web Terminal 页面并等待终端加载完成。
    Basic Auth 由 context 的 http_credentials 自动处理。
    """
    logger.info("打开 SSH Web Terminal...")
    # 始终执行 goto，即使 URL 已是 /ssh/host
    # 断连后 WebSocket 已死，必须重新加载页面建立新连接
    page.goto(f"{base_url}/ssh/host", timeout=timeout_page_load)
    current_url = page.url
    if "session" in current_url or "login" in current_url:
        raise RuntimeError(
            f"SSH 终端页面被重定向到登录页 ({current_url})，会话已过期"
        )
    page.wait_for_selector('.xterm', state='visible', timeout=timeout_terminal_ready)
    logger.info("SSH Web Terminal 已加载")


def fetch_gateways(
    page: Page, base_url: str,
    status: str = "all", timeout: int = 30000,
) -> list:
    """
    从 AC 平台获取网关列表。

    Args:
        status: "all" (所有), "online" (在线), "offline" (离线)
    """
    status_label = {"all": "所有", "online": "在线", "offline": "离线"}.get(status, status)
    logger.info(f"正在从 AC 获取{status_label}网关列表...")
    safe_base = base_url.replace("\\", "\\\\").replace('"', '\\"')

    # 构建 URL: all 不带 status 参数
    if status in ("online", "offline"):
        api_url = f"{safe_base}/ap?status={status}"
    else:
        api_url = f"{safe_base}/ap"

    try:
        data = page.evaluate(f"""async () => {{
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), {timeout});
            let resp;
            try {{
                resp = await fetch("{api_url}", {{
                    credentials: "same-origin",
                    headers: {{ "X-Requested-With": "XMLHttpRequest" }},
                    signal: controller.signal
                }});
            }} catch (e) {{
                clearTimeout(timer);
                if (e.name === 'AbortError') throw new Error("获取网关列表超时 ({timeout}ms)");
                throw e;
            }}
            clearTimeout(timer);
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            return await resp.json();
        }}""")
        if isinstance(data, list):
            logger.info(f"获取到 {len(data)} 个{status_label}网关")
            return data
        logger.warning(f"AC 返回的数据格式异常（非数组）: {str(data)[:200]}")
        return []
    except Exception as e:
        logger.error(f"获取网关列表失败: {e}")
        return []


# 保持向后兼容
def fetch_online_gateways(page: Page, base_url: str, timeout: int = 30000) -> list:
    """向后兼容: 等同于 fetch_gateways(status="online")"""
    return fetch_gateways(page, base_url, status="online", timeout=timeout)


def extract_gateway_info(gw: dict) -> dict:
    """从 AC API 返回的网关对象中提取关键元数据字段。"""
    container = gw.get("container") or {}
    apps = container.get("apps", [])
    app_version = ""
    if isinstance(apps, list) and apps:
        app = apps[0]
        app_version = f"{app.get('name', '')}.{app.get('version', '')}"

    return {
        "mac": gw.get("mac", ""),
        "name": gw.get("name", ""),
        "model": gw.get("model", ""),
        "sn": gw.get("reserved3", ""),
        "status": gw.get("status", ""),
        "uplink": (gw.get("ap") or {}).get("uplink", ""),
        "version": gw.get("version", ""),
        "containerVersion": container.get("version", ""),
        "appVersion": app_version,
    }


# ============================================================
# 错误分类辅助函数
# ============================================================

def is_session_expired_error(e: Exception) -> bool:
    """判断异常是否由会话过期引起"""
    msg = str(e).lower()
    return "401" in msg or "session" in msg or "login" in msg


def is_network_error(e: Exception) -> bool:
    """判断异常是否由网络问题引起"""
    if isinstance(e, ConnectionError):
        return True
    msg = str(e).lower()
    network_keywords = [
        "net::err_connection",
        "net::err_network",
        "net::err_internet",
        "net::err_timed_out",
        "net::err_name_not",
        "fetch 超时",
        "获取在线网关列表超时",
        "websocket 连接已断开",
        "target page, context or browser has been closed",
        "connection refused",
        "connection reset",
        "econnrefused",
        "econnreset",
        "etimedout",
        "enetunreach",
    ]
    return any(kw in msg for kw in network_keywords)
