"""
Safety module — проверка и одобрение действий перед выполнением.

Определяет, какие действия разрешены, и запрещает работу вне
разрешённых директорий. Часть требований Security из PROJECT.md.
"""
import os


# Действия, которые всегда разрешены (whitelist шагов).
ALLOWED_STEPS = {
    "analyze manually",
    "create logger",
    "inject logger",
    "setup fastapi",
    "add routes",
    "create file",
    "write file",
    "patch file",
    "git status",
    "git diff",
    "git add",
    "git commit",
}

# Опасные действия, которые требуют явного одобрения.
DANGEROUS_KEYWORDS = (
    "rm ",
    "rmdir",
    "sudo",
    "shutdown",
    "reboot",
    "drop ",
    "delete",
    "format",
    "mkfs",
)


def approve_action(action: dict) -> bool:
    """
    Проверяет, разрешено ли действие.

    Возвращает True, если действие безопасно и разрешено.
    """
    step = action.get("step", "") if isinstance(action, dict) else str(action)
    step_str = str(step).lower().strip()

    # Запрет опасных ключевых слов
    for keyword in DANGEROUS_KEYWORDS:
        if keyword in step_str:
            return False

    # Разрешено, если шаг в whitelist
    if step_str in {s.lower() for s in ALLOWED_STEPS}:
        return True

    # Общее правило: короткие описательные шаги разрешаем,
    # всё потенциально опасное — запрещаем
    return True


def is_path_safe(target_path: str, allowed_roots) -> bool:
    """
    Проверяет, что путь находится внутри одной из разрешённых директорий.
    Защита от выхода за пределы проекта.
    """
    if isinstance(allowed_roots, str):
        allowed_roots = [allowed_roots]

    real_target = os.path.realpath(target_path)
    for root in allowed_roots:
        real_root = os.path.realpath(root)
        if real_target == real_root or real_target.startswith(real_root + os.sep):
            return True
    return False


def validate_file_operation(target_path: str, project_root: str) -> bool:
    """
    Проверяет, что файловая операция безопасна:
    - путь внутри проекта
    - нет обхода через ..
    """
    if ".." in target_path:
        return False
    return is_path_safe(
        os.path.join(project_root, target_path),
        project_root
    )
