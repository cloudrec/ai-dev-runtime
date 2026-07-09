# AI Runtime Agent v0.1

## Цель проекта

Создать постоянно работающий сервис (daemon) на сервере, который принимает безопасные команды, изменяет файлы проекта, выполняет Git-операции и возвращает результат. Сервис — основа AI Dev Runtime для всех текущих и будущих проектов.

---

## Что уже сделано (текущий скелет)

### Структура папок

```
/root/ai-dev-runtime/
├── api/
│   └── main.py              # FastAPI сервер (endpoint /run)
├── cli/
│   └── runtime.py            # CLI запуск: python runtime.py "<cmd>" "<path>"
├── configs/                  # пусто — конфиги не написаны
├── core/
│   ├── executor.py           # исполняет план — баг: 2 одинаковые функции, реальные изменения не делает
│   ├── file_writer.py        # FileWriter — запись файла (базовый)
│   ├── git_bridge.py         # GitBridge — status, add, commit, push
│   ├── llm.py                # LLM — заглушка (мок), не подключён к реальному API
│   ├── parser.py             # Parser — парсит команду, определяет intent
│   ├── patch_engine.py       # PatchEngine — создаёт unified diff, применяет изменения
│   ├── planner.py            # Planner — возвращает шаги по intent
│   └── safety.py             # НЕ СУЩЕСТВУЕТ — импорт из executor.py висит
├── runtime/
│   ├── context_builder.py    # собирает контекст проекта (git status, список файлов)
│   └── engine.py             # RuntimeEngine — оркестратор: parse -> plan -> execute
├── tests/                    # пусто — тестов нет
├── requirements.txt          # fastapi + uvicorn + pydantic + GitPython + pyyaml + rich
├── task.txt                  # оригинальный текстовый файл с требованиями
├── test_run.py               # тестовый запуск
├── PROJECT.md                # этот файл
```

### Что работает сейчас

- Можно запустить CLI: `python cli/runtime.py "добавь логи" /root/ai-dev-runtime`
- Или API: `uvicorn api.main:app` и POST `/run?command=...&project_path=...`
- Executor имитирует изменения (реально не пишет) и делает git commit
- PatchEngine умеет делать diff и записывать файл

### Известные проблемы

1. `core/executor.py` — объявлены **две одинаковые функции** `execute_plan`: первая (реальная) перезаписывается второй (заглушкой safe mode)
2. `core/safety.py` — **отсутствует**, но импортируется в executor.py (`from core.safety import approve_action`)
3. LLM — только мок, нет реального подключения к OpenAI или другому API
4. Нет очереди задач (сейчас синхронный вызов)
5. Нет Backup Engine
6. Нет Dockerfile
7. Нет тестов
8. Нет Swagger/OpenAPI документации
9. Нет security (API Key, HMAC, whitelist директорий)
10. configs/ — пусто, нет .env

---

## Что нужно сделать (полный список требований)

### 1. Daemon
- systemd unit
- автозапуск после перезагрузки

### 2. REST API
- POST /task — создать задачу
- GET /status/{task_id} — статус задачи
- GET /health — здоровье сервиса
- GET /logs — логи
- POST /shutdown — остановка

### 3. Очередь задач (Task Queue)
- Каждая задача: UUID, время создания, статус, лог, результат, ошибки
- Асинхронное выполнение

### 4. File Engine
- Создать файл
- Заменить файл полностью
- Заменить функцию/блок кода
- Вставить код в указанное место
- Удалить файл
- Создать директорию
- Все операции ТОЛЬКО внутри разрешённых директорий

### 5. Git Engine
- status, diff, add, commit, branch, checkout, push, pull
- Автоматическое сообщение коммита

### 6. Backup Engine
- backup перед каждым изменением
- история изменений
- rollback

### 7. Patch Engine (уже начат)
- unified diff
- поиск функции
- замена блока
- проверка результата

### 8. Проверка после изменений
- запуск formatter
- запуск тестов
- проверка импортов
- commit только после успешной проверки

### 9. Security
- API Key
- HMAC подпись
- whitelist директорий
- запрет на работу вне проекта
- журнал всех действий

### 10. Плагины (архитектура)
- plugins/ — GitHub, Docker, Python, WordPress, Telegram, PostgreSQL

### 11. Конфигурация
- .env: пути, Git, API, модели AI, лимиты

### 12. Логи
- Структурированные JSON-логи

### 13. Docker
- Запуск одной командой

### 14. Документация
- Swagger/OpenAPI (авто)

---

## Приоритет на сейчас (немедленные исправления)

1. **Удалить дубль `execute_plan`** в `core/executor.py` и сделать рабочий executor
2. **Создать `core/safety.py`** — функция approve_action
3. **Починить импорт** — проверить что всё импортируется без ошибок
4. **Запустить и проверить** — `python cli/runtime.py "test" /root/ai-dev-runtime`
5. **Добавить очередь задач** — `core/task_queue.py`
6. **Добавить Backup Engine** — `core/backup_engine.py`

---

## Как запустить сейчас (для проверки)

```bash
cd /root/ai-dev-runtime
python cli/runtime.py "добавь api" /root/ai-dev-runtime
# Или через API:
pip install -r requirements.txt
uvicorn api.main:app --reload
# POST /run?command=add%20api&project_path=/root/ai-dev-runtime
```
