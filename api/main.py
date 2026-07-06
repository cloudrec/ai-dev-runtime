from fastapi import FastAPI
from runtime.engine import RuntimeEngine

app = FastAPI()
engine = RuntimeEngine()


@app.post("/run")
def run(command: str, project_path: str):
    return engine.run(command, project_path)
