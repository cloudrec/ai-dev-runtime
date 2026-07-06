import subprocess
import os


def build_context(project_path: str):
    try:
        git_status = subprocess.check_output(
            ["git", "-C", project_path, "status"],
            text=True
        )
    except:
        git_status = "no git repo"

    files = []
    for root, _, filenames in os.walk(project_path):
        for f in filenames:
            files.append(os.path.join(root, f))

    return {
        "project_path": project_path,
        "git_status": git_status,
        "files": files[:200]
    }
