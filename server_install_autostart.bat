@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Регистрирует автозапуск приложения при включении сервера.
REM Запускать ОТ ИМЕНИ АДМИНИСТРАТОРА (правой кнопкой — «Запуск от имени
REM администратора»). Достаточно выполнить один раз.

net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo Нужны права администратора.
    echo Закройте это окно, нажмите на файл правой кнопкой мыши
    echo и выберите "Запуск от имени администратора".
    echo.
    pause
    exit /b 1
)

set TASKNAME=Scheta i KP

schtasks /Query /TN "%TASKNAME%" >nul 2>&1
if not errorlevel 1 (
    echo Автозапуск уже настроен — обновляю...
    schtasks /Delete /TN "%TASKNAME%" /F >nul
)

schtasks /Create /TN "%TASKNAME%" /TR "\"%~dp0server_start.bat\"" ^
    /SC ONSTART /RU SYSTEM /RL HIGHEST /F
if errorlevel 1 (
    echo.
    echo Не удалось создать задачу автозапуска.
    pause
    exit /b 1
)

echo.
echo ================================================================
echo  Готово. Приложение будет запускаться само при включении сервера.
echo.
echo  Запустить прямо сейчас, не перезагружаясь:
echo      schtasks /Run /TN "%TASKNAME%"
echo  Остановить:
echo      schtasks /End /TN "%TASKNAME%"
echo  Убрать автозапуск:
echo      schtasks /Delete /TN "%TASKNAME%" /F
echo ================================================================
echo.
pause
