import json
from typing import Any, Dict, List, Optional
from utils.logger import get_logger
from ai.client import AIClient
from core.tools import ToolRegistry
from core.planner import TaskPlanner
from core.executor import ToolExecutor
from core.permissions import PermissionManager, Role
from memory.conversation import ConversationMemory
from memory.long_term import LongTermMemory
from ai.prompts import build_system_prompt


logger = get_logger("core.agent")


class AutonomousAgent:
    """
    Core Autonomous AI Agent with multi-step reasoning, OpenAI-compatible
    Function Calling, dynamic tool selection, error recovery, and RBAC enforcement.
    """

    def __init__(
        self,
        ai_client: AIClient,
        tool_registry: ToolRegistry,
        conv_memory: ConversationMemory,
        lt_memory: LongTermMemory,
    ) -> None:
        self.ai_client = ai_client
        self.tool_registry = tool_registry
        self.conv_memory = conv_memory
        self.lt_memory = lt_memory
        self.planner = TaskPlanner()
        self.executor = ToolExecutor(tool_registry)
        self.permissions = PermissionManager()
        self.is_paused: bool = False

    async def process_command(
        self,
        user_id: int,
        user_message: str,
        channel: str = "telegram",
        on_step: Any = None,
    ) -> Dict[str, Any]:
        """
        Analyze user intent, plan multi-step execution, run permitted tools
        sequentially, verify outputs, and formulate final response.
        on_step(step_idx, tool_name, phase) is an optional progress callback
        (phase "start"/"done") so the chat can show what the agent is doing
        right now instead of one frozen "thinking" line during long tools.
        """
        role = self.permissions.get_user_role(user_id)
        logger.info(f"Agent processing request from user_id={user_id} (role={role.value})")

        # Load long-term page style and admin preferences
        page_style = await self.lt_memory.get_preference("page_tone_style", "حرفه‌ای، صمیمی، جذاب و دقیق")
        active_account = await self.lt_memory.get_preference("active_ig_account", "default")

        # Retrieve conversation context
        history = await self.conv_memory.get_recent_messages(str(user_id), limit=12)
        await self.conv_memory.add_message(str(user_id), "user", user_message, channel=channel)

        # Filter tools strictly by user role (Server-Side RBAC)
        available_schemas = self.tool_registry.get_openai_tool_schemas(role=role)

        system_prompt = build_system_prompt(
            role=role,
            page_style=page_style,
            active_account=active_account,
            is_paused=self.is_paused,
        )

        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for item in history:
            messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": user_message})

        execution_trace: List[Dict[str, Any]] = []
        max_steps = 8

        for step_idx in range(1, max_steps + 1):
            ai_response = await self.ai_client.chat_completion(
                messages=messages,
                tools=available_schemas if available_schemas else None,
            )

            if "error" in ai_response:
                logger.error(f"AI API failure at step {step_idx}: {ai_response['error']}")
                if ai_response["error"] == "content_filter":
                    return {
                        "status": "ناموفق",
                        "reply": "این موضوع را نمی‌توانم بررسی کنم، چون مدل به‌خاطر فیلتر محتوا جواب نمی‌دهد. سوالت را جور دیگری بپرس.",
                        "trace": execution_trace,
                    }
                if ai_response["error"] == "tools_validation":
                    return {
                        "status": "ناموفق",
                        "reply": "لیست ابزارها مشکل دارد؛ افزونه‌ای که آخر اضافه شد اسکیمای درستی ندارد. افزونه را با اسکیمای درست دوباره بفرست.",
                        "trace": execution_trace,
                    }
                return {
                    "status": "ناموفق",
                    "reply": f"⚠️ خطا در ارتباط با سرویس هوش مصنوعی: {ai_response['error']}",
                    "trace": execution_trace,
                }

            assistant_msg = ai_response["message"]
            messages.append(assistant_msg)

            tool_calls = assistant_msg.get("tool_calls") or []
            if not tool_calls:
                final_text = assistant_msg.get("content") or "عملیات با موفقیت انجام شد."
                await self.conv_memory.add_message(str(user_id), "assistant", final_text, channel=channel)
                return {
                    "status": "موفق",
                    "reply": final_text,
                    "trace": execution_trace,
                }

            # Execute each requested tool call with server-side RBAC check
            for tc in tool_calls:
                fn_name = tc["function"]["name"]
                raw_args = tc["function"].get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    parsed_args = {}

                if not self.permissions.can_execute_tool(user_id, fn_name):
                    denied_res = {
                        "status": "permission_denied",
                        "error": f"عدم دسترسی: کاربر عادی مجاز به اجرای ابزار مدیریتی '{fn_name}' نیست.",
                    }
                    execution_trace.append({"step": step_idx, "tool": fn_name, "status": "denied"})
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": fn_name,
                        "content": json.dumps(denied_res, ensure_ascii=False),
                    })
                    continue

                # Tell the chat which tool is about to run (long tools like
                # a download otherwise look frozen for minutes).
                if on_step is not None:
                    try:
                        await on_step(step_idx, fn_name, "start")
                    except Exception:
                        pass

                step_result = await self.executor.execute(fn_name, parsed_args)

                if on_step is not None:
                    try:
                        await on_step(step_idx, fn_name, step_result.get("status", "done"))
                    except Exception:
                        pass
                execution_trace.append({
                    "step": step_idx,
                    "tool": fn_name,
                    "args": parsed_args,
                    "status": step_result.get("status", "completed"),
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": fn_name,
                    "content": json.dumps(step_result, ensure_ascii=False),
                })

        # The model used all its steps without a final message. Never answer
        # with a robotic line — report what the tools actually did, using the
        # last tool result's own message (e.g. "فایل برای شما ارسال شد.").
        fallback_reply = "کارها انجام شد ولی جواب نهایی آماده نشد؛ دوباره بگو چه کار کنم."
        for m in reversed(messages):
            if m.get("role") == "tool":
                try:
                    res = json.loads(m.get("content") or "{}")
                    fallback_reply = res.get("message") or res.get("error") or fallback_reply
                except Exception:
                    pass
                break
        await self.conv_memory.add_message(str(user_id), "assistant", fallback_reply, channel=channel)
        return {
            "status": "موفق",
            "reply": fallback_reply,
            "trace": execution_trace,
        }
