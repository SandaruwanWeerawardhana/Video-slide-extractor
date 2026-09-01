@echo off
setlocal

set "PROJECT_DIR=%~dp0"
set "PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe"
set "INPUT_DIR=%PROJECT_DIR%input"
set "EXTRA_ARGS="

if "%~1"=="" (
    set "VIDEO="
    for %%F in ("%INPUT_DIR%\*.mp4" "%INPUT_DIR%\*.mkv" "%INPUT_DIR%\*.mov" "%INPUT_DIR%\*.avi" "%INPUT_DIR%\*.webm") do (
        if not defined VIDEO if exist "%%~fF" set "VIDEO=%%~fF"
    )
) else (
    set "VIDEO=%~1"
    shift
)

:collect_args
if "%~1"=="" goto after_collect_args
set EXTRA_ARGS=%EXTRA_ARGS% "%~1"
shift
goto collect_args

:after_collect_args

if not exist "%PYTHON%" (
    echo Python virtual environment not found:
    echo   %PYTHON%
    echo.
    echo Run this once:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

if not defined VIDEO (
    echo Usage:
    echo   ss w1.mp4
    echo   ss "C:\path\to\your-video.mp4"
    echo.
    echo Shortest way:
    echo   1. Put your video inside: %INPUT_DIR%
    echo   2. Run: ss
    exit /b 2
)

if /i "%VIDEO:~0,7%"=="http://" goto run_extractor
if /i "%VIDEO:~0,8%"=="https://" goto run_extractor

if not exist "%VIDEO%" if exist "%INPUT_DIR%\%VIDEO%" set "VIDEO=%INPUT_DIR%\%VIDEO%"

if not exist "%VIDEO%" (
    echo Video not found:
    echo   %VIDEO%
    echo.
    echo Put the video in:
    echo   %INPUT_DIR%
    echo Then run:
    echo   ss w1.mp4
    exit /b 1
)

:run_extractor
"%PYTHON%" "%PROJECT_DIR%extract_slides.py" "%VIDEO%" --output "%PROJECT_DIR%slides" %EXTRA_ARGS%
