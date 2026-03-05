@echo off
echo Starting compilation...

:: Создаем папку dist если её нет
if not exist dist mkdir dist

:: Очищаем dist перед сборкой
echo Cleaning dist folder...
del /q "dist\*" 2>nul

:: Запускаем компиляцию
echo Running compiler...
docker compose run --rm compiler

if %errorlevel% equ 0 (
    echo.
    echo ✅ SUCCESS! Executable is in the dist folder:
    dir dist\*.exe
) else (
    echo.
    echo ❌ Compilation failed!
)