"""
Модуль для работы с Meshtastic через нативный Python API.

Предоставляет:
- Маппинги строковых констант на protobuf-коды (роль, регион, пресет, режим).
- Утилиты валидации, генерации ключей, чтения/записи ``config.json``.
- Функции подключения к устройству через Serial-интерфейс.
- Атомарные операторы записи (owner, LoRa, device, channel).
- Главную функцию ``write_settings_to_device`` — полный цикл прошивки.

Все функции работы с устройством являются блокирующими и предназначены
для вызова из фонового потока (Worker в mainw.py).
"""

import secrets
import base64
import json
import os
import re
import time
import logging
import traceback
import threading
from typing import Optional, Dict, Any, Tuple, List
from datetime import datetime

try:
    import meshtastic.serial_interface
    MESHTASTIC_AVAILABLE = True
except ImportError as e:
    MESHTASTIC_AVAILABLE = False
    logging.error(f"Ошибка импорта meshtastic: {e}")

CONFIG_FILE = "config.json"

logger = logging.getLogger(__name__)

# Подавляем многословный вывод библиотеки meshtastic
logging.getLogger("meshtastic").setLevel(logging.WARNING)
logging.getLogger("meshtastic.mesh_interface").setLevel(logging.WARNING)


# ── Маппинги enum → protobuf-код ─────────────────────────────────────────────

ROLE_MAPPING: Dict[str, int] = {
    "CLIENT":        0,
    "CLIENT_MUTE":   1,
    "ROUTER":        2,
    "REPEATER":      4,
    "TRACKER":       5,
    "SENSOR":        6,
    "TAK":           7,
    "CLIENT_HIDDEN": 8,
    "LOST_AND_FOUND":9,
    "TAK_TRACKER":  10,
    "ROUTER_LATE":  11,
    "CLIENT_BASE":  12,
}
"""Соответствие строковых названий ролей их protobuf-кодам."""

REGION_MAPPING: Dict[str, int] = {
    "MY_433": 16,
    "RU":      9,
}
"""Соответствие строковых регионов (частотных планов) их protobuf-кодам."""

MODEM_PRESET_MAPPING: Dict[str, int] = {
    "LONG_FAST":     0,
    "LONG_SLOW":     1,
    "VERY_LONG_SLOW":2,
    "MEDIUM_SLOW":   3,
    "MEDIUM_FAST":   4,
    "SHORT_SLOW":    5,
    "SHORT_FAST":    6,
    "LONG_MODERATE": 7,
    "SHORT_TURBO":   8,
    "LONG_TURBO":    9,
}
"""Соответствие названий modem preset их protobuf-кодам."""

REBROADCAST_MODE_MAPPING: Dict[str, int] = {
    "LOCAL_ONLY": 2,
    "KNOWN_ONLY": 3,
    "ALL":        0,
    "NONE":       4,
}
"""Соответствие режимов ретрансляции их protobuf-кодам."""

# ── Таймауты (секунды) ────────────────────────────────────────────────────────

TIMEOUT_WRITE:      int = 400
"""
Максимальное время полного цикла записи настроек.
5 шагов × 30 с паузы + время подключений + финальное чтение ≈ 250–300 с.
"""

TIMEOUT_REBOOT:     int = 15
"""Пауза после отправки команды перезагрузки (legacy, в новом цикле не используется)."""

TIMEOUT_READ:       int = 30
"""Таймаут при открытии SerialInterface (передаётся в библиотеку meshtastic)."""

TIMEOUT_OPERATION:  int = 5
"""Пауза внутри шага между атомарными вызовами writeConfig/writeChannel."""

TIMEOUT_INTER_STEP: int = 30
"""
Пауза между шагами записи (owner → lora → device/position → channels).
Необходима, так как устройство уходит в аппаратный сброс после каждого
важного writeConfig — соединение закрывается, и новое открывается только
после того, как прошивка полностью загрузится.
"""


# ── Внутренние утилиты ────────────────────────────────────────────────────────

def _close_interface(iface) -> None:
    """
    Безопасно закрывает SerialInterface Meshtastic.

    Игнорирует все исключения, возникающие при закрытии.

    Параметры:
        iface — объект ``meshtastic.serial_interface.SerialInterface`` или ``None``.
    """
    if iface:
        try:
            iface.close()
        except Exception:
            pass


def _code_to_name(mapping: Dict[str, int], code) -> Optional[str]:
    """
    Выполняет обратный поиск: возвращает строковое имя по числовому коду.

    Параметры:
        mapping — словарь ``{str: int}`` (например, ``ROLE_MAPPING``).
        code    — числовой protobuf-код.

    Returns:
        Строковое имя или ``None``, если код не найден.
    """
    for name, c in mapping.items():
        if c == code:
            return name
    return None


def _get_primary_channel(node) -> Optional[Any]:
    """
    Возвращает объект primary-канала (индекс 0) из node.channels.

    Поддерживает как ``dict``-формат (``{int: channel}``), так и
    ``list``-формат (``[channel, ...]``).

    Параметры:
        node — объект ``localNode`` Meshtastic.

    Returns:
        Объект канала или ``None``, если каналы не найдены.
    """
    ch = getattr(node, "channels", None)
    if not ch:
        return None
    if isinstance(ch, dict):
        return ch.get(0)
    if isinstance(ch, list) and ch:
        return ch[0]
    return None


