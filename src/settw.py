"""
Модуль окна настроек (CustomTkinter).

Содержит:
- CTkSpinbox      — кастомный виджет спинбокса (−/entry/+).
- _ConfigReader   — фоновый поток чтения конфига с устройства.
- SettingsDialog  — модальное окно настроек Meshtastic.
"""

import json
import logging
import os
import threading

import customtkinter as ctk
import tkinter as tk

import serial.tools.list_ports

import meshc


# ── Цветовая палитра (та же, что в mainw) ─────────────────────────────────────
_C_ACCENT       = "#0078d4"
_C_ACCENT_HOVER = "#1a86d9"
_C_GREEN        = "#28a745"
_C_RED          = "#e81123"
_C_ORANGE       = "#f7630c"
_C_GRAY         = "#888888"
_C_BG           = "#f3f3f3"
_C_CARD         = "#ffffff"
_C_BORDER       = "#e5e5e5"
_C_BTN          = "#f0f0f0"
_C_BTN_TXT      = "#1a1a1a"
_C_BTN_HOV      = "#e0e0e0"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_card(
    parent: ctk.CTkBaseClass,
    title: str,
) -> tuple[ctk.CTkFrame, ctk.CTkFrame]:
    """
    Создаёт виджет-карточку (белый фрейм) с заголовком и разделителем.

    Параметры:
        parent — родительский контейнер.
        title  — текст заголовка.

    Returns:
        Кортеж ``(outer, content)``:
        - ``outer``   — внешний фрейм для упаковки в родителя.
        - ``content`` — внутренний прозрачный фрейм для дочерних виджетов.
    """
    outer = ctk.CTkFrame(
        parent,
        fg_color=_C_CARD,
        corner_radius=8,
        border_width=1,
        border_color=_C_BORDER,
    )

    header = ctk.CTkFrame(outer, fg_color="transparent", height=38)
    header.pack(fill="x", padx=14, pady=(10, 0))
    header.pack_propagate(False)

    ctk.CTkLabel(
        header, text=title, anchor="w",
        font=("Segoe UI", 11, "bold"), text_color="#1a1a1a",
    ).pack(side="left", fill="y")

    ctk.CTkFrame(outer, height=1, fg_color=_C_BORDER, corner_radius=0).pack(
        fill="x", padx=10, pady=(0, 4)
    )

    content = ctk.CTkFrame(outer, fg_color="transparent")
    content.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    return outer, content


# ── CTkSpinbox ────────────────────────────────────────────────────────────────

class CTkSpinbox(ctk.CTkFrame):
    """
    Кастомный виджет спинбокса для CustomTkinter.

    Представляет собой горизонтальную группу «[−] [entry] [+]».
    Поддерживает прямой ввод с клавиатуры; кнопки инкрементируют/декрементируют
    значение в пределах ``[from_, to]``.

    Пример::

        spin = CTkSpinbox(parent, from_=0, to=104, initial_value=6)
        spin.pack()
        val = spin.get()   # -> int
        spin.set(10)
    """

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        from_: int = 0,
        to: int = 100,
        initial_value: int = 0,
        entry_width: int = 70,
        **kwargs,
    ) -> None:
        """
        Параметры:
            parent        — родительский контейнер.
            from_         — минимально допустимое значение.
            to            — максимально допустимое значение.
            initial_value — начальное значение.
            entry_width   — ширина поля ввода в пикселях.
            **kwargs      — дополнительные аргументы для ``CTkFrame``.
        """
        super().__init__(parent, fg_color="transparent", **kwargs)

        self._from = from_
        self._to   = to
        self._var  = tk.StringVar(value=str(initial_value))

        btn_kw = dict(
            width=30, height=30,
            font=("Segoe UI", 13),
            fg_color=_C_BTN,
            text_color=_C_BTN_TXT,
            hover_color=_C_BTN_HOV,
            corner_radius=4,
        )

        ctk.CTkButton(self, text="−", command=self._decrement, **btn_kw).pack(side="left")

        self._entry = ctk.CTkEntry(
            self,
            textvariable=self._var,
            width=entry_width,
            height=30,
            justify="center",
        )
        self._entry.pack(side="left", padx=2)

        ctk.CTkButton(self, text="+", command=self._increment, **btn_kw).pack(side="left")

    # ── private ───────────────────────────────────────────────────────────────

    def _current_int(self) -> int:
        """
        Возвращает текущее целочисленное значение поля, зажатое в ``[from_, to]``.

        При невалидном вводе возвращает ``from_``.
        """
        try:
            return max(self._from, min(self._to, int(self._var.get())))
        except (ValueError, tk.TclError):
            return self._from

    def _increment(self) -> None:
        """Увеличивает значение на 1, не превышая ``to``."""
        v = self._current_int()
        self._var.set(str(min(self._to, v + 1)))

    def _decrement(self) -> None:
        """Уменьшает значение на 1, не опускаясь ниже ``from_``."""
        v = self._current_int()
        self._var.set(str(max(self._from, v - 1)))

    # ── public ────────────────────────────────────────────────────────────────

    def get(self) -> int:
        """
        Возвращает текущее значение как ``int``, зажатое в ``[from_, to]``.

        Returns:
            Целое число в допустимом диапазоне.
        """
        return self._current_int()

    def set(self, value: int | str) -> None:
        """
        Устанавливает значение спинбокса.

        Параметры:
            value — новое значение (будет преобразовано в ``int``).
        """
        self._var.set(str(int(value)))


