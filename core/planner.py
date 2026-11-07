from typing import Any, Dict, List


class TaskPlanner:
    """Decomposes complex multi-step human instructions into structured execution plans."""

    def build_execution_plan(self, goal: str, required_tools: List[str]) -> Dict[str, Any]:
        steps = []
        for idx, tool_name in enumerate(required_tools, start=1):
            steps.append({
                "step_number": idx,
                "tool": tool_name,
                "state": "در حال بررسی",
            })
        return {
            "goal": goal,
            "total_steps": len(steps),
            "steps": steps,
        }
