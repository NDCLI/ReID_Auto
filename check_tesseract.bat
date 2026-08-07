@echo off
echo ====================================
echo   Kiem tra cai dat Tesseract OCR
echo ====================================
echo.

echo [1/3] Kiem tra Tesseract da duoc cai dat...
where tesseract >nul 2>&1
if %errorlevel% neq 0 (
    echo [X] Tesseract chua duoc cai dat hoac chua them vao PATH
    echo.
    echo Vui long:
    echo 1. Cai dat Tesseract tu: https://github.com/UB-Mannheim/tesseract/wiki
    echo 2. Chon file: tesseract-ocr-w64-setup-5.x.x.exe
    echo 3. Cai dat vao: C:\Program Files\Tesseract-OCR
    echo 4. Them vao PATH: C:\Program Files\Tesseract-OCR
    echo 5. Khoi dong lai terminal va chay lai script nay
    echo.
    pause
    exit /b 1
)

echo [OK] Tesseract da duoc cai dat!
echo.

echo [2/3] Phien ban Tesseract:
tesseract --version
echo.

echo [3/3] Kiem tra thu viec doc chu tren anh...
echo Dang tao anh test...

REM Create a simple test using Python
.venv\Scripts\python -c "import cv2; import numpy as np; img = np.ones((200, 600, 3), dtype=np.uint8) * 255; cv2.putText(img, 'TIME: 14:30:25', (50, 100), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 0, 0), 3); cv2.imwrite('test_ocr_image.png', img); print('Da tao anh test: test_ocr_image.png')"

echo.
echo Dang chay OCR test...
.venv\Scripts\python test_ocr.py

echo.
echo ====================================
echo   Hoan tat kiem tra!
echo ====================================
echo.
echo Neu tat ca OK, ban co the bat tinh nang OCR trong config.py:
echo   ENABLE_OCR_TIMESTAMP_FILTER = True
echo.
pause
