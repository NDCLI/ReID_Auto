@echo off
chcp 65001 >nul
title Cai dat ReID Auto Draw OSNet
echo ===================================================
echo   CHUONG TRINH CAI DAT REID AUTO DRAW OSNET
echo ===================================================
echo.

:: Kiem tra Python
python --version >nul 2>&1
IF %ERRORLEVEL% EQU 0 GOTO PYTHON_FOUND

echo [CANH BAO] Khong tim thay Python!
echo Dang tu dong tai va cai dat Python tu Microsoft Store. Co the mat vai phut.
echo Xin vui long cho doi...

:: Tu dong cai dat bang winget
winget install --id 9NRWMVN37LXX --exact --source msstore --accept-package-agreements --accept-source-agreements

echo.
echo Kiem tra lai sau khi cai dat...
python --version >nul 2>&1
IF %ERRORLEVEL% EQU 0 GOTO PYTHON_FOUND

echo [LOI] Khong the cai dat Python tu dong bang winget!
echo Vui long mo Windows Store va tim "Python 3.11" de cai dat thu cong.
pause
exit /b

:PYTHON_FOUND
echo [OK] Da tim thay Python:
python --version
echo.

:: Kiem tra cac thu vien da cai dat chua
echo Dang kiem tra cac thu vien...
python -c "import cv2, numpy, PIL, win32api, openvino, customtkinter, pystray" >nul 2>&1
IF %ERRORLEVEL% EQU 0 GOTO LIBS_FOUND

:: Cap nhat pip
echo Dang cap nhat cong cu pip...
python -m pip install --upgrade pip >nul 2>&1

:: Cai dat thu vien
echo Dang cai dat cac thu vien can thiet tu requirements.txt...
python -m pip install -r requirements.txt

IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo [LOI] Co loi xay ra trong qua trinh cai dat thu vien!
    echo Hay kiem tra lai ket noi mang.
    pause
    exit /b
)

:LIBS_FOUND
echo [OK] Toan bo thu vien deu da san sang!

