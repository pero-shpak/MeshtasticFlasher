# MeshtasticFlasher

Десктопное приложение для настройки Meshtastic-узлов (TAK Node Setup). Позволяет подключаться к устройству через COM-порт, читать и записывать конфигурацию: имя, роль, параметры LoRa, канал и ключ шифрования.

## Возможности

- **Подключение** — автоопределение COM-портов, проверка соединения с устройством
- **Устройство** — короткое имя (до 4 символов), длинное имя, роль (TAK, TAK_TRACKER, CLIENT, REPEATER)
- **LoRa-радио** — регион (MY_433, RU), modem preset, hop limit, rebroadcast mode, smart distance, frequency slot
- **Канал** — имя канала (до 12 символов), точность позиции, PSK-ключ (base64), генератор ключей
- **Журнал** — вывод логов операций в реальном времени

Настройки сохраняются в `config.json` при успешном применении и автоматически восстанавливаются при следующем запуске.

## Требования

- **Python** 3.10+
- **Поддерживаемые ОС:** Windows, Linux (для работы нужен доступ к COM/Serial-порту)

## Установка и запуск

### Вариант 1: Локальный запуск (разработка)

```bash
cd src
pip install -r requirements.txt
python main.py
```

### Вариант 2: Собранный исполняемый файл (Windows)

После сборки (см. раздел «Сборка») запустите `dist/MeshtasticFlasher.exe`.

## Зависимости

| Пакет           | Версия   | Назначение                          |
|-----------------|----------|-------------------------------------|
| customtkinter   | ≥5.2.0   | GUI-фреймворк                       |
| meshtastic      | ≥2.7.7   | API для работы с Meshtastic         |
| pyserial        | ≥3.5     | Доступ к COM-портам                 |
| pypubsub        | ≥4.0.3   | Внутренние зависимости meshtastic   |
| protobuf        | ≥4.21.0  | Сериализация protobuf               |
| python-dotenv   | ≥1.0.0   | Переменные окружения (.env)         |

## Структура проекта

```
├── src/
│   ├── main.py      # Точка входа, инициализация CustomTkinter
│   ├── mainw.py     # Главное окно (формы, кнопки, логика UI)
│   ├── meshc.py     # Работа с Meshtastic API (чтение/запись настроек)
│   ├── settw.py     # Модуль модального окна настроек (альтернативный UI)
│   └── requirements.txt
├── build/
│   ├── Dockerfile      # Образ для сборки под Windows
│   └── build-script.cmd # Скрипт PyInstaller
├── docker-compose.yml
├── compile.cmd         # Сборка через Docker (команда: docker compose run --rm compiler)
├── build-builder.cmd    # Сборка Docker-образа
└── rebuild-all.cmd      # Полная пересборка
```

## Сборка (Windows executable)

Сборка выполняется в Docker-контейнере на базе Windows Server Core и Visual Studio Build Tools.

### 1. Собрать образ сборщика

```bash
docker compose --profile build-image build
```

> **Примечание:** Для сборки образа нужны установщики `python-3.12.10-amd64.exe` и `vs_BuildTools.exe` в `build/`. См. `build/Dockerfile`.

### 2. Собрать исполняемый файл

```bash
compile.cmd
```

Или напрямую:

```bash
docker compose run --rm compiler
```

Готовый `.exe` будет в папке `dist/`.

### Переменные .env

Для кастомизации сборки создайте `.env` в корне или в `src/`:

```
BUILD_VERSION=1.0.0
EXE_NAME=MeshtasticFlasher
WINDOW_TITLE=Прошивка Meshtastic Node
```

## Конфигурация (config.json)

После успешного «Применить» приложение сохраняет настройки в `config.json` в текущей директории:

- `com_port` — последний использованный порт
- `region`, `modem_preset`, `hop_limit`, `frequency_slot`
- `rebroadcast_mode`, `smart_distance`
- `channel_name`, `position_precision`, `encryption_key`

## Лицензия

Apache License 2.0 — см. файл [LICENSE](LICENSE).
