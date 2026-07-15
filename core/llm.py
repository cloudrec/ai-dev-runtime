import os


class LLM:
    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY")

    def ask(self, prompt: str) -> str:
        return self._mock(prompt)

    def _mock(self, prompt: str):
        return f"[MOCK RESPONSE] {prompt}" 
