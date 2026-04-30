#!/bin/bash
# Diagnostic startup wrapper. Mirrors feishu-cc's pattern: prints env
# basics, then runs an import probe so any module-level explosion shows
# up with a real traceback in Railway logs (rather than silently
# dying before uvicorn binds the port).

set -e
exec 2>&1

echo "===== pmo-bot startup ====="
echo "running as: $(id)"
echo "HOME=${HOME}"
echo "PORT=${PORT:-not_set}"
echo "Python: $(python --version)"
echo "Working dir: $(pwd)"
echo "Files: $(ls)"

echo "===== import probe ====="
python -c "
import sys
try:
    print('  importing config...'); import config
    print('  importing db.client...'); from db import client
    print('  importing db.queries...'); from db import queries
    print('  importing agent.tools...'); from agent import tools
    print('  importing agent.runner...'); from agent import runner
    print('  importing feishu.client...'); from feishu import client as fc
    print('  importing feishu.events...'); from feishu import events
    print('  importing app...'); import app
    print('ALL IMPORTS OK')
except Exception as e:
    import traceback
    traceback.print_exc()
    sys.exit(1)
"

echo "===== launching uvicorn on 0.0.0.0:${PORT:-8080} ====="
exec uvicorn app:app --host 0.0.0.0 --port "${PORT:-8080}"
