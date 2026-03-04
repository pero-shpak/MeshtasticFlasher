"""
Модуль главного окна приложения (CustomTkinter).

Единое прокручиваемое окно с секциями:
    «Подключение» → «Устройство» → «LoRa-радио» → «Канал» → «Журнал»

Все поля ввода и настройки находятся в одном окне — отдельные диалоги
не требуются. Запись и чтение данных с устройства — через ``meshc``.

Компоненты:
    Worker            — фоновый поток с callback-уведомлением в UI.
    _QueueLogHandler  — потокобезопасный обработчик Python-логгера.
    _section_header() — разделитель секции (иконка + заголовок + линия).
    _form_row()       — строка формы (метка), возвращает фрейм для виджета.
    MainWindow        — главное окно CTk.
"""

import logging
import queue
import re
import threading

import customtkinter as ctk
import tkinter as tk

import serial.tools.list_ports

import meshc


# ── Палитра ───────────────────────────────────────────────────────────────────
_C_ACCENT      = "#0078d4"   # синий: кнопка «Автоопределить», роль
_C_ACCENT_H    = "#1986d8"   # hover синего
_C_GREEN       = "#2d8c3c"   # зелёный: кнопка «Применить»
_C_GREEN_H     = "#3aa34a"   # hover зелёного
_C_GRAY_BTN    = "#5a6268"   # серый: второстепенные кнопки
_C_GRAY_BTN_H  = "#4e555b"   # hover серого
_C_HEADER_BG   = "#ffffff"   # фон шапки
_C_SEC_LINE    = "#c8c8c8"   # линии разделителей
_C_SEC_TITLE   = "#1a1a1a"   # заголовки секций
_C_LABEL       = "#444444"   # метки формы
_C_INPUT_BG    = "#ffffff"   # фон полей ввода
_C_BORDER      = "#cccccc"   # рамка полей ввода
_C_LOG_BG      = "#1e1e1e"   # фон лог-консоли
_C_LOG_FG      = "#d4d4d4"   # текст лог-консоли

# Глобальная потокобезопасная очередь для лог-сообщений
_log_queue: queue.Queue = queue.Queue()


# ── Worker ────────────────────────────────────────────────────────────────────

class Worker(threading.Thread):
    """
    Выполняет произвольную функцию в фоновом потоке (daemon).

    По завершении вызывает ``callback(result)`` в UI-потоке через
    ``root.after(0, ...)``, обеспечивая потокобезопасное обновление виджетов.

    Пример::

        Worker(meshc.test_device_connection, "COM3",
               callback=self._on_done, root=self).start()
    """

    def __init__(self, fn, *args, callback=None, root=None, **kwargs):
        """
        Параметры:
            fn       — функция, выполняемая в фоне.
            *args    — позиционные аргументы для ``fn``.
            callback — ``(result) -> None``, вызывается в UI-потоке.
            root     — CTk-виджет (нужен для ``root.after``).
            **kwargs — именованные аргументы для ``fn``.
        """
        super().__init__(daemon=True)
        self._fn       = fn
        self._args     = args
        self._kwargs   = kwargs
        self._callback = callback
        self._root     = root

    def run(self) -> None:
        """Выполняет ``fn``, перехватывает исключения, передаёт результат в UI."""
        try:
            result = self._fn(*self._args, **self._kwargs)
        except Exception as exc:
            logging.error(f"Worker: {exc}")
            result = {"success": False, "message": str(exc)}

        if self._callback and self._root:
            self._root.after(0, lambda r=result: self._callback(r))


# ── Log handler ───────────────────────────────────────────────────────────────

