# build/Dockerfile
FROM mcr.microsoft.com/windows/servercore:ltsc2022

SHELL ["cmd", "/S", "/C"]

# Копируем установщики
COPY python-3.12.10-amd64.exe C:/
COPY vs_BuildTools.exe C:/

# Устанавливаем Visual Studio Build Tools
RUN C:\vs_BuildTools.exe --quiet --wait --norestart --nocache \
    --add Microsoft.VisualStudio.Workload.MSBuildTools \
    --add Microsoft.VisualStudio.Workload.UniversalBuildTools \
    --add Microsoft.VisualStudio.Component.Windows10SDK.17763

# Устанавливаем Python
RUN C:\python-3.12.10-amd64.exe /quiet InstallAllUsers=1 PrependPath=1 TargetDir=C:\Python312

# Обновляем PATH
RUN setx /M PATH "%PATH%;C:\Python312;C:\Python312\Scripts"
RUN set PATH=%PATH%;C:\Python312;C:\Python312\Scripts

# Обновляем pip и устанавливаем pyinstaller глобально
RUN python -m pip install --upgrade pip
RUN pip install pyinstaller

# Создаем рабочую директорию
WORKDIR C:/app

# Создаем скрипт сборки
COPY build-script.cmd C:/build-script.cmd
RUN C:\Windows\System32\icacls C:\build-script.cmd /grant Everyone:F

# Команда по умолчанию (будет переопределена в docker-compose)
CMD ["cmd", "/C", "echo Build container ready && cmd"]