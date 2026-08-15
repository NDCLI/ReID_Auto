@echo off
set "APP_DIR=%~dp0"
if exist "%APP_DIR%.venv\Scripts\pythonw.exe" (
    start "AutoMarker Re-ID" /D "%APP_DIR%" "%APP_DIR%.venv\Scripts\pythonw.exe" "%APP_DIR%app_gui.pyw"
) else (
    start "AutoMarker Re-ID" /D "%APP_DIR%" pythonw "%APP_DIR%app_gui.pyw"
)
