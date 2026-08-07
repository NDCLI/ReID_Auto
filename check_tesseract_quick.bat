@echo off
echo Checking Tesseract...
where tesseract
if %errorlevel% equ 0 (
    echo.
    echo [OK] Tesseract found!
    tesseract --version
) else (
    echo [X] Tesseract not found in PATH
    echo Please restart terminal after adding to PATH
)
pause
