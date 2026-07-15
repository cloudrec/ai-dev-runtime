"""
CLI запуск AI Runtime Agent.

Использование:
    python cli/runtime.py "<команда>" "<путь_к_проекту>"
"""
import os
import sys

# Добавляем корень проекта в sys.path, чтобы импорты core/runtime работали
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from runtime.engine import RuntimeEngine


def main():
    if len(sys.argv) < 3:
        print("Использование: python cli/runtime.py \"<команда>\" <путь_к_проекту>")
        sys.exit(1)

    command = sys.argv[1]
    project_path = sys.argv[2]

    engine = RuntimeEngine()
    result = engine.run(command, project_path)

    from rich import print as rprint
    rprint(result)


if __name__ == "__main__":
    main()
