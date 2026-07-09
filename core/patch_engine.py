import os
from difflib import unified_diff


class PatchEngine:
    def __init__(self, base_path: str):
        self.base_path = base_path

    def apply_patch(self, file_path: str, new_content: str):
        full_path = os.path.join(self.base_path, file_path)

        old_content = ""
        if os.path.exists(full_path):
            with open(full_path, "r") as f:
                old_content = f.read()

        diff = list(unified_diff(
            old_content.splitlines(),
            new_content.splitlines(),
            fromfile="old",
            tofile="new"
        ))

        with open(full_path, "w") as f:
            f.write(new_content)

        return {
            "file": full_path,
            "changed": True,
            "diff": "\n".join(diff)
        }
