FROM python:3.12-slim

WORKDIR /app

# системные зависимости (git нужен для GitBridge)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# код проекта
COPY . .

# порт API
EXPOSE 8199

# запуск через uvicorn
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8199"]
