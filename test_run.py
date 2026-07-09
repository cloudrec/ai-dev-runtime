from runtime.engine import RuntimeEngine

engine = RuntimeEngine()

result = engine.run("добавь логирование", ".")

print("\n=== RESULT ===\n")
print(result)
