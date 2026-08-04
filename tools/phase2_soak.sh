#!/bin/sh
# Restart-persistent wrapper: if the recorder dies, restart it until the window ends.
END=$(( $(date +%s) + 24*3600 ))
while [ "$(date +%s)" -lt "$END" ]; do
  /root/ai-dev-runtime/venv/bin/python /root/ai-dev-runtime/tools/phase2_soak.py >> /root/ai-dev-runtime/reports/phase2_soak.err 2>&1
  sleep 5
done
