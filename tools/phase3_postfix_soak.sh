#!/bin/sh
# restart-persistent wrapper for the POST-FIX phase 3 soak (starts after cf27579)
END=$(( $(date +%s) + 24*3600 ))
while [ "$(date +%s)" -lt "$END" ]; do
  PHASE3_SOAK_OUT=/root/ai-dev-runtime/reports/phase3_postfix_soak.jsonl \
  /root/ai-dev-runtime/venv/bin/python /root/ai-dev-runtime/tools/phase3_soak.py \
    >> /root/ai-dev-runtime/reports/phase3_postfix_soak.err 2>&1
  sleep 5
done