# ── Генерация ключа шифрования ────────────────────────────────────────────────

def generate_encryption_key() -> str:
    """
    Генерирует случайный 32-байтовый ключ шифрования в кодировке base64.

    Использует криптографически стойкий генератор (``secrets``).

    Returns:
        Строка base64 длиной 44 символа (32 байта → base64).
        При ошибке возвращает ``"AQ=="`` (открытый канал).

    Пример::

        key = generate_encryption_key()
        # "k7Pq3...=="
    """
    try:
        return base64.b64encode(secrets.token_bytes(32)).decode("utf-8")
    except Exception as exc:
        logger.error(f"Ошибка генерации ключа: {exc}")
        return "AQ=="


# ── Валидация ─────────────────────────────────────────────────────────────────

def validate_short_name(short_name: str) -> Tuple[bool, str]:
    """
    Проверяет допустимость короткого имени устройства.

    Правила:
    - Не пустое.
    - Длина не более 4 символов.
    - Допустимые символы: буквы (A–Z, a–z), цифры (0–9), ``_``, ``-``.

    Параметры:
        short_name — проверяемая строка.

    Returns:
        Кортеж ``(ok: bool, error_message: str)``.
        При успехе — ``(True, '')``.
        При ошибке — ``(False, описание_ошибки)``.
    """
    if not short_name:
        return False, "Поле 'Короткое имя' обязательно для заполнения"
    if len(short_name) > 4:
        return False, "Короткое имя должно быть не более 4 символов"
    if not re.match(r"^[A-Za-z0-9_-]+$", short_name):
        return False, "Короткое имя может содержать только буквы, цифры, '_' и '-'"
    return True, ""


# ── Маппинг-функции ───────────────────────────────────────────────────────────

def map_role_to_proto(role_str: str) -> int:
    """
    Преобразует строковое название роли в protobuf-код.

    Параметры:
        role_str — строка из ``ROLE_MAPPING`` (например, ``"TAK"``).

    Returns:
        Числовой protobuf-код роли. При неизвестной строке — код ``"TAK"`` (7).
    """
    return ROLE_MAPPING.get(role_str, ROLE_MAPPING["TAK"])


def map_region_to_proto(region_str: str) -> int:
    """
    Преобразует строковое название региона в protobuf-код.

    Параметры:
        region_str — строка из ``REGION_MAPPING`` (например, ``"RU"``).

    Returns:
        Числовой protobuf-код региона. При неизвестном — код ``"MY_433"`` (16).
    """
    return REGION_MAPPING.get(region_str, REGION_MAPPING["MY_433"])


def map_modem_preset(preset_str: str) -> int:
    """
    Преобразует строковое название modem preset в protobuf-код.

    Параметры:
        preset_str — строка из ``MODEM_PRESET_MAPPING`` (например, ``"LONG_FAST"``).

    Returns:
        Числовой protobuf-код пресета. При неизвестном — код ``"LONG_FAST"`` (0).
    """
    return MODEM_PRESET_MAPPING.get(preset_str, MODEM_PRESET_MAPPING["LONG_FAST"])


def map_rebroadcast_mode(mode_str: str) -> int:
    """
    Преобразует строковое название режима ретрансляции в protobuf-код.

    Параметры:
        mode_str — строка из ``REBROADCAST_MODE_MAPPING`` (например, ``"LOCAL_ONLY"``).

    Returns:
        Числовой protobuf-код режима. При неизвестном — код ``"LOCAL_ONLY"`` (2).
    """
    return REBROADCAST_MODE_MAPPING.get(mode_str, REBROADCAST_MODE_MAPPING["LOCAL_ONLY"])


# ── config.json ───────────────────────────────────────────────────────────────

def save_application_settings(settings: Dict[str, Any]) -> bool:
    """
    Сохраняет настройки приложения в файл ``config.json`` в текущей директории.

    Параметры:
        settings — словарь настроек для сериализации в JSON.

    Returns:
        ``True`` при успехе, ``False`` при ошибке записи.
    """
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        logger.info(f"Настройки сохранены в {CONFIG_FILE}")
        return True
    except Exception as exc:
        logger.error(f"Ошибка сохранения настроек: {exc}")
        return False


def load_application_settings() -> Dict[str, Any]:
    """
    Загружает настройки приложения из файла ``config.json``.

    Если файл не существует или не может быть прочитан — возвращает
    словарь значений по умолчанию.

    Returns:
        Словарь настроек. Гарантированно содержит ключи:
        ``region``, ``modem_preset``, ``frequency_slot``, ``hop_limit``,
        ``rebroadcast_mode``, ``smart_distance``, ``channel_name``,
        ``position_precision``, ``encryption_key``.
    """
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                settings = json.load(f)
            logger.debug(f"Загруженные настройки: {settings}")
            return settings
    except Exception as exc:
        logger.error(f"Ошибка загрузки конфигурации: {exc}")

    default: Dict[str, Any] = {
        "region":             "MY_433",
        "modem_preset":       "LONG_FAST",
        "frequency_slot":     6,
        "hop_limit":          7,
        "rebroadcast_mode":   "LOCAL_ONLY",
        "smart_distance":     5,
        "channel_name":       "2-9",
        "position_precision": 32,
        "encryption_key":     "",
    }
    logger.debug(f"Используются настройки по умолчанию: {default}")
    return default


