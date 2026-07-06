def parse_command(command: str, context: dict):
    return {
        "raw": command,
        "intent": guess_intent(command)
    }


def guess_intent(command: str):
    c = command.lower()

    if "лог" in c:
        return "add_logging"

    if "api" in c:
        return "create_api"

    return "general"
