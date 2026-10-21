@echo off
cd /d "%~dp0"
"%~dp0.venv\Scripts\python.exe" -u -m maritime_tip_cue.local_runner %*
set "scene_result=%errorlevel%"
echo.
pause
exit /b %scene_result%
