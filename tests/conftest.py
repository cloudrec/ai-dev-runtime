"""Конфиг pytest — общие фикстуры."""
import sys
import os

# корень проекта доступен для импортов
sys.path.insert(0, "/root/ai-dev-runtime")
os.environ.setdefault("PYTHONPATH", "/root/ai-dev-runtime")