# ── Проверка порта ────────────────────────────────────────────────────────────

def _check_port_access(port: str) -> Optional[Dict[str, Any]]:
    """
    Быстрая проверка доступности COM-порта через ``serial.Serial``.

    Пытается открыть и немедленно закрыть порт.
    Используется перед созданием SerialInterface Meshtastic.

    Параметры:
        port — имя COM-порта (например, ``"COM3"``).

    Returns:
        ``None`` если порт доступен.
        Словарь ``{"success": False, "message": ...}`` при ошибке.
    """
    try:
        import serial
        ser = serial.Serial(port, timeout=1, write_timeout=1)
        ser.close()
        return None
    except Exception as exc:
        msg = str(exc)
        if "Access is denied" in msg or "Permission denied" in msg:
            return {"success": False, "message": f"Порт {port} занят другим приложением"}
        if "does not exist" in msg or "FileNotFoundError" in msg:
            return {"success": False, "message": f"Порт {port} не существует"}
        return {"success": False, "message": f"Ошибка доступа к порту: {msg[:100]}"}


# ── Единое чтение (идентификатор + конфиг) ───────────────────────────────────

def read_device_full(port: str) -> Dict[str, Any]:
    """
    Читает данные устройства и полную конфигурацию **в одном соединении**.

    Объединяет логику ``test_device_connection`` и ``read_device_config``,
    открывая Serial-интерфейс только один раз. Это важно: каждое закрытие
    соединения у ряда прошивок Meshtastic вызывает аппаратный сброс устройства.

    Параметры:
        port — имя COM-порта (например, ``"COM3"``).

    Returns:
        Словарь:

        - ``success`` (bool)   — результат операции.
        - ``short_name`` (str) — короткое имя.
        - ``long_name`` (str)  — длинное имя.
        - ``role`` (str)       — строковое название роли.
        - ``config`` (dict)    — LoRa/channel-конфиг (те же ключи что у ``read_device_config``).
        - ``message`` (str)    — описание ошибки (при неудаче).
    """
    logger.info(f"Полное чтение с порта {port}")

    if not MESHTASTIC_AVAILABLE:
        return {"success": False, "message": "Библиотека meshtastic не установлена"}

    # _check_port_access намеренно не вызывается: открытие/закрытие сырого serial
    # переключает DTR, что на ESP32-платах Meshtastic вызывает аппаратный сброс.
    # SerialInterface сам вернёт исчерпывающее исключение при недоступности порта.
    interface = None
    try:
        interface = meshtastic.serial_interface.SerialInterface(
            port, timeout=TIMEOUT_READ, noNodes=True,
        )
        if not interface:
            return {"success": False, "message": "Не удалось создать интерфейс"}

        time.sleep(1)

        # ── Идентификатор владельца ──────────────────────────────────────────
        short_name = ""
        long_name  = ""
        role_name  = ""

        my_num     = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
        node_entry = None

        if my_num is not None and hasattr(interface, "nodesByNum"):
            node_entry = interface.nodesByNum.get(my_num)
        if not node_entry and hasattr(interface, "nodes") and interface.nodes:
            node_entry = next(iter(interface.nodes.values()), None)

        if isinstance(node_entry, dict):
            user       = node_entry.get("user") or {}
            short_name = user.get("shortName", "") or ""
            long_name  = user.get("longName",  "") or ""
            role_value = user.get("role")
            if role_value is not None:
                role_name = (
                    _code_to_name(ROLE_MAPPING, role_value)
                    if isinstance(role_value, int)
                    else str(role_value)
                ) or ""

        logger.info(
            f"Устройство: short='{short_name}', long='{long_name}', role='{role_name}'"
        )

        # ── Конфигурация ─────────────────────────────────────────────────────
        cfg: Dict[str, Any] = {}
        node = interface.localNode

        try:
            lora = node.localConfig.lora
            cfg["region"]         = _code_to_name(REGION_MAPPING, getattr(lora, "region", None))
            cfg["modem_preset"]   = _code_to_name(MODEM_PRESET_MAPPING, getattr(lora, "modem_preset", None))
            cfg["frequency_slot"] = getattr(lora, "channel_num", None)
            cfg["hop_limit"]      = getattr(lora, "hop_limit", None)
        except Exception as exc:
            logger.warning(f"LoRa: {exc}")

        try:
            cfg["rebroadcast_mode"] = _code_to_name(
                REBROADCAST_MODE_MAPPING,
                getattr(node.localConfig.device, "rebroadcast_mode", None),
            )
        except Exception as exc:
            logger.warning(f"device: {exc}")

        try:
            cfg["smart_distance"] = getattr(
                node.localConfig.position, "broadcast_smart_minimum_distance", None,
            )
        except Exception as exc:
            logger.warning(f"position: {exc}")

        try:
            primary = _get_primary_channel(node)
            if primary and hasattr(primary, "settings"):
                ch_name = getattr(primary.settings, "name", "") or ""
                if ch_name:
                    cfg["channel_name"] = ch_name
                pp = getattr(
                    getattr(primary.settings, "module_settings", None),
                    "position_precision", None,
                )
                if pp is not None:
                    cfg["position_precision"] = pp
        except Exception as exc:
            logger.warning(f"channels: {exc}")

        cfg = {k: v for k, v in cfg.items() if v is not None}
        logger.info(f"Конфигурация: {cfg}")

        return {
            "success":    True,
            "message":    f"Устройство на {port}",
            "short_name": short_name,
            "long_name":  long_name,
            "role":       role_name,
            "config":     cfg,
        }

    except Exception as exc:
        logger.error(f"Ошибка полного чтения: {exc}")
        return {"success": False, "message": str(exc)[:200]}
    finally:
        _close_interface(interface)


