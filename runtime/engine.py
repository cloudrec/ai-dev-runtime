from core.parser import parse_command
from core.planner import plan_task
from core.executor import execute_plan
from runtime.context_builder import build_context


class RuntimeEngine:
    def run(self, command: str, project_path: str):
        context = build_context(project_path)

        parsed = parse_command(command, context)
        plan = plan_task(parsed, context)
        result = execute_plan(plan, context)

        return {
            "parsed": parsed,
            "plan": plan,
            "result": result
        }
