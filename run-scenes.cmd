@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -m app.backend.redis_tunnel
if errorlevel 1 (echo Redis tunnel unavailable & pause & exit /b 1)
"%~dp0.venv\Scripts\python.exe" -u -m maritime_tip_cue.local_runner %*
set "scene_result=%errorlevel%"
echo.
pause
exit /b %scene_result%
