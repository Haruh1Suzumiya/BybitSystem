@echo off

:: Run as administrator
>nul 2>&1 "%SYSTEMROOT%\system32\cacls.exe" "%SYSTEMROOT%\system32\config\system"
if '%errorlevel%' NEQ '0' (
    echo Administrator privileges are required. Attempting to elevate...
    goto UACPrompt
) else ( goto gotAdmin )

:UACPrompt
    echo Set UAC = CreateObject^("Shell.Application"^) > "%temp%\getadmin.vbs"
    echo UAC.ShellExecute "%~s0", "", "", "runas", 1 >> "%temp%\getadmin.vbs"
    "%temp%\getadmin.vbs"
    exit /B

:gotAdmin
    if exist "%temp%\getadmin.vbs" ( del "%temp%\getadmin.vbs" )
    pushd "%CD%"
    CD /D "%~dp0"

:: Install pip
echo Installing pip...
curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
python get-pip.py

:: Create virtual environment
echo Creating virtual environment...
python -m venv myvenv

:: Activate virtual environment
echo Activating virtual environment...
call myvenv\Scripts\activate

:: Install libraries from requirements.txt
echo Installing required libraries...
pip install -r requirements.txt

echo Setup completed. Press Enter to exit.
pause >nul
