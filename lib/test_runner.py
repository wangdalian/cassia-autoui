"""
测试执行器

负责测试文件保存、语法校验、可选的 pytest 完整验证。

数据流:
    TestRunner.save(code, name) → test_path
    TestRunner.syntax_check(code) → (passed, error)
    TestRunner.generate_and_verify(generator, ...) → (code, path, passed)
    TestRunner.run(test_path, headed=True) → (passed, output)  [独立验证时使用]
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Callable

logger = logging.getLogger("cassia")

OnChunkCallback = Callable[[str], None] | None


class TestRunner:
    """
    测试执行器。

    生成阶段: generate_and_verify → 生成代码 → 语法检查 → 保存
    验证阶段: run() → subprocess pytest 执行 (独立步骤，用户手动触发)

    使用方式:
        runner = TestRunner("tests/generated")

        # 生成阶段 (在 Agent 流程中)
        code, path, passed = runner.generate_and_verify(
            generator, trace_text, instruction, "login_success"
        )

        # 独立验证 (用户手动或 CI)
        passed, output = runner.run(path)
    """

    def __init__(self, output_dir: str = "tests/generated"):
        self._output_dir = output_dir

    def list_tests(self) -> list[dict]:
        """列出测试目录下所有测试文件，返回 [{name, path}] 列表（path 为绝对路径）。"""
        if not os.path.isdir(self._output_dir):
            return []
        abs_dir = os.path.abspath(self._output_dir)
        return [
            {"name": f, "path": os.path.join(abs_dir, f)}
            for f in sorted(os.listdir(self._output_dir))
            if f.startswith("test_") and f.endswith(".py")
        ]

    def save(self, test_code: str, test_name: str) -> str:
        """保存测试代码到文件，返回绝对路径。"""
        os.makedirs(self._output_dir, exist_ok=True)

        name = test_name.strip().replace(" ", "_").replace("-", "_")
        if not name.startswith("test_"):
            name = f"test_{name}"
        if not name.endswith(".py"):
            name = f"{name}.py"

        path = os.path.join(self._output_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(test_code)

        logger.info(f"[TestRunner] 测试文件已保存: {path}")
        return os.path.abspath(path)

    @staticmethod
    def syntax_check(test_code: str) -> tuple[bool, str]:
        """编译检查 Python 语法，不执行代码。"""
        try:
            compile(test_code, "<generated_test>", "exec")
            return True, ""
        except SyntaxError as e:
            return False, f"语法错误 (行 {e.lineno}): {e.msg}"

    def generate_and_verify(
        self,
        generator,
        trace_text: str,
        instruction: str,
        test_name: str,
        extra_fixtures: set[str] | None = None,
        on_chunk: OnChunkCallback = None,
    ) -> tuple[str, str, bool]:
        """
        编排: LLM 生成 → 语法检查 → 失败则修正一次 → 保存。

        Args:
            extra_fixtures: format_trace() 检测到的额外 fixture 名称
            on_chunk: 可选回调，流式接收 LLM 生成的 token

        Returns:
            (最终代码, 文件路径, 语法是否通过)
        """
        logger.info(f"[TestRunner] 生成测试: {test_name}")

        test_code = generator.generate(
            trace_text, instruction,
            extra_fixtures=extra_fixtures,
            on_chunk=on_chunk,
        )

        passed, error = self.syntax_check(test_code)
        if not passed:
            logger.info(f"[TestRunner] 语法检查失败, LLM 修正中: {error}")
            if on_chunk:
                on_chunk("\n\n--- 语法检查未通过，正在修正 ---\n\n")
            test_code = generator.fix(test_code, error, on_chunk=on_chunk)
            passed, error = self.syntax_check(test_code)

        test_path = self.save(test_code, test_name)

        if passed:
            logger.info(f"[TestRunner] 语法检查通过: {test_path}")
        else:
            logger.warning(f"[TestRunner] 语法仍有问题，已保存为 draft: {test_path}")

        return test_code, test_path, passed

    def run(
        self,
        test_path: str,
        headed: bool = True,
        slowmo: int = 300,
        marker: str | None = None,
        timeout: int = 180,
    ) -> tuple[bool, str]:
        """
        执行单个测试文件 (独立步骤，非生成阶段使用)。

        pytest-playwright 在子进程中启动独立的浏览器实例，
        与 Agent 的浏览器互不干扰。

        Args:
            test_path: 测试文件路径
            headed: 是否显示浏览器窗口 (默认 True，方便直观观察)
            slowmo: 操作间隔毫秒数 (headed 模式下默认 300ms，便于观察)
            marker: pytest marker 过滤 (如 "smoke", "login")
            timeout: 执行超时秒数

        Returns:
            (是否通过, pytest 输出文本)
        """
        logger.info(f"[TestRunner] 执行测试: {test_path} (headed={headed}, slowmo={slowmo})")
        cmd = [
            sys.executable, "-m", "pytest",
            test_path,
            "-v",
            "--tb=short",
            "--no-header",
            "-x",
            # pytest.ini addopts 会注入 --reruns 2 --timeout 120，
            # 这里用同名参数覆盖（argparse 最后一个生效）
            "--reruns", "0",
            "--timeout", "0",
        ]
        if headed:
            cmd.append("--headed")
        if headed and slowmo > 0:
            cmd.extend(["--slowmo", str(slowmo)])
        if marker:
            cmd.extend(["-m", marker])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = result.stdout + result.stderr
            passed = result.returncode == 0
            logger.info(f"[TestRunner] 测试{'通过' if passed else '失败'}: returncode={result.returncode}")
            return passed, output
        except subprocess.TimeoutExpired:
            logger.warning(f"[TestRunner] 测试执行超时 ({timeout}s)")
            return False, f"测试执行超时 ({timeout}s)"
        except Exception as e:
            logger.error(f"[TestRunner] 执行异常: {e}")
            return False, f"执行异常: {e}"
