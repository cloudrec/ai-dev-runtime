import subprocess


class GitBridge:
    def __init__(self, project_path: str):
        self.project_path = project_path

    def status(self):
        return subprocess.check_output(
            ["git", "-C", self.project_path, "status"],
            text=True
        )

    def add_all(self):
        subprocess.run(["git", "-C", self.project_path, "add", "."])

    def commit(self, message: str):
        subprocess.run(["git", "-C", self.project_path, "commit", "-m", message])

    def push(self):
        subprocess.run(["git", "-C", self.project_path, "push"])
