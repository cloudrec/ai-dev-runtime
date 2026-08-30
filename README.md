# AI Runtime Agent v0.5

Постоянно работающий сервис (daemon) на сервере. Принимает безопасные команды,
изменяет файлы проекта, выполняет Git-операции, возвращает результат.

## Возможности

- ✅ **REST API** — `/task`, `/status/{id}`, `/tasks`, `/health`, `/logs`, `/shutdown`
- ✅ **Очередь задач** — асинхронное выполнение, UUID, статусы, логи, результаты
- ✅ **Backup Engine** — snapshot перед изменениями, история, rollback
- ✅ **Patch Engine** — unified diff, замена блоков кода
- ✅ **Git Engine** — status, add, commit, push
- ✅ **Safety** — whitelist директорий, блок опасных действий
- ✅ **CLI** — `python cli/runtime.py "<команда>" <путь>`
- ✅ **systemd daemon** — автозапуск после перезагрузки
- ✅ **Swagger/OpenAPI** — авто-документация на `/docs`

## Быстрый старт

### CLI
```bash
cd /root/ai-dev-runtime
source venv/bin/activate
python cli/runtime.py "добавь логирование" /root/ai-dev-runtime
```

### API (через systemd-сервис)
Сервис уже запущен на `127.0.0.1:8199`.

```bash
# создать задачу
curl -X POST http://127.0.0.1:8199/task \
  -H "Content-Type: application/json" \
  -d '{"command":"добавь логирование","project_path":"/root/ai-dev-runtime"}'

# проверить статус (подставить task_id)
curl http://127.0.0.1:8199/status/<task_id>

# здоровье
curl http://127.0.0.1:8199/health

# документация (в браузере)
# http://127.0.0.1:8199/docs
```

## Управление сервисом

```bash
systemctl status ai-runtime     # статус
systemctl restart ai-runtime    # перезапуск
systemctl stop ai-runtime       # остановка
journalctl -u ai-runtime -f     # логи в реальном времени
```

## Owner OS: новый чат ChatGPT?

Стучалка (wake bridge) не хранит ссылку в коде, `.env` или systemd — только в
`control_plane.db` → `wake_target`. Одна команда, без рестарта:

```bash
tools/rebind_chat.py https://chatgpt.com/c/<conversation-id>
tools/rebind_chat.py --show      # что стучалка разбудит прямо сейчас
```

Полная процедура, backup/аудит, smoke test и список «чего НЕ делать» —
[docs/OWNER_OS_CHAT_REBIND.md](docs/OWNER_OS_CHAT_REBIND.md).

## Структура

```
/root/ai-dev-runtime/
├── api/main.py              # FastAPI сервер
├── cli/runtime.py           # CLI запуск
├── configs/.env             # конфигурация
├── core/
│   ├── backup_engine.py     # Backup Engine (snapshot, rollback)
│   ├── executor.py          # исполняет план
│   ├── file_writer.py       # запись файлов
│   ├── git_bridge.py        # Git-операции
│   ├── llm.py               # LLM (мок)
│   ├── logger.py            # логгер (создаётся командой)
│   ├── parser.py            # парсинг команд
│   ├── patch_engine.py      # unified diff
│   ├── planner.py           # планирование шагов
│   ├── safety.py            # проверки безопасности
│   └── task_queue.py        # асинхронная очередь задач
├── runtime/
│   ├── context_builder.py   # контекст проекта
│   └── engine.py            # оркестратор
├── deploy/ai-runtime.service  # systemd unit
└── tests/                   # (плагины/тесты)
```

## Backup / Rollback

```python
from core.backup_engine import BackupEngine
be = BackupEngine("/root/ai-dev-runtime")
be.snapshot(reason="before changes")   # создать бэкап
be.list_backups()                       # история
be.rollback("backup_20260709_113047")  # откат
```

## Что ещё не сделано (roadmap)

- [ ] Реальное подключение к LLM (OpenAI/и т.д.)
- [ ] Проверка после изменений (formatter, тесты, импорты)
- [ ] Security: API Key, HMAC подпись запросов
- [ ] Плагины (GitHub, Docker, Python, WordPress, Telegram, PostgreSQL)
- [ ] Dockerfile (запуск одной командой)
- [ ] Тесты (tests/)