# ── Проверка соединения ───────────────────────────────────────────────────────

def test_device_connection(port: str) -> Dict[str, Any]:
    """
    Подключается к Meshtastic-устройству и читает его идентификационные данные.

    Выполняет подключение с ``noNodes=True`` (без загрузки всей сетевой БД),
    читает short_name, long_name и role из ``nodesByNum``/``nodes``.

    Параметры:
        port — имя COM-порта (например, ``"COM3"``).

    Returns:
        Словарь со следующими ключами:

        - ``success`` (bool) — результат операции.
        - ``message`` (str) — описание результата или ошибки.
        - ``short_name`` (str) — короткое имя устройства (при успехе).
        - ``long_name`` (str)  — полное имя устройства (при успехе).
        - ``role`` (str)       — строковое название роли (при успехе).
    """
    logger.info(f"Проверка соединения с портом {port}")

    if not MESHTASTIC_AVAILABLE:
        return {"success": False, "message": "Библиотека meshtastic не установлена"}

    # _check_port_access не вызывается: сырой serial переключает DTR → reboot на ESP32.
    interface = None
    try:
        interface = meshtastic.serial_interface.SerialInterface(
            port, timeout=TIMEOUT_READ, noNodes=True,
        )
        if not interface:
            return {"success": False, "message": "Не удалось создать интерфейс"}

        time.sleep(1)

        short_name = ""
        long_name  = ""
        my_num     = getattr(getattr(interface, "myInfo", None), "my_node_num", None)
        node_entry = None

        if my_num is not None and hasattr(interface, "nodesByNum"):
            node_entry = interface.nodesByNum.get(my_num)
        if not node_entry and hasattr(interface, "nodes") and interface.nodes:
            node_entry = next(iter(interface.nodes.values()), None)

        role_name = ""
        if isinstance(node_entry, dict):
            user       = node_entry.get("user") or {}
            short_name = user.get("shortName", "") or ""
            long_name  = user.get("longName",  "") or ""
            role_value = user.get("role")
            if role_value is not None:
                if isinstance(role_value, int):
                    role_name = _code_to_name(ROLE_MAPPING, role_value) or ""
                else:
                    role_name = str(role_value)

        logger.info(
            f"Устройство на {port}: short='{short_name}', "
            f"long='{long_name}', role='{role_name}'"
        )
        return {
            "success":    True,
            "message":    f"Устройство доступно на {port}",
            "short_name": short_name,
            "long_name":  long_name,
            "role":       role_name,
        }

    except Exception as exc:
        logger.error(f"Ошибка при проверке устройства: {exc}")
        return {"success": False, "message": str(exc)[:200]}
    finally:
        _close_interface(interface)


# ── Чтение конфигурации ───────────────────────────────────────────────────────

def read_device_config(port: str) -> Dict[str, Any]:
    """
    Читает текущую конфигурацию устройства Meshtastic.

    Читает LoRa-настройки, режим ретрансляции, smart distance и параметры
    primary-канала. Используется кнопкой «Заполнить» в окне настроек.

    Подключение выполняется с ``noNodes=True`` для ускорения операции.

    Параметры:
        port — имя COM-порта (например, ``"COM3"``).

    Returns:
        Словарь:

        - ``success`` (bool)  — результат операции.
        - ``config`` (dict)   — прочитанные настройки (при успехе). Ключи:
          ``region``, ``modem_preset``, ``frequency_slot``, ``hop_limit``,
          ``rebroadcast_mode``, ``smart_distance``, ``channel_name``,
          ``position_precision``.
        - ``message`` (str)   — описание ошибки (при неудаче).
    """
    logger.info(f"Чтение конфигурации с порта {port}")

    if not MESHTASTIC_AVAILABLE:
        return {"success": False, "message": "Библиотека meshtastic не установлена"}

    interface = None
    try:
        interface = meshtastic.serial_interface.SerialInterface(
            port, timeout=TIMEOUT_READ, noNodes=True,
        )
        if not interface:
            return {"success": False, "message": "Не удалось создать интерфейс"}

        time.sleep(1)

        cfg: Dict[str, Any] = {}
        node = interface.localNode

        # LoRa-блок
        try:
            lora = node.localConfig.lora
            cfg["region"]       = _code_to_name(REGION_MAPPING, getattr(lora, "region", None))
            cfg["modem_preset"] = _code_to_name(MODEM_PRESET_MAPPING, getattr(lora, "modem_preset", None))
            cfg["frequency_slot"] = getattr(lora, "channel_num", None)
            cfg["hop_limit"]    = getattr(lora, "hop_limit", None)
        except Exception as exc:
            logger.warning(f"Не удалось прочитать LoRa: {exc}")

        # Device-блок
        try:
            cfg["rebroadcast_mode"] = _code_to_name(
                REBROADCAST_MODE_MAPPING,
                getattr(node.localConfig.device, "rebroadcast_mode", None),
            )
        except Exception as exc:
            logger.warning(f"Не удалось прочитать device: {exc}")

        # Position-блок
        try:
            cfg["smart_distance"] = getattr(
                node.localConfig.position,
                "broadcast_smart_minimum_distance",
                None,
            )
        except Exception as exc:
            logger.warning(f"Не удалось прочитать position: {exc}")

        # Channel-блок
        try:
            primary = _get_primary_channel(node)
            if primary and hasattr(primary, "settings"):
                ch_name = getattr(primary.settings, "name", "") or ""
                if ch_name:
                    cfg["channel_name"] = ch_name
                pp = getattr(
                    getattr(primary.settings, "module_settings", None),
                    "position_precision",
                    None,
                )
                if pp is not None:
                    cfg["position_precision"] = pp
        except Exception as exc:
            logger.warning(f"Не удалось прочитать каналы: {exc}")

        cfg = {k: v for k, v in cfg.items() if v is not None}
        logger.info(f"Прочитанная конфигурация: {cfg}")
        return {"success": True, "config": cfg}

    except Exception as exc:
        logger.error(f"Ошибка чтения конфигурации: {exc}")
        return {"success": False, "message": str(exc)[:200]}
    finally:
        _close_interface(interface)


