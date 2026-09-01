@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ============ НАСТРОЙКА ОФИСНОГО СЕРВЕРА ============
REM ОБЯЗАТЕЛЬНО задайте пароль: приложение будет доступно из интернета.
REM Впишите его сразу после знака "=", без кавычек и без пробелов.
set APP_PASSWORD=

REM ИИ-разбор сметы (необязательно). Впишите ключ Anthropic после "=":
set ANTHROPIC_API_KEY=

REM Порт, на котором работает приложение. Менять обычно не нужно.
set PORT=8000
REM ====================================================

if "%APP_PASSWORD%"=="" (
    echo.
    echo ================================================================
    echo  ВНИМАНИЕ: пароль не задан!
    echo  Откройте server_start.bat в Блокноте и впишите пароль
    echo  в строку "set APP_PASSWORD=" — иначе войти сможет кто угодно.
    echo ================================================================
    echo.
)

python --version >nul 2>&1
if errorlevel 1 (
    echo Python не найден. Установите его с https://www.python.org/downloads/
    echo При установке поставьте галочку "Add Python to PATH".
    pause
    exit /b 1
)

python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Не удалось установить зависимости. Проверьте интернет.
    pause
    exit /b 1
)

python serve.py
