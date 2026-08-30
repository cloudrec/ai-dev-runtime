#!/bin/sh
# restart-persistent wrapper for the phase 3 soak
END=$(( $(date +%s) + 24*3600 ))
while [ "$(date +%s)" -lt "$END" ]; do
  /root/ai-dev-runtime/venv/bin/python /root/ai-dev-runtime/tools/phase3_soak.py >> /root/ai-dev-runtime/reports/phase3_soak.err 2>&1
  sleep 5
done
