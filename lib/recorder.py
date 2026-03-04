"""
动作录制器

被动观察者模块 — 记录 Agent 操作的结构化轨迹。
Recorder 自身不调用任何 Snapshot 方法，数据完全由外部传入:
  - ToolExecutor.execute() 传入: 工具名/参数/元素信息/URL/结果
  - core.py ReAct 循环传入: 已计算的 observation 快照文本

始终在记录，每次 run() 开始时自动 reset。

format_trace() 为非 UI 工具预计算测试侧代码提示 (Code: 字段)，
使 LLM 只需组装代码和添加断言，避免幻想不存在的 UI 元素。
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

logger = logging.getLogger("cassia")


# ============================================================
# 工具 → 测试代码 统一注册表
# ============================================================
# 每个条目: tool_name → ToolTestMapping(fixture, code_gen, fixture_desc)
#   fixture:      测试 fixture 名称 (空字符串表示无需 fixture)
#   code_gen:     接收 (arguments, element_info) 返回测试侧代码字符串
#   fixture_desc: fixture 的 API 说明 (用于 LLM prompt 中的签名提示)
#
# 仅为非 UI 工具提供映射；UI 工具由 LLM 根据 element_info 生成 locator。
# 扩展方式: 新增工具只需在此添加一项 + 在 conftest.py 写对应 fixture。

CodeGenerator = Callable[[dict, dict | None], str]

@dataclass(frozen=True)
class ToolTestMapping:
    """单个非 UI 工具的测试侧映射信息。"""
    fixture: str
    code_gen: CodeGenerator
    fixture_desc: str = ""

_TOOL_TEST_REGISTRY: dict[str, ToolTestMapping] = {
    # ---- SSH ----
    "ssh_to_gateway": ToolTestMapping(
        fixture="ssh_helper",
        code_gen=lambda a, _: f'ssh_helper.connect("{a["mac"]}")',
        fixture_desc=(
            "`ssh_helper`: SSH 辅助器。"
            "`ssh_helper.connect(mac)` 连接网关, "
            "`ssh_helper.run_command(cmd)` 执行命令并返回输出文本, "
            "`ssh_helper.check_emmc()` 检查 eMMC 健康"
        ),
    ),
    "run_gateway_command": ToolTestMapping(
        fixture="ssh_helper",
        code_gen=lambda a, _: f'output = ssh_helper.run_command("{a["command"]}")',
    ),
    # ---- eMMC (依赖 ssh_helper) ----
    "check_emmc_health": ToolTestMapping(
        fixture="ssh_helper",
        code_gen=lambda a, _: "emmc = ssh_helper.check_emmc()",
    ),
    "batch_check_emmc": ToolTestMapping(
        fixture="ssh_helper",
        code_gen=lambda a, _: "# batch_check_emmc 是批量编排工具，测试中逐个使用 ssh_helper.connect() + ssh_helper.check_emmc()",
    ),
    # ---- AC API ----
    "ac_api_call": ToolTestMapping(
        fixture="api_helper",
        code_gen=lambda a, _: (
            f'result = api_helper.call("{a.get("method", "GET")}", "{a.get("path", "/")}"'
            + (f", body={a['body']}" if a.get("body") else "")
            + (f', query="{a["query"]}"' if a.get("query") else "")
            + ")"
        ),
        fixture_desc=(
            "`api_helper`: API 辅助器。"
            "`api_helper.call(method, path)` 调用 AC API, "
            "`api_helper.fetch_gateways(status)` 获取网关列表"
        ),
    ),
    "fetch_gateways": ToolTestMapping(
        fixture="api_helper",
        code_gen=lambda a, _: f'gateways = api_helper.fetch_gateways(status="{a.get("status", "all")}")',
    ),
    "search_data": ToolTestMapping(
        fixture="api_helper",
        code_gen=lambda a, _: '# search_data 是 Agent 缓存搜索，测试中直接使用 api_helper.call() 获取数据',
    ),
    # ---- 文件工具 — 测试中通常不需要 ----
    "write_local_file": ToolTestMapping(
        fixture="",
        code_gen=lambda a, _: '# write_local_file: 文件输出操作，测试中可跳过',
    ),
}

# 提取去重的 fixture 描述，供 test_generator 使用
def get_fixture_descriptions() -> dict[str, str]:
    """返回 {fixture_name: description} 映射（去重，只取每个 fixture 的第一个非空描述）。"""
    result: dict[str, str] = {}
    for mapping in _TOOL_TEST_REGISTRY.values():
        if mapping.fixture and mapping.fixture not in result and mapping.fixture_desc:
            result[mapping.fixture] = mapping.fixture_desc
    return result


@dataclass
class ActionRecord:
    """单步操作记录"""

    step: int = 0
    tool_name: str = ""
    arguments: dict = field(default_factory=dict)
    element_info: dict | None = None
    url_before: str = ""
    url_after: str = ""
    result: str = ""
    snapshot_after: str = ""
    timestamp: float = field(default_factory=lambda: datetime.now().timestamp())


class ActionRecorder:
    """
    被动观察者录制器 — 始终在记录，不主动获取任何数据。

    数据流:
        1. execute() 调用 record() 传入操作基础信息
        2. core.py 调用 update_last_snapshot() 补充 observation 文本
        3. generate_test 调用时读取 records + format_trace()

    使用方式:
        recorder = ActionRecorder()
        # 每轮 run() 开始时:
        recorder.reset()
        # execute() 中:
        recorder.record(ActionRecord(...))
        # core.py get_observation 之后:
        recorder.update_last_snapshot(observation_text)
        # generate_test 中:
        trace_text, extra_fixtures = recorder.format_trace()
    """

    def __init__(self):
        self._records: list[ActionRecord] = []
        self._step = 0

    def reset(self):
        """重置录制（每轮 run() 开始时调用）"""
        self._records.clear()
        self._step = 0
        logger.debug("[Recorder] 已重置")

    def record(self, record: ActionRecord):
        """记录一步操作（snapshot_after 留空，由 update_last_snapshot 补充）"""
        self._step += 1
        record.step = self._step
        self._records.append(record)
        logger.debug(
            f"[Recorder] Step {self._step}: {record.tool_name}"
            f"({_brief_args(record.arguments)})"
        )

    def update_last_snapshot(self, observation: str):
        """将 core.py 已计算的 observation 文本补充到最近一条记录"""
        if self._records:
            self._records[-1].snapshot_after = observation

    @property
    def records(self) -> list[ActionRecord]:
        return list(self._records)

    def format_trace(self) -> tuple[str, set[str]]:
        """
        将录制结果格式化为 LLM 可读的文本，并返回需要的额外 fixture 名称。

        非 UI 工具（SSH/API/eMMC）会包含预计算的 Code: 字段，
        LLM 应直接使用这些代码提示，无需自行编写。

        Returns:
            (trace_text, extra_fixtures)
            extra_fixtures: 如 {"ssh_helper", "api_helper"}

        输出示例:
            Step 1: browser_goto(url="/ap?view")
              URL: http://ac:8882/ → http://ac:8882/ap?view
              Result: 已导航到: http://ac:8882/ap?view

            Step 2: browser_fill(ref=5, value="b8")
              Element: textbox "搜索"
              URL: http://ac:8882/ap?view (unchanged)
              Result: 已填写 [5] textbox "搜索" = "b8"

            Step 3: ssh_to_gateway(mac="CC:1B:E0:E2:E9:B8")
              Code: ssh_helper.connect("CC:1B:E0:E2:E9:B8")
              Result: 已通过 SSH 连接到网关 (root)

            Step 4: run_gateway_command(command="ca_read country")
              Code: output = ssh_helper.run_command("ca_read country")
              Result: CN
        """
        if not self._records:
            return "(无录制记录)", set()

        extra_fixtures: set[str] = set()
        lines: list[str] = []

        for r in self._records:
            lines.append(f"Step {r.step}: {r.tool_name}({_brief_args(r.arguments)})")

            # 非 UI 工具: 输出预计算的测试代码
            mapping = _TOOL_TEST_REGISTRY.get(r.tool_name)
            if mapping:
                if mapping.fixture:
                    extra_fixtures.add(mapping.fixture)
                try:
                    code = mapping.code_gen(r.arguments, r.element_info)
                    lines.append(f"  Code: {code}")
                except (KeyError, TypeError) as e:
                    logger.debug(f"[Recorder] 代码提示生成失败 ({r.tool_name}): {e}")

            # UI 工具: 输出 element_info 供 LLM 生成 locator
            if r.element_info:
                ei = r.element_info
                name_part = f' "{ei["name"]}"' if ei.get("name") else ""
                nth_part = f" (nth={ei['nth']})" if ei.get("nth", 0) > 0 else ""
                parent_part = ""
                if ei.get("parent_role"):
                    pname = f' "{ei["parent_name"]}"' if ei.get("parent_name") else ""
                    parent_part = f", parent: {ei['parent_role']}{pname}"
                lines.append(f"  Element: {ei.get('role', '?')}{name_part}{nth_part}{parent_part}")

            if r.url_before == r.url_after:
                lines.append(f"  URL: {r.url_before} (unchanged)")
            else:
                lines.append(f"  URL: {r.url_before} → {r.url_after}")

            lines.append(f"  Result: {r.result}")

            if r.snapshot_after:
                snap_lines = r.snapshot_after.split("\n")[:30]
                lines.append(f"  Page snapshot ({len(snap_lines)} lines):")
                for sl in snap_lines:
                    lines.append(f"    {sl}")

            lines.append("")

        return "\n".join(lines), extra_fixtures


def _brief_args(arguments: dict) -> str:
    """将参数字典格式化为简短的 key=value 字符串"""
    parts = []
    for k, v in arguments.items():
        if isinstance(v, str) and len(v) > 40:
            v = v[:37] + "..."
        parts.append(f'{k}="{v}"' if isinstance(v, str) else f"{k}={v}")
    return ", ".join(parts)
