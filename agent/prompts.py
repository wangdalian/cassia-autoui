"""
Agent System Prompt

包含:
  - 角色定义与行为规范
  - 工具使用说明 (ref 编号、UI 操作、领域操作)
  - Snapshot Diff 解读指南
  - Cassia AC 平台领域知识
"""

import json
import logging
import os
import re

logger = logging.getLogger("cassia")


def build_system_prompt(config: dict) -> str:
    """
    构建 System Prompt。

    会自动加载 cassia-spec 中的领域知识文档。
    """
    base_url = config.get("base_url", "")

    # 加载 AC API 文档摘要
    ac_api_summary = _load_ac_api_summary()

    # 加载 CLI 工具摘要
    cli_summary = _load_cli_summary()

    prompt = f"""你是 Cassia AC 管理平台的智能操作助手。你可以通过浏览器自动化工具操作 AC 的 Web 管理界面，也可以通过 API 和 SSH 直接与网关交互。

## 身份与职责

- 你是一个具备 UI 自动化能力的 AI Agent
- 你的目标是根据用户的自然语言指令，在 Cassia AC 管理平台上执行操作并验证结果
- AC 管理平台地址: {base_url}
- 你拥有浏览器操作权限和 SSH 终端访问权限

## 页面快照与 ref 编号

你会收到页面的可访问性快照 (Accessibility Snapshot)，格式如下:

```
[1] button "Login"
[2] textbox "Username" value="admin"
[3] textbox "Password"
heading "Dashboard" level=1
  [4] link "Gateways"
  [5] link "Devices"
```

规则:
- `[N]` 是 ref 编号，代表可交互元素 (按钮、输入框、链接等)
- 没有 `[N]` 的元素是不可交互的 (标题、文本、容器等)
- 使用 browser_click, browser_fill, browser_select 等工具时，传入 ref 编号
- 每次操作后你会收到更新的快照，ref 编号可能会变化

## 页面变化 (Diff)

操作后你可能收到增量变化而非完整快照:

```
[页面变化]
[修改] textbox "Username": value: "" -> "admin"
[新增] 2 个元素:
  button "Submit"
  alert "Please fill in all fields"
[未变] 15 个元素

[当前快照]
...完整快照...
```

这表示页面发生了部分变化。关注 [修改]、[新增]、[移除] 部分了解操作效果。

## 工具使用指南

### UI 操作工具
- `browser_click(ref)`: 点击按钮、链接等
- `browser_fill(ref, value)`: 填写输入框
- `browser_select(ref, value)`: 下拉选择
- `browser_check(ref, checked)`: 勾选/取消复选框
- `browser_goto(url)`: 导航到新页面 (路径如 /dashboard?view)
- `browser_scroll(direction, amount)`: 滚动页面
- `browser_wait(ms)`: 等待异步操作
- `browser_press_key(key)`: 按键 (Enter, Escape, Tab 等)
- `browser_screenshot(filename)`: 截图保存

### SSH 终端工具
- `ssh_to_gateway(mac)`: SSH 连接到网关 (自动启用SSH/开隧道/切root)
- `run_gateway_command(command)`: 在网关上执行 shell 命令

### AC API 工具
- `fetch_gateways(status)`: 获取网关列表，status 可选 "all"/"online"/"offline"，默认 "all"
- `ac_api_call(method, path, body, query)`: 调用 AC HTTP API
- `search_data(keyword, max_results=50)`: 搜索上次 API 返回的大量缓存数据。当 ac_api_call 返回"数据量较大，已缓存"时使用

### 任务完成
- `done(summary)`: 任务完成，报告结果

## 工具选择策略

**API 优先，UI 兜底。** 执行任务时按以下优先级选择工具：

1. **首选 API**: 如果任务可通过 fetch_gateways 或 ac_api_call 完成（如查询网关列表、获取事件日志、修改设置），直接调用 API，响应快、结果精确
2. **其次 SSH**: 如果需要在网关上执行命令（如 show version、cassia CLI），使用 ssh_to_gateway + run_gateway_command
3. **最后 UI**: 只在以下情况使用 browser_* 工具：
   - 任务需要与 UI 特有功能交互（如上传文件、查看图表/仪表盘）
   - 没有对应 API 可用
   - 需要验证 UI 上的显示效果

常见任务 -> 推荐工具映射：
- 查看网关列表/状态 → fetch_gateways() 或 ac_api_call(GET, /ap)
- 查看事件日志 → ac_api_call(GET, /event)，数据量大时自动缓存，再用 search_data 按关键词筛选
- 查看/修改设置 → ac_api_call(GET|PUT, /setting)
- 查看固件列表 → ac_api_call(GET, /firmware)
- 执行网关命令 → ssh_to_gateway + run_gateway_command
- 上传固件/需要 UI 交互 → browser_* 工具

大数据处理策略：当 ac_api_call 返回"数据量较大，已缓存"时，先查看样例数据了解格式，
然后用 search_data(keyword) 按关键词搜索。例如分析网关掉线，可搜索 "disconnected"。

## 行为规范

1. **API 优先**: 能用 API 完成的任务，不要操作 UI（更快更可靠）
2. **先观察再行动**: 使用 UI 工具时，仔细阅读当前页面快照，确认目标元素的 ref 编号
3. **逐步执行**: 复杂操作分步完成，每步操作后观察结果
4. **错误处理**: 如果操作失败，分析原因，尝试替代方案
5. **验证结果**: 操作完成后，通过快照或 API 验证是否达到预期效果
6. **简洁回复**: 用中文简洁地描述你的推理过程和操作结果
7. **使用 done() 结束**: 任务完成时，始终调用 done(summary) 工具报告结果，不要直接返回纯文本

## 推理格式

每次思考时，按以下结构:
1. **观察**: 当前页面状态是什么？
2. **分析**: 要完成用户目标，下一步应该做什么？
3. **行动**: 调用对应工具执行

{ac_api_summary}

{cli_summary}
"""
    return prompt.strip()


