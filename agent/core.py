"""
Agent 核心引擎

CassiaAgent: 实现 ReAct 循环 + OpenAI function calling，
驱动 LLM 观察页面、推理、调用工具、验证结果。

支持上下文压缩以适应 LLM 上下文窗口限制。
"""

import json
import logging
from typing import Callable

from openai import OpenAI
from playwright.sync_api import Page

from lib.snapshot import SnapshotParser
from agent.tools import TOOL_DEFINITIONS, ToolExecutor
from agent.prompts import build_system_prompt

logger = logging.getLogger("cassia")


class CassiaAgent:
    """
    Cassia AC 管理平台 AI Agent。

    ReAct 循环:
      1. 获取页面状态 (Snapshot / Diff)
      2. 发送给 LLM，附上历史对话和工具定义
      3. LLM 决定调用哪个工具
      4. 执行工具，获取结果
      5. 将结果反馈给 LLM
      6. 重复直到 LLM 调用 done() 或达到最大步数
    """

    def __init__(
        self,
        page: Page,
        config: dict,
        on_thinking: Callable[[str], None] | None = None,
        on_thinking_chunk: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, dict, str], None] | None = None,
    ):
        """
        Args:
            page: Playwright Page 对象
            config: 配置字典
            on_thinking: LLM 思考完成回调 (full_text) — 非流式 fallback 时使用
            on_thinking_chunk: LLM 思考流式回调 (text_chunk) — 逐字输出
            on_tool_call: 工具调用回调 (tool_name, args, result)
        """
        self.page = page
        self.config = config

        # LLM 客户端
        llm_config = config.get("llm", {})
        self._client = OpenAI(
            api_key=llm_config.get("api_key", "sk-placeholder"),
            base_url=llm_config.get("base_url", "https://api.openai.com/v1"),
        )
        self._model = llm_config.get("model", "gpt-4o")
        self._temperature = llm_config.get("temperature", 0.1)
        # kimi 系列模型不支持自定义 temperature，直接置空
        if "kimi" in self._model.lower():
            self._temperature = None

        # Agent 参数
        agent_config = config.get("agent", {})
        self._max_steps = agent_config.get("max_steps", 30)
        self._wait_after_action = agent_config.get("wait_after_action_ms", 1000)
        self._context_max_messages = agent_config.get("context_max_messages", 40)

        # 核心组件
        diff_threshold = agent_config.get("diff_threshold", 0.6)
        snapshot_max_lines = agent_config.get("snapshot_max_lines", None)
        self.snapshot = SnapshotParser(diff_threshold=diff_threshold, max_lines=snapshot_max_lines)
        self.executor = ToolExecutor(page, self.snapshot, config)

        # 对话历史
        self._system_prompt = build_system_prompt(config)
        self._messages: list[dict] = []

        # 回调
        self._on_thinking = on_thinking
        self._on_thinking_chunk = on_thinking_chunk
        self._on_tool_call = on_tool_call

        # 追踪最后一次流式输出的完整文本 (用于避免 cli 重复渲染)
        self.last_streamed_content: str | None = None

    def run(self, user_instruction: str) -> str:
        """
        执行一轮 Agent 任务。

        Args:
            user_instruction: 用户的自然语言指令

        Returns:
            任务完成的总结文本
        """
        # 重置流式输出追踪
        self.last_streamed_content = None

        # 获取初始页面快照
        observation = self.snapshot.get_observation(self.page)

        # 构建初始用户消息
        user_message = f"""用户指令: {user_instruction}

当前页面 URL: {self.page.url}

{observation}"""

        self._messages.append({"role": "user", "content": user_message})

        # ReAct 循环
        for step in range(1, self._max_steps + 1):
            logger.info(f"[Agent] 第 {step} 步")

            # 流式调用 LLM
            content, tool_calls, message_dict = self._call_llm_stream()

            if message_dict is None:
                return "LLM 调用失败，任务终止"

            # 保存 assistant 消息
            self._messages.append(message_dict)

            # 如果有文本回复: 流式过程中已通过 on_thinking_chunk 逐字输出
            if content:
                logger.debug(f"[Agent] 思考: {content[:200]}")

            # 如果没有 tool_calls，说明 LLM 认为任务完成或需要回复
            if not tool_calls:
                result = content or "任务完成 (LLM 未返回总结)"
                self.last_streamed_content = result
                return result

            # 执行所有 tool calls
            for tc in tool_calls:
                tool_name = tc["function"]["name"]
                try:
                    arguments = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    arguments = {}

                logger.debug(f"[Agent] 调用工具: {tool_name}({json.dumps(arguments, ensure_ascii=False)[:200]})")

                # 执行工具
                result = self.executor.execute(tool_name, arguments)

                # 检查是否完成
                if result.startswith("__DONE__:"):
                    summary = result[9:]
                    self._messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": summary,
                    })
                    if self._on_tool_call:
                        self._on_tool_call(tool_name, arguments, summary)
                    return summary

                # 工具执行后等待页面稳定
                if tool_name.startswith("browser_") and tool_name != "browser_wait":
                    self.page.wait_for_timeout(self._wait_after_action)

                # 获取操作后的页面状态
                if tool_name.startswith("browser_") or tool_name == "ssh_to_gateway":
                    observation = self.snapshot.get_observation(self.page)
                    result_with_observation = f"{result}\n\n当前页面 URL: {self.page.url}\n\n{observation}"
                else:
                    result_with_observation = result

                logger.debug(f"[Agent] 工具结果: {result[:200]}")

                if self._on_tool_call:
                    self._on_tool_call(tool_name, arguments, result)

                # 保存 tool 结果到对话历史
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_with_observation,
                })

            # 上下文压缩
            self._compress_context()

        return f"达到最大步数 ({self._max_steps})，任务未完成"

    def add_message(self, role: str, content: str):
        """手动添加消息到对话历史"""
        self._messages.append({"role": role, "content": content})

    def reset(self):
        """重置 Agent 状态 (保留配置)"""
        self._messages = []
        self.snapshot.reset()
        if self.executor.capture:
            self.executor.capture.reset()
        self.executor._ssh_connected = False
        self.executor._ws_polling_mode = False
        self.executor.cleanup()  # 清理临时缓存文件

    def _call_llm_stream(self) -> tuple[str | None, list[dict] | None, dict | None]:
        """
        流式调用 LLM API。

        Returns:
            (content, tool_calls, message_dict):
            - content: 完整的思考文本 (已通过 on_thinking_chunk 逐字输出)
            - tool_calls: 完整的 tool_calls 列表 (dict 格式)
            - message_dict: 用于存入 _messages 的 assistant 消息 dict
            三者均为 None 表示调用失败。
        """
        try:
            messages = [
                {"role": "system", "content": self._system_prompt},
                *self._messages,
            ]

            kwargs: dict = {
                "model": self._model,
                "messages": messages,
                "tools": TOOL_DEFINITIONS,
                "tool_choice": "auto",
                "stream": True,
            }
            if self._temperature is not None:
                kwargs["temperature"] = self._temperature

            stream = self._client.chat.completions.create(**kwargs)

            # 累积流式数据
            content_parts: list[str] = []
            reasoning_parts: list[str] = []  # 某些模型 (kimi/deepseek) 返回 reasoning_content
            tool_calls_acc: dict[int, dict] = {}
            has_started_thinking = False
            stream_interrupted = False

            try:
                for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    # 累积 reasoning_content (kimi/deepseek thinking 模式)
                    reasoning_chunk = getattr(delta, "reasoning_content", None)
                    if reasoning_chunk:
                        reasoning_parts.append(reasoning_chunk)

                    # 累积文本内容
                    if delta.content:
                        if not has_started_thinking:
                            has_started_thinking = True
                            # 流式开始前换行
                            if self._on_thinking_chunk:
                                self._on_thinking_chunk("\n")
                        content_parts.append(delta.content)
                        if self._on_thinking_chunk:
                            self._on_thinking_chunk(delta.content)

                    # 累积 tool_calls (分片到达)
                    if delta.tool_calls:
                        for tc_delta in delta.tool_calls:
                            idx = tc_delta.index
                            if idx not in tool_calls_acc:
                                tool_calls_acc[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc_delta.id:
                                tool_calls_acc[idx]["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    tool_calls_acc[idx]["function"]["name"] = tc_delta.function.name
                                if tc_delta.function.arguments:
                                    tool_calls_acc[idx]["function"]["arguments"] += tc_delta.function.arguments
            except Exception as stream_err:
                stream_interrupted = True
                logger.warning(f"[Agent] 流式传输中断: {stream_err}")
                if has_started_thinking and self._on_thinking_chunk:
                    self._on_thinking_chunk("\n[流式传输中断]\n")

            # 流式结束后换行
            if has_started_thinking and self._on_thinking_chunk:
                self._on_thinking_chunk("\n")

            # 组装完整结果
            content = "".join(content_parts) if content_parts else None
            reasoning_content = "".join(reasoning_parts) if reasoning_parts else None
            tool_calls = (
                [tool_calls_acc[i] for i in sorted(tool_calls_acc)]
                if tool_calls_acc else None
            )

            # 构建 message dict (用于存入对话历史)
            # 只在有值时才设置 content，避免 content: None 被部分 API 拒绝
            message_dict: dict = {"role": "assistant"}
            if content:
                message_dict["content"] = content
            if reasoning_content:
                message_dict["reasoning_content"] = reasoning_content
            if tool_calls:
                message_dict["tool_calls"] = tool_calls

            return content, tool_calls, message_dict

        except Exception as e:
            err_msg = str(e).lower()
            # 某些模型不支持自定义 temperature
            if "temperature" in err_msg and self._temperature is not None:
                logger.warning(
                    f"[Agent] 模型不支持 temperature={self._temperature}，去除后重试"
                )
                self._temperature = None
                return self._call_llm_stream()
            # 某些模型不支持 streaming，回退到非流式
            if "stream" in err_msg:
                logger.warning("[Agent] 模型不支持 streaming，回退到非流式调用")
                return self._call_llm_fallback()
            logger.error(f"[Agent] LLM 调用失败: {e}")
            return None, None, None

    def _call_llm_fallback(self) -> tuple[str | None, list[dict] | None, dict | None]:
        """非流式 fallback (当模型不支持 streaming 时使用)"""
        try:
            messages = [
                {"role": "system", "content": self._system_prompt},
                *self._messages,
            ]

            kwargs: dict = {
                "model": self._model,
                "messages": messages,
                "tools": TOOL_DEFINITIONS,
                "tool_choice": "auto",
            }
            if self._temperature is not None:
                kwargs["temperature"] = self._temperature

            response = self._client.chat.completions.create(**kwargs)
            if not response.choices:
                logger.error("[Agent] LLM 返回空 choices")
                return None, None, None

            message = response.choices[0].message

            content = message.content
            if content and self._on_thinking:
                self._on_thinking(content)

            # 手动构建干净的 message_dict，避免 model_dump() 引入多余字段
            # (refusal, audio, function_call, annotations 等会污染消息历史)
            message_dict: dict = {"role": "assistant"}
            if content:
                message_dict["content"] = content
            # 保留 reasoning_content (kimi/deepseek thinking 模式必需)
            reasoning_content = getattr(message, "reasoning_content", None)
            if reasoning_content:
                message_dict["reasoning_content"] = reasoning_content
            tool_calls = None
            if message.tool_calls:
                tool_calls = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ]
                message_dict["tool_calls"] = tool_calls

            return content, tool_calls, message_dict
        except Exception as e:
            logger.error(f"[Agent] LLM 调用失败 (fallback): {e}")
            return None, None, None

    def _compress_context(self):
        """
        上下文压缩: 当消息数量超过阈值时，压缩早期消息。

        策略: 在 turn 边界 (user 消息之前) 切割，保证 assistant + tool 消息组不被拆开。
        OpenAI API 要求每个 role:"tool" 消息必须紧跟在包含对应 tool_call_id 的
        assistant 消息之后，所以不能在 assistant/tool 组中间截断。
        """
        if len(self._messages) <= self._context_max_messages:
            return

        # 找到安全切割点: user 消息的索引 (在这些位置切割不会拆散 assistant+tool 组)
        keep_target = self._context_max_messages // 2
        cut_index = 0

        # 从后往前找，保留至少 keep_target 条消息
        # 切割点必须是 user 消息或第一条消息
        candidate = len(self._messages) - keep_target
        if candidate <= 0:
            return

        # 从 candidate 向前找最近的 user 消息作为切割点
        for i in range(candidate, -1, -1):
            if self._messages[i].get("role") == "user":
                cut_index = i
                break

        if cut_index <= 0:
            return  # 找不到安全切割点

        old_messages = self._messages[:cut_index]
        new_messages = self._messages[cut_index:]

        # 构建摘要
        summary_parts = []
        for msg in old_messages:
            role = msg.get("role", "")
            if role == "user":
                content = msg.get("content", "")
                if "用户指令:" in content:
                    instruction = content.split("用户指令:")[1].split("\n")[0].strip()
                    summary_parts.append(f"用户: {instruction}")
            elif role == "assistant":
                content = msg.get("content", "")
                if content:
                    summary_parts.append(f"助手: {content[:100]}")
                tool_calls = msg.get("tool_calls") or []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        summary_parts.append(f"工具调用: {fn.get('name', '')}")

        if summary_parts:
            summary = "[历史摘要]\n" + "\n".join(summary_parts)
            new_messages.insert(0, {"role": "user", "content": summary})

        self._messages = new_messages
        logger.info(
            f"[Agent] 上下文已压缩: {len(old_messages)} → 1 条摘要, "
            f"保留 {len(new_messages)-1} 条近期消息"
        )
