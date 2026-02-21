"""
Accessibility Snapshot 引擎

核心类 SnapshotParser:
- 获取页面可访问性树并压缩为 LLM 友好的文本格式
- 为可交互元素分配 [N] ref 编号
- 语义级 Diff: 对比两次快照，输出新增/移除/修改
- ref -> Playwright Locator 反查
- 智能决策: 首次发全量，后续根据变化量选择发 Diff 或全量
"""

import logging
import re
from playwright.sync_api import Page, Locator

logger = logging.getLogger("cassia")

# aria_snapshot YAML 行解析正则
# 格式: - role "name" [attr=val] [attr2=val2]:
_ARIA_ATTR_RE = re.compile(r'\[(\w[\w-]*)(?:=([^\]]*))?\]')

# 可交互的角色类型 (需要分配 ref 编号)
INTERACTIVE_ROLES = {
    "button", "textbox", "combobox", "checkbox", "radio",
    "link", "menuitem", "tab", "slider", "switch",
    "option", "searchbox", "spinbutton", "menuitemcheckbox",
    "menuitemradio", "treeitem",
}

# 纯装饰角色 (可省略以减少 token)
SKIP_ROLES = {
    "none", "presentation", "generic", "paragraph",
    "LineBreak", "InlineTextBox",
}