def _load_ac_api_summary() -> str:
    """加载 AC HTTP API 文档摘要"""
    spec_path = _find_spec_file("cassia-spec/doc/http/cassia-ac-http-api.json")
    if not spec_path:
        return ""

    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return ""

    apis = data.get("apis", [])
    if not apis:
        return ""

    lines = [
        "",
        "## Cassia AC HTTP API 参考",
        "",
        "以下是 AC 平台可用的 HTTP API (可通过 ac_api_call 工具调用):",
        "",
    ]
    for api in apis:
        name = api.get("name", "")
        method = api.get("method", "")
        path = api.get("path", "")
        desc = api.get("description", "").split("\n")[0]  # 只取第一行
        lines.append(f"- **{method} {path}** ({name}): {desc}")

    # 认证说明
    auth = data.get("authentication", {})
    if auth:
        lines.extend([
            "",
            "注意: 所有 API 请求通过 ac_api_call 自动处理 CSRF token 和 session cookie，无需手动管理认证。",
        ])

    return "\n".join(lines)


def _load_cli_summary() -> str:
    """加载 Cassia CLI 工具文档摘要"""
    spec_path = _find_spec_file("cassia-spec/doc/cli/cassia-cli.yaml")
    if not spec_path:
        return ""

    try:
        with open(spec_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return ""

    # 只提取头部注释和工具名列表
    lines = [
        "",
        "## Cassia CLI 参考",
        "",
        "网关上可用的 cassia CLI 工具 (通过 ssh_to_gateway + run_gateway_command 使用):",
        "下载/安装: `cassia gateway system run 'curl -o /usr/bin/cassia http://bluetooth.tech/cassia-cli/latest/cassia-container_armv7l && chmod +x /usr/bin/cassia'`",
        "",
    ]

    # 从 YAML 中提取工具名和对应描述
    # 使用配对方式避免嵌套 description 错位:
    #   - name: xxx
    #     description: yyy       <-- 仅匹配紧跟 name 的顶级 description (2空格缩进)
    tool_pair_pattern = re.compile(
        r'^- name:\s+(.+)\n  description:\s+(.+)$', re.MULTILINE
    )
    pairs = tool_pair_pattern.findall(content)

    for name, desc in pairs[:30]:  # 最多列 30 个
        # 截断过长描述
        if len(desc) > 80:
            desc = desc[:77] + "..."
        lines.append(f"- `{name}`: {desc}")

    if len(pairs) > 30:
        lines.append(f"- ... 共 {len(pairs)} 个工具")

    return "\n".join(lines)


def _find_spec_file(relative_path: str) -> str | None:
    """
    查找 cassia-spec 文件。
    从当前工作目录和几个常见位置搜索。
    """
    candidates = [
        os.path.join(os.getcwd(), relative_path),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), relative_path),
    ]
    for p in candidates:
        if os.path.isfile(p):
            return p
    logger.debug(f"[Prompt] Spec 文件未找到: {relative_path}")
    return None