# ── _ConfigReader ─────────────────────────────────────────────────────────────

class _ConfigReader(threading.Thread):
    """
    Фоновый поток для чтения конфигурации с Meshtastic-устройства.

    По завершении вызывает ``callback(result)`` в UI-потоке через
    ``root.after(0, ...)``.

    Параметры конструктора:
        port     — имя COM-порта (например, ``'COM3'``).
        callback — функция ``(result: dict) -> None``.
        root     — CTkToplevel-диалог (для ``after``).
    """

    def __init__(
        self,
        port: str,
        callback,
        root: ctk.CTkToplevel,
    ) -> None:
        super().__init__(daemon=True)
        self._port     = port
        self._callback = callback
        self._root     = root

    def run(self) -> None:
        """Вызывает ``meshc.read_device_config`` и передаёт результат в UI."""
        try:
            result = meshc.read_device_config(self._port)
        except Exception as exc:
            result = {"success": False, "message": str(exc)}
        self._root.after(0, lambda r=result: self._callback(r))


# ── SettingsDialog ────────────────────────────────────────────────────────────

class SettingsDialog(ctk.CTkToplevel):
    """
    Модальное окно настроек Meshtastic.

    Карточка «Ключ шифрования»:
    - Генерация случайного 32-байтового ключа.
    - Поле ввода ключа (пустое = открытый канал AQ==).

    Карточка «Основные настройки» (2 колонки):
    - Регион, Modem Preset, Frequency Slot, Hop Limit.
    - Rebroadcast, Smart Distance, Имя канала, Точность позиции.

    Кнопки:
    - «Заполнить» — читает настройки с подключённого устройства.
    - «Сохранить» — записывает настройки в ``config.json``.
    - «Закрыть»   — закрывает окно без сохранения.

    Строка статуса показывает результат последней операции.
    """

    def __init__(
        self,
        parent: ctk.CTk,
        current_settings: dict | None = None,
        *,
        port: str | None = None,
    ) -> None:
        """
        Параметры:
            parent           — главное окно MainWindow.
            current_settings — словарь текущих настроек (для предзаполнения).
            port             — COM-порт для операции «Заполнить» (может быть None).
        """
        super().__init__(parent)
        self.title("Настройки")
        self.geometry("780x510")
        self.minsize(660, 460)

        self._settings: dict = dict(current_settings or {})
        self._port: str | None = port
        self._reader: _ConfigReader | None = None

        self._build_ui()
        self._load_settings()
        self._update_status_from_file()
        logging.info("Окно настроек открыто")

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Строит весь интерфейс диалога настроек."""
        root = ctk.CTkFrame(self, fg_color=_C_BG)
        root.pack(fill="both", expand=True, padx=0, pady=0)

        inner = ctk.CTkFrame(root, fg_color=_C_BG)
        inner.pack(fill="both", expand=True, padx=20, pady=(14, 16))

        # Заголовок
        ctk.CTkLabel(
            inner, text="Настройки Meshtastic",
            font=("Segoe UI", 15, "bold"), text_color="#1a1a1a",
        ).pack(pady=(0, 10))

        # Карточки
        self._build_key_card(inner).pack(fill="x", pady=(0, 8))
        self._build_settings_card(inner).pack(fill="x", pady=(0, 8))

        # Кнопки действий
        self._build_buttons(inner).pack(fill="x", pady=(4, 0))

        # Статус
        self._build_status_bar(inner).pack(fill="x", pady=(8, 0))

    def _build_key_card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        """
        Строит карточку «Ключ шифрования».

        Returns:
            Внешний фрейм карточки.
        """
        outer, content = _make_card(parent, "Ключ шифрования")

        ctk.CTkButton(
            content, text="Сгенерировать ключ", width=180,
            fg_color=_C_BTN, text_color=_C_BTN_TXT, hover_color=_C_BTN_HOV,
            command=self._generate_key,
        ).pack(anchor="w", padx=4, pady=(4, 2))

        ctk.CTkLabel(
            content,
            text="(если не указан — будет использован открытый канал AQ==)",
            font=("Segoe UI", 9), text_color=_C_GRAY,
        ).pack(anchor="w", padx=4, pady=(0, 4))

        key_row = ctk.CTkFrame(content, fg_color="transparent")
        key_row.pack(fill="x", padx=4, pady=(0, 4))

        ctk.CTkLabel(key_row, text="Ключ:", width=50, anchor="w").pack(side="left")

        self._key_edit = ctk.CTkEntry(key_row, placeholder_text="base64...", width=400)
        self._key_edit.pack(side="left", fill="x", expand=True, padx=(4, 0))

        return outer

    def _build_settings_card(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        """
        Строит карточку «Основные настройки» с двухколоночным расположением.

        Левая колонка:  Регион, Modem Preset, Frequency Slot, Hop Limit.
        Правая колонка: Rebroadcast, Smart Distance, Канал, Точность позиции.

        Returns:
            Внешний фрейм карточки.
        """
        outer, content = _make_card(parent, "Основные настройки")

        grid = ctk.CTkFrame(content, fg_color="transparent")
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        # ── левая колонка ──────────────────────────────────────────────────────
        self._region_combo = ctk.CTkComboBox(
            grid, values=["MY_433", "RU"], width=160, state="readonly",
        )
        self._region_combo.set("MY_433")
        self._grid_row(grid, 0, 0, "Регион:", self._region_combo,
                       tooltip="MY_433 — 433 МГц, RU — 868 МГц")

        self._preset_combo = ctk.CTkComboBox(
            grid,
            values=[
                "LONG_FAST", "LONG_MODERATE", "LONG_TURBO",
                "MEDIUM_FAST", "MEDIUM_SLOW",
                "SHORT_FAST", "SHORT_SLOW", "SHORT_TURBO",
            ],
            width=160, state="readonly",
        )
        self._preset_combo.set("LONG_FAST")
        self._grid_row(grid, 1, 0, "Modem Preset:", self._preset_combo)

        self._freq_slot_spin = CTkSpinbox(grid, from_=0, to=104, initial_value=6)
        self._grid_row(grid, 2, 0, "Frequency Slot:", self._freq_slot_spin,
                       tooltip="0 — авто по имени канала, 6 — рекомендуется для MY_433")

        self._hop_spin = CTkSpinbox(grid, from_=1, to=7, initial_value=7)
        self._grid_row(grid, 3, 0, "Hop Limit:", self._hop_spin,
                       tooltip="Максимальное число пересылок (1–7)")

        # ── правая колонка ────────────────────────────────────────────────────
        self._mode_combo = ctk.CTkComboBox(
            grid, values=["LOCAL_ONLY", "KNOWN_ONLY", "ALL", "NONE"],
            width=160, state="readonly",
        )
        self._mode_combo.set("LOCAL_ONLY")
        self._grid_row(grid, 0, 2, "Rebroadcast:", self._mode_combo)

        self._dist_spin = CTkSpinbox(
            grid, from_=1, to=10000, initial_value=5, entry_width=80,
        )
        self._grid_row(grid, 1, 2, "Smart Distance (м):", self._dist_spin,
                       tooltip="Минимальная дистанция smart position (метры)")

        # Имя канала с ограничением 12 символов
        self._channel_var = tk.StringVar()
        self._channel_var.trace_add("write", self._limit_channel_name)
        self._channel_edit = ctk.CTkEntry(
            grid, textvariable=self._channel_var,
            placeholder_text="≤ 12 символов", width=160,
        )
        self._grid_row(grid, 2, 2, "Канал:", self._channel_edit,
                       tooltip="Имя Primary канала — не более 12 символов")

        self._prec_spin = CTkSpinbox(grid, from_=0, to=32, initial_value=32)
        self._grid_row(grid, 3, 2, "Точность позиции:", self._prec_spin,
                       tooltip="Точность позиции (0–32)")

        return outer

    @staticmethod
    def _grid_row(
        grid: ctk.CTkFrame,
        row: int,
        col_start: int,
        label_text: str,
        widget: ctk.CTkBaseClass,
        tooltip: str = "",
    ) -> None:
        """
        Вспомогательный метод: размещает пару «метка + виджет» в grid-сетке.

        Параметры:
            grid       — контейнер с grid-менеджером.
            row        — номер строки (0-based).
            col_start  — начальная колонка (0 = левая, 2 = правая).
            label_text — текст метки.
            widget     — виджет значения.
            tooltip    — текст всплывающей подсказки (пока не отображается,
                         хранится как атрибут ``widget.tooltip_text``).
        """
        lbl = ctk.CTkLabel(
            grid, text=label_text, anchor="w",
            font=("Segoe UI", 10), text_color="#1a1a1a",
        )
        lbl.grid(row=row, column=col_start, sticky="w", padx=(8, 4), pady=5)
        widget.grid(row=row, column=col_start + 1, sticky="ew", padx=(0, 16), pady=5)
        if tooltip:
            widget.tooltip_text = tooltip  # type: ignore[attr-defined]

    def _build_buttons(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        """
        Строит панель кнопок «Заполнить», «Сохранить», «Закрыть».

        Returns:
            Фрейм с кнопками.
        """
        frame = ctk.CTkFrame(parent, fg_color="transparent")

        self._btn_fill = ctk.CTkButton(
            frame, text="Заполнить", width=130,
            fg_color=_C_BTN, text_color=_C_BTN_TXT, hover_color=_C_BTN_HOV,
            command=self._fill_from_device,
        )
        self._btn_fill.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            frame, text="Сохранить", width=130,
            fg_color=_C_ACCENT, hover_color=_C_ACCENT_HOVER,
            command=self._save,
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            frame, text="Закрыть", width=110,
            fg_color=_C_BTN, text_color=_C_BTN_TXT, hover_color=_C_BTN_HOV,
            command=self.destroy,
        ).pack(side="left")

        return frame

    def _build_status_bar(self, parent: ctk.CTkFrame) -> ctk.CTkFrame:
        """
        Строит строку статуса: горизонтальный разделитель + точка + текст.

        Returns:
            Фрейм строки статуса.
        """
        container = ctk.CTkFrame(parent, fg_color="transparent")

        ctk.CTkFrame(
            container, height=1, fg_color=_C_BORDER, corner_radius=0,
        ).pack(fill="x", pady=(0, 6))

        status_row = ctk.CTkFrame(container, fg_color="transparent")
        status_row.pack(fill="x")

        self._status_dot = ctk.CTkLabel(
            status_row, text="●", width=20,
            font=("Segoe UI", 13), text_color=_C_GRAY,
        )
        self._status_dot.pack(side="left", padx=(0, 6))

        self._status_label = ctk.CTkLabel(
            status_row, text="", anchor="w",
            font=("Segoe UI", 9), text_color=_C_GRAY,
        )
        self._status_label.pack(side="left", fill="x", expand=True)

        return container

    # ── Status helper ─────────────────────────────────────────────────────────

    _STATUS_COLOR_MAP: dict[str, str] = {
        "green":  "#28a745",
        "red":    "#e81123",
        "orange": "#f7630c",
        "blue":   "#0078d4",
        "gray":   "#888888",
    }

    def _set_status(self, text: str, color: str) -> None:
        """
        Обновляет строку статуса.

        Параметры:
            text  — отображаемое сообщение.
            color — ключ из ``_STATUS_COLOR_MAP`` или CSS-цвет.
        """
        css = self._STATUS_COLOR_MAP.get(color, color)
        self._status_dot.configure(text_color=css)
        self._status_label.configure(text=text, text_color=css)

    # ── Validation ────────────────────────────────────────────────────────────

    def _limit_channel_name(self, *_) -> None:
        """Обрезает имя канала до 12 символов при каждом изменении."""
        val = self._channel_var.get()
        if len(val) > 12:
            self._channel_var.set(val[:12])

    # ── Data load/save ────────────────────────────────────────────────────────

    def _load_settings(self) -> None:
        """
        Загружает настройки из ``config.json`` (если существует) и заполняет
        виджеты формы значениями. При ошибке чтения файла логирует её.
        """
        if os.path.exists("config.json"):
            try:
                with open("config.json", encoding="utf-8") as f:
                    self._settings.update(json.load(f))
            except Exception as exc:
                logging.error(f"Ошибка загрузки настроек: {exc}")

        s = self._settings
        self._region_combo.set(s.get("region", "MY_433"))
        self._preset_combo.set(s.get("modem_preset", "LONG_FAST"))
        self._freq_slot_spin.set(int(s.get("frequency_slot", 6)))
        self._hop_spin.set(int(s.get("hop_limit", 7)))
        self._mode_combo.set(s.get("rebroadcast_mode", "LOCAL_ONLY"))
        self._dist_spin.set(int(s.get("smart_distance", 5)))
        self._channel_var.set(s.get("channel_name", "2-9"))
        self._prec_spin.set(int(s.get("position_precision", 32)))
        self._key_edit.delete(0, "end")
        self._key_edit.insert(0, s.get("encryption_key", ""))

    def _update_status_from_file(self) -> None:
        """
        Отображает состояние файла конфигурации в строке статуса:
        - серый  — файл не найден,
        - зелёный — файл загружен,
        - оранжевый — файл пуст,
        - красный — ошибка чтения.
        """
        if not os.path.exists("config.json"):
            self._set_status(
                "Файл конфигурации не найден (настройки по умолчанию)", "gray"
            )
            return
        try:
            with open("config.json", encoding="utf-8") as f:
                data = json.load(f)
            if data:
                self._set_status("Конфигурация загружена", "green")
            else:
                self._set_status("Файл конфигурации пуст", "orange")
        except Exception:
            self._set_status("Ошибка чтения конфигурации", "red")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _generate_key(self) -> None:
        """Генерирует случайный 32-байтовый ключ шифрования и вставляет в поле."""
        key = meshc.generate_encryption_key()
        self._key_edit.delete(0, "end")
        self._key_edit.insert(0, key)
        logging.info("Сгенерирован новый ключ шифрования")

    def _save(self) -> None:
        """
        Читает значения всех виджетов и сохраняет их в ``config.json``.

        При успехе отображает зелёный статус, при ошибке — красный.
        """
        settings = {
            "region":             self._region_combo.get(),
            "modem_preset":       self._preset_combo.get(),
            "frequency_slot":     self._freq_slot_spin.get(),
            "hop_limit":          self._hop_spin.get(),
            "rebroadcast_mode":   self._mode_combo.get(),
            "smart_distance":     self._dist_spin.get(),
            "channel_name":       self._channel_var.get().strip(),
            "position_precision": self._prec_spin.get(),
            "encryption_key":     self._key_edit.get(),
        }
        try:
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
            logging.info(f"Настройки сохранены: {settings}")
            self._set_status("Конфигурация сохранена", "green")
        except Exception as exc:
            logging.error(f"Ошибка сохранения: {exc}")
            self._set_status(f"Ошибка сохранения: {exc}", "red")

    def _fill_from_device(self) -> None:
        """
        Запускает фоновое чтение конфигурации с устройства.

        Если ``_port`` не задан, использует первый доступный COM-порт.
        При отсутствии портов отображает ошибку.
        Результат обрабатывается в ``_on_fill_done``.
        """
        port = self._port
        if not port:
            ports = [p.device for p in serial.tools.list_ports.comports()]
            if not ports:
                self._set_status("Нет доступных COM портов", "red")
                return
            port = ports[0]

        self._btn_fill.configure(state="disabled")
        self._set_status("Чтение настроек с устройства…", "blue")
        logging.info(f"Заполнить: чтение с порта {port}")

        self._reader = _ConfigReader(port, self._on_fill_done, self)
        self._reader.start()

    def _on_fill_done(self, result: dict) -> None:
        """
        Обрабатывает результат чтения конфига с устройства.

        Заполняет виджеты полученными значениями.
        При ошибке отображает сообщение в строке статуса.

        Параметры:
            result — словарь от ``meshc.read_device_config``.
        """
        self._btn_fill.configure(state="normal")

        if not result or not result.get("success"):
            msg = (result or {}).get("message", "неизвестная ошибка")
            self._set_status(f"Ошибка чтения: {msg[:50]}", "red")
            logging.error(f"Заполнить: ошибка — {msg}")
            return

        cfg = result.get("config", {})

        if cfg.get("region"):
            self._region_combo.set(cfg["region"])
        if cfg.get("modem_preset"):
            self._preset_combo.set(cfg["modem_preset"])
        if cfg.get("frequency_slot") is not None:
            self._freq_slot_spin.set(int(cfg["frequency_slot"]))
        if cfg.get("hop_limit") is not None:
            self._hop_spin.set(int(cfg["hop_limit"]))
        if cfg.get("rebroadcast_mode"):
            self._mode_combo.set(cfg["rebroadcast_mode"])
        if cfg.get("smart_distance") is not None:
            self._dist_spin.set(int(cfg["smart_distance"]))
        if cfg.get("channel_name"):
            self._channel_var.set(cfg["channel_name"])
        if cfg.get("position_precision") is not None:
            self._prec_spin.set(int(cfg["position_precision"]))

        self._set_status("Настройки загружены с устройства", "green")
        logging.info(f"Заполнить: {cfg}")
