import os


class FileWriter:
    def __init__(self, base_path: str):
        self.base_path = base_path

    def write_file(self, path: str, content: str):
        full_path = os.path.join(self.base_path, path)

        os.makedirs(os.path.dirname(full_path), exist_ok=True)

        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "written": True,
            "path": full_path
        }
