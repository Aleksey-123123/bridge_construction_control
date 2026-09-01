@echo off
chcp 65001 >nul
cd /d "%~dp0"

REM Резервная копия базы и файлов счетов.
REM Чтобы копии складывались в сетевую папку или на другой диск,
REM впишите путь после знака "=", например:
REM set BACKUP_DIR=D:\Backups\Scheta
set BACKUP_DIR=

if "%BACKUP_DIR%"=="" (
    python backup.py
) else (
    python backup.py "%BACKUP_DIR%"
)

if errorlevel 1 (
    echo Не удалось сделать копию.
    exit /b 1
)
