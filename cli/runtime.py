import sys
from runtime.engine import RuntimeEngine

engine = RuntimeEngine()

if __name__ == "__main__":
    command = sys.argv[1]
    path = sys.argv[2]

    print(engine.run(command, path))
