def plan_task(parsed, context):
    intent = parsed["intent"]

    if intent == "add_logging":
        return {"steps": ["create logger", "inject logger"]}

    if intent == "create_api":
        return {"steps": ["setup fastapi", "add routes"]}

    return {"steps": ["analyze manually"]}