# ── Применение отдельных блоков настроек ─────────────────────────────────────

def apply_owner_settings(node, device_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Записывает данные владельца (имя, роль) на устройство.

    Вызывает ``node.setOwner()``. При ошибке установки роли через setOwner
    делает повторную попытку без аргумента role (для совместимости со старыми
    прошивками).

    После записи делает паузу ``TIMEOUT_OPERATION`` секунд.

    Параметры:
        node        — объект ``localNode`` Meshtastic.
        device_data — словарь с ключами ``short_name``, ``long_name``, ``role``.

    Returns:
        Словарь с применёнными параметрами:
        ``short_name``, ``long_name``, ``role`` и (опционально) ``role_changed``.
    """
    applied: Dict[str, Any] = {}
    try:
        short_name = device_data.get("short_name", "")
        long_name  = device_data.get("long_name", "")
        role_str   = device_data.get("role", "TAK")
        role_proto = map_role_to_proto(role_str)

        # Читаем текущие значения, чтобы пропустить запись если ничего не изменилось
        cur_short = cur_long = cur_role = None
        try:
            owner     = node.owner
            cur_short = getattr(owner, "short_name", None)
            cur_long  = getattr(owner, "long_name",  None)
            cur_role  = getattr(owner, "role",       None)
        except Exception:
            pass

        cur_role_str = None
        if cur_role is not None:
            cur_role_str = (
                _code_to_name(ROLE_MAPPING, cur_role)
                if isinstance(cur_role, int)
                else str(cur_role)
            )

        if cur_short == short_name and cur_long == long_name and cur_role_str == role_str:
            logger.info("Владелец: изменений нет, setOwner пропущен")
            return applied  # пустой dict → _step не будет ждать

        logger.info(f"Владелец: short='{short_name}' long='{long_name}' role={role_str}")

        try:
            node.setOwner(long_name=long_name, short_name=short_name, role=role_proto)
        except Exception:
            # Старые прошивки могут не принимать аргумент role
            node.setOwner(long_name=long_name, short_name=short_name)
            applied["role_changed"] = True

        time.sleep(TIMEOUT_OPERATION)
        applied.update({
            "short_name": short_name,
            "long_name":  long_name,
            "role":       role_str,
        })
    except Exception as exc:
        logger.error(f"Ошибка установки владельца: {exc}")
    return applied


def apply_lora_settings(node, settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Применяет LoRa-настройки к устройству.

    Записывает только изменившиеся поля: region, modem_preset, frequency_slot,
    hop_limit. Вызывает ``node.writeConfig("lora")`` при наличии изменений.

    После записи делает паузу ``TIMEOUT_OPERATION`` секунд.

    Параметры:
        node     — объект ``localNode`` Meshtastic.
        settings — словарь настроек (из ``config.json``).

    Returns:
        Словарь с применёнными параметрами (только изменённые ключи).
    """
    applied: Dict[str, Any] = {}
    try:
        lora    = node.localConfig.lora
        changed = False

        if "region" in settings:
            rp = map_region_to_proto(settings["region"])
            if lora.region != rp:
                lora.region = rp
                applied["region"] = settings["region"]
                changed = True

        if "modem_preset" in settings:
            mp = map_modem_preset(settings["modem_preset"])
            if lora.modem_preset != mp:
                lora.modem_preset = mp
                changed = True
            if not lora.use_preset:
                lora.use_preset = True
                changed = True
            applied["modem_preset"] = settings["modem_preset"]

        if "frequency_slot" in settings:
            fs = int(settings["frequency_slot"])
            if lora.channel_num != fs:
                lora.channel_num = fs
                applied["frequency_slot"] = fs
                changed = True

        if "hop_limit" in settings:
            hl = settings["hop_limit"]
            if lora.hop_limit != hl:
                lora.hop_limit = hl
                applied["hop_limit"] = hl
                changed = True

        if changed:
            node.writeConfig("lora")
            logger.info(f"LoRa записано: {applied}")
            time.sleep(TIMEOUT_OPERATION)
        else:
            logger.info("LoRa: изменений нет")
    except Exception as exc:
        logger.error(f"Ошибка применения LoRa: {exc}")
    return applied


def apply_device_settings(
    node,
    settings: Dict[str, Any],
    owner_applied: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Применяет настройки блоков ``device`` и ``position`` к устройству.

    Записывает rebroadcast_mode (если изменился) через ``writeConfig("device")``.
    Записывает smart_distance через ``writeConfig("position")``.

    Если ``owner_applied`` содержит ``role_changed=True``, дополнительно
    прописывает роль в ``device``-конфиге (для прошивок, не поддерживающих role
    через setOwner).

    После каждой записи делает паузу ``TIMEOUT_OPERATION`` секунд.

    Параметры:
        node          — объект ``localNode`` Meshtastic.
        settings      — словарь настроек (из ``config.json``).
        owner_applied — словарь из ``apply_owner_settings`` (для ``role_changed``).

    Returns:
        Словарь с применёнными параметрами.
    """
    applied: Dict[str, Any] = {}
    try:
        dev        = node.localConfig.device
        dev_changed = False

        # Дозапись роли, если setOwner не принял её
        if owner_applied.get("role_changed"):
            rp = map_role_to_proto(owner_applied.get("role", "TAK"))
            if dev.role != rp:
                dev.role = rp
                dev_changed = True

        if "rebroadcast_mode" in settings:
            mp = map_rebroadcast_mode(settings["rebroadcast_mode"])
            if dev.rebroadcast_mode != mp:
                dev.rebroadcast_mode = mp
                applied["rebroadcast_mode"] = settings["rebroadcast_mode"]
                dev_changed = True

        if dev_changed:
            node.writeConfig("device")
            logger.info(f"Device записано: {applied}")
            time.sleep(TIMEOUT_OPERATION)

        # Position
        pos        = node.localConfig.position
        pos_changed = False
        if "smart_distance" in settings:
            sd = settings["smart_distance"]
            if pos.broadcast_smart_minimum_distance != sd:
                pos.broadcast_smart_minimum_distance = sd
                applied["smart_distance"] = sd
                pos_changed = True

        if pos_changed:
            node.writeConfig("position")
            logger.info(f"Position записано: smart_distance={sd}")
            time.sleep(TIMEOUT_OPERATION)

        if not applied and not dev_changed:
            logger.info("Device/Position: изменений нет")
    except Exception as exc:
        logger.error(f"Ошибка применения device/position: {exc}")
    return applied


def apply_channel_settings(node, settings: Dict[str, Any]) -> Dict[str, Any]:
    """
    Применяет настройки primary-канала: имя и ключ шифрования (PSK).

    Сравнивает текущие значения с новыми; вызывает ``node.writeChannel(0)``
    только при наличии изменений.

    PSK ``b"\\x01"`` (один байт 0x01) означает открытый канал (AQ== в base64).

    После записи делает паузу ``TIMEOUT_OPERATION`` секунд.

    Параметры:
        node     — объект ``localNode`` Meshtastic.
        settings — словарь настроек (ключи: ``channel_name``, ``encryption_key``).

    Returns:
        Словарь с применёнными параметрами (``channel_name``, ``encryption_key``).
    """
    applied: Dict[str, Any] = {}
    try:
        primary = _get_primary_channel(node)
        if not primary:
            logger.error("Primary канал не найден")
            return applied

        cs      = primary.settings
        changed = False

        new_name = (settings.get("channel_name") or "").strip()
        if new_name and cs.name != new_name:
            cs.name = new_name
            applied["channel_name"] = new_name
            changed = True

        enc_key = settings.get("encryption_key", "")
        if enc_key and enc_key != "AQ==":
            try:
                target_psk = base64.b64decode(enc_key)
            except Exception:
                target_psk = b"\x01"
        else:
            target_psk = b"\x01"

        if cs.psk != target_psk:
            cs.psk = target_psk
            applied["encryption_key"] = enc_key or "AQ=="
            changed = True

        if changed:
            node.writeChannel(0)
            logger.info(f"Канал записан: {applied}")
            time.sleep(TIMEOUT_OPERATION)
        else:
            logger.info("Канал: изменений нет")
    except Exception as exc:
        logger.error(f"Ошибка применения настроек канала: {exc}")
    return applied


def disable_secondary_channels(node) -> Tuple[bool, int]:
    """
    Отключает все вторичные каналы (индексы > 0), устанавливая role = 0.

    Для каждого такого канала вызывает ``node.writeChannel(idx)``.
    После всех записей делает паузу ``TIMEOUT_OPERATION`` секунд.

    Параметры:
        node — объект ``localNode`` Meshtastic.

    Returns:
        Кортеж ``(success: bool, disabled_count: int)``:
        - ``success``       — ``True`` при успешном выполнении.
        - ``disabled_count``— количество отключённых каналов.
    """
    try:
        ch = getattr(node, "channels", None)
        if not ch:
            return True, 0

        items = ch.items() if isinstance(ch, dict) else enumerate(ch)
        count = 0
        for idx, channel in items:
            if idx == 0 or not channel:
                continue
            if hasattr(channel, "role") and channel.role != 0:
                channel.role = 0
                node.writeChannel(idx)
                count += 1

        if count:
            logger.info(f"Отключено вторичных каналов: {count}")
            time.sleep(TIMEOUT_OPERATION)
        return True, count
    except Exception as exc:
        logger.error(f"Ошибка отключения вторичных каналов: {exc}")
        return False, 0


def read_device_settings(
    node,
    app_settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Читает текущие настройки устройства для последующего логирования.

    Используется после успешной записи всех параметров для верификации.

    Параметры:
        node         — объект ``localNode`` Meshtastic.
        app_settings — словарь настроек приложения (для добавления
                       ``channel_name_configured`` в результат).

    Returns:
        Словарь прочитанных значений. Возможные ключи:
        ``short_name``, ``long_name``, ``channel_name``, ``encryption_key``,
        ``region``, ``modem_preset``, ``frequency_slot``, ``hop_limit``,
        ``rebroadcast_mode``, ``smart_distance``, ``role``,
        ``channel_name_configured``.
    """
    info: Dict[str, Any] = {}
    try:
        if hasattr(node, "owner") and node.owner:
            info["short_name"] = getattr(node.owner, "short_name", "N/A")
            info["long_name"]  = getattr(node.owner, "long_name",  "N/A")

        primary = _get_primary_channel(node)
        if primary and hasattr(primary, "settings"):
            info["channel_name"] = getattr(primary.settings, "name", "N/A")
            psk = getattr(primary.settings, "psk", None)
            if psk:
                if isinstance(psk, bytes) and len(psk) == 1 and psk[0] == 1:
                    info["encryption_key"] = "AQ== (открытый)"
                else:
                    info["encryption_key"] = (
                        f"{base64.b64encode(psk).decode()[:10]}... (закрытый)"
                    )

        if hasattr(node, "localConfig") and node.localConfig:
            lora = node.localConfig.lora
            info["region"]       = _code_to_name(REGION_MAPPING, getattr(lora, "region", None)) or "N/A"
            info["modem_preset"] = _code_to_name(MODEM_PRESET_MAPPING, getattr(lora, "modem_preset", None)) or "N/A"
            info["frequency_slot"] = getattr(lora, "channel_num", "N/A")
            info["hop_limit"]    = getattr(lora, "hop_limit", "N/A")
            info["rebroadcast_mode"] = (
                _code_to_name(
                    REBROADCAST_MODE_MAPPING,
                    getattr(node.localConfig.device, "rebroadcast_mode", None),
                ) or "N/A"
            )
            info["smart_distance"] = getattr(
                node.localConfig.position,
                "broadcast_smart_minimum_distance",
                "N/A",
            )

        role_code = getattr(getattr(node, "owner", None), "role", None)
        if role_code is None:
            role_code = getattr(
                getattr(getattr(node, "localConfig", None), "device", None),
                "role", None,
            )
        info["role"] = _code_to_name(ROLE_MAPPING, role_code) or "N/A"

        if app_settings and "channel_name" in app_settings:
            info.setdefault("channel_name_configured", app_settings["channel_name"])

        logger.info(
            "Настройки устройства: " + ", ".join(f"{k}={v}" for k, v in info.items())
        )
    except Exception as exc:
        logger.error(f"Ошибка чтения настроек: {exc}")
    return info


def reboot_device(node) -> bool:
    """
    Отправляет устройству команду перезагрузки и ждёт ``TIMEOUT_REBOOT`` секунд.

    Параметры:
        node — объект ``localNode`` Meshtastic.

    Returns:
        ``True`` при успехе, ``False`` при исключении.
    """
    try:
        node.reboot()
        logger.info("Команда перезагрузки отправлена")
        time.sleep(TIMEOUT_REBOOT)
        return True
    except Exception as exc:
        logger.error(f"Ошибка перезагрузки: {exc}")
        return False


# ── Главная функция записи ────────────────────────────────────────────────────

def _open_node(com_port: str):
    """
    Открывает SerialInterface и возвращает пару ``(interface, localNode)``.

    Ожидает 1 секунду после подключения, чтобы прошивка успела
    передать начальные данные.

    Параметры:
        com_port — имя COM-порта (например, ``"COM3"``).

    Returns:
        Кортеж ``(SerialInterface, localNode)``.

    Raises:
        RuntimeError: если интерфейс или ``localNode`` не получены.
    """
    iface = meshtastic.serial_interface.SerialInterface(
        com_port, timeout=TIMEOUT_READ, noNodes=True,
    )
    if not iface or not iface.localNode:
        _close_interface(iface)
        raise RuntimeError("Не удалось подключиться: localNode недоступен")
    time.sleep(1)
    return iface, iface.localNode


def write_settings_to_device(
    device_data: Dict[str, Any],
    app_settings: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Полный цикл записи настроек на Meshtastic-устройство.

    Каждый шаг выполняется в **отдельном соединении**: интерфейс открывается,
    настройки записываются, интерфейс закрывается, затем следует пауза
    ``TIMEOUT_INTER_STEP`` секунд — за это время устройство успевает
    завершить аппаратный сброс, вызванный ``writeConfig`` / ``setOwner``.

    Последовательность шагов:
    1. Запись данных владельца (``setOwner``).
    2. Запись LoRa-настроек (``writeConfig("lora")``).
    3. Запись device / position-настроек.
    4. Отключение вторичных каналов.
    5. Запись primary-канала (имя + PSK).
    6. Финальное чтение через ``read_device_full`` для верификации.

    Принудительная перезагрузка ``reboot_device`` **не вызывается** — устройство
    само перезагружается после каждого важного ``writeConfig``.

    Функция выполняется синхронно, так как вызывается из ``Worker``-потока
    ``mainw.py``. Внутреннего потока нет.

    Параметры:
        device_data  — данные устройства:
                       ``short_name``, ``long_name``, ``role``, ``com_port``.
        app_settings — настройки из ``config.json`` (LoRa, канал и т.д.).

    Returns:
        Словарь:

        - ``success`` (bool)        — общий результат.
        - ``message`` (str)         — описание итога или ошибки.
        - ``applied`` (dict)        — фактически применённые параметры.
        - ``read_result`` (dict)    — результат ``read_device_full`` (при успехе).
        - ``elapsed_seconds`` (float) — время выполнения.
    """
    start = datetime.now()
    logger.info("=" * 60)
    logger.info("ЗАПИСЬ НАСТРОЕК В УСТРОЙСТВО")
    logger.info("=" * 60)

    if not MESHTASTIC_AVAILABLE:
        return {"success": False, "message": "Библиотека meshtastic не установлена"}

    ok, err = validate_short_name(device_data.get("short_name", ""))
    if not ok:
        return {"success": False, "message": err}

    com_port = device_data.get("com_port")
    if not com_port:
        return {"success": False, "message": "COM порт не указан"}

    applied: Dict[str, Any] = {}

    # Вспомогательная функция: выполняет один шаг записи и закрывает соединение
    def _step(step_num: int, step_name: str, fn, *args) -> bool:
        """
        Открывает соединение, применяет fn(node, *args), закрывает соединение.

        Пауза ``TIMEOUT_INTER_STEP`` выдерживается **только если** функция
        действительно что-то записала на устройство:

        - dict-результат непустой → была запись → ждём (устройство ребутится).
        - tuple-результат (``disable_secondary_channels``) → ждём только если
          хотя бы один канал был изменён (``count > 0``).
        - Пустой dict / count == 0 → запись не производилась → пропускаем паузу.

        Returns:
            True при успехе, False при исключении.
        """
        logger.info(f"── Шаг {step_num}/5: {step_name}...")
        iface   = None
        written = False  # флаг: была ли реальная запись на устройство
        try:
            iface, node = _open_node(com_port)
            result = fn(node, *args)
            if isinstance(result, dict):
                if result:
                    applied.update(result)
                    written = True
            elif isinstance(result, tuple):
                # disable_secondary_channels → (bool, int)
                ok_ch, disabled = result
                if ok_ch and disabled > 0:
                    applied["channels_disabled"] = disabled
                    written = True
        except Exception as exc:
            logger.error(f"Шаг {step_num} ({step_name}) — ОШИБКА: {exc}")
            logger.error(traceback.format_exc())
            return False
        finally:
            _close_interface(iface)

        if written:
            logger.info(
                f"Шаг {step_num} завершён — запись выполнена. "
                f"Ожидание {TIMEOUT_INTER_STEP} с (перезагрузка устройства)..."
            )
            time.sleep(TIMEOUT_INTER_STEP)
        else:
            logger.info(f"Шаг {step_num} завершён — изменений нет, пауза не нужна.")
        return True

    try:
        _step(1, "Настройки владельца",      apply_owner_settings,       device_data)
        _step(2, "LoRa-настройки",           apply_lora_settings,        app_settings)
        _step(3, "Device/Position-настройки", apply_device_settings,      app_settings, applied)
        _step(4, "Вторичные каналы",         disable_secondary_channels)
        _step(5, "Primary-канал",            apply_channel_settings,     app_settings)

    except Exception as exc:
        logger.error(f"КРИТИЧЕСКАЯ ОШИБКА: {exc}")
        logger.error(traceback.format_exc())
        elapsed = (datetime.now() - start).total_seconds()
        return {"success": False, "message": f"Ошибка: {str(exc)[:200]}",
                "applied": applied, "elapsed_seconds": elapsed}

    # ── Финальная верификация ─────────────────────────────────────────────────
    elapsed = (datetime.now() - start).total_seconds()
    logger.info("=" * 60)
    logger.info(f"Все шаги завершены за {elapsed:.1f} с. Проверка доступности устройства...")
    logger.info("=" * 60)

    read_result = read_device_full(com_port)

    if read_result.get("success"):
        logger.info("Устройство доступно. Настройки верифицированы:")
        cfg = read_result.get("config", {})
        for k, v in {**{
            "short_name": read_result.get("short_name"),
            "long_name":  read_result.get("long_name"),
            "role":       read_result.get("role"),
        }, **cfg}.items():
            logger.info(f"  {k}: {v}")
    else:
        logger.warning(
            f"Устройство недоступно после записи: {read_result.get('message', '?')}"
        )

    logger.info("=" * 60)
    logger.info(f"ИТОГ: {len(applied)} параметров записано за {elapsed:.1f} с")
    logger.info("=" * 60)

    return {
        "success":         True,
        "message":         f"Настройки записаны на {com_port}.",
        "applied":         applied,
        "read_result":     read_result if read_result.get("success") else None,
        "elapsed_seconds": elapsed,
    }


# Псевдоним для обратной совместимости
test_connection = test_device_connection
