@echo off
cd /d "%~dp0.."
if not exist app\frontend\dist\index.html (
  pushd app\frontend
  call npm install --no-fund --no-audit
  call npm run build
  popd
)
start "" http://127.0.0.1:8765/
.venv\Scripts\python -m app.backend.server
