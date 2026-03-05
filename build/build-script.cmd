@echo off
setlocal enabledelayedexpansion

echo ===== Starting build process =====

cd C:\app

:: Очищаем предыдущие сборки
if exist dist rmdir /s /q dist 2>nul
if exist build rmdir /s /q build 2>nul
if exist *.spec del /q *.spec 2>nul

:: Копируем все файлы из src в рабочую директорию (как в оригинальном Dockerfile)
echo Copying source files...
copy src\*.py . 2>nul
copy src\*.png . 2>nul
copy src\.env . 2>nul
copy C:\build\meshtastic.ico . 2>nul

:: Читаем переменные из .env
for /f "usebackq tokens=1,* delims==" %%a in (".env") do set "%%a=%%b"
echo EXE_NAME=%EXE_NAME%
echo BUILD_VERSION=%BUILD_VERSION%

:: Устанавливаем зависимости если есть requirements.txt
if exist src\requirements.txt (
    echo Found requirements.txt, installing dependencies...
    copy src\requirements.txt .
    pip install -r requirements.txt
    if !errorlevel! neq 0 (
        echo Failed to install dependencies
        exit /b 1
    )
) else (
    echo No requirements.txt found, skipping dependency installation
)

:: Запускаем PyInstaller
:: --collect-data customtkinter обязателен: включает темы и ассеты библиотеки
echo Building executable...
pyinstaller --onefile --windowed --icon="meshtastic.ico" --name=%EXE_NAME% --add-data="meshtastic.png;." --add-data="meshtastic.ico;." --add-data=".env;." --collect-data customtkinter main.py

:: Проверяем результат
if exist "dist\%EXE_NAME%.exe" (
    echo SUCCESS: Executable created
    copy /y "dist\%EXE_NAME%.exe" "C:\%EXE_NAME%.exe" >nul
    copy /y "dist\%EXE_NAME%.exe" "C:\app\dist\%EXE_NAME%.exe" >nul
    echo Executable copied to C:\app\dist\%EXE_NAME%.exe
) else (
    echo FAIL: Executable not found
    exit /b 1
)

echo ===== Build completed =====