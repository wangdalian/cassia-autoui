"""
测试代码生成器

基于 LLM 将录制轨迹 + 用户意图转为 pytest-playwright 测试代码。
不负责执行测试或文件保存（由 TestRunner 处理）。

数据流:
    ActionRecorder.format_trace() → (trace_text, extra_fixtures)
    TestGenerator.generate(trace_text, instruction, extra_fixtures) → test_code: str
    TestGenerator.fix(test_code, error) → fixed_code: str

非 UI 工具的测试代码由 recorder 预计算 (Code: 字段)，
LLM 只需组装代码、添加断言和注释，无需自行翻译非 UI 操作。
"""

from __future__ import annotations

import logging
import re
from typing import Callable

logger = logging.getLogger("cassia")

OnChunkCallback = Callable[[str], None] | None

# ============================================================
# Prompt 模板
# ============================================================
# BASE_PROMPT: 通用指令（适用于所有测试）
# _FIXTURE_HINT: 当检测到额外 fixture 时动态追加的签名提示
# _CODE_HINT_INSTRUCTION: 当 trace 中包含 Code: 行时追加的说明

_BASE_PROMPT = """你是 Playwright 测试专家。根据以下执行轨迹，生成一个 pytest-playwright 测试函数。

## 用户测试意图
{instruction}

## 执行轨迹
{trace}

## 生成要求

1. **Locator 策略** (适用于 browser_* UI 操作):
   - 首选 `page.get_by_role(role, name=name, exact=True)`
   - name 不唯一时，结合父级 locator 或 `page.locator("selector").get_by_role(...)` 缩小范围
   - 避免使用 `.nth()`，优先用语义定位
   - 有 placeholder 属性时可用 `page.get_by_placeholder()`

2. **断言**:
   - 关键步骤后加 `expect()` 或 `assert` 断言
   - UI 操作: 用 `expect(page).to_have_url(...)`, `expect(locator).to_be_visible(...)` 等
   - 非 UI 操作 (SSH 命令/API 调用): 用 `assert "expected" in output` 等
   - 用 `timeout` 参数处理异步等待

3. **环境无关**:
   - `base_url` 从函数参数获取
   - 用户名密码从 `credentials` fixture 获取: `credentials["username"]`, `credentials["password"]`
   - URL 使用 `f"{{base_url}}/path"` 拼接，不硬编码 IP 地址

4. **测试隔离**:
   - 函数独立可运行，不依赖其他测试的执行顺序
   - 有副作用的操作（创建、删除、修改）在结尾做清理

5. **等待策略**:
   - 优先用 `expect` 的 `timeout` 参数隐式等待
   - 仅在必要时使用 `page.wait_for_timeout(ms)`（如等待动画完成）

6. **Fixture 选择**:
   - 如果测试的是登录功能本身、未授权访问等不需要预登录的场景，使用 `page` fixture（pytest-playwright 默认提供的全新未登录 page）
   - 其他所有需要登录后操作的测试，使用 `authenticated_page` fixture（已完成登录）
   - 当使用 `authenticated_page` 时，函数签名里写 `authenticated_page`，函数体中也用 `authenticated_page` 变量名操作
   - AC 登录页面的 input 用 CSS selector 定位: `page.locator('input[name="username"]')`, `page.locator('input[name="password"]')`, 登录按钮: `page.locator('button:has-text("Login"), button:has-text("登录")')`

7. **代码格式**:
   - 登录测试签名: `def test_xxx(page, base_url, credentials):`
   - 非登录测试签名: `def test_xxx(authenticated_page, base_url, credentials):`
   - 根据测试内容添加 `@pytest.mark.xxx` 标签 (smoke/login/gateway/device/settings/ssh/api/emmc)
   - 文件头包含 `from playwright.sync_api import Page, expect` 和 `import pytest`
   - 每步操作前加简短中文注释说明意图

8. **非 UI 操作 (Code: 提示)**:
   - 轨迹中标有 `Code:` 的步骤已提供预计算的测试代码，**直接使用**，不要尝试用 Playwright UI 操作替代
   - SSH/API/eMMC 等操作不通过 UI 按钮完成，不要编造不存在的 UI 元素

{extra_instructions}

9. **仅输出 Python 代码**，用 ```python ``` 包裹，不要输出任何解释文字。
"""

_FIXTURE_HINT = """**额外 Fixture**: 本测试需要以下额外 fixture，请加入函数签名:
{fixture_list}
非登录测试签名示例: `def test_xxx(authenticated_page, base_url, credentials, {fixture_params}):`
"""

FIX_PROMPT_TEMPLATE = """以下 pytest-playwright 测试代码执行失败，请根据错误信息修正。

## 当前代码
```python
{test_code}
```

## 错误输出
```
{error_output}
```

## 修正要求
1. 只修正导致失败的部分，保持其他代码不变
2. 如果是 locator 找不到元素，尝试更宽松的匹配 (去掉 exact=True, 或换 locator 策略)
3. 如果是超时，增大 timeout 参数
4. 如果是断言失败，修正期望值或改用更合适的断言
5. 仅输出完整的修正后 Python 代码，用 ```python ``` 包裹
"""


# Fixture 描述从 recorder 统一注册表获取 (单一数据源，避免分散维护)
def _get_fixture_descriptions() -> dict[str, str]:
    from lib.recorder import get_fixture_descriptions
    return get_fixture_descriptions()


class TestGenerator:
    """
    测试代码生成器。

    使用方式:
        generator = TestGenerator(llm_client, llm_config)
        code = generator.generate(trace_text, "测试登录功能")
        code = generator.generate(trace_text, "SSH测试", extra_fixtures={"ssh_helper"})
        fixed = generator.fix(code, "AssertionError: ...")

    支持通过 on_chunk 回调实时推送 LLM 流式输出。
    """

    def __init__(self, llm_client, llm_config: dict):
        self._client = llm_client
        self._model = llm_config.get("model", "kimi-k2.5")
        self._temperature: float | None = llm_config.get("temperature", 0.1)
        if "kimi" in self._model.lower():
            self._temperature = None

    def generate(
        self,
        trace_text: str,
        instruction: str,
        extra_fixtures: set[str] | None = None,
        on_chunk: OnChunkCallback = None,
    ) -> str:
        """
        调用 LLM 生成测试代码。

        Args:
            trace_text: ActionRecorder.format_trace() 的 trace 文本
            instruction: 用户的测试意图描述
            extra_fixtures: format_trace() 检测到的额外 fixture 名称集合
            on_chunk: 可选回调，接收每个流式 token 文本片段

        Returns:
            生成的 Python 测试代码字符串
        """
        extra_instructions = _build_fixture_hint(extra_fixtures)

        prompt = _BASE_PROMPT.format(
            instruction=instruction,
            trace=trace_text,
            extra_instructions=extra_instructions,
        )
        logger.info(
            f"[TestGen] 调用 LLM 生成测试代码"
            f" (extra_fixtures={extra_fixtures or set()})"
        )
        content = self._call_llm(prompt, on_chunk=on_chunk)
        code = _extract_python_code(content)
        logger.info(f"[TestGen] 生成代码 {len(code)} 字符")
        return code

    def fix(
        self,
        test_code: str,
        error_output: str,
        on_chunk: OnChunkCallback = None,
    ) -> str:
        """
        根据 pytest 错误输出让 LLM 修正测试代码。

        Args:
            test_code: 当前测试代码
            error_output: pytest 的错误输出文本
            on_chunk: 可选回调，接收每个流式 token 文本片段

        Returns:
            修正后的 Python 测试代码
        """
        prompt = FIX_PROMPT_TEMPLATE.format(
            test_code=test_code,
            error_output=error_output[-3000:],
        )
        logger.info("[TestGen] 调用 LLM 修正测试代码")
        content = self._call_llm(prompt, on_chunk=on_chunk)
        code = _extract_python_code(content)
        logger.info(f"[TestGen] 修正后代码 {len(code)} 字符")
        return code

    def _call_llm(self, prompt: str, on_chunk: OnChunkCallback = None) -> str:
        """
        调用 LLM 并返回响应文本。
        当 on_chunk 回调存在时使用流式 API，实时推送每个 token。
        """
        kwargs: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        if on_chunk is None:
            response = self._client.chat.completions.create(**kwargs)
            msg = response.choices[0].message
            # kimi/deepseek 可能将内容放在 reasoning_content 中
            return msg.content or getattr(msg, "reasoning_content", None) or ""

        chunks: list[str] = []
        stream = self._client.chat.completions.create(**kwargs, stream=True)
        for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if not delta:
                continue
            # kimi/deepseek 模型可能将内容放在 reasoning_content 中
            text = delta.content or getattr(delta, "reasoning_content", None)
            if text:
                chunks.append(text)
                on_chunk(text)
        return "".join(chunks)


def _build_fixture_hint(extra_fixtures: set[str] | None) -> str:
    """根据检测到的额外 fixture 构建签名提示文本。"""
    if not extra_fixtures:
        return ""

    descs = _get_fixture_descriptions()
    fixture_lines = []
    for name in sorted(extra_fixtures):
        desc = descs.get(name, f"`{name}`")
        fixture_lines.append(f"   - {desc}")

    if not fixture_lines:
        return ""

    return _FIXTURE_HINT.format(
        fixture_list="\n".join(fixture_lines),
        fixture_params=", ".join(sorted(extra_fixtures)),
    )


def _extract_python_code(content: str) -> str:
    """
    从 LLM 响应中提取 Python 代码块。
    优先匹配 ```python ... ``` 代码块，
    回退到匹配任意 ``` ... ``` 代码块，
    最后回退到整个内容。
    """
    # ```python ... ```
    match = re.search(r"```python\s*\n(.+?)```", content, re.DOTALL)
    if match:
        return match.group(1).strip()

    # ``` ... ```
    match = re.search(r"```\s*\n(.+?)```", content, re.DOTALL)
    if match:
        return match.group(1).strip()

    return content.strip()
