# Deployed host artifacts

These files mirror what's actually deployed on the host, so changes go through
git instead of ad-hoc edits to the live files. Deploy paths:

- `ai_task_watcher.py` -> `/usr/local/bin/ai_task_watcher.py`
- `ai-task-watcher.service` -> `/etc/systemd/system/ai-task-watcher.service`
- `ai-task-watcher.timer` -> `/etc/systemd/system/ai-task-watcher.timer`
- `ai-runtime.service` -> `/etc/systemd/system/ai-runtime.service`

After editing here, copy to the deploy path and run:
`systemctl daemon-reload && systemctl restart <unit>`
