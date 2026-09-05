@echo off
setlocal

rem Browser/H5P slide capture in one word.
rem
rem   bs <url>                 capture that URL into the default folder
rem   bs <url> w5              capture into <BASE_OUTPUT_DIR>\w5
rem   bs <url> w5 png          same, as PNG instead of JPG
rem   bs <url> --headless      any extra flags are passed straight through
rem   bs                       re-use the URL saved in session.cmd

set "PROJECT_DIR=%~dp0"
set "PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe"
set "CAPTURE=%PROJECT_DIR%capture_browser_video.py"

rem Where lesson folders are created when a short name is given.
set "BASE_OUTPUT_DIR=browser-slides"

rem Defaults tuned for lecture slides at speed.
set "IMAGE_FORMAT=jpg"
set "RATE=5"
set "SAMPLE_RATE=3"
set "STABILITY=0.5"
set "MIN_SLIDE=1"

rem session.cmd holds the login cookie and is gitignored. It should contain:
rem     set "MOODLE_SESSION=your_cookie_here"
rem     set "VIDEO_URL=https://online.codl.lk/..."   (optional)
if exist "%PROJECT_DIR%session.cmd" call "%PROJECT_DIR%session.cmd"

if not exist "%PYTHON%" (
    echo Python virtual environment not found:
    echo   %PYTHON%
    echo.
    echo Run this once:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    exit /b 1
)

rem --- first argument: the URL, or fall back to VIDEO_URL from session.cmd ---
set "URL=%~1"
if defined URL shift
if not defined URL set "URL=%VIDEO_URL%"

if not defined URL (
    echo Usage:
    echo   bs "https://online.codl.lk/mod/hvp/embed.php?id=NNNN"
    echo   bs "https://online.codl.lk/...mp4" w5
    echo   bs "https://online.codl.lk/...mp4" w5 png --headless
    echo.
    echo Save the cookie once in session.cmd so you never retype it:
    echo   set "MOODLE_SESSION=your_cookie_here"
    exit /b 2
)

rem --- optional positional: output folder name, then image format ---
set "OUTPUT_DIR="
set "EXTRA_ARGS="

:collect
if "%~1"=="" goto run
if /i "%~1"=="png" set "IMAGE_FORMAT=png" & shift & goto collect
if /i "%~1"=="jpg" set "IMAGE_FORMAT=jpg" & shift & goto collect
if /i "%~1"=="jpeg" set "IMAGE_FORMAT=jpg" & shift & goto collect
set "ARG=%~1"
if "%ARG:~0,1%"=="-" goto extras
if defined OUTPUT_DIR goto extras
rem A bare word is a lesson folder; a full path is used as-is.
if "%ARG:~1,1%"==":" (set "OUTPUT_DIR=%ARG%") else (set "OUTPUT_DIR=%BASE_OUTPUT_DIR%\%ARG%")
shift
goto collect

:extras
if "%~1"=="" goto run
set EXTRA_ARGS=%EXTRA_ARGS% "%~1"
shift
goto extras

:run
if not defined MOODLE_SESSION (
    echo Warning: MOODLE_SESSION is not set. A protected lesson will fail.
    echo          Put this line in session.cmd:
    echo            set "MOODLE_SESSION=your_cookie_here"
    echo.
)

set "OUTPUT_ARG="
if defined OUTPUT_DIR set OUTPUT_ARG=--output "%OUTPUT_DIR%"

"%PYTHON%" "%CAPTURE%" "%URL%" %OUTPUT_ARG% --duration 0 --manual-delay 0 ^
    --format "%IMAGE_FORMAT%" --playback-rate %RATE% -r %SAMPLE_RATE% ^
    --stability-duration %STABILITY% --min-slide-duration %MIN_SLIDE% %EXTRA_ARGS%
