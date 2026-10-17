@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Career Radar is not installed yet. Follow the Install steps in README.md first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" "%~dp0start_dashboard.pyw"