:: Tai model ReID chinh xac cao vao cache nguoi dung (chi tai mot lan)
set "REID_MODEL_CACHE=%LOCALAPPDATA%\ReIDAutoOSNet\models"
if not exist "%REID_MODEL_CACHE%" mkdir "%REID_MODEL_CACHE%"
if not exist "%REID_MODEL_CACHE%\reid_0277.xml" (
    echo Dang tai model ReID 0277 chinh xac cao...
    curl.exe --fail --location --retry 3 "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/person-reidentification-retail-0277/FP16/person-reidentification-retail-0277.xml" -o "%REID_MODEL_CACHE%\reid_0277.xml.download"
    if errorlevel 1 goto MODEL_DOWNLOAD_ERROR
    move /Y "%REID_MODEL_CACHE%\reid_0277.xml.download" "%REID_MODEL_CACHE%\reid_0277.xml" >nul
)
if not exist "%REID_MODEL_CACHE%\reid_0277.bin" (
    curl.exe --fail --location --retry 3 "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/person-reidentification-retail-0277/FP16/person-reidentification-retail-0277.bin" -o "%REID_MODEL_CACHE%\reid_0277.bin.download"
    if errorlevel 1 goto MODEL_DOWNLOAD_ERROR
    move /Y "%REID_MODEL_CACHE%\reid_0277.bin.download" "%REID_MODEL_CACHE%\reid_0277.bin" >nul
)
if not exist "%REID_MODEL_CACHE%\reid_0286.xml" (
    echo Dang tai model ReID 0286...
    curl.exe --fail --location --retry 3 "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/person-reidentification-retail-0286/FP16/person-reidentification-retail-0286.xml" -o "%REID_MODEL_CACHE%\reid_0286.xml.download"
    if errorlevel 1 goto MODEL_DOWNLOAD_ERROR
    move /Y "%REID_MODEL_CACHE%\reid_0286.xml.download" "%REID_MODEL_CACHE%\reid_0286.xml" >nul
)
if not exist "%REID_MODEL_CACHE%\reid_0286.bin" (
    curl.exe --fail --location --retry 3 "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/person-reidentification-retail-0286/FP16/person-reidentification-retail-0286.bin" -o "%REID_MODEL_CACHE%\reid_0286.bin.download"
    if errorlevel 1 goto MODEL_DOWNLOAD_ERROR
    move /Y "%REID_MODEL_CACHE%\reid_0286.bin.download" "%REID_MODEL_CACHE%\reid_0286.bin" >nul
)
if not exist "%REID_MODEL_CACHE%\face-detection-retail-0005.xml" (
    echo Dang tai model phat hien khuon mat...
    curl.exe --fail --location --retry 3 "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/face-detection-retail-0005/FP16/face-detection-retail-0005.xml" -o "%REID_MODEL_CACHE%\face-detection-retail-0005.xml.download"
    if errorlevel 1 goto MODEL_DOWNLOAD_ERROR
    move /Y "%REID_MODEL_CACHE%\face-detection-retail-0005.xml.download" "%REID_MODEL_CACHE%\face-detection-retail-0005.xml" >nul
)
if not exist "%REID_MODEL_CACHE%\face-detection-retail-0005.bin" (
    curl.exe --fail --location --retry 3 "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/face-detection-retail-0005/FP16/face-detection-retail-0005.bin" -o "%REID_MODEL_CACHE%\face-detection-retail-0005.bin.download"
    if errorlevel 1 goto MODEL_DOWNLOAD_ERROR
    move /Y "%REID_MODEL_CACHE%\face-detection-retail-0005.bin.download" "%REID_MODEL_CACHE%\face-detection-retail-0005.bin" >nul
)
if not exist "%REID_MODEL_CACHE%\face-reidentification-retail-0095.xml" (
    echo Dang tai model nhan dang khuon mat...
    curl.exe --fail --location --retry 3 "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/face-reidentification-retail-0095/FP16/face-reidentification-retail-0095.xml" -o "%REID_MODEL_CACHE%\face-reidentification-retail-0095.xml.download"
    if errorlevel 1 goto MODEL_DOWNLOAD_ERROR
    move /Y "%REID_MODEL_CACHE%\face-reidentification-retail-0095.xml.download" "%REID_MODEL_CACHE%\face-reidentification-retail-0095.xml" >nul
)
if not exist "%REID_MODEL_CACHE%\face-reidentification-retail-0095.bin" (
    curl.exe --fail --location --retry 3 "https://storage.openvinotoolkit.org/repositories/open_model_zoo/2023.0/models_bin/1/face-reidentification-retail-0095/FP16/face-reidentification-retail-0095.bin" -o "%REID_MODEL_CACHE%\face-reidentification-retail-0095.bin.download"
    if errorlevel 1 goto MODEL_DOWNLOAD_ERROR
    move /Y "%REID_MODEL_CACHE%\face-reidentification-retail-0095.bin.download" "%REID_MODEL_CACHE%\face-reidentification-retail-0095.bin" >nul
)

:: Tao shortcut ngoai Desktop, dung icon cua app va chay an cua so terminal
echo Dang tao shortcut RE-ID Auto Draw OSNet ngoai Desktop...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1"
if errorlevel 1 (
    echo [CANH BAO] Khong tao duoc shortcut. Ban van co the chay Run.bat.
) else (
    echo [OK] Da tao shortcut RE-ID Auto Draw OSNet ngoai Desktop.
)

echo.
echo ===================================================
echo [THANH CONG] Da cai dat xong toan bo moi thu!
echo Ban co the mo shortcut 'RE-ID Auto Draw OSNet' ngoai Desktop de su dung.
echo ===================================================
pause
exit /b 0

:MODEL_DOWNLOAD_ERROR
echo.
echo [LOI] Khong the tai model AI. Kiem tra ket noi mang roi chay lai install.bat.
del /Q "%REID_MODEL_CACHE%\*.download" >nul 2>&1
pause
exit /b 1
