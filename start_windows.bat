@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM ================== НАСТРОЙКА ==================
REM Чтобы включить общий пароль для входа, впишите его после знака "=", например:
REM set APP_PASSWORD=ofis2026
REM Оставьте пустым, чтобы заходить без пароля.
set APP_PASSWORD=

REM ИИ-разбор сметы (необязательно). Впишите ключ Anthropic после "=":
REM set ANTHROPIC_API_KEY=sk-ant-ваш-ключ
REM Пусто — функция выключена, группы бюджета вносятся вручную.
set ANTHROPIC_API_KEY=
REM ==============================================

echo Проверяю Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo Python не найден. Установите его с https://www.python.org/downloads/
    echo При установке поставьте галочку "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo Устанавливаю зависимости (первый запуск - до пары минут)...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo Не удалось установить зависимости. Проверьте интернет и запустите снова.
    pause
    exit /b 1
)

for /f %%i in ('python find_ip.py') do set LANIP=%%i

echo.
echo ============================================================
echo   Приложение запущено. НЕ закрывайте это окно, пока работаете.
echo.
echo   На этом компьютере:         http://localhost:8000
echo   С телефона (тот же Wi-Fi):  http://%LANIP%:8000
echo.
echo   Если Windows спросит про доступ к сети - нажмите "Разрешить".
echo ============================================================
echo.

python app.py
pause
