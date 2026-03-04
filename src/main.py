"""
Основной файл запуска приложения Meshtastic (CustomTkinter).

Порядок инициализации:
1. Загрузка .env (поддерживается как обычный запуск, так и PyInstaller --onefile).
2. Настройка логирования.
3. Конфигурация CustomTkinter (светлая тема, синяя палитра).
4. Поиск иконки, создание и запуск главного окна MainWindow.
"""

import sys
import os
import logging

from dotenv import load_dotenv

# ── Поиск .env в нескольких возможных местах ─────────────────────────────────
_env_dirs = [
    getattr(sys, "_MEIPASS", ""),          # PyInstaller --onefile bundle
    os.path.dirname(os.path.abspath(__file__)),
    os.path.dirname(sys.executable),
    os.getcwd(),
]
for _d in _env_dirs:
    if _d and os.path.isfile(os.path.join(_d, ".env")):
        load_dotenv(os.path.join(_d, ".env"))
        break

BUILD_VERSION = os.getenv("BUILD_VERSION", "0.0.0")
EXE_NAME      = os.getenv("EXE_NAME",      "MeshtasticFlasher")
WINDOW_TITLE  = os.getenv("WINDOW_TITLE",  "Прошивка Meshtastic Node")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logging.info(f"Запуск {EXE_NAME} v_{BUILD_VERSION}")

import customtkinter as ctk


def _find_icon() -> str | None:
    """
    Ищет файл иконки приложения в нескольких стандартных директориях.

    Проверяет meshtastic.ico, затем meshtastic.png.

    Returns:
        Абсолютный путь к найденному файлу иконки или None.
    """
    base_dirs = [
        getattr(sys, "_MEIPASS", ""),
        os.path.dirname(os.path.abspath(__file__)),
        os.path.dirname(sys.executable),
        os.getcwd(),
    ]
    for name in ("meshtastic.ico", "meshtastic.png"):
        for d in base_dirs:
            if not d:
                continue
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def main() -> None:
    """
    Точка входа в приложение.

    Настраивает CustomTkinter, создаёт и запускает главное окно.
    При критической ошибке записывает её в лог.
    """
    try:
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        from mainw import MainWindow

        app = MainWindow()
        # Устанавливаем заголовок строки ОС (из .env).
        # Внутренний визуальный заголовок задан в mainw._build_header().
        app.title(f"{WINDOW_TITLE}  v{BUILD_VERSION}")

        icon_path = _find_icon()
        if icon_path:
            try:
                if icon_path.endswith(".ico"):
                    app.iconbitmap(icon_path)
                else:
                    from PIL import Image, ImageTk  # type: ignore
                    img = ImageTk.PhotoImage(Image.open(icon_path))
                    app.iconphoto(True, img)
            except Exception as exc:
                logging.warning(f"Не удалось установить иконку: {exc}")

        app.mainloop()

    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    main()