class SnapshotParser:
    """
    Accessibility Snapshot 解析引擎。

    使用方法:
        parser = SnapshotParser()
        observation = parser.get_observation(page)  # 获取页面状态 (自动处理 diff)
        locator = parser.ref_to_locator(page, 3)    # ref=3 对应的 Locator
        parser.reset()                                # 页面导航后重置
    """

    def __init__(self, diff_threshold: float = 0.6):
        """
        Args:
            diff_threshold: 变化比例阈值。
                变化 < threshold → 发 diff;  >= threshold → 发全量。
        """
        self._diff_threshold = diff_threshold
        self._counter = 0
        self._ref_map: dict[int, dict] = {}  # ref -> {role, name, value, ...}
        self._last_tree: dict | None = None   # 上次快照的原始树
        self._last_text: str = ""             # 上次快照的文本
        self._last_elements: dict[tuple, dict] | None = None  # (role, name) -> node

    def reset(self):
        """重置状态 (页面导航后调用)"""
        self._counter = 0
        self._ref_map = {}  # ref -> {role, name, value, nth}
        self._role_name_count: dict[tuple, int] = {}  # (role, name) -> 出现次数
        self._last_tree = None
        self._last_text = ""
        self._last_elements = None

    # ============================================================
    # 公共 API
    # ============================================================

    def get_observation(self, page: Page) -> str:
        """
        获取当前页面状态，智能选择全量或增量。

        返回 LLM 可直接阅读的文本:
        - 首次调用: "[页面快照]\n..."
        - 后续无变化: "[页面无变化]"
        - 后续有变化且较小: "[页面变化]\n... diff ...\n\n[当前快照]\n..."
        - 后续变化较大: "[页面快照]\n..." (全量)
        """
        tree = self._take_snapshot(page)
        if tree is None:
            return "[页面快照]\n(空白页面)"

        # 重新分配 ref 编号
        self._counter = 0
        self._ref_map = {}
        self._role_name_count = {}
        text_lines = self._walk(tree, depth=0)
        current_text = "\n".join(text_lines)
        current_elements = self._flatten_to_dict(tree)

        # 首次快照 → 全量
        if self._last_elements is None:
            self._last_tree = tree
            self._last_text = current_text
            self._last_elements = current_elements
            return f"[页面快照]\n{current_text}"

        # 计算语义 diff
        diff_result = self._semantic_diff(self._last_elements, current_elements)

        # 保存当前状态
        self._last_tree = tree
        self._last_text = current_text
        self._last_elements = current_elements

        # 无变化
        total_changes = len(diff_result["added"]) + len(diff_result["removed"]) + len(diff_result["modified"])
        if total_changes == 0:
            return "[页面无变化]"

        # 变化量判断
        total_elements = max(
            len(diff_result["added"]) + len(diff_result["removed"])
            + len(diff_result["modified"]) + diff_result["unchanged_count"],
            1
        )
        change_ratio = total_changes / total_elements

        if change_ratio >= self._diff_threshold:
            # 变化太大 → 全量
            return f"[页面快照]\n{current_text}"

        # 变化较小 → diff + 完整快照
        diff_text = self._format_diff(diff_result)
        return f"[页面变化]\n{diff_text}\n\n[当前快照]\n{current_text}"

    def get_full_snapshot(self, page: Page) -> str:
        """强制获取完整快照 (不做 diff)"""
        tree = self._take_snapshot(page)
        if tree is None:
            return "(空白页面)"

        self._counter = 0
        self._ref_map = {}
        self._role_name_count = {}
        text_lines = self._walk(tree, depth=0)
        current_text = "\n".join(text_lines)

        self._last_tree = tree
        self._last_text = current_text
        self._last_elements = self._flatten_to_dict(tree)

        return current_text

    def ref_to_locator(self, page: Page, ref_id: int) -> Locator:
        """
        通过 ref 编号找到对应的 Playwright Locator。
        使用 page.get_by_role() + nth 序号精确定位 (即使多个元素同名)。
        """
        if ref_id not in self._ref_map:
            raise ValueError(f"未知的 ref: [{ref_id}]，当前有效 ref: {list(self._ref_map.keys())}")

        info = self._ref_map[ref_id]
        role = info["role"]
        name = info.get("name", "")
        nth = info.get("nth", 0)

        # 用 get_by_role 定位
        if name:
            locator = page.get_by_role(role, name=name, exact=True)
            count = locator.count()
            if count == 0:
                # exact 匹配失败，回退到模糊匹配
                locator = page.get_by_role(role, name=name)
                count = locator.count()
            if count > 1:
                # 多个同名元素，用 nth 序号精确选中
                locator = locator.nth(nth)
        else:
            # 没有 name，只能靠 role + nth
            locator = page.get_by_role(role).nth(nth)

        return locator

    def execute_action(self, page: Page, action_type: str, ref_id: int, **kwargs) -> str:
        """
        执行基于 ref 的 UI 操作。

        Args:
            page: Playwright Page
            action_type: click / fill / select / check / uncheck
            ref_id: 元素 ref 编号
            **kwargs: fill 需要 value, select 需要 value

        Returns:
            操作结果描述
        """
        locator = self.ref_to_locator(page, ref_id)
        info = self._ref_map.get(ref_id, {})
        desc = f'[{ref_id}] {info.get("role", "")} "{info.get("name", "")}"'

        if action_type == "click":
            locator.click()
            return f"已点击 {desc}"
        elif action_type == "fill":
            value = kwargs.get("value", "")
            locator.fill(value)
            return f"已填写 {desc} = \"{value}\""
        elif action_type == "select":
            value = kwargs.get("value", "")
            locator.select_option(value)
            return f"已选择 {desc} = \"{value}\""
        elif action_type == "check":
            locator.check()
            return f"已勾选 {desc}"
        elif action_type == "uncheck":
            locator.uncheck()
            return f"已取消勾选 {desc}"
        else:
            raise ValueError(f"不支持的操作类型: {action_type}")

    # ============================================================
    # 内部实现
    # ============================================================

    def _take_snapshot(self, page: Page) -> dict | None:
        """
        获取页面可访问性快照。

        Playwright 1.48+ 移除了 page.accessibility，改用 locator.aria_snapshot()
        返回 YAML 字符串，需解析为树结构。
        """
        try:
            yaml_str = page.locator("body").aria_snapshot()
            if not yaml_str or not yaml_str.strip():
                return None
            return self._parse_aria_yaml(yaml_str)
        except Exception as e:
            logger.warning(f"[Snapshot] 获取可访问性快照失败: {e}")
            return None

    def _parse_aria_yaml(self, yaml_str: str) -> dict:
        """
        解析 aria_snapshot() 返回的 YAML 字符串为树结构。

        YAML 格式示例:
            - heading "Dashboard" [level=1]
            - navigation "Main":
              - link "Home"
              - link "Settings"
            - textbox "Search" [value=""]
        """
        root: dict = {"role": "WebArea", "name": "", "children": []}
        stack: list[tuple[int, dict]] = [(-2, root)]

        for line in yaml_str.split("\n"):
            stripped = line.lstrip()
            if not stripped.startswith("- "):
                continue

            indent = len(line) - len(stripped)
            content = stripped[2:]  # 去掉 "- " 前缀

            node = self._parse_aria_line(content)
            if node is None:
                continue

            # 回溯栈找到父节点 (indent 比当前小的)
            while len(stack) > 1 and stack[-1][0] >= indent:
                stack.pop()

            parent = stack[-1][1]
            if "children" not in parent:
                parent["children"] = []
            parent["children"].append(node)

            stack.append((indent, node))

        return root

    def _parse_aria_line(self, content: str) -> dict | None:
        """
        解析单行 aria snapshot 内容 (已去掉 '- ' 前缀)。

        支持格式:
            role "name" [attr=val]      → 叶节点
            role "name" [attr=val]:     → 有子节点 (末尾冒号)
            role [attr=val]             → 无名节点
            role                        → 最简节点
            "text content"              → 纯文本节点
        """
        content = content.rstrip()
        # 去掉末尾的 : (表示有子节点)
        if content.endswith(":"):
            content = content[:-1].rstrip()

        if not content:
            return None

        # 纯文本节点 (以引号开头)
        if content.startswith('"'):
            end = content.find('"', 1)
            name = content[1:end] if end != -1 else content[1:]
            return {"role": "text", "name": name}

        # 正则模式 (以 / 开头) — 作为文本节点
        if content.startswith('/'):
            return {"role": "text", "name": "(pattern)"}

        # 正常格式: role ["name"] [attrs]
        parts = content.split(None, 1)
        role = parts[0]
        rest = parts[1] if len(parts) > 1 else ""

        node: dict = {"role": role, "name": ""}

        # 提取 "name" (带引号的字符串)
        if rest.startswith('"'):
            end = rest.find('"', 1)
            if end != -1:
                node["name"] = rest[1:end]
                rest = rest[end + 1:].strip()
        elif rest.startswith('/'):
            # 正则模式 — 跳过
            end = rest.find('/', 1)
            if end != -1:
                rest = rest[end + 1:].strip()

        # 提取 [key=value] 属性
        for m in _ARIA_ATTR_RE.finditer(rest):
            key = m.group(1)
            val = m.group(2) or "true"
            # 去掉 value 外层引号
            if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                val = val[1:-1]
            if key == "level":
                try:
                    node["level"] = int(val)
                except ValueError:
                    pass
            elif key == "checked":
                node["checked"] = val.lower() not in ("false", "no", "0")
            elif key == "expanded":
                node["expanded"] = val.lower() not in ("false", "no", "0")
            elif key == "selected":
                node["selected"] = val.lower() not in ("false", "no", "0")
            elif key == "pressed":
                node["pressed"] = val.lower() not in ("false", "no", "0")
            elif key == "disabled":
                node["disabled"] = val.lower() not in ("false", "no", "0")
            elif key == "value":
                node["value"] = val

        return node

    def _walk(self, node: dict, depth: int) -> list[str]:
        """
        递归遍历可访问性树，生成压缩文本。
        为可交互元素分配 [N] ref 编号。
        """
        role = node.get("role", "")
        name = node.get("name", "")
        value = node.get("value", "")
        checked = node.get("checked")
        expanded = node.get("expanded")
        selected = node.get("selected")
        level = node.get("level")

        # 跳过装饰性节点 (但保留有子节点的)
        children = node.get("children", [])
        if role in SKIP_ROLES and not name and not children:
            return []

        # 对无角色但有子节点的容器，直接递归子节点
        if role in SKIP_ROLES and not name and children:
            lines = []
            for child in children:
                lines.extend(self._walk(child, depth))
            return lines

        indent = "  " * depth
        parts = []

        # 可交互元素分配 ref (记录 nth 序号以区分同名元素)
        if role in INTERACTIVE_ROLES:
            self._counter += 1
            rn_key = (role, name)
            nth = self._role_name_count.get(rn_key, 0)
            self._role_name_count[rn_key] = nth + 1
            parts.append(f"[{self._counter}]")
            self._ref_map[self._counter] = {
                "role": role,
                "name": name,
                "value": value,
                "nth": nth,
            }

        parts.append(role)

        if name:
            parts.append(f'"{name}"')
        if level is not None:
            parts.append(f"level={level}")
        if value:
            parts.append(f'value="{value}"')
        if checked is not None:
            parts.append(f'checked={"yes" if checked else "no"}')
        if expanded is not None:
            parts.append(f'expanded={"yes" if expanded else "no"}')
        if selected:
            parts.append("(selected)")

        lines = [f"{indent}{' '.join(parts)}"]

        for child in children:
            lines.extend(self._walk(child, depth + 1))

        return lines

    def _flatten_to_dict(self, tree: dict) -> dict[tuple, dict]:
        """把树拍平为 {(role, name): node_info} 字典"""
        result = {}
        self._flatten_recursive(tree, result)
        return result

    def _flatten_recursive(self, node: dict, result: dict):
        """递归拍平"""
        role = node.get("role", "")
        name = node.get("name", "")

        if role not in SKIP_ROLES and (role or name):
            key = (role, name)
            # 如果有重名元素，用 (role, name, index) 区分
            if key in result:
                i = 2
                while (role, name, i) in result:
                    i += 1
                key = (role, name, i)
            result[key] = {
                "role": role,
                "name": name,
                "value": node.get("value", ""),
                "checked": node.get("checked"),
                "expanded": node.get("expanded"),
                "selected": node.get("selected"),
            }

        for child in node.get("children", []):
            self._flatten_recursive(child, result)

    def _semantic_diff(
        self,
        old_elements: dict[tuple, dict],
        new_elements: dict[tuple, dict],
    ) -> dict:
        """
        语义级 diff: 对比两次快照的扁平化元素。
        返回 { added: [...], removed: [...], modified: [...], unchanged_count: int }
        """
        old_keys = set(old_elements.keys())
        new_keys = set(new_elements.keys())

        added = []
        for key in new_keys - old_keys:
            added.append(new_elements[key])

        removed = []
        for key in old_keys - new_keys:
            removed.append(old_elements[key])

        modified = []
        unchanged_count = 0
        for key in old_keys & new_keys:
            old_e = old_elements[key]
            new_e = new_elements[key]
            if (old_e.get("value") != new_e.get("value") or
                    old_e.get("checked") != new_e.get("checked") or
                    old_e.get("expanded") != new_e.get("expanded") or
                    old_e.get("selected") != new_e.get("selected")):
                modified.append({
                    "element": new_e,
                    "old_value": old_e.get("value"),
                    "new_value": new_e.get("value"),
                    "old_checked": old_e.get("checked"),
                    "new_checked": new_e.get("checked"),
                })
            else:
                unchanged_count += 1

        return {
            "added": added,
            "removed": removed,
            "modified": modified,
            "unchanged_count": unchanged_count,
        }

    def _format_diff(self, diff_result: dict) -> str:
        """将语义 diff 格式化为 LLM 可读文本"""
        lines = []

        if diff_result["modified"]:
            for m in diff_result["modified"]:
                elem = m["element"]
                desc = f'{elem["role"]} "{elem["name"]}"'
                changes = []
                if m["old_value"] != m["new_value"]:
                    changes.append(f'value: "{m["old_value"]}" -> "{m["new_value"]}"')
                if m["old_checked"] != m["new_checked"]:
                    changes.append(f'checked: {m["old_checked"]} -> {m["new_checked"]}')
                if changes:
                    lines.append(f"[修改] {desc}: {'; '.join(changes)}")

        if diff_result["added"]:
            lines.append(f"[新增] {len(diff_result['added'])} 个元素:")
            for elem in diff_result["added"]:
                desc = f'  {elem["role"]} "{elem["name"]}"'
                if elem.get("value"):
                    desc += f' value="{elem["value"]}"'
                lines.append(desc)

        if diff_result["removed"]:
            lines.append(f"[移除] {len(diff_result['removed'])} 个元素:")
            for elem in diff_result["removed"]:
                lines.append(f'  {elem["role"]} "{elem["name"]}"')

        lines.append(f"[未变] {diff_result['unchanged_count']} 个元素")

        return "\n".join(lines)