class _QueueLogHandler(logging.Handler):
    """
    Направляет Python-логи в глобальную потокобезопасную очередь
    ``_log_queue``, откуда UI-поток вычитывает их через ``after()``.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s  %(levelname)-7s  %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        """Форматирует запись и кладёт в очередь без блокировки."""
        try:
            _log_queue.put_nowait(self.format(record))
        except Exception:
            pass


# ── Layout helpers ────────────────────────────────────────────────────────────

def _section_header(parent: ctk.CTkBaseClass, icon: str, title: str) -> None:
    """
    Добавляет в ``parent`` строку-разделитель секции.

    Вид: иконка + жирный заголовок + горизонтальная серая линия.

    Параметры:
        parent — родительский контейнер (обычно CTkScrollableFrame).
        icon   — эмодзи/символ перед заголовком.
        title  — текст заголовка секции.
    """
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=(12, 6))

    ctk.CTkLabel(
        row,
        text=f"{icon}  {title}",
        font=("Segoe UI", 11, "bold"),
        text_color=_C_SEC_TITLE,
        anchor="w",
    ).pack(side="left", padx=(0, 12))

    ctk.CTkFrame(
        row, height=1, corner_radius=0, fg_color=_C_SEC_LINE,
    ).pack(side="left", fill="x", expand=True)


def _form_row(
    parent: ctk.CTkBaseClass,
    label_text: str,
    label_w: int = 200,
) -> ctk.CTkFrame:
    """
    Создаёт горизонтальный фрейм-строку формы и размещает в нём метку.

    Возвращает фрейм — вызывающий код должен создать и упаковать
    виджет значения непосредственно в этот фрейм::

        row = _form_row(parent, "COM-порт:")
        entry = ctk.CTkEntry(row, ...)
        entry.pack(side="left", fill="x", expand=True)

    Параметры:
        parent    — родительский контейнер.
        label_text — текст метки (с двоеточием).
        label_w   — ширина колонки метки в пикселях.

    Returns:
        Горизонтальный ``CTkFrame`` с упакованной меткой.
    """
    row = ctk.CTkFrame(parent, fg_color="transparent")
    row.pack(fill="x", pady=3)

    ctk.CTkLabel(
        row,
        text=label_text,
        anchor="w",
        width=label_w,
        font=("Segoe UI", 10),
        text_color=_C_LABEL,
    ).pack(side="left")

    return row


def _make_entry(parent: ctk.CTkBaseClass, **kwargs) -> ctk.CTkEntry:
    """
    Создаёт ``CTkEntry`` с общим стилем приложения (белый фон, тонкая рамка).

    Параметры:
        parent   — родительский виджет (должен быть строка формы, не scroll-фрейм!).
        **kwargs — дополнительные параметры ``CTkEntry``.

    Returns:
        Готовый (не упакованный) ``CTkEntry``.
    """
    return ctk.CTkEntry(
        parent,
        fg_color=_C_INPUT_BG,
        border_color=_C_BORDER,
        border_width=1,
        corner_radius=4,
        height=32,
        **kwargs,
    )


def _make_combo(
    parent: ctk.CTkBaseClass,
    values: list[str],
    **kwargs,
) -> ctk.CTkComboBox:
    """
    Создаёт readonly ``CTkComboBox`` с общим стилем приложения.

    Параметры:
        parent   — родительский виджет (строка формы).
        values   — список допустимых значений.
        **kwargs — дополнительные параметры ``CTkComboBox``.

    Returns:
        Готовый (не упакованный) ``CTkComboBox``.
    """
    return ctk.CTkComboBox(
        parent,
        values=values,
        state="readonly",
        fg_color=_C_INPUT_BG,
        border_color=_C_BORDER,
        border_width=1,
        corner_radius=4,
        height=32,
        button_color="#b0b0b0",
        button_hover_color="#909090",
        dropdown_fg_color=_C_INPUT_BG,
        dropdown_text_color=_C_SEC_TITLE,
        dropdown_hover_color="#e8f2fc",
        **kwargs,
    )


# ── MainWindow ────────────────────────────────────────────────────────────────

class MainWindow(ctk.CTk):
    """
    Главное и единственное окно приложения Meshtastic TAK Node Setup.

    Структура::

        ┌──────────────────────────────────────┐
        │  CTkScrollableFrame:                 │
        │    📍 Подключение                    │
        │    📟 Устройство                     │
        │    📡 LoRa-радио                     │
        │    📢 Канал                          │
        │    📋 Журнал                         │
        ├──────────────────────────────────────┤
        │  [Прочитать] [Применить] [Оч. лог]  │  ← фиксированный футер
        └──────────────────────────────────────┘

    Все настройки собраны в одном окне.
    Данные сохраняются в ``config.json`` кнопкой «Применить».
    """

    def __init__(self) -> None:
        super().__init__()
        self.geometry("720x800")
        self.minsize(640, 640)

        self._is_busy: bool = False

        self._setup_log_handler()
        self._build_content()
        self._build_footer()

        self._load_saved_settings()
        self._poll_log_queue()

    # ── Logging ───────────────────────────────────────────────────────────────

    def _setup_log_handler(self) -> None:
        """Регистрирует ``_QueueLogHandler`` в корневом Python-логгере."""
        logging.getLogger().addHandler(_QueueLogHandler())

    def _poll_log_queue(self) -> None:
        """
        Каждые 100 мс вычитывает сообщения из ``_log_queue``
        и добавляет их в лог-консоль через ``_log_append``.
        """
        while True:
            try:
                self._log_append(_log_queue.get_nowait())
            except queue.Empty:
                break
        self.after(100, self._poll_log_queue)

    def _log_append(self, text: str) -> None:
        """Добавляет строку в лог-консоль и прокручивает до конца."""
        self._log_box.configure(state="normal")
        self._log_box.insert("end", text + "\n")
        self._log_box.see("end")
        self._log_box.configure(state="disabled")

    # ── Scrollable content ────────────────────────────────────────────────────

    def _build_content(self) -> None:
        """Создаёт прокручиваемую область и размещает в ней все секции."""
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=(20, 6), pady=(10, 0))

        # Внутренний контейнер с правым отступом: отделяет контент от скроллбара.
        # CTkScrollableFrame размещает скроллбар ВНУТРИ своей ширины, поэтому
        # правый padding нужен именно здесь, а не в padx самого scroll.
        # 32 px: достаточно для видимого зазора и при этом влезает 44-символьный
        # base64-ключ (AES-256 PSK) при шрифте Segoe UI 10pt.
        inner = ctk.CTkFrame(scroll, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=(0, 32))

        self._build_connection_section(inner)
        self._build_device_section(inner)
        self._build_lora_section(inner)
        self._build_channel_section(inner)
        self._build_log_section(inner)

    # ── Connection ────────────────────────────────────────────────────────────

    def _build_connection_section(self, p: ctk.CTkBaseClass) -> None:
        """
        Секция «Подключение»: выпадающий список COM-портов, кнопки
        «Обновить» (пересканировать порты) и «Проверить» (тест соединения).

        Параметры:
            p — прокручиваемый контейнер (CTkScrollableFrame).
        """
        _section_header(p, "📍", "Подключение")

        row = _form_row(p, "COM-порт:")

        self._port_combo = _make_combo(row, values=[], width=160)
        self._port_combo.pack(side="left", fill="x", expand=True, padx=(0, 8))

        # «Обновить» — пересканировать доступные порты
        self._btn_refresh = ctk.CTkButton(
            row,
            text="Обновить",
            width=100,
            height=32,
            corner_radius=4,
            fg_color=_C_GRAY_BTN,
            hover_color=_C_GRAY_BTN_H,
            command=self._scan_ports,
        )
        self._btn_refresh.pack(side="left", padx=(0, 6))

        # «Проверить» — тест соединения без чтения всей конфигурации
        self._btn_check = ctk.CTkButton(
            row,
            text="Проверить",
            width=110,
            height=32,
            corner_radius=4,
            fg_color=_C_ACCENT,
            hover_color=_C_ACCENT_H,
            command=self._check_port,
        )
        self._btn_check.pack(side="left")

        # Метка-подсказка: показывается когда портов нет
        self._no_ports_label = ctk.CTkLabel(
            p,
            text="COM-порты не обнаружены",
            font=("Segoe UI", 9),
            text_color="#e81123",
        )

        # Первичное сканирование при запуске
        self._scan_ports()

    # ── Device ────────────────────────────────────────────────────────────────

    def _build_device_section(self, p: ctk.CTkBaseClass) -> None:
        """
        Секция «Устройство»: короткое имя (≤ 4 символа), длинное имя, роль.

        Параметры:
            p — прокручиваемый контейнер.
        """
        _section_header(p, "📟", "Устройство")

        # Короткое имя — ограничено 4 символами через StringVar.trace
        self._short_var = tk.StringVar()
        self._short_var.trace_add("write", self._limit_short)
        row1 = _form_row(p, "Короткое имя (макс 4):")
        self._short_edit = _make_entry(
            row1,
            textvariable=self._short_var,
            placeholder_text="Максимум 4 символа",
        )
        self._short_edit.pack(side="left", fill="x", expand=True)

        row2 = _form_row(p, "Длинное имя:")
        self._long_edit = _make_entry(row2, placeholder_text="Длинное имя устройства")
        self._long_edit.pack(side="left", fill="x", expand=True)

        # Роль — синий комбобокс (выделяется среди остальных полей)
        row3 = _form_row(p, "Роль:")
        self._role_combo = ctk.CTkComboBox(
            row3,
            values=["TAK", "TAK_TRACKER", "CLIENT", "REPEATER"],
            state="readonly",
            height=32,
            corner_radius=4,
            fg_color=_C_ACCENT,
            text_color="#ffffff",
            border_color=_C_ACCENT,
            button_color=_C_ACCENT_H,
            button_hover_color=_C_ACCENT_H,
            dropdown_fg_color=_C_INPUT_BG,
            dropdown_text_color=_C_SEC_TITLE,
            dropdown_hover_color="#e8f2fc",
        )
        self._role_combo.set("TAK")
        self._role_combo.pack(side="left", fill="x", expand=True)

    # ── LoRa ──────────────────────────────────────────────────────────────────

    def _build_lora_section(self, p: ctk.CTkBaseClass) -> None:
        """
        Секция «LoRa-радио»: регион, modem preset, hop limit,
        rebroadcast mode, smart distance, frequency slot.

        Параметры:
            p — прокручиваемый контейнер.
        """
        _section_header(p, "📡", "LoRa-радио")

        row = _form_row(p, "Регион:")
        self._region_combo = _make_combo(row, ["MY_433", "RU"])
        self._region_combo.set("MY_433")
        self._region_combo.pack(side="left", fill="x", expand=True)

        row = _form_row(p, "Modem preset:")
        self._preset_combo = _make_combo(row, [
            "LONG_FAST", "LONG_MODERATE", "LONG_TURBO",
            "MEDIUM_FAST", "MEDIUM_SLOW",
            "SHORT_FAST", "SHORT_SLOW", "SHORT_TURBO",
        ])
        self._preset_combo.set("LONG_FAST")
        self._preset_combo.pack(side="left", fill="x", expand=True)

        row = _form_row(p, "Hop limit:")
        self._hop_entry = _make_entry(row, placeholder_text="1–7")
        self._hop_entry.pack(side="left", fill="x", expand=True)

        row = _form_row(p, "Rebroadcast mode:")
        self._rebroadcast_combo = _make_combo(
            row, ["LOCAL_ONLY", "KNOWN_ONLY", "ALL", "NONE"],
        )
        self._rebroadcast_combo.set("LOCAL_ONLY")
        self._rebroadcast_combo.pack(side="left", fill="x", expand=True)

        row = _form_row(p, "Smart distance (м):")
        self._smart_dist_entry = _make_entry(row, placeholder_text="метры")
        self._smart_dist_entry.pack(side="left", fill="x", expand=True)

        row = _form_row(p, "Frequency slot:")
        self._freq_slot_entry = _make_entry(row, placeholder_text="0–104")
        self._freq_slot_entry.pack(side="left", fill="x", expand=True)

    # ── Channel ───────────────────────────────────────────────────────────────

    def _build_channel_section(self, p: ctk.CTkBaseClass) -> None:
        """
        Секция «Канал»: имя канала, точность позиции, PSK-ключ,
        кнопка генерации ключа и чекбокс «Показать ключ».

        Параметры:
            p — прокручиваемый контейнер.
        """
        _section_header(p, "📢", "Канал")

        self._channel_var = tk.StringVar()
        self._channel_var.trace_add("write", self._limit_channel)
        row = _form_row(p, "Имя канала:")
        self._channel_edit = _make_entry(
            row,
            textvariable=self._channel_var,
            placeholder_text="Имя канала (≤ 12 символов)",
        )
        self._channel_edit.pack(side="left", fill="x", expand=True)

        row = _form_row(p, "Точность позиции (бит):")
        self._pos_prec_entry = _make_entry(row, placeholder_text="0–32")
        self._pos_prec_entry.pack(side="left", fill="x", expand=True)

        self._psk_var = tk.StringVar()
        row = _form_row(p, "PSK ключ (base64):")
        self._psk_edit = _make_entry(
            row,
            textvariable=self._psk_var,
            placeholder_text="base64 ключ (пусто = открытый канал)",
            show="●",
        )
        self._psk_edit.pack(side="left", fill="x", expand=True)

        # Строка с кнопкой генерации и чекбоксом видимости ключа.
        # Используем CTkLabel(width=200) как spacer — он не раздувает высоту строки,
        # в отличие от CTkFrame.
        key_ctrl = ctk.CTkFrame(p, fg_color="transparent")
        key_ctrl.pack(fill="x", pady=(4, 2))

        ctk.CTkLabel(key_ctrl, text="", width=200, height=28,
                     fg_color="transparent").pack(side="left")

        ctk.CTkButton(
            key_ctrl,
            text="Сгенерировать ключ",
            width=160,
            height=28,
            corner_radius=4,
            fg_color="#ececec",
            text_color=_C_SEC_TITLE,
            hover_color="#d8d8d8",
            border_width=1,
            border_color=_C_BORDER,
            command=self._gen_key,
        ).pack(side="left", padx=(0, 12))

        self._show_key_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            key_ctrl,
            text="Показать ключ",
            variable=self._show_key_var,
            command=self._toggle_key,
            font=("Segoe UI", 10),
            text_color=_C_LABEL,
            height=28,
        ).pack(side="left")

    # ── Log ───────────────────────────────────────────────────────────────────

    def _build_log_section(self, p: ctk.CTkBaseClass) -> None:
        """
        Секция «Журнал»: тёмная монотипная консоль для лог-вывода.

        Параметры:
            p — прокручиваемый контейнер.
        """
        _section_header(p, "📋", "Журнал")

        self._log_box = ctk.CTkTextbox(
            p,
            height=180,
            font=("Cascadia Mono", 9),
            fg_color=_C_LOG_BG,
            text_color=_C_LOG_FG,
            border_width=1,
            border_color=_C_SEC_LINE,
            corner_radius=4,
            wrap="none",
        )
        self._log_box.pack(fill="x", pady=(0, 12))
        self._log_box.configure(state="disabled")

    # ── Footer ────────────────────────────────────────────────────────────────

    def _build_footer(self) -> None:
        """
        Строит фиксированную нижнюю панель с тремя равными кнопками:
        «Прочитать настройки», «Применить», «Очистить лог».
        """
        ctk.CTkFrame(self, height=1, corner_radius=0, fg_color=_C_SEC_LINE).pack(fill="x")

        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.pack(fill="x", padx=20, pady=12)
        footer.columnconfigure(0, weight=1)
        footer.columnconfigure(1, weight=1)
        footer.columnconfigure(2, weight=1)

        btn_kw = dict(height=36, corner_radius=4)

        self._btn_read = ctk.CTkButton(
            footer,
            text="📋  Прочитать настройки",
            fg_color=_C_GRAY_BTN,
            hover_color=_C_GRAY_BTN_H,
            command=self._read_from_device,
            **btn_kw,
        )
        self._btn_read.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        self._btn_apply = ctk.CTkButton(
            footer,
            text="✏  Применить",
            fg_color=_C_GREEN,
            hover_color=_C_GREEN_H,
            command=self._apply_settings,
            **btn_kw,
        )
        self._btn_apply.grid(row=0, column=1, padx=4, sticky="ew")

        self._btn_clear = ctk.CTkButton(
            footer,
            text="🗑  Очистить лог",
            fg_color=_C_GRAY_BTN,
            hover_color=_C_GRAY_BTN_H,
            command=self._clear_log,
            **btn_kw,
        )
        self._btn_clear.grid(row=0, column=2, padx=(4, 0), sticky="ew")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _scan_ports(self) -> None:
        """
        Сканирует доступные COM-порты и обновляет выпадающий список.

        При наличии портов выбирает первый. При отсутствии — показывает
        метку-подсказку и блокирует кнопку «Проверить».
        """
        try:
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except Exception as exc:
            logging.error(f"Ошибка сканирования портов: {exc}")
            ports = []

        if ports:
            self._port_combo.configure(values=ports, state="readonly")
            self._port_combo.set(ports[0])
            self._btn_check.configure(state="normal")
            self._no_ports_label.pack_forget()
            logging.info(
                f"Найдено портов: {len(ports)} — {', '.join(ports)}. Выбран {ports[0]}"
            )
        else:
            self._port_combo.configure(values=[""], state="disabled")
            self._port_combo.set("")
            self._btn_check.configure(state="disabled")
            self._no_ports_label.pack(anchor="w", padx=(200, 0), pady=(2, 0))
            logging.warning("COM-порты не обнаружены")

    def _check_port(self) -> None:
        """
        Проверяет соединение с устройством на выбранном порту.

        Выполняет быстрое подключение через ``meshc.test_device_connection``
        (без чтения полной конфигурации) и логирует результат.
        Не изменяет поля формы.
        """
        if self._is_busy:
            return
        port = self._port_combo.get().strip()
        if not port:
            logging.warning("Порт не выбран")
            return

        self._is_busy = True
        self._set_buttons(False)
        logging.info(f"Проверка соединения: {port}…")

        def _done(result: dict) -> None:
            self._is_busy = False
            self._set_buttons(True)
            if result and result.get("success"):
                short = result.get("short_name", "")
                long_ = result.get("long_name", "")
                role  = result.get("role", "")
                logging.info(
                    f"Устройство доступно на {port}: "
                    f"short='{short}', long='{long_}', role='{role}'"
                )
            else:
                logging.error(
                    f"Устройство на {port} недоступно: "
                    f"{(result or {}).get('message', '?')}"
                )
            self.bell()

        Worker(
            meshc.test_device_connection, port,
            callback=_done, root=self,
        ).start()

    def _read_from_device(self) -> None:
        """
        Читает настройки с устройства в фоновом потоке и заполняет все поля.

        Объединяет ``meshc.test_device_connection`` (имя, роль) и
        ``meshc.read_device_config`` (LoRa, канал) в одной задаче.
        """
        if self._is_busy:
            return
        port = self._port_combo.get().strip()
        if not port:
            logging.warning("Укажите COM-порт перед чтением")
            return

        self._is_busy = True
        self._set_buttons(False)
        logging.info(f"Чтение настроек с {port}…")

        # Используем read_device_full — одно соединение вместо двух.
        # Двойной connect/disconnect провоцировал аппаратный сброс устройства.
        Worker(
            meshc.read_device_full, port,
            callback=self._on_read_done, root=self,
        ).start()

    def _on_read_done(self, result: dict, _silent: bool = False) -> None:
        """
        Обрабатывает результат чтения с устройства: заполняет поля или логирует ошибку.

        Параметры:
            result  — словарь, вернувшийся из ``read_device_full``.
            _silent — если ``True``, не сбрасывает ``_is_busy`` / кнопки и не звонит
                      (используется при вызове из ``_done`` после записи настроек,
                      когда состояние уже сброшено).
        """
        if not _silent:
            self._is_busy = False
            self._set_buttons(True)

        if not result or not result.get("success"):
            logging.error(f"Ошибка чтения: {(result or {}).get('message', '?')}")
            if not _silent:
                self.bell()
            return

        short = result.get("short_name", "")
        long_ = result.get("long_name",  "")
        role  = result.get("role", "")
        if short:
            self._short_var.set(short)
        if long_:
            self._set_entry(self._long_edit, long_)
        if role and role in ["TAK", "TAK_TRACKER", "CLIENT", "REPEATER"]:
            self._role_combo.set(role)

        cfg = result.get("config", {})
        if cfg.get("region"):
            self._region_combo.set(cfg["region"])
        if cfg.get("modem_preset"):
            self._preset_combo.set(cfg["modem_preset"])
        if cfg.get("hop_limit") is not None:
            self._set_entry(self._hop_entry, str(cfg["hop_limit"]))
        if cfg.get("rebroadcast_mode"):
            self._rebroadcast_combo.set(cfg["rebroadcast_mode"])
        if cfg.get("smart_distance") is not None:
            self._set_entry(self._smart_dist_entry, str(cfg["smart_distance"]))
        if cfg.get("frequency_slot") is not None:
            self._set_entry(self._freq_slot_entry, str(cfg["frequency_slot"]))
        if cfg.get("channel_name"):
            self._channel_var.set(cfg["channel_name"])
        if cfg.get("position_precision") is not None:
            self._set_entry(self._pos_prec_entry, str(cfg["position_precision"]))

        logging.info("Настройки успешно прочитаны с устройства")
        if not _silent:
            self.bell()

    def _apply_settings(self) -> None:
        """
        Валидирует форму и записывает все настройки на устройство в фоновом потоке.
        После успешной записи сохраняет настройки в ``config.json``.
        """
        if self._is_busy:
            return
        port = self._port_combo.get().strip()
        if not port:
            logging.warning("Укажите COM-порт перед применением")
            return

        short = self._short_var.get().strip()
        ok, err = self._validate_short(short)
        if not ok:
            logging.error(f"Ошибка валидации: {err}")
            return

        device_data = {
            "com_port":   port,
            "short_name": short,
            "long_name":  self._long_edit.get().strip(),
            "role":       self._role_combo.get(),
        }
        app_settings = {
            "region":             self._region_combo.get(),
            "modem_preset":       self._preset_combo.get(),
            "hop_limit":          self._int(self._hop_entry.get(),          7,  1,  7),
            "rebroadcast_mode":   self._rebroadcast_combo.get(),
            "smart_distance":     self._int(self._smart_dist_entry.get(),   5,  1,  10000),
            "frequency_slot":     self._int(self._freq_slot_entry.get(),    6,  0,  104),
            "channel_name":       self._channel_var.get().strip(),
            "position_precision": self._int(self._pos_prec_entry.get(),    32,  0,  32),
            "encryption_key":     self._psk_var.get().strip(),
        }

        logging.info(f"Запись: {device_data}")
        self._is_busy = True
        self._set_buttons(False)

        def _done(result: dict) -> None:
            self._is_busy = False
            self._set_buttons(True)
            if result and result.get("success"):
                meshc.save_application_settings(app_settings)
                logging.info("Настройки успешно записаны и сохранены")
                # Если устройство было доступно после финальной верификации —
                # загружаем прочитанные значения в поля формы
                read_res = result.get("read_result")
                if read_res and read_res.get("success"):
                    logging.info("Загружаем верифицированные настройки в поля формы...")
                    self._on_read_done(read_res, _silent=True)
                else:
                    logging.warning(
                        "Устройство недоступно для верификации — поля не обновлены"
                    )
            else:
                logging.error(f"Ошибка записи: {(result or {}).get('message', '?')}")
            self.bell()

        Worker(
            meshc.write_settings_to_device, device_data, app_settings,
            callback=_done, root=self,
        ).start()

    def _clear_log(self) -> None:
        """Очищает содержимое лог-консоли."""
        self._log_box.configure(state="normal")
        self._log_box.delete("1.0", "end")
        self._log_box.configure(state="disabled")

    def _gen_key(self) -> None:
        """Генерирует случайный PSK-ключ и вставляет в поле."""
        self._psk_var.set(meshc.generate_encryption_key())
        logging.info("Сгенерирован новый ключ шифрования")

    # ── Field helpers ─────────────────────────────────────────────────────────

    def _limit_short(self, *_) -> None:
        """Обрезает ``_short_var`` до 4 символов при каждом изменении."""
        v = self._short_var.get()
        if len(v) > 4:
            self._short_var.set(v[:4])

    def _limit_channel(self, *_) -> None:
        """Обрезает ``_channel_var`` до 12 символов при каждом изменении."""
        v = self._channel_var.get()
        if len(v) > 12:
            self._channel_var.set(v[:12])

    def _toggle_key(self) -> None:
        """Переключает видимость PSK-ключа (скрыть/показать)."""
        self._psk_edit.configure(show="" if self._show_key_var.get() else "●")

    @staticmethod
    def _set_entry(entry: ctk.CTkEntry, value: str) -> None:
        """
        Устанавливает значение ``CTkEntry``, предварительно очищая его.

        Параметры:
            entry — целевой виджет.
            value — новое строковое значение.
        """
        entry.delete(0, "end")
        entry.insert(0, value)

    @staticmethod
    def _int(val: str, default: int, lo: int = 0, hi: int = 9999) -> int:
        """
        Безопасно конвертирует строку в ``int``, зажатый в ``[lo, hi]``.

        Параметры:
            val     — входная строка.
            default — значение при ошибке парсинга.
            lo, hi  — границы допустимого диапазона.

        Returns:
            Целое число в диапазоне ``[lo, hi]``.
        """
        try:
            return max(lo, min(hi, int(val)))
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _validate_short(name: str) -> tuple[bool, str]:
        """
        Проверяет допустимость короткого имени устройства.

        Правила: не пустое, ≤ 4 символа, только ``[A-Za-z0-9_-]``.

        Returns:
            ``(True, '')`` при успехе или ``(False, сообщение)`` при ошибке.
        """
        if not name:
            return False, "Имя обязательно"
        if len(name) > 4:
            return False, "Максимум 4 символа"
        if not re.match(r"^[A-Za-z0-9_-]+$", name):
            return False, "Только буквы, цифры, _ и -"
        return True, ""

    def _set_buttons(self, enabled: bool) -> None:
        """
        Включает или отключает все три кнопки управления.

        Параметры:
            enabled — ``True`` для включения, ``False`` для блокировки.
        """
        state = "normal" if enabled else "disabled"
        self._btn_read.configure(state=state)
        self._btn_apply.configure(state=state)
        self._btn_clear.configure(state=state)

    def _load_saved_settings(self) -> None:
        """
        Загружает настройки из ``config.json`` и заполняет поля формы.
        При отсутствии файла устанавливает значения по умолчанию из ``meshc``.
        """
        s = meshc.load_application_settings()

        if s.get("region"):
            self._region_combo.set(s["region"])
        if s.get("modem_preset"):
            self._preset_combo.set(s["modem_preset"])
        if s.get("hop_limit") is not None:
            self._set_entry(self._hop_entry, str(s["hop_limit"]))
        if s.get("rebroadcast_mode"):
            self._rebroadcast_combo.set(s["rebroadcast_mode"])
        if s.get("smart_distance") is not None:
            self._set_entry(self._smart_dist_entry, str(s["smart_distance"]))
        if s.get("frequency_slot") is not None:
            self._set_entry(self._freq_slot_entry, str(s["frequency_slot"]))
        if s.get("channel_name"):
            self._channel_var.set(s["channel_name"])
        if s.get("position_precision") is not None:
            self._set_entry(self._pos_prec_entry, str(s["position_precision"]))
        if s.get("encryption_key"):
            self._psk_var.set(s["encryption_key"])

        # Восстанавливаем сохранённый порт, если он доступен в текущем списке
        saved_port = s.get("com_port", "")
        if saved_port:
            current_ports = self._port_combo.cget("values")
            if saved_port in current_ports:
                self._port_combo.set(saved_port)
                logging.info(f"Восстановлен порт из config.json: {saved_port}")
            else:
                logging.debug(
                    f"Сохранённый порт {saved_port!r} недоступен, "
                    "используется первый из списка"
                )

        logging.info("Настройки загружены из config.json")
