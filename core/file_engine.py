"""
File Engine — все файловые операции с проверкой безопасности.

Операции:
  - create_file       создать файл
  - replace_file      заменить файл полностью
  - replace_block     заменить функцию/блок кода (по сигнатуре/маркерам)
  - insert_at         вставить код в указанное место (после строки с маркером)
  - delete_file       удалить файл
  - create_dir        создать директорию

Все операции ТОЛЬКО внутри разрешённой директории (project_root).
Попытка выйти за пределы проекта → отказ.
"""
import os
from core.safety import validate_file_operation


class FileEngine:
    def __init__(self, project_root: str):
        self.project_root = os.path.realpath(project_root)

    # ---- проверка ----

    def _safe(self, rel_path: str) -> str:
        """Проверяет путь и возвращает абсолютный путь, иначе кидает ValueError."""
        if not validate_file_operation(rel_path, self.project_root):
            raise ValueError(f"небезопасный путь (вне проекта): {rel_path}")
        return os.path.join(self.project_root, rel_path)

    # ---- операции ----

    def create_file(self, rel_path: str, content: str = "") -> dict:
        full = self._safe(rel_path)
        if os.path.exists(full):
            raise FileExistsError(f"файл уже существует: {rel_path}")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return {"action": "create_file", "path": rel_path, "ok": True}

    def replace_file(self, rel_path: str, content: str) -> dict:
        full = self._safe(rel_path)
        if not os.path.exists(full):
            raise FileNotFoundError(f"файл не найден: {rel_path}")
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        return {"action": "replace_file", "path": rel_path, "ok": True}

    def replace_block(self, rel_path: str, start_marker: str, end_marker: str,
                      new_block: str) -> dict:
        """
        Заменяет блок между start_marker и end_marker (включительно).
        Маркеры — строки, которые должны присутствовать в файле.
        """
        full = self._safe(rel_path)
        with open(full, "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        i = 0
        replaced = False
        while i < len(lines):
            if start_marker in lines[i] and not replaced:
                # пропускаем старый блок до end_marker
                new_lines.append(lines[i])  # оставляем start-маркер
                i += 1
                while i < len(lines) and end_marker not in lines[i]:
                    i += 1
                # вставляем новый блок
                if not new_block.endswith("\n"):
                    new_block += "\n"
                new_lines.append(new_block)
                if i < len(lines):
                    new_lines.append(lines[i])  # оставляем end-маркер
                    i += 1
                replaced = True
            else:
                new_lines.append(lines[i])
                i += 1

        if not replaced:
            raise ValueError(f"маркеры не найдены: {start_marker} ... {end_marker}")

        with open(full, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
        return {"action": "replace_block", "path": rel_path, "ok": True, "replaced": True}

    def insert_at(self, rel_path: str, after_marker: str, code: str) -> dict:
        """Вставляет код сразу после строки, содержащей after_marker."""
        full = self._safe(rel_path)
        with open(full, "r", encoding="utf-8") as f:
            lines = f.readlines()

        inserted = False
        for i, line in enumerate(lines):
            if after_marker in line:
                if not code.endswith("\n"):
                    code += "\n"
                lines.insert(i + 1, code)
                inserted = True
                break

        if not inserted:
            raise ValueError(f"маркер не найден: {after_marker}")

        with open(full, "w", encoding="utf-8") as f:
            f.writelines(lines)
        return {"action": "insert_at", "path": rel_path, "ok": True, "after": after_marker}

    def delete_file(self, rel_path: str) -> dict:
        full = self._safe(rel_path)
        if not os.path.exists(full):
            raise FileNotFoundError(f"файл не найден: {rel_path}")
        if os.path.isdir(full):
            raise IsADirectoryError(f"это директория, используйте create_dir/delete_dir: {rel_path}")
        os.remove(full)
        return {"action": "delete_file", "path": rel_path, "ok": True}

    def create_dir(self, rel_path: str) -> dict:
        full = self._safe(rel_path)
        os.makedirs(full, exist_ok=True)
        return {"action": "create_dir", "path": rel_path, "ok": True}
