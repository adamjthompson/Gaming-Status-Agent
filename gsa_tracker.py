import os
import sys
import json
import re
import time
import base64
import logging
import winreg
import threading
import ctypes
import ctypes.wintypes as wintypes
import urllib.request
from logging.handlers import RotatingFileHandler
from datetime import datetime
import tkinter as tk
from tkinter import messagebox, ttk, scrolledtext
import paho.mqtt.client as mqtt
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import pystray
from PIL import Image, ImageDraw
import psutil
 
# Keep in step with version_info.txt, which stamps the same numbers into the
# exe so Windows shows "Gaming Status Agent" rather than "Gaming Status Agent.exe".
GSA_VERSION = "1.1.9"

# --- GLOBALS & PATHS ---
client = None
observer = None
custom_tracker = None
CONFIG = {}
PROFILE_SANITIZED = "user"

# Guards every read-modify-write of GLOBAL_STATE. The watchdog observer thread,
# the poller thread and the MQTT network thread all publish state.
STATE_LOCK = threading.RLock()

GLOBAL_STATE = {
    "status": "idle",
    "game": "Offline",
    "launcher": "None",
    "start_time": "None"
}

# Only one instance of each settings window at a time; two open copies would
# both hold a stale CONFIG and the last save would silently win.
OPEN_WINDOWS = {}

# Tk does not hold a reference to an iconphoto, so the fallback window icon has
# to be kept alive here or it is garbage collected and the icon reverts.
_WINDOW_ICON = None

# Epic games currently believed to be running, keyed by Epic AppName:
#   {"title": str, "exe": str, "launched_at": float, "seen_running": bool}
# Epic's log reliably announces launches but not exits, so liveness is confirmed
# by the poller instead. More than one game can be running at a time.
EPIC_LOCK = threading.RLock()
EPIC_GAMES = {}

# The Ubisoft game named by the launcher log, as {"id": str, "title": str} or
# None. The log handler only records it; the poller decides what is published.
# Publishing straight from the log let the burst of product ids Ubisoft Connect
# writes while a game boots (DLC, other owned games, id 0) flap the sensor
# against the poller several times a second.
UBISOFT_LOCK = threading.Lock()
UBISOFT_GAME = None

# Installed GOG games, read from the registry at startup:
# GOG_BY_PATH maps a normalised full executable path -> game name (exact, used
# first); GOG_BY_NAME maps a bare executable name -> game name, for processes
# whose full path cannot be read.
GOG_BY_PATH = {}
GOG_BY_NAME = {}

# Installed Battle.net games: normalised install directory (with trailing
# separator) -> game name. Any process running from inside one is that game.
BATTLENET_BY_DIR = {}

# Installed Steam games, read from the .acf manifests at startup:
# STEAM_BY_DIR maps a normalised steamapps\common install directory -> name;
# STEAM_BY_APPID maps the appid string -> name, which is what the running-appid
# registry signal resolves through.
STEAM_BY_DIR = {}
STEAM_BY_APPID = {}

# Installed Xbox / Microsoft Store games: normalised package root -> name.
XBOX_BY_DIR = {}
# Store package roots that were NOT counted as games: root -> package full
# name. Diagnostics only, to explain why a running Store game was missed.
XBOX_OTHER_ROOTS = {}

# Rebuilt by start_services() from the ENABLE_* toggles. The static LAUNCHERS
# and EXCLUDED_ANCESTOR_EXES tables stay as written; these are what the
# ancestry scan actually consults, so turning a platform off takes effect on
# the next Save & Apply without editing any table.
ACTIVE_LAUNCHERS = {}
ACTIVE_EXCLUSIONS = set()

# Filled in by the Steam and Xbox scans so diagnostics can show whether they
# found anything, and where they looked.
STEAM_SCAN_INFO = {"root": "", "libraries": [], "games": 0}
XBOX_SCAN_INFO = {"packages": 0, "games": 0, "unnamed": 0}

# Filled in by get_ubisoft_info() so diagnostics can show whether the Ubisoft
# registry fallback actually found anything. It previously failed silently.
UBISOFT_REGISTRY_INFO = {"total": 0, "matched": 0, "added": 0,
                         "unmatched": [], "names": []}

if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "gsa_config.json")
DEBUG_LOG_FILE = os.path.join(BASE_DIR, "gsa_debug.log")
UBI_CACHE_FILE = os.path.join(BASE_DIR, "gsa_ubi_ids.json")
LOCAL_APPDATA = os.environ.get('LOCALAPPDATA', '')
PROGRAM_DATA = os.environ.get('PROGRAMDATA', '')

EPIC_LOG_DIR = os.path.join(LOCAL_APPDATA, "EpicGamesLauncher", "Saved", "Logs")
EPIC_MANIFEST_DIR = os.path.join(PROGRAM_DATA, "Epic", "EpicGamesLauncher", "Data", "Manifests")
EPIC_LOG_FILE = "EpicGamesLauncher.log"
UBI_LOG_FILE = "launcher_log.txt"

# Overridable via the CATALOG_URL config key for forks, self-hosted copies, and
# networks that block raw.githubusercontent.com. Config-file only by design:
# the common "this one game is named wrong" case is served by UBISOFT_OVERRIDES,
# so this does not belong in front of every user in the Settings window.
DEFAULT_CATALOG_URL = "https://raw.githubusercontent.com/adamjthompson/Gaming-Status-Agent/main/catalog.json"
MAX_CATALOG_BYTES = 4 * 1024 * 1024
MAX_TITLE_LEN = 255
MAX_ANCESTRY_DEPTH = 20
DPAPI_PREFIX = "dpapi:"

# How long to wait for a launched Epic game's process to appear before giving up
# on it. Shield/EAC/prerequisite installers can delay a first launch noticeably.
EPIC_STARTUP_GRACE = 180
# Consecutive polls a changed state must hold before the poller publishes it.
SETTLE_TICKS = 2

GENERIC_ACCOUNT_NAMES = {"administrator", "admin", "user", "default", "guest", "owner"}


def _default_device_name():
    """Seed the device name from the Windows account on first run.

    This ends up in the MQTT topic, the entity id and the HA device name, so it
    must never ship as one particular person's name. Taking it from the signed-in
    account means a fresh install is already personalised, and generic or service
    account names fall back to a placeholder that invites editing.
    """
    raw = (os.environ.get("USERNAME") or "").strip()
    if raw and raw.lower() not in GENERIC_ACCOUNT_NAMES:
        return raw
    return "Gamer"


DEFAULT_CONFIG = {
    "HA_DEVICE_NAME": _default_device_name(),
    # Blank rather than "YourEpicGamerTag": an unset profile falls back to the
    # device name via profile_for_launcher(), so a new user never sees a
    # placeholder published to Home Assistant.
    "EPIC_PROFILE_NAME": "",
    "UBISOFT_PROFILE_NAME": "",
    "GOG_PROFILE_NAME": "",
    "BATTLENET_PROFILE_NAME": "",
    "EA_PROFILE_NAME": "",
    "STEAM_PROFILE_NAME": "",
    "XBOX_PROFILE_NAME": "",
    # Per-platform switches, all editable from the tray under "Platforms".
    # Steam and Xbox default to off: each has an official Home Assistant
    # integration of its own, and turning them on here without asking would
    # silently publish a second, competing source for the same play session.
    # Custom defaults to off because a fresh install has no rules to run; it is
    # switched on automatically for a config that already has some (see
    # load_config), so an upgrade never loses working rules.
    "ENABLE_EPIC": True,
    "ENABLE_UBISOFT": True,
    "ENABLE_GOG": True,
    "ENABLE_BATTLENET": True,
    "ENABLE_EA": True,
    "ENABLE_AMAZON": True,
    "ENABLE_PLAYNITE": True,
    "ENABLE_CUSTOM": False,
    "ENABLE_STEAM": False,
    "ENABLE_XBOX": False,
    "MQTT_BROKER": "192.168.1.xxx",
    "MQTT_PORT": 1883,
    # Blank so a broker that allows anonymous access connects on first run.
    # Literal "username"/"password" were sent as real credentials and rejected.
    "MQTT_USER": "",
    "MQTT_PASS": "",
    "MQTT_TLS": False,
    "MQTT_CA_CERT": "",
    "POLL_INTERVAL": 5,
    "CATALOG_URL": DEFAULT_CATALOG_URL,
    "CUSTOM_GAMES": [],
    "UBISOFT_OVERRIDES": {}
}

# A launcher gets its own profile name only where the service has a distinct
# public identity. Steam and Xbox are detected from local data that carries no
# gamertag, so the field is the only way to publish one. Omitted on purpose:
# Amazon Games, because the account is a plain Amazon login with no gamertag;
# Playnite and Custom, because they are local with no account at all. Anything
# unlisted falls back to HA_DEVICE_NAME.
LAUNCHER_PROFILE_KEYS = {
    "Epic": "EPIC_PROFILE_NAME",
    "Ubisoft": "UBISOFT_PROFILE_NAME",
    "GOG": "GOG_PROFILE_NAME",
    "Battle.net": "BATTLENET_PROFILE_NAME",
    "EA": "EA_PROFILE_NAME",
    "Steam": "STEAM_PROFILE_NAME",
    "Xbox": "XBOX_PROFILE_NAME"
}

# Launcher label -> the config key that switches it on. Every detector consults
# this through platform_enabled(); a label missing from here is always on.
PLATFORM_ENABLE_KEYS = {
    "Epic": "ENABLE_EPIC",
    "Ubisoft": "ENABLE_UBISOFT",
    "GOG": "ENABLE_GOG",
    "Battle.net": "ENABLE_BATTLENET",
    "EA": "ENABLE_EA",
    "Amazon Games": "ENABLE_AMAZON",
    "Playnite": "ENABLE_PLAYNITE",
    "Steam": "ENABLE_STEAM",
    "Xbox": "ENABLE_XBOX",
    "Custom": "ENABLE_CUSTOM"
}

# Order shown in the Platforms window, and the order the poller tries sources in.
PLATFORM_ORDER = ("Epic", "Steam", "GOG", "Battle.net", "Xbox",
                  "Ubisoft", "EA", "Amazon Games", "Playnite", "Custom")


def platform_enabled(launcher_name):
    """True if this platform is switched on. Unknown labels are always on, so a
    detector added without a toggle keeps working rather than silently dying."""
    key = PLATFORM_ENABLE_KEYS.get(launcher_name)
    if not key:
        return True
    return bool(CONFIG.get(key, DEFAULT_CONFIG.get(key, True)))


def _clean_name(value):
    """A trimmed string, or "" for anything blank or not a string."""
    return value.strip() if isinstance(value, str) else ""


def profile_for_launcher(launcher_name):
    """Account name to publish for this launcher.

    Every payload builder goes through here. Two of them used to derive the
    profile independently and drifted apart, so the reconnect path published the
    device name where the normal path published the gamertag.

    Falls back to the device name when the launcher has no gamertag of its own,
    or when its field is blank, rather than publishing an empty string. Each
    candidate is trimmed first: a whitespace-only value is truthy, so a
    hand-edited "   " used to be published verbatim as the profile.

    "Unknown" is the last resort. It is reachable only from a config whose
    device name is blank, which load_config repairs, so it should never be seen.
    """
    key = LAUNCHER_PROFILE_KEYS.get(launcher_name)
    gamertag = _clean_name(CONFIG.get(key)) if key else ""
    return gamertag or _clean_name(CONFIG.get("HA_DEVICE_NAME")) or "Unknown"


EPIC_APP_RE = re.compile(r'-epicapp=(\w+)')
UBI_PRODUCT_RE = re.compile(r'product id (\d+)')
UBI_GAME_RE = re.compile(r'for game (\d+)')

LAUNCHERS = {
    "upc.exe": "Ubisoft",
    "ubisoftgamelauncher.exe": "Ubisoft",
    "ea.exe": "EA",
    "eadesktop.exe": "EA",
    # GOG Galaxy's process is GalaxyClient.exe. "gog galaxy.exe" is the install
    # folder, not an executable, and never matched anything; kept in case an
    # older client really does use it.
    "galaxyclient.exe": "GOG",
    "gog galaxy.exe": "GOG",
    "battle.net.exe": "Battle.net",
    # Amazon starts games from its service process, not from the UI:
    # game.exe <- Amazon Games Services.exe <- Amazon Games.exe. All three are
    # listed so the chain matches however the client is arranged.
    "amazon games.exe": "Amazon Games",
    "amazon games services.exe": "Amazon Games",
    "amazon games ui.exe": "Amazon Games",
    # Playnite launches everything it manages, including emulators, as its own
    # children, so ancestry catches retro and emulated titles too. Only the two
    # shell processes belong here; Playnite's browser helper launches nothing.
    "playnite.desktopapp.exe": "Playnite",
    "playnite.fullscreenapp.exe": "Playnite"
}

# A launcher's own windows are never a game. Helper and embedded-browser
# processes belong here too, and only here: they launch nothing, so listing one
# as a launcher would let its browser window be published as a game.
IGNORE_EXES = {
    "upc.exe", "ubisoftgamelauncher.exe", "epicgameslauncher.exe",
    "epicwebhelper.exe", "steam.exe", "steamwebhelper.exe",
    "ea.exe", "eadesktop.exe", "eabackgroundservice.exe",
    "gog galaxy.exe", "galaxyclient.exe", "galaxyclientservice.exe",
    "galaxycommunication.exe", "galaxyoverlay.exe",
    "battle.net.exe", "blizzardbrowser.exe", "uplaywebcore.exe",
    "steamservice.exe", "steamwebhelper.exe",
    "amazon games.exe", "amazon games ui.exe", "amazon games services.exe",
    "playnite.desktopapp.exe", "playnite.fullscreenapp.exe",
    "playnite.browserprocess.exe",
    "overlay64.exe", "gameoverlayui.exe",
    # Xbox / Microsoft Store shell and service processes. gamelaunchhelper.exe
    # matters most: it lives inside the game's own package folder, so without
    # this it would be published as the game by the install-directory match.
    "gamelaunchhelper.exe", "gamingservices.exe", "gamingservicesnet.exe",
    "gamebar.exe", "gamebarftserver.exe", "gamebarpresencewriter.exe",
    "xboxpcapp.exe", "xbox.exe", "xboxpcappft.exe", "gameinputsvc.exe",
    "xboxgamebarwidgets.exe"
}

# Executable names too generic to identify a game on their own. A GOG title
# whose exe is one of these is matched by full install path only.
GENERIC_EXE_NAMES = {
    "game.exe", "launcher.exe", "start.exe", "play.exe", "main.exe",
    "run.exe", "setup.exe", "config.exe", "client.exe", "app.exe"
}

# GOG Galaxy records every installed game here at install time.
GOG_REG_PATHS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\GOG.com\Games"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\GOG.com\Games"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\GOG.com\Games"),
)

# Battle.net has no per-game registry tree like GOG, but its installers write an
# ordinary uninstall entry per title carrying the name and install location.
UNINSTALL_REG_PATHS = (
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)
BLIZZARD_PUBLISHER_HINTS = ("blizzard", "activision")
BATTLENET_CLIENT_NAMES = {"battle.net", "blizzard battle.net", "blizzard app"}

# Epic is deliberately absent from LAUNCHERS: its games are named from the log,
# not from window titles. This set is only used as a liveness signal.
EPIC_LAUNCHER_EXES = {"epicgameslauncher.exe"}

# Substrings used only by diagnostics to decide which running processes are
# worth listing as launcher-related. These must be specific: a bare "ea" token
# matched searchhost.exe, securityhealthservice.exe and veeam.endpoint.exe,
# reporting Windows Search and backup software as unknown game launchers.
LAUNCHER_FAMILY_TOKENS = (
    "playnite", "amazon games", "epicgames", "epicwebhelper",
    "galaxyclient", "galaxycommunication", "galaxyoverlay", "gog galaxy",
    "battle.net", "blizzard", "steam", "ubisoft", "uplay", "upc.exe",
    "eadesktop", "eabackgroundservice", "origin",
    "xbox", "gamingservices", "gamelaunchhelper", "gamebar"
)

# Anything descended from these is never auto-detected while its platform is
# switched off, so Gaming Status Agent does not publish a competing state for
# games Home Assistant may already report natively. Steam's own HA integration
# reads the played game from the Steam Web API and works even while this machine
# is off. Enabling Steam clears this (see ACTIVE_EXCLUSIONS). Explicit Custom
# Games rules always apply: those are deliberate user configuration, not
# automatic detection.
EXCLUDED_ANCESTOR_EXES = {"steam.exe"}

BROWSER_SUFFIXES = (
    " - google chrome", " - mozilla firefox", " - microsoft edge",
    " - brave", " - opera", " - vivaldi", " - youtube", " - discord"
)

# Top-level windows that carry a title but are never what a person is looking
# at. D3DProxyWindow is created by the Direct3D runtime and sits alongside the
# real game window, so a game can easily be published under this name instead.
JUNK_WINDOW_TITLES = {
    "d3dproxywindow", "msctfime ui", "default ime", "gdi+ window",
    "olemainthreadwndname", "program manager", "windows input experience",
    "nvidia geforce overlay", "directx shader cache"
}

# Win32 constants for the standard "would this appear in Alt+Tab?" test.
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
GW_OWNER = 4


class _WindowPlacement(ctypes.Structure):
    """Carries rcNormalPosition: a window's restored size, which stays correct
    while the window is minimized."""
    _fields_ = [
        ("length", wintypes.UINT),
        ("flags", wintypes.UINT),
        ("showCmd", wintypes.UINT),
        ("ptMinPosition", wintypes.POINT),
        ("ptMaxPosition", wintypes.POINT),
        ("rcNormalPosition", wintypes.RECT)
    ]

# --- INIT TKINTER MAIN THREAD ---
ROOT = tk.Tk()
ROOT.withdraw()

# --- LOGGING ---
_logger = logging.getLogger("gsa")


def _init_logging():
    _logger.setLevel(logging.INFO)
    try:
        handler = RotatingFileHandler(
            DEBUG_LOG_FILE, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        _logger.addHandler(handler)
    except Exception:
        _logger.addHandler(logging.NullHandler())


def debug_log(message):
    try:
        _logger.info(message)
    except Exception:
        pass


# --- HELPER FUNCTIONS ---
def resource_path(relative_path):
    base_path = getattr(sys, '_MEIPASS', BASE_DIR)
    return os.path.join(base_path, relative_path)


def sanitize_topic_part(name):
    """MQTT topics cannot contain / + #, and an empty segment breaks discovery."""
    cleaned = re.sub(r'[^a-z0-9_]', '_', str(name).strip().lower().replace(" ", "_"))
    cleaned = cleaned.strip("_")
    return cleaned or "user"


def get_state_topic():
    return f"homeassistant/sensor/gsa_{PROFILE_SANITIZED}/state"


def get_config_topic():
    return f"homeassistant/sensor/gsa_{PROFILE_SANITIZED}/config"


# --- CREDENTIAL STORAGE (Windows DPAPI via ctypes, no extra dependency) ---
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_call(func, data):
    """Run CryptProtectData / CryptUnprotectData. Returns None if unavailable."""
    try:
        buffer = ctypes.create_string_buffer(data, len(data))
        blob_in = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DataBlob()
        func.restype = wintypes.BOOL
        if not func(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def encrypt_secret(plaintext):
    """Encrypt to a user+machine scoped blob. Falls back to plaintext if DPAPI fails."""
    if not plaintext:
        return ""
    blob = _dpapi_call(ctypes.windll.crypt32.CryptProtectData, plaintext.encode("utf-8"))
    if blob is None:
        debug_log("DPAPI encryption unavailable; storing MQTT password as plaintext.")
        return plaintext
    return DPAPI_PREFIX + base64.b64encode(blob).decode("ascii")


def decrypt_secret(stored):
    """Decrypt a stored secret. Plain values (first run, hand-edited) pass through."""
    if not isinstance(stored, str) or not stored.startswith(DPAPI_PREFIX):
        return stored if isinstance(stored, str) else ""
    try:
        blob = base64.b64decode(stored[len(DPAPI_PREFIX):])
    except Exception:
        return ""
    plain = _dpapi_call(ctypes.windll.crypt32.CryptUnprotectData, blob)
    if plain is None:
        # DPAPI blobs are scoped to this Windows user on this machine. A config
        # copied or cloud-synced from another PC cannot be decrypted here.
        debug_log("Could not decrypt stored MQTT password (different user or machine). "
                  "Re-enter it in Settings.")
        return ""
    try:
        return plain.decode("utf-8")
    except Exception:
        return ""


# --- CONFIG PERSISTENCE ---
def _coerce_int(value, default, low, high):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if low <= result <= high else default


def load_config():
    """Read config from disk, merged over defaults and coerced to safe values."""
    data = {}
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            data = loaded
        else:
            debug_log("Config file is not a JSON object; using defaults.")
    except FileNotFoundError:
        pass
    except Exception as e:
        debug_log(f"Could not read config ({e}); using defaults. Existing file left untouched.")

    merged = dict(DEFAULT_CONFIG)
    merged.update(data)

    # The device name is the MQTT topic, the entity id and the fallback profile.
    # Blank, whitespace or a non-string silently produced an empty Profile Name
    # and moved the sensor to the generic sensor.gsa_user topic, so it is
    # repaired here rather than being allowed downstream.
    device_name = _clean_name(merged.get("HA_DEVICE_NAME"))
    if not device_name:
        device_name = DEFAULT_CONFIG["HA_DEVICE_NAME"]
        debug_log(f"HA_DEVICE_NAME is blank; falling back to {device_name!r}.")
    merged["HA_DEVICE_NAME"] = device_name

    merged["MQTT_PORT"] = _coerce_int(merged.get("MQTT_PORT"), 1883, 1, 65535)
    merged["POLL_INTERVAL"] = _coerce_int(merged.get("POLL_INTERVAL"), 5, 1, 3600)
    merged["MQTT_TLS"] = bool(merged.get("MQTT_TLS"))

    # Custom ships off, but a config written before the toggle existed and
    # carrying rules was relying on them running. Switching it on here keeps
    # those working across the upgrade; once the key is present the user's own
    # choice is what counts, including turning it back off with rules defined.
    if "ENABLE_CUSTOM" not in data and data.get("CUSTOM_GAMES"):
        debug_log("Existing Custom Games rules found; enabling the Custom platform.")
        merged["ENABLE_CUSTOM"] = True

    # A hand-edited config can carry "false" or 0 here; bool("false") is True,
    # so the string forms are resolved before coercing.
    for enable_key in PLATFORM_ENABLE_KEYS.values():
        raw = merged.get(enable_key, DEFAULT_CONFIG[enable_key])
        if isinstance(raw, str):
            raw = raw.strip().lower() not in ("", "0", "false", "no", "off")
        merged[enable_key] = bool(raw)
    merged["MQTT_PASS"] = decrypt_secret(merged.get("MQTT_PASS", ""))

    # Only http(s) is fetchable here. Anything else - file://, ftp://, a bare
    # path, a non-string - silently becomes the default rather than being handed
    # to urlopen.
    catalog = merged.get("CATALOG_URL")
    catalog = catalog.strip() if isinstance(catalog, str) else ""
    if not catalog.lower().startswith(("https://", "http://")):
        if catalog:
            debug_log(f"Ignoring CATALOG_URL {catalog!r}: only http(s) URLs are allowed.")
        catalog = DEFAULT_CATALOG_URL
    merged["CATALOG_URL"] = catalog

    if not isinstance(merged.get("CUSTOM_GAMES"), list):
        debug_log("CUSTOM_GAMES is not a list; ignoring it.")
        merged["CUSTOM_GAMES"] = []
    merged["CUSTOM_GAMES"] = [g for g in merged["CUSTOM_GAMES"] if isinstance(g, dict)]

    if not isinstance(merged.get("UBISOFT_OVERRIDES"), dict):
        debug_log("UBISOFT_OVERRIDES is not an object; ignoring it.")
        merged["UBISOFT_OVERRIDES"] = {}

    return merged


def save_config(config=None):
    """Write config atomically so a crash mid-write cannot truncate the file."""
    source = CONFIG if config is None else config
    to_write = dict(source)
    to_write["MQTT_PASS"] = encrypt_secret(source.get("MQTT_PASS", ""))

    tmp_path = CONFIG_FILE + ".tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(to_write, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, CONFIG_FILE)
        return True
    except Exception as e:
        debug_log(f"Failed to save config: {e}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False


def get_epic_mapping():
    """Map Epic AppName -> {title, exe}. The executable comes from the manifest's
    LaunchExecutable and is what lets the poller tell when a game has exited."""
    mapping = {}
    if not os.path.exists(EPIC_MANIFEST_DIR):
        return mapping
    try:
        filenames = os.listdir(EPIC_MANIFEST_DIR)
    except OSError as e:
        debug_log(f"Could not list Epic manifests: {e}")
        return mapping
    for filename in filenames:
        if filename.endswith(".item"):
            try:
                with open(os.path.join(EPIC_MANIFEST_DIR, filename), 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get("AppName") and data.get("DisplayName"):
                    launch_exe = data.get("LaunchExecutable", "") or ""
                    mapping[data["AppName"]] = {
                        "title": data["DisplayName"],
                        "exe": os.path.basename(launch_exe.replace("\\", "/")).lower()
                    }
            except Exception:
                pass
    return mapping


def _reg_value(key, name):
    try:
        value, _ = winreg.QueryValueEx(key, name)
        return str(value).strip() if value else ""
    except OSError:
        return ""


def _enum_subkeys(hive, reg_path):
    """Yield (subkey_name, open_subkey) pairs, skipping anything unreadable."""
    try:
        with winreg.OpenKey(hive, reg_path) as parent:
            try:
                count = winreg.QueryInfoKey(parent)[0]
            except OSError:
                return
            names = []
            for i in range(count):
                try:
                    names.append(winreg.EnumKey(parent, i))
                except OSError:
                    continue
            for sub_name in names:
                try:
                    with winreg.OpenKey(parent, sub_name) as sub:
                        yield sub_name, sub
                except OSError:
                    continue
    except OSError:
        return


def _normalised_dir(path):
    """Absolute, case-folded, with a trailing separator so that a prefix test
    cannot match a sibling folder sharing a name prefix."""
    return os.path.join(os.path.normcase(os.path.abspath(path)), "")


def get_gog_mapping():
    """Map installed GOG games to their executables using the registry entries
    GOG Galaxy writes at install time.

    Matching on the process rather than on launcher ancestry matters here: GOG
    games are DRM free and are often started straight from a shortcut, with
    Galaxy never running at all.

    Returns (by_path, by_name).
    """
    by_path, by_name = {}, {}
    for hive, reg_path in GOG_REG_PATHS:
        for _, game_key in _enum_subkeys(hive, reg_path):
            try:
                name = _reg_value(game_key, "gameName")
                exe_path = _reg_value(game_key, "exe")
                exe_file = _reg_value(game_key, "exeFile")
                install_dir = _reg_value(game_key, "path")

                if not name:
                    continue
                if not exe_path and install_dir and exe_file:
                    exe_path = os.path.join(install_dir, exe_file)
                if exe_path:
                    by_path[os.path.normcase(os.path.abspath(exe_path))] = name

                base = os.path.basename((exe_file or exe_path or "").replace("\\", "/")).lower()
                if base and base not in GENERIC_EXE_NAMES:
                    by_name.setdefault(base, name)
            except Exception:
                continue

    debug_log(f"Found {len(by_path)} installed GOG games in the registry.")
    return by_path, by_name


def get_battlenet_mapping():
    """Map install directory -> Blizzard game name from Windows uninstall entries.

    Battle.net keeps no per-game registry tree the way GOG does, but every title
    it installs leaves an ordinary uninstall entry carrying a display name and an
    install location. Recognising a game by the folder its process runs from also
    works when the game was started outside the Battle.net client.
    """
    mapping = {}
    for hive, reg_path in UNINSTALL_REG_PATHS:
        for _, sub in _enum_subkeys(hive, reg_path):
            try:
                publisher = _reg_value(sub, "Publisher").lower()
                if not any(hint in publisher for hint in BLIZZARD_PUBLISHER_HINTS):
                    continue

                name = _reg_value(sub, "DisplayName")
                location = _reg_value(sub, "InstallLocation")
                if not name or not location:
                    continue
                # The launcher itself also has an uninstall entry.
                if name.strip().lower() in BATTLENET_CLIENT_NAMES:
                    continue
                if not os.path.isdir(location):
                    continue

                mapping[_normalised_dir(location)] = name[:MAX_TITLE_LEN]
            except Exception:
                continue

    debug_log(f"Found {len(mapping)} installed Battle.net games in the registry.")
    return mapping


def find_game_by_install_dir(processes, dir_map):
    """Return the name of a running game located inside a known install folder."""
    if not dir_map:
        return None
    for exe_name, exe_path in processes:
        if not exe_path or exe_name in IGNORE_EXES:
            continue
        try:
            resolved = os.path.normcase(os.path.abspath(exe_path))
        except Exception:
            continue
        for install_dir, name in dir_map.items():
            if resolved.startswith(install_dir):
                return name
    return None


def _clean_xbox_title(name):
    """Drop the platform suffix Store listings carry: "Doom Eternal - PC"."""
    return re.sub(r'\s*(?:-\s*PC|\(PC\)|for Windows(?: 10)?)\s*$', '', name,
                  flags=re.IGNORECASE).strip() or name


def find_xbox_games_folder_game(processes):
    """Name a game running from <drive>:\\XboxGames\\<Game>\\Content.

    The registry's PackageRootFolder for these points into WindowsApps, which
    is only a mount of the real install, so the process path never matches it.
    The Xbox app names the folder after the game's Store title, so the folder
    name is used, preferring the scanned display name when they agree.
    """
    names_by_folder = {_clean_xbox_title(n).lower(): n for n in XBOX_BY_DIR.values()}
    for exe_name, exe_path in processes:
        if not exe_path or exe_name in IGNORE_EXES:
            continue
        parts = os.path.normpath(exe_path).split(os.sep)
        lowered = [p.lower() for p in parts]
        if XBOX_GAMES_DIR_NAME not in lowered:
            continue
        index = lowered.index(XBOX_GAMES_DIR_NAME)
        # Needs a folder beneath the game folder, i.e. an actual file inside it.
        if index + 2 >= len(parts):
            continue
        folder = parts[index + 1]
        title = names_by_folder.get(_clean_xbox_title(folder).lower(), folder)
        return _clean_xbox_title(title)[:MAX_TITLE_LEN]
    return None


# (package root, exe name) -> whether that file exists, so the per-tick
# fallback below touches the disk once per pair rather than every poll.
_XBOX_EXE_CACHE = {}


def find_xbox_game(processes):
    """Return the name of a running Xbox / Store game, or None.

    Store games commonly run as protected processes whose path psutil cannot
    read, and find_game_by_install_dir skips those. For them, test whether a
    file of that name exists in the package root instead. os.path.exists on a
    known name works inside WindowsApps without elevation.
    """
    found = find_game_by_install_dir(processes, XBOX_BY_DIR)
    if found:
        return found
    found = find_xbox_games_folder_game(processes)
    if found or not XBOX_BY_DIR:
        return found
    for exe_name, exe_path in processes:
        if exe_path or not exe_name or exe_name in IGNORE_EXES:
            continue
        for root, name in XBOX_BY_DIR.items():
            key = (root, exe_name)
            if key not in _XBOX_EXE_CACHE:
                _XBOX_EXE_CACHE[key] = os.path.exists(os.path.join(root, exe_name))
            if _XBOX_EXE_CACHE[key]:
                return name
    return None


def find_gog_game(processes):
    """Return the name of a running GOG game, or None.

    processes is a list of (exe_name_lower, full_path_or_None).
    """
    for _, exe_path in processes:
        if exe_path:
            key = os.path.normcase(os.path.abspath(exe_path))
            if key in GOG_BY_PATH:
                return GOG_BY_PATH[key]

    # Elevated games hide their path from psutil, and a game moved after install
    # no longer matches by path, so fall back to the executable name.
    for exe_name, _ in processes:
        if exe_name in GOG_BY_NAME:
            return GOG_BY_NAME[exe_name]
    return None


# Steam's client registry. RunningAppID is the appid of the game Steam believes
# is running right now, and 0 when none is. Apps\<id>\Running is the per-game
# form of the same thing, and those keys also carry the display Name.
STEAM_REG_PATH = r"Software\Valve\Steam"
STEAM_APPS_REG_PATH = r"Software\Valve\Steam\Apps"

# libraryfolders.vdf and the .acf manifests are Valve's KeyValues format. Every
# field this needs is a flat "key" "value" pair, so a line regex reads them
# without pulling in a VDF parser. Nested blocks are simply not matched.
VDF_PAIR_RE = re.compile(r'^\s*"([^"]+)"\s+"([^"]*)"\s*$')

# Microsoft writes one subkey per installed package here, readable by the
# signed-in user. Program Files\WindowsApps itself is ACL-locked and cannot be
# listed without elevation, so this is how installed Store games are found.
APPMODEL_REG_PATH = (r"Software\Classes\Local Settings\Software\Microsoft"
                     r"\Windows\CurrentVersion\AppModel\Repository\Packages")

# Files that mark a package as a game rather than an ordinary Store app.
# MicrosoftGame.config is the GDK game manifest; gamelaunchhelper.exe is the
# shim every Game Pass PC title is launched through.
XBOX_GAME_MARKERS = ("MicrosoftGame.config", "gamelaunchhelper.exe")

# Xbox infrastructure packages, matched by package name (the part before the
# first "_"). Some ship a game marker, and their processes run constantly, so
# without this Gaming Services was reported in place of the real game.
XBOX_NON_GAME_PACKAGES = {
    "microsoft.gamingservices", "microsoft.gamingapp", "microsoft.xboxapp",
    "microsoft.xboxgamingoverlay", "microsoft.xboxgameoverlay",
    "microsoft.xboxidentityprovider", "microsoft.xboxspeechtotextoverlay",
    "microsoft.xbox.tcui", "microsoft.gameinput",
}

# System packages live under the Windows folder and are never games. Games are
# not confined to WindowsApps: the Xbox app installs them to <drive>:\XboxGames
# (or any folder the user picks), so the marker file is the real test.
WINDOWS_DIR = _normalised_dir(os.environ.get("SystemRoot", r"C:\Windows"))

# The Xbox app's default install folder. Only games are installed there, so a
# package under it counts even without a marker file; older Game Pass titles
# packaged before the GDK carry neither marker.
XBOX_GAMES_DIR_NAME = "xboxgames"


def _read_vdf_pairs(path):
    """Yield (key_lowercase, value) for every flat pair in a KeyValues file."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for raw_line in f:
                match = VDF_PAIR_RE.match(raw_line)
                if match:
                    yield match.group(1).lower(), match.group(2)
    except OSError:
        return


def get_steam_library_paths(steam_root):
    """Every Steam library folder on this machine, including the default one.

    Games are routinely installed to a second drive, so reading only the Steam
    install folder would miss most of a large collection.
    """
    libraries = []
    seen = set()

    def add(path):
        if not path:
            return
        try:
            resolved = os.path.normcase(os.path.abspath(path))
        except Exception:
            return
        if resolved not in seen and os.path.isdir(resolved):
            seen.add(resolved)
            libraries.append(path)

    add(steam_root)
    vdf_path = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    # In the current format each library is a block with its own "path"; the
    # older format numbered them "1", "2" and so on with the path as the value.
    for key, value in _read_vdf_pairs(vdf_path):
        if key == "path" or key.isdigit():
            add(value.replace("\\\\", "\\"))
    return libraries


def get_steam_mapping():
    """Map installed Steam games from the .acf manifests Steam writes per game.

    Returns (by_dir, by_appid). Reading the manifests rather than the Steam Web
    API means no API key, no internet and no public profile is needed, and a
    game is named correctly the moment it is installed.
    """
    by_dir, by_appid = {}, {}
    STEAM_SCAN_INFO.update({"root": "", "libraries": [], "games": 0})

    steam_root = ""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, STEAM_REG_PATH) as key:
                steam_root = _reg_value(key, "SteamPath") or _reg_value(key, "InstallPath")
        except OSError:
            continue
        if steam_root:
            break

    if not steam_root or not os.path.isdir(steam_root):
        debug_log("Steam does not appear to be installed; skipping its game scan.")
        return by_dir, by_appid

    STEAM_SCAN_INFO["root"] = steam_root
    libraries = get_steam_library_paths(steam_root)
    STEAM_SCAN_INFO["libraries"] = list(libraries)

    for library in libraries:
        steamapps = os.path.join(library, "steamapps")
        try:
            filenames = os.listdir(steamapps)
        except OSError:
            continue

        for filename in filenames:
            if not (filename.startswith("appmanifest_") and filename.endswith(".acf")):
                continue
            fields = dict(_read_vdf_pairs(os.path.join(steamapps, filename)))
            appid = fields.get("appid", "").strip()
            name = fields.get("name", "").strip()
            install_dir = fields.get("installdir", "").strip()
            if not name:
                continue

            name = name[:MAX_TITLE_LEN]
            if appid:
                by_appid[appid] = name
            if install_dir:
                by_dir[_normalised_dir(os.path.join(steamapps, "common", install_dir))] = name

    STEAM_SCAN_INFO["games"] = len(by_appid) or len(by_dir)
    debug_log(f"Found {len(by_appid)} installed Steam games across "
              f"{len(libraries)} librar{'y' if len(libraries) == 1 else 'ies'}.")
    return by_dir, by_appid


def find_steam_running_game():
    """Name of the Steam game Steam itself reports as running, or None.

    This is the authoritative signal: it is what Steam tells its own overlay and
    friends list, so it is right even for a game started from a desktop shortcut
    or installed outside steamapps\\common.
    """
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STEAM_REG_PATH) as key:
            running_appid = str(_reg_value(key, "RunningAppID") or "0").strip()
    except OSError:
        running_appid = "0"

    if running_appid and running_appid != "0":
        return STEAM_BY_APPID.get(running_appid) or f"Steam App {running_appid}"

    # RunningAppID is not written by every client build, so fall back to the
    # per-game Running flag. These keys also carry the name, which covers a
    # game that has no manifest in any library we could read.
    for appid, sub in _enum_subkeys(winreg.HKEY_CURRENT_USER, STEAM_APPS_REG_PATH):
        try:
            if _reg_value(sub, "Running") not in ("1", "True"):
                continue
            name = _reg_value(sub, "Name") or STEAM_BY_APPID.get(appid.strip())
            if name:
                return name[:MAX_TITLE_LEN]
            return f"Steam App {appid.strip()}"
        except Exception:
            continue
    return None


def _load_indirect_string(value):
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        result = ctypes.windll.shlwapi.SHLoadIndirectString(
            ctypes.c_wchar_p(value), buffer, len(buffer), None)
        if result == 0 and buffer.value.strip():
            return buffer.value.strip()
    except Exception:
        pass
    return ""


def _resolve_indirect_string(value, package_full_name=""):
    """Resolve a localised Store display name to its literal title.

    Store packages usually store a resource reference rather than the title,
    either as @{PackageFullName?ms-resource://...} or as a bare
    "ms-resource:Name" relative to the package. Returns "" when it cannot be
    resolved, so the caller falls back rather than publishing the raw URI.
    """
    if value.startswith("@{"):
        return _load_indirect_string(value)
    if not value.lower().startswith("ms-resource:"):
        return value
    if not package_full_name:
        return ""

    resource = value[len("ms-resource:"):].lstrip("/")
    package_name = package_full_name.split("_")[0]
    if resource.lower().startswith(package_name.lower() + "/"):
        candidates = [f"ms-resource://{resource}"]
    elif "/" in resource:
        candidates = [f"ms-resource:///{resource}"]
    else:
        # Bare names usually live in the Resources map, occasionally at the root.
        candidates = [f"ms-resource://{package_name}/Resources/{resource}",
                      f"ms-resource:///Resources/{resource}",
                      f"ms-resource://{package_name}/{resource}"]
    for uri in candidates:
        resolved = _load_indirect_string(f"@{{{package_full_name}?{uri}}}")
        if resolved and not resolved.lower().startswith("ms-resource:"):
            return resolved
    return ""


def _package_family_title(package_full_name):
    """Last-resort title from a package name: 'Publisher.SomeGame_1.0_x64__hash'
    becomes 'SomeGame'. Better on the sensor than a raw package identifier."""
    stem = package_full_name.split("_")[0]
    leaf = stem.split(".")[-1] if "." in stem else stem
    spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', leaf).strip()
    return (spaced or leaf)[:MAX_TITLE_LEN]


def get_xbox_mapping():
    """Map installed Xbox / Microsoft Store games to their package folders.

    Returns {normalised package root -> game name}, in the same shape as the
    Battle.net map so find_game_by_install_dir() can consume it unchanged.

    Only packages carrying a game marker file are kept, which is what stops
    Calculator, Photos and every other Store app from being reported as a game.
    """
    mapping = {}
    packages = 0
    unnamed = 0
    XBOX_OTHER_ROOTS.clear()
    _XBOX_EXE_CACHE.clear()

    for package_full_name, sub in _enum_subkeys(winreg.HKEY_CURRENT_USER, APPMODEL_REG_PATH):
        packages += 1
        try:
            if package_full_name.split("_")[0].lower() in XBOX_NON_GAME_PACKAGES:
                continue
            root = _reg_value(sub, "PackageRootFolder")
            if not root:
                continue

            # System packages are skipped before touching the disk at all.
            if _normalised_dir(root).startswith(WINDOWS_DIR):
                continue

            if not os.path.isdir(root):
                continue
            # Every other package root is kept, unnamed, so diagnostics can say
            # which package a running game belongs to when it was not counted.
            XBOX_OTHER_ROOTS[_normalised_dir(root)] = package_full_name

            # os.path.exists on a known filename succeeds inside WindowsApps
            # where listing the directory does not, so no elevation is needed.
            parts = {part.lower() for part in os.path.normpath(root).split(os.sep)}
            if XBOX_GAMES_DIR_NAME not in parts and not any(
                    os.path.exists(os.path.join(root, marker))
                    for marker in XBOX_GAME_MARKERS):
                continue
            del XBOX_OTHER_ROOTS[_normalised_dir(root)]

            name = _resolve_indirect_string(_reg_value(sub, "DisplayName"),
                                            package_full_name)
            if not name:
                name = _package_family_title(package_full_name)
                unnamed += 1

            mapping[_normalised_dir(root)] = _clean_xbox_title(name)[:MAX_TITLE_LEN]
        except Exception:
            continue

    XBOX_SCAN_INFO.update({"packages": packages, "games": len(mapping), "unnamed": unnamed})
    debug_log(f"Found {len(mapping)} installed Xbox/Store games "
              f"among {packages} packages.")
    return mapping


UPLAY_KEY_PREFIX = "uplay install "
# The launcher's own uninstall entry, not a game.
UBISOFT_CLIENT_NAMES = {"uplay", "ubisoft connect", "ubisoft game launcher"}


def get_ubisoft_registry_names():
    """Map Uplay game id -> display name from Windows uninstall entries.

    Ubisoft Connect registers each installed game as "Uplay Install <id>". This
    used to scan only the 32-bit HKLM view with every error silently swallowed,
    so a miss was indistinguishable from there being nothing to find.

    Returns (mapping, unmatched) where unmatched lists Ubisoft-published entries
    that did NOT fit the expected pattern. That list is what would reveal a
    changed naming scheme, rather than leaving it to guesswork.
    """
    mapping = {}
    unmatched = []
    for hive, reg_path in UNINSTALL_REG_PATHS:
        for sub_name, sub in _enum_subkeys(hive, reg_path):
            try:
                if sub_name.lower().startswith(UPLAY_KEY_PREFIX):
                    game_id = sub_name[len(UPLAY_KEY_PREFIX):].strip()
                    display_name = _reg_value(sub, "DisplayName")
                    if not game_id:
                        continue
                    if not display_name:
                        debug_log(f"Uninstall entry {sub_name!r} has no DisplayName; skipped.")
                        continue
                    mapping.setdefault(game_id, str(display_name)[:MAX_TITLE_LEN])
                elif "ubisoft" in _reg_value(sub, "Publisher").lower():
                    # Publisher alone is far too loose: a Ubisoft-published game
                    # bought on Steam, GOG or Epic also matches, and reporting
                    # those as "possibly missed" sends people chasing nothing.
                    # A genuine Ubisoft Connect entry is uninstalled by Ubisoft's
                    # own uninstaller, so key on that instead.
                    uninstall = _reg_value(sub, "UninstallString").lower()
                    if "uplay" not in uninstall and "ubisoft" not in uninstall:
                        continue
                    names = {sub_name.strip().lower(),
                             _reg_value(sub, "DisplayName").strip().lower()}
                    if names & UBISOFT_CLIENT_NAMES:
                        continue
                    unmatched.append(sub_name)
            except Exception:
                continue
    return mapping, unmatched


def _flatten_catalog(raw_data):
    """Catalog is {category: {id: title}}, with a legacy flat {id: title} form."""
    mapping = {}
    if not isinstance(raw_data, dict):
        raise ValueError("catalog root is not an object")
    for category, games in raw_data.items():
        if isinstance(games, dict):
            for game_id, title in games.items():
                mapping[str(game_id)] = str(title)[:MAX_TITLE_LEN]
        else:
            mapping[str(category)] = str(games)[:MAX_TITLE_LEN]
    return mapping


def get_ubisoft_info():
    mapping = {}
    log_dir = r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\logs"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Ubisoft\Launcher") as key:
            install_dir, _ = winreg.QueryValueEx(key, "InstallDir")
            log_dir = os.path.join(install_dir, "logs")
    except Exception:
        pass

    # 1. Fetch Latest Community Database
    catalog_url = CONFIG.get("CATALOG_URL") or DEFAULT_CATALOG_URL
    try:
        req = urllib.request.Request(
            catalog_url,
            headers={'User-Agent': 'Gaming-Status-Agent/1.0'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            # Bounded read: never pull an unbounded remote body into memory.
            content = response.read(MAX_CATALOG_BYTES + 1)
            if len(content) > MAX_CATALOG_BYTES:
                raise ValueError("catalog exceeds size limit")
            mapping = _flatten_catalog(json.loads(content.decode('utf-8')))

        if mapping:
            try:
                with open(UBI_CACHE_FILE, 'w', encoding='utf-8') as f:
                    json.dump(mapping, f, indent=4)
            except Exception as e:
                debug_log(f"Could not write Ubisoft cache: {e}")
            debug_log(f"Successfully downloaded remote Ubisoft database ({len(mapping)} games).")

    except Exception as e:
        debug_log(f"Failed to fetch Ubisoft catalog from {catalog_url}: {e}")
        if os.path.exists(UBI_CACHE_FILE):
            try:
                with open(UBI_CACHE_FILE, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                if isinstance(cached, dict):
                    mapping = {str(k): str(v)[:MAX_TITLE_LEN] for k, v in cached.items()}
            except Exception:
                pass

    # 2. Add Windows Uninstall Registry as a backup.
    # start_services() calls this function unprotected, so a registry surprise
    # here must never prevent Gaming Status Agent from starting.
    try:
        reg_names, unmatched = get_ubisoft_registry_names()
    except Exception as e:
        debug_log(f"Ubisoft registry scan failed: {e}")
        reg_names, unmatched = {}, []
    added = sum(1 for game_id in reg_names if game_id not in mapping)
    for game_id, display_name in reg_names.items():
        mapping.setdefault(game_id, display_name)

    UBISOFT_REGISTRY_INFO["matched"] = len(reg_names)
    UBISOFT_REGISTRY_INFO["added"] = added
    UBISOFT_REGISTRY_INFO["unmatched"] = unmatched
    UBISOFT_REGISTRY_INFO["names"] = sorted(
        f"{game_id} = {name}" for game_id, name in reg_names.items())
    debug_log(f"Ubisoft registry: {len(reg_names)} 'Uplay Install' entries found, "
              f"{added} added to the mapping.")
    if unmatched:
        # Names the Ubisoft-published entries that did not fit the expected
        # pattern, which is the only way to discover a changed naming scheme.
        debug_log(f"Ubisoft-published uninstall entries not matching "
                  f"'Uplay Install <id>': {', '.join(unmatched[:10])}")

    # 3. Add Custom Config Overrides (Manual Fallback)
    overrides = CONFIG.get("UBISOFT_OVERRIDES", {})
    for str_id, name in overrides.items():
        mapping[str(str_id)] = str(name)[:MAX_TITLE_LEN]

    UBISOFT_REGISTRY_INFO["total"] = len(mapping)
    return log_dir, mapping


def get_active_window_titles():
    user32 = ctypes.windll.user32

    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.POINTER(_WindowPlacement)]
    user32.GetWindowPlacement.restype = wintypes.BOOL

    titles = []

    def foreach_window(hwnd, lParam):
        if not user32.IsWindowVisible(hwnd):
            return True

        # The standard Alt+Tab test: a real application window has no owner and
        # is not a tool window. Direct3D proxies and other helper windows fail
        # it, which is what kept D3DProxyWindow out of the running.
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if (ex_style & WS_EX_TOOLWINDOW) and not (ex_style & WS_EX_APPWINDOW):
            return True

        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buff = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buff, length + 1)
        title = buff.value
        if title.strip().lower() in JUNK_WINDOW_TITLES:
            return True

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        # A minimized window reports a meaningless off-screen rect a few dozen
        # pixels across. Alt-tabbing out of a game would otherwise shrink it
        # below the other candidates and change which title gets published, so
        # use the restored size instead.
        if user32.IsIconic(hwnd):
            placement = _WindowPlacement()
            placement.length = ctypes.sizeof(_WindowPlacement)
            if user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
                rect = placement.rcNormalPosition

        width = rect.right - rect.left
        height = rect.bottom - rect.top
        if width <= 1 or height <= 1:
            return True

        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

        titles.append({"title": title, "pid": pid.value, "area": width * height})
        return True

    try:
        user32.EnumWindows(EnumWindowsProc(foreach_window), 0)
    except Exception as e:
        debug_log(f"EnumWindows failed: {e}")
    return titles


def publish_global_state(status, game_title, launcher_name, wait=False):
    global GLOBAL_STATE

    game_title = str(game_title)[:MAX_TITLE_LEN]

    with STATE_LOCK:
        if (GLOBAL_STATE["game"] == game_title
                and GLOBAL_STATE["status"] == status
                and GLOBAL_STATE["launcher"] == (launcher_name if status == "playing" else "None")):
            return

        # Derive the profile from the launcher we are actually going to publish,
        # not from the argument, so Launcher and Profile Name always agree.
        effective_launcher = launcher_name if status == "playing" else "None"
        active_profile = profile_for_launcher(effective_launcher)

        start_time = GLOBAL_STATE["start_time"]
        # Restart the clock whenever a different game begins, not only on
        # idle -> playing. Switching games directly would otherwise inherit
        # the previous game's start time.
        if status == "playing" and (GLOBAL_STATE["status"] == "idle"
                                    or GLOBAL_STATE["game"] != game_title
                                    or start_time == "None"):
            start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        end_time = "None"
        if status == "idle":
            end_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            start_time = "None"

        GLOBAL_STATE = {
            "status": status,
            "game": game_title,
            "launcher": effective_launcher,
            "start_time": start_time
        }

        payload = {
            "Profile Name": active_profile,
            "Game Title": game_title,
            "Launcher": GLOBAL_STATE["launcher"],
            "Start Time": start_time,
            "End Time": end_time
        }

    debug_log(f"Attempting to publish via MQTT: {game_title} on {launcher_name}")
    _publish_payload(payload, wait=wait)


def _offline_payload():
    return {
        "Profile Name": profile_for_launcher("None"),
        "Game Title": "Offline",
        "Launcher": "None",
        "Start Time": "None",
        "End Time": "None"
    }


def _publish_payload(payload, wait=False):
    if not client:
        return
    try:
        # QoS 1: a retained QoS 0 state message is dropped outright if the
        # link is down, which leaves Home Assistant showing a stale game.
        info = client.publish(get_state_topic(), json.dumps(payload), qos=1, retain=True)
        if wait:
            info.wait_for_publish(timeout=2)
        debug_log("MQTT Publish Successful")
    except Exception as e:
        debug_log(f"MQTT Publish Failed: {e}")


def epic_active_title():
    """The most recently launched Epic game still believed to be running."""
    with EPIC_LOCK:
        if not EPIC_GAMES:
            return None
        newest = max(EPIC_GAMES.values(), key=lambda i: i["launched_at"])
        return newest["title"]


def register_epic_launch(app_id, title, exe):
    with EPIC_LOCK:
        EPIC_GAMES[app_id] = {
            "title": title,
            "exe": exe,
            "launched_at": time.monotonic(),
            "seen_exe": False,
            "seen_running": False
        }


def reconcile_epic_games(running_exes, epic_window_alive):
    """Drop Epic games whose process is gone. Returns True if anything changed.

    Epic's log announces launches but not exits, so liveness is confirmed here.
    A game's own executable is the authoritative signal once it has been seen -
    the launcher-descended-window signal is shared by every running Epic game,
    so relying on it would let one open game keep a closed one alive forever.
    It is only used for titles whose executable is unknown, or that start the
    real game from a short-lived helper process.
    """
    now = time.monotonic()
    changed = False
    with EPIC_LOCK:
        for app_id, info in list(EPIC_GAMES.items()):
            if info["exe"] and info["exe"] in running_exes:
                info["seen_exe"] = True
                info["seen_running"] = True
                continue

            if info["seen_exe"]:
                debug_log(f"Epic game exited: {info['title']}")
                del EPIC_GAMES[app_id]
                changed = True
            elif epic_window_alive:
                info["seen_running"] = True
            elif info["seen_running"]:
                debug_log(f"Epic game exited: {info['title']}")
                del EPIC_GAMES[app_id]
                changed = True
            elif now - info["launched_at"] > EPIC_STARTUP_GRACE:
                debug_log(f"Epic game never started within {EPIC_STARTUP_GRACE}s, "
                          f"dropping: {info['title']}")
                del EPIC_GAMES[app_id]
                changed = True
    return changed


def set_ubisoft_game(game):
    global UBISOFT_GAME
    with UBISOFT_LOCK:
        UBISOFT_GAME = game


def ubisoft_active_title():
    with UBISOFT_LOCK:
        return UBISOFT_GAME["title"] if UBISOFT_GAME else None


def republish_current_state():
    """Re-send discovery and state after a reconnect; HA may have restarted too."""
    with STATE_LOCK:
        state = dict(GLOBAL_STATE)
    payload = {
        # Must match what publish_global_state would send for this same state:
        # hardcoding the device name here reverted the profile on every reconnect.
        "Profile Name": profile_for_launcher(state["launcher"]),
        "Game Title": state["game"],
        "Launcher": state["launcher"],
        "Start Time": state["start_time"],
        "End Time": "None"
    }
    _publish_payload(payload)


# --- EPIC & UBISOFT LOG HANDLER ---
class GameLogHandler(FileSystemEventHandler):
    def __init__(self, launcher_name, log_file_path, games_mapping):
        self.launcher_name = launcher_name
        self.log_file_path = log_file_path
        self.target_path = os.path.normcase(os.path.abspath(log_file_path))
        # Epic supplies {id: {title, exe}}; Ubisoft supplies {id: "Title"}.
        self.games_mapping = {
            str(k): (v if isinstance(v, dict) else {"title": str(v), "exe": ""})
            for k, v in (games_mapping or {}).items()
        }
        self.last_position = 0
        self.current_game_id = None

        if os.path.exists(self.log_file_path):
            self.last_position = os.path.getsize(self.log_file_path)

    def _is_target(self, path):
        # A plain endswith() also matches files like "old_launcher_log.txt",
        # which would then be read at this handler's byte offset.
        return os.path.normcase(os.path.abspath(path)) == self.target_path

    def on_created(self, event):
        if not event.is_directory and self._is_target(event.src_path):
            # Start at the end, not byte 0 -- a replacement log can already
            # have content by the time this event arrives, and any of it
            # would be historical. Mirrors the constructor's own seek-to-EOF.
            try:
                self.last_position = os.path.getsize(event.src_path)
            except OSError:
                self.last_position = 0

    def on_modified(self, event):
        if event.is_directory or not self._is_target(event.src_path):
            return
        try:
            # Launchers rotate and truncate their logs. Without this reset the
            # offset stays past EOF and detection stops until a restart.
            size = os.path.getsize(event.src_path)
            if size < self.last_position:
                debug_log(
                    f"{self.launcher_name} log truncated or rotated; resuming from end."
                )
                # Resume at the NEW end of file, NOT byte 0. Re-reading a
                # rotated log from the start replays every historical
                # game-start line in it, which publishes a burst of phantom
                # start/close events for games that ran days ago -- observed
                # live as 20+ retained MQTT publishes inside a single second,
                # cycling through titles nobody had launched. Ubisoft Connect
                # rotates this file every time it starts, so that fired on
                # every login and self-update. Only lines written from here
                # on describe the current state; skipping the handful that
                # landed between the rotation and this event is the correct
                # trade.
                self.last_position = size

            with open(event.src_path, 'r', encoding='utf-8', errors='ignore') as f:
                f.seek(self.last_position)
                new_lines = f.readlines()
                self.last_position = f.tell()
                for line in new_lines:
                    self.process_line(line)
        except Exception as e:
            debug_log(f"Error reading {self.launcher_name} log: {e}")

    def process_line(self, line):
        if self.launcher_name == "Epic":
            self.process_epic_line(line)
        elif self.launcher_name == "Ubisoft":
            self.process_ubisoft_line(line)

    def process_epic_line(self, line):
        if "FCommunityPortalLaunchAppTask: Launching app" in line:
            match = EPIC_APP_RE.search(line)
            if not match:
                return
            app_id = match.group(1)
            entry = self.games_mapping.get(app_id, {})
            title = entry.get("title") or "Unknown Epic Game"
            exe = entry.get("exe", "")
            register_epic_launch(app_id, title, exe)
            if not exe:
                debug_log(f"No launch executable in the Epic manifest for {title}; "
                          f"falling back to window ancestry to detect its exit.")
            debug_log(f"Epic Launcher detected game start: {title}")
            publish_global_state("playing", title, self.launcher_name)
            return

        if "Process has exited" in line or "FCommunityPortalLaunchAppTask: Finished" in line:
            # These strings are not present in every launcher build, and the line
            # rarely names the app. Only act when it unambiguously identifies one
            # game; otherwise leave it to the poller's liveness check, so that
            # closing one game cannot knock a second running game offline.
            with EPIC_LOCK:
                matched = [app_id for app_id in EPIC_GAMES if app_id in line]
            if not matched:
                debug_log("Epic close line seen but it names no app; "
                          "deferring to the process liveness check.")
                return
            with EPIC_LOCK:
                for app_id in matched:
                    info = EPIC_GAMES.pop(app_id, None)
                    if info:
                        debug_log(f"Epic Launcher detected game close: {info['title']}")
            remaining = epic_active_title()
            if remaining:
                publish_global_state("playing", remaining, self.launcher_name)
            else:
                publish_global_state("idle", "Offline", "None")

    def process_ubisoft_line(self, line):
        if "started with product id" in line or "successfully started for game" in line:
            match = UBI_PRODUCT_RE.search(line) or UBI_GAME_RE.search(line)
            if not match or match.group(1) == "0":
                return
            game_id = match.group(1)
            title = self.games_mapping.get(game_id, {}).get("title")
            with UBISOFT_LOCK:
                current = UBISOFT_GAME
            # An unmapped id logged while a known game is starting is almost
            # always a DLC or entitlement check, not a second game.
            if not title and current and current["id"] in self.games_mapping:
                debug_log(f"Ubisoft log named unmapped product {game_id} while "
                          f"{current['title']} is active; ignoring it.")
                return
            title = title or f"Unknown Ubisoft Game ({game_id})"
            self.current_game_id = game_id
            set_ubisoft_game({"id": game_id, "title": title})
            debug_log(f"Ubisoft Launcher detected game start: {title}")

        elif self.current_game_id and ("Game process ended" in line or "successfully deleted for game" in line):
            debug_log("Ubisoft Launcher detected game close")
            set_ubisoft_game(None)
            self.current_game_id = None


# --- UNIVERSAL PROCESS ANCESTRY TRACKER ---
def ancestor_names(pid, snapshot):
    """Yield the exe names of pid's ancestors, walking a pre-built snapshot
    instead of making per-window psutil calls."""
    info = snapshot.get(pid)
    if not info:
        return
    seen = {pid}
    ppid = info[0]
    child_created = info[2]
    depth = 0
    while ppid and ppid not in seen and depth < MAX_ANCESTRY_DEPTH:
        parent = snapshot.get(ppid)
        if not parent:
            return
        # Windows reuses PIDs. A "parent" created after its child is an
        # unrelated process that inherited a dead parent's PID; following it
        # credited a Notepad window to whatever launcher owned the new PID.
        if parent[2] and child_created and parent[2] > child_created:
            return
        yield parent[1]
        seen.add(ppid)
        ppid = parent[0]
        child_created = parent[2]
        depth += 1


def find_launcher_ancestor(pid, snapshot):
    info = snapshot.get(pid)
    if not info or info[1] in IGNORE_EXES:
        return None
    for name in ancestor_names(pid, snapshot):
        # Checked before the launcher table so that the nearest ancestor wins.
        # While Steam is switched off, a Steam game started through Playnite has
        # Steam below Playnite in the chain and must stay excluded rather than
        # being reported as a Playnite game.
        if name in ACTIVE_EXCLUSIONS:
            return None
        if name in ACTIVE_LAUNCHERS:
            return ACTIVE_LAUNCHERS[name]
    return None


def descends_from_epic(pid, snapshot):
    """True if this window belongs to a game started by the Epic launcher."""
    info = snapshot.get(pid)
    if not info or info[1] in IGNORE_EXES:
        return False
    return any(name in EPIC_LAUNCHER_EXES for name in ancestor_names(pid, snapshot))


def snapshot_processes():
    """One process enumeration per call, shared by the poller and diagnostics.

    Returns (snapshot, processes, running_exes) where snapshot maps
    pid -> (ppid, exe_name, create_time) and processes is a list of
    (exe_name, full_path). 'exe' is None where the path cannot be read, and
    create_time is 0 where it cannot; psutil does not raise for either.
    """
    snapshot = {}
    processes = []
    running_exes = set()
    try:
        for proc in psutil.process_iter(['pid', 'ppid', 'name', 'exe', 'create_time']):
            info = proc.info
            name = (info.get('name') or "").lower()
            if info.get('pid') is not None:
                snapshot[info['pid']] = (info.get('ppid'), name,
                                         info.get('create_time') or 0)
            if name:
                running_exes.add(name)
                processes.append((name, info.get('exe')))
    except Exception as e:
        debug_log(f"Could not enumerate processes: {e}")
    return snapshot, processes, running_exes


# Platforms whose detector needs the process list. Epic needs it to notice a
# game has exited; the rest match a running process against an install folder
# or executable. Used to decide whether a poll needs to enumerate processes at
# all, so a machine with everything switched off does no work per tick.
PROCESS_BACKED_PLATFORMS = ("Epic", "Steam", "GOG", "Battle.net", "Xbox", "Custom")


def poll_work_needed():
    """What this tick actually has to collect, given the enabled platforms.

    Returns (needs_windows, needs_processes). Enumerating top-level windows and
    walking every process are by far the most expensive things the poller does,
    so neither runs unless some enabled source consumes the result.
    """
    # Window titles feed the ancestry scan, the Custom window-title rules, and
    # Epic's "is a launcher-descended window still open" liveness check.
    needs_windows = bool(ACTIVE_LAUNCHERS) or platform_enabled("Custom") \
        or platform_enabled("Epic")
    needs_processes = bool(ACTIVE_LAUNCHERS) or any(
        platform_enabled(name) for name in PROCESS_BACKED_PLATFORMS)
    return needs_windows, needs_processes


def resolve_named_sources(processes, epic_title):
    """Every platform that can name a running game, in priority order.

    Returns a list of (launcher_label, title_or_None), skipping platforms that
    are switched off. The poller and the diagnostics report both walk this, so
    the report can never disagree with what would actually be published.

    Epic leads because its log names the game outright. Steam is next because
    RunningAppID is an exact signal from the client itself. Xbox sits below the
    registry-backed sources because a package root match is the broadest test
    here and should not outrank a precise one. Ubisoft follows Steam: a Ubisoft
    game bought on Steam also starts Ubisoft Connect, whose log names several
    products while it boots, and Steam's RunningAppID is the exact answer.
    """
    sources = [
        ("Epic", lambda: epic_title),
        ("Steam", lambda: find_steam_running_game()
            or find_game_by_install_dir(processes, STEAM_BY_DIR)),
        ("Ubisoft", ubisoft_active_title),
        ("GOG", lambda: find_gog_game(processes) if (GOG_BY_PATH or GOG_BY_NAME) else None),
        ("Battle.net", lambda: find_game_by_install_dir(processes, BATTLENET_BY_DIR)),
        ("Xbox", lambda: find_xbox_game(processes)),
    ]

    resolved = []
    for launcher_name, resolver in sources:
        if not platform_enabled(launcher_name):
            continue
        try:
            resolved.append((launcher_name, resolver()))
        except Exception as e:
            # One platform's registry or filesystem going wrong must not stop
            # the others from reporting.
            debug_log(f"{launcher_name} detection failed this tick: {e}")
            resolved.append((launcher_name, None))
    return resolved


class CustomGameTracker(threading.Thread):
    def __init__(self):
        super().__init__()
        self.stop_event = threading.Event()
        self.active_custom_game = None
        # The last tick's desired state and how many ticks in a row it has held.
        self.pending_state = None
        self.pending_ticks = 0

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            poll_interval = CONFIG.get("POLL_INTERVAL", 5)
            try:
                self.poll_once()
            except Exception as e:
                # Never let one bad poll kill the thread; nothing restarts it.
                debug_log(f"Poller iteration failed: {e}")
            self.stop_event.wait(poll_interval)

    def poll_once(self):
        custom_enabled = platform_enabled("Custom")
        epic_enabled = platform_enabled("Epic")
        custom_games = CONFIG.get("CUSTOM_GAMES", []) if custom_enabled else []

        found_game = None
        found_launcher = None

        # Collect only what an enabled source will actually read. Both of these
        # are expensive, and a disabled platform must not pay for either.
        needs_windows, needs_processes = poll_work_needed()
        windows = get_active_window_titles() if needs_windows else []
        if needs_processes:
            snapshot, processes, running_exes = snapshot_processes()
        else:
            snapshot, processes, running_exes = {}, [], set()

        # 1. Dynamic Window Ancestry Scan
        epic_window_alive = False
        candidates = []
        # With every ancestry launcher switched off there is nothing to match,
        # and Epic's liveness check is pointless once Epic itself is off.
        if windows and (ACTIVE_LAUNCHERS or epic_enabled):
            for win in windows:
                title = win["title"]
                if title.lower().endswith(BROWSER_SUFFIXES):
                    continue
                if win["pid"] <= 0:
                    continue

                if epic_enabled and not epic_window_alive \
                        and descends_from_epic(win["pid"], snapshot):
                    epic_window_alive = True

                if ACTIVE_LAUNCHERS:
                    launcher = find_launcher_ancestor(win["pid"], snapshot)
                    if launcher:
                        candidates.append((win.get("area", 0), title, launcher))

        if candidates:
            # Pick the biggest window rather than the first one enumerated.
            # Games routinely own several top-level windows (D3D proxies, splash
            # screens, crash handlers) and EnumWindows returns them in Z-order,
            # so first-match made the published name a matter of luck. max()
            # keeps the earliest window on a tie, preserving prior behaviour.
            _, found_game, found_launcher = max(candidates, key=lambda c: c[0])

        # 2. Check JSON Custom Games (Using Dropdown/Type Rules)
        if not found_game and custom_games:
            filtered_titles = [
                w["title"].lower() for w in windows
                if not w["title"].lower().endswith(BROWSER_SUFFIXES)
            ]

            for game in custom_games:
                try:
                    match = self.match_custom_game(game, running_exes, filtered_titles)
                except Exception as e:
                    debug_log(f"Skipping malformed custom game entry: {e}")
                    continue
                if match:
                    found_game = match
                    found_launcher = "Custom"
                    break

        # 3. Retire Epic games whose process has gone away
        epic_title = None
        if epic_enabled:
            reconcile_epic_games(running_exes, epic_window_alive)
            epic_title = epic_active_title()

        # 4. Installed games from each platform's own database, matched by the
        # running process. Each source is skipped when its platform is off.
        named_sources = resolve_named_sources(processes, epic_title)

        # 5. Apply State. This is the single decision point: publishing the whole
        # desired state every tick (deduplicated downstream) means a game closing
        # cannot leave a stale sensor behind, whichever source detected it.
        # Sources that know a game's real name outrank raw window titles.
        desired = ("idle", "Offline", "None")
        for launcher_name, title in named_sources:
            if title:
                desired = ("playing", title, launcher_name)
                break
        else:
            if found_game:
                desired = ("playing", found_game, found_launcher)

        if desired[1] != self.active_custom_game:
            self.active_custom_game = desired[1]
            if desired[0] == "playing":
                debug_log(f"Poller state: {desired[1]} on {desired[2]}")
            else:
                debug_log("Poller state: no game running.")

        # Settle delay: a new state must hold for SETTLE_TICKS polls in a row
        # before it is published. Launchers briefly name the wrong game while
        # one boots, and a single errant publish is enough to fire a Home
        # Assistant notification.
        if desired == self.pending_state:
            self.pending_ticks += 1
        else:
            self.pending_state = desired
            self.pending_ticks = 1
        if self.pending_ticks >= SETTLE_TICKS:
            publish_global_state(*desired)
        else:
            debug_log(f"Waiting for {desired[1]} to settle before publishing.")

    def match_custom_game(self, game, running_exes, filtered_titles):
        """Return the game title if this entry matches, else None."""
        title = game.get("title", "Unknown Custom Game")
        g_type = game.get("type", "")
        target = str(game.get("target", "")).strip().lower()
        match_type = game.get("match", "Starts With")

        # Backward compatibility for legacy config structures
        if not g_type:
            if game.get("exe"):
                g_type = "Executable (.exe)"
                target = str(game.get("exe", "")).strip().lower()
            elif game.get("window"):
                g_type = "Window Title"
                target = str(game.get("window", "")).strip().lower()

        if not target:
            return None

        if g_type == "Executable (.exe)":
            return title if target in running_exes else None

        if g_type == "Window Title":
            if match_type == "Exact Match":
                return title if any(t == target for t in filtered_titles) else None
            if match_type == "Contains":
                return title if any(target in t for t in filtered_titles) else None
            if match_type == "Starts With":
                return title if any(t.startswith(target) for t in filtered_titles) else None

        return None


# --- DIAGNOSTICS ---
def build_diagnostic_report():
    """A plain-text snapshot of what detection currently sees.

    Read-only: it never mutates EPIC_GAMES or publishes anything, so running it
    cannot itself change the sensor. The MQTT password is never included, since
    this report is meant to be copied and shared.
    """
    out = []
    line = "=" * 74

    def section(title):
        out.append("")
        out.append(line)
        out.append(title)
        out.append(line)

    out.append(f"Gaming Status Agent {GSA_VERSION} diagnostics - "
               f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    section("CONFIGURATION")
    out.append(f"  Device name      : {CONFIG.get('HA_DEVICE_NAME', '')}")
    out.append(f"  MQTT topic       : {get_state_topic()}")
    out.append(f"  Broker           : {CONFIG.get('MQTT_BROKER', '')}:{CONFIG.get('MQTT_PORT', '')}")
    out.append(f"  TLS              : {'on' if CONFIG.get('MQTT_TLS') else 'off'}")
    out.append(f"  Username set     : {'yes' if CONFIG.get('MQTT_USER') else 'no'}")
    out.append(f"  Password set     : {'yes' if CONFIG.get('MQTT_PASS') else 'no'}")
    out.append(f"  Poll interval    : {CONFIG.get('POLL_INTERVAL', '')}s")
    catalog_url = CONFIG.get("CATALOG_URL", DEFAULT_CATALOG_URL)
    out.append(f"  Catalog URL      : {catalog_url}"
               f"{'' if catalog_url == DEFAULT_CATALOG_URL else '   (custom)'}")
    out.append(f"  Custom games     : {len(CONFIG.get('CUSTOM_GAMES', []))}")

    section("PLATFORMS")
    out.append("  Change these from the tray icon under \"Platforms\".")
    out.append("")
    for _label in PLATFORM_ORDER:
        _state = "on" if platform_enabled(_label) else "off"
        out.append(f"  {_label:16s} : {_state}")

    connected = "unknown"
    if client:
        try:
            connected = "yes" if client.is_connected() else "no"
        except Exception:
            connected = "unknown"
    else:
        connected = "no client"
    out.append(f"  MQTT connected   : {connected}")

    section("CURRENTLY PUBLISHED STATE")
    with STATE_LOCK:
        state = dict(GLOBAL_STATE)
    for key, value in state.items():
        out.append(f"  {key:16s} : {value}")

    section("INSTALLED GAME DATABASES")
    out.append(f"  GOG games known        : {len(GOG_BY_PATH)}")
    out.append(f"  Battle.net games known : {len(BATTLENET_BY_DIR)}")
    out.append(f"  Steam games known      : {len(STEAM_BY_APPID)}")
    if platform_enabled("Steam"):
        out.append(f"      Steam install      : {STEAM_SCAN_INFO.get('root') or '<not found>'}")
        for _library in (STEAM_SCAN_INFO.get("libraries") or [])[:10]:
            out.append(f"      library            : {_library}")
        if not STEAM_BY_APPID and STEAM_SCAN_INFO.get("root"):
            out.append("      No appmanifest_*.acf files were readable. Steam can still")
            out.append("      be detected live from its running-app registry key.")
    out.append(f"  Xbox games known       : {len(XBOX_BY_DIR)}")
    if platform_enabled("Xbox"):
        out.append(f"      Store packages seen: {XBOX_SCAN_INFO.get('packages', 0)}")
        for _root, _name in sorted(XBOX_BY_DIR.items(), key=lambda kv: kv[1])[:15]:
            out.append(f"      {_name}  ({_root})")
        _unnamed = XBOX_SCAN_INFO.get("unnamed", 0)
        if _unnamed:
            out.append(f"      {_unnamed} named from the package id because the Store display")
            out.append("      name could not be resolved.")
    _matched = UBISOFT_REGISTRY_INFO.get("matched", 0)
    _added = UBISOFT_REGISTRY_INFO.get("added", 0)
    if _matched and not _added:
        _note = "  (all already named by the catalog)"
    elif not _matched:
        _note = "  (none installed, or the naming scheme changed)"
    else:
        _note = ""
    out.append(f"  Ubisoft names total    : {UBISOFT_REGISTRY_INFO.get('total', 0)}")
    out.append(f"  Ubisoft registry names : {_matched} found, {_added} used{_note}")
    for _entry in (UBISOFT_REGISTRY_INFO.get("names") or [])[:10]:
        out.append(f"      {_entry}")
    _unmatched = UBISOFT_REGISTRY_INFO.get("unmatched") or []
    if _unmatched:
        out.append("  Ubisoft Connect entries NOT named 'Uplay Install <id>' - if any of")
        out.append("  these are installed games, the naming scheme has changed:")
        for _name in _unmatched[:10]:
            out.append(f"      {_name}")
    with EPIC_LOCK:
        epic_tracked = [(i["title"], i["exe"], i["seen_exe"]) for i in EPIC_GAMES.values()]
    out.append(f"  Epic games tracked     : {len(epic_tracked)}")
    for title, exe, seen in epic_tracked:
        out.append(f"      {title}  (exe: {exe or 'unknown'}, process seen: {seen})")

    windows = get_active_window_titles()
    snapshot, processes, running_exes = snapshot_processes()

    section(f"VISIBLE WINDOWS ({len(windows)}) -> ANCESTRY -> VERDICT")
    for win in windows:
        pid = win["pid"]
        title = win["title"]
        if pid <= 0:
            continue
        info = snapshot.get(pid)
        own_exe = info[1] if info else "<process not found>"
        chain = list(ancestor_names(pid, snapshot))
        verdict = find_launcher_ancestor(pid, snapshot)

        note = ""
        if title.lower().endswith(BROWSER_SUFFIXES):
            note = "skipped: browser/Discord-like title"
        elif own_exe in IGNORE_EXES:
            note = "skipped: launcher or helper process, not a game"
        elif not verdict:
            excluded = [n for n in chain if n in ACTIVE_EXCLUSIONS]
            off = [LAUNCHERS[n] for n in chain
                   if n in LAUNCHERS and n not in ACTIVE_LAUNCHERS]
            if excluded:
                note = f"skipped: descends from {excluded[0]} (deliberately excluded)"
            elif off:
                note = f"skipped: {off[0]} is switched off in Platforms"

        out.append("")
        out.append(f"  title  : {title[:64]}")
        out.append(f"  pid    : {pid}   exe: {own_exe}   size: {win.get('area', 0):,} px")
        out.append(f"  parents: {' -> '.join(chain) if chain else '<none / chain broken>'}")
        try:
            exe_path = psutil.Process(pid).exe()
        except Exception:
            exe_path = ""
        out.append(f"  path   : {exe_path or '<unreadable>'}")
        if exe_path and platform_enabled("Xbox"):
            resolved = os.path.normcase(os.path.abspath(exe_path))
            package = next((pkg for root, pkg in XBOX_OTHER_ROOTS.items()
                            if resolved.startswith(root)), None)
            if package:
                out.append(f"  xbox   : inside Store package {package}, which has no")
                out.append("           game marker file, so it is not counted as a game")
        out.append(f"  verdict: {verdict or 'NOT DETECTED'}")
        if note:
            out.append(f"  reason : {note}")

    # Recompute the ancestry decision without touching any shared state.
    candidates = []
    for win in windows:
        title = win["title"]
        if title.lower().endswith(BROWSER_SUFFIXES) or win["pid"] <= 0:
            continue
        launcher = find_launcher_ancestor(win["pid"], snapshot)
        if launcher:
            candidates.append((win.get("area", 0), title, launcher))

    found_game = found_launcher = None
    if candidates:
        _, found_game, found_launcher = max(candidates, key=lambda c: c[0])

    epic_title = epic_active_title()
    named_sources = resolve_named_sources(processes, epic_title)

    # How each named source finds its game, for the report only.
    source_methods = {
        "Epic": "launcher log",
        "Steam": "running appid + manifests",
        "Ubisoft": "launcher log",
        "GOG": "registry + process",
        "Battle.net": "install dir",
        "Xbox": "package folder"
    }

    section("DETECTION SOURCES, IN PRIORITY ORDER")
    _step = 0
    for _label, _title in named_sources:
        _step += 1
        _how = f"{_label} ({source_methods.get(_label, 'installed games')})"
        out.append(f"  {_step}. {_how:32s}: {_title or '-'}")
    _step += 1
    out.append(f"  {_step}. {'Window ancestry':32s}: {found_game or '-'}"
               f"{f'  [{found_launcher}]' if found_launcher else ''}")

    _off = [name for name in PLATFORM_ORDER if not platform_enabled(name)]
    if _off:
        out.append("")
        out.append(f"  Not checked, switched off: {', '.join(_off)}")

    if len(candidates) > 1:
        out.append("")
        out.append("  Competing windows (largest wins):")
        for area, title, launcher_name in sorted(candidates, reverse=True):
            mark = "  <-- chosen" if title == found_game else ""
            out.append(f"    {area:>12,} px  {title[:40]:42s} [{launcher_name}]{mark}")

    decision = ("idle", "Offline", "None")
    for _label, _title in named_sources:
        if _title:
            decision = ("playing", _title, _label)
            break
    else:
        if found_game:
            decision = ("playing", found_game, found_launcher)
    out.append("")
    out.append(f"  => would publish: {decision[1]}   (launcher: {decision[2]})")

    section("LAUNCHER-FAMILY PROCESSES AND THEIR ROLE")
    unknown = []
    for exe in sorted(e for e in running_exes
                      if any(t in e for t in LAUNCHER_FAMILY_TOKENS)):
        if exe in ACTIVE_LAUNCHERS:
            role = f"launcher -> {ACTIVE_LAUNCHERS[exe]}"
        elif exe in LAUNCHERS:
            role = f"{LAUNCHERS[exe]} is switched off in Platforms"
        elif exe in ACTIVE_EXCLUSIONS:
            role = "excluded (Home Assistant reports this natively)"
        elif exe in IGNORE_EXES:
            role = "helper / launcher window (ignored, correct)"
        elif exe in EPIC_LAUNCHER_EXES:
            role = "Epic liveness signal"
        else:
            role = "UNRECOGNISED"
            unknown.append(exe)
        out.append(f"  {exe:36s} {role}")

    if unknown:
        out.append("")
        out.append("  UNRECOGNISED processes are unknown to Gaming Status Agent. Only a process that")
        out.append("  actually STARTS games belongs in the launcher list; browser and")
        out.append("  service helpers should be ignored instead.")

    if not any("playnite" in e for e in running_exes):
        out.append("")
        out.append("  Playnite is not running. A game started from Playnite that then")
        out.append("  closed Playnite has a broken ancestry chain and cannot be")
        out.append("  attributed to it.")

    out.append("")
    return "\n".join(out)


# --- SERVICE MANAGEMENT ---
def setup_mqtt_discovery(mqtt_client):
    device_name = CONFIG.get("HA_DEVICE_NAME", "User")
    config_payload = {
        "name": None,
        "has_entity_name": True,
        "default_entity_id": f"sensor.gsa_{PROFILE_SANITIZED}",
        "state_topic": get_state_topic(),
        "value_template": "{{ value_json['Game Title'] }}",
        "json_attributes_topic": get_state_topic(),
        "unique_id": f"gsa_{PROFILE_SANITIZED}",
        "device": {
            "identifiers": [f"gsa_client_{PROFILE_SANITIZED}"],
            "name": f"Gaming Status Agent {device_name}",
            "manufacturer": "Gaming Status Agent"
        },
        "icon": "mdi:controller"
    }
    mqtt_client.publish(get_config_topic(), json.dumps(config_payload), qos=1, retain=True)


def _build_mqtt_client():
    """paho-mqtt 2.x requires an explicit callback API version; 1.6 rejects it."""
    try:
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        return mqtt.Client()


def _rc_is_success(rc):
    value = getattr(rc, 'value', rc)
    return value == 0


def _on_connect(mqtt_client, userdata, *args):
    # paho 1.6 passes (flags, rc); 2.x passes (flags, reason_code, properties).
    rc = args[1] if len(args) >= 2 else None
    if _rc_is_success(rc):
        debug_log(f"MQTT Connected successfully to {CONFIG.get('MQTT_BROKER')}")
        try:
            setup_mqtt_discovery(mqtt_client)
            republish_current_state()
        except Exception as e:
            debug_log(f"Post-connect publish failed: {e}")
    else:
        debug_log(f"MQTT connection refused (check credentials/TLS): {rc}")


def _on_disconnect(mqtt_client, userdata, *args):
    # paho 1.6 passes (rc); 2.x passes (disconnect_flags, reason_code, properties),
    # so the reason code sits at a different index in each.
    rc = args[1] if len(args) >= 2 else (args[0] if args else None)
    if not _rc_is_success(rc):
        debug_log(f"MQTT connection lost ({rc}); paho will retry automatically.")


def stop_services():
    global client, observer, custom_tracker
    debug_log("Stopping all background services...")

    if custom_tracker:
        custom_tracker.stop()
        custom_tracker.join(timeout=2)
        custom_tracker = None

    if observer:
        try:
            observer.stop()
            observer.join(timeout=2)
        except Exception:
            pass
        observer = None

    # Fresh handlers are built on restart; stale entries would resurrect games.
    with EPIC_LOCK:
        EPIC_GAMES.clear()
    set_ubisoft_game(None)

    if client:
        try:
            # Block until the final Offline message is actually on the wire;
            # loop_stop() otherwise discards it while it is still queued.
            publish_global_state("idle", "Offline", "None", wait=True)
            client.loop_stop()
            client.disconnect()
        except Exception as e:
            debug_log(f"Error during MQTT shutdown: {e}")
        client = None


def rebuild_active_tables():
    """Re-derive the tables the ancestry scan consults from the ENABLE_* toggles.

    Called on every start and restart, so a platform switched off in the
    Platforms window stops being detected on Save & Apply. Standalone entry
    points (gsa_diagnose.py) must call this too, or the ancestry scan sees an
    empty launcher table and reports nothing.
    """
    global ACTIVE_LAUNCHERS, ACTIVE_EXCLUSIONS

    ACTIVE_LAUNCHERS = {exe: label for exe, label in LAUNCHERS.items()
                        if platform_enabled(label)}

    # With Steam enabled, ancestry becomes a useful last-resort fallback for a
    # title the manifest scan could not name, so the exclusion is dropped.
    ACTIVE_EXCLUSIONS = set() if platform_enabled("Steam") else set(EXCLUDED_ANCESTOR_EXES)

    disabled = [name for name in PLATFORM_ORDER if not platform_enabled(name)]
    if disabled:
        debug_log(f"Platforms switched off: {', '.join(disabled)}")


def start_services():
    global client, observer, custom_tracker, CONFIG, PROFILE_SANITIZED
    global GOG_BY_PATH, GOG_BY_NAME, BATTLENET_BY_DIR
    global STEAM_BY_DIR, STEAM_BY_APPID, XBOX_BY_DIR
    debug_log("Starting Gaming Status Agent services...")

    CONFIG = load_config()
    PROFILE_SANITIZED = sanitize_topic_part(CONFIG.get("HA_DEVICE_NAME", "User"))
    rebuild_active_tables()

    client = _build_mqtt_client()
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect

    user = CONFIG.get("MQTT_USER", "")
    password = CONFIG.get("MQTT_PASS", "")
    if user and password:
        client.username_pw_set(user, password)

    if CONFIG.get("MQTT_TLS"):
        try:
            ca_cert = CONFIG.get("MQTT_CA_CERT", "").strip()
            client.tls_set(ca_certs=ca_cert or None)
            debug_log("MQTT TLS enabled.")
        except Exception as e:
            debug_log(f"Failed to enable TLS, continuing without it: {e}")

    # Last Will: if this machine sleeps, crashes or loses power, the broker
    # publishes Offline on our behalf instead of leaving a stale game retained.
    try:
        client.will_set(get_state_topic(), json.dumps(_offline_payload()), qos=1, retain=True)
    except Exception as e:
        debug_log(f"Could not set MQTT last will: {e}")

    try:
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        # connect_async + loop_start retries on its own, so a broker that is
        # still booting at login no longer leaves us permanently disconnected.
        client.connect_async(CONFIG.get("MQTT_BROKER", "localhost"),
                             CONFIG.get("MQTT_PORT", 1883), 60)
        client.loop_start()
    except Exception as e:
        debug_log(f"MQTT setup failed (check IP/Port): {e}")

    observer = Observer()
    if platform_enabled("Epic") and os.path.exists(EPIC_LOG_DIR):
        epic_mapping = get_epic_mapping()
        epic_handler = GameLogHandler("Epic", os.path.join(EPIC_LOG_DIR, EPIC_LOG_FILE), epic_mapping)
        observer.schedule(epic_handler, path=EPIC_LOG_DIR, recursive=False)
        debug_log(f"Monitoring Epic Logs at: {EPIC_LOG_DIR}")

    if not platform_enabled("Ubisoft"):
        UBISOFT_REGISTRY_INFO.update({"total": 0, "matched": 0, "added": 0,
                                      "unmatched": [], "names": []})
    else:
        ubi_log_dir, ubi_mapping = get_ubisoft_info()
        if ubi_log_dir and os.path.exists(ubi_log_dir):
            ubi_handler = GameLogHandler("Ubisoft", os.path.join(ubi_log_dir, UBI_LOG_FILE), ubi_mapping)
            observer.schedule(ubi_handler, path=ubi_log_dir, recursive=False)
            debug_log(f"Monitoring Ubisoft Logs at: {ubi_log_dir}")

    try:
        observer.start()
    except Exception as e:
        debug_log(f"Could not start log observer: {e}")

    # Refreshed on every restart, so newly installed games appear after a
    # Save & Apply rather than needing Gaming Status Agent to be closed and reopened.
    # A platform that is off is left with an empty map, which every lookup
    # already treats as "nothing to match".
    GOG_BY_PATH, GOG_BY_NAME = get_gog_mapping() if platform_enabled("GOG") else ({}, {})
    BATTLENET_BY_DIR = get_battlenet_mapping() if platform_enabled("Battle.net") else {}
    STEAM_BY_DIR, STEAM_BY_APPID = get_steam_mapping() if platform_enabled("Steam") else ({}, {})
    XBOX_BY_DIR = get_xbox_mapping() if platform_enabled("Xbox") else {}

    custom_tracker = CustomGameTracker()
    custom_tracker.daemon = True
    custom_tracker.start()
    debug_log("Universal Game Poller started successfully")


def restart_services():
    stop_services()
    start_services()


# --- GUI & SYSTEM TRAY ---
def check_initial_config():
    global CONFIG

    if not os.path.exists(CONFIG_FILE):
        save_config(DEFAULT_CONFIG)
        CONFIG = load_config()
        messagebox.showinfo(
            "Gaming Status Agent First Run",
            f"A default configuration has been created.\n\n"
            f"Device name: {CONFIG.get('HA_DEVICE_NAME')}\n"
            f"This becomes your Home Assistant sensor "
            f"(sensor.gsa_{sanitize_topic_part(CONFIG.get('HA_DEVICE_NAME', ''))}).\n\n"
            f"Gaming Status Agent is now running in your System Tray. Right-click the icon to set "
            f"your MQTT broker, change the device name, or choose which Platforms to track."
        )
    else:
        CONFIG = load_config()


def _focus_existing(name):
    """Return True if this window is already open, raising it instead of duplicating."""
    win = OPEN_WINDOWS.get(name)
    try:
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return True
    except tk.TclError:
        pass
    OPEN_WINDOWS.pop(name, None)
    return False


def _make_settings_window(key, title, geometry):
    win = tk.Toplevel(ROOT)
    OPEN_WINDOWS[key] = win
    win.title(title)
    win.geometry(geometry)
    win.resizable(False, False)
    win.attributes('-topmost', True)
    win.protocol("WM_DELETE_WINDOW", lambda: (OPEN_WINDOWS.pop(key, None), win.destroy()))
    return win


def _add_entry_rows(win, fields, row=0):
    """Lay out label/entry pairs and return their StringVars plus the next row."""
    vars_dict = {}
    for key, label_text in fields:
        tk.Label(win, text=label_text).grid(row=row, column=0, padx=15, pady=8, sticky="e")
        var = tk.StringVar(value=str(CONFIG.get(key, "")))
        vars_dict[key] = var
        entry = tk.Entry(win, textvariable=var, width=32)
        if "PASS" in key:
            entry.config(show="*")
        entry.grid(row=row, column=1, padx=10, pady=8, sticky="w")
        row += 1
    return vars_dict, row


def _finish_settings_window(win, row, on_save):
    tk.Button(win, text="Save & Apply", command=on_save, width=20).grid(
        row=row, column=0, columnspan=2, pady=15)
    win.focus_force()
    win.after_idle(win.attributes, '-topmost', False)


def show_settings_ui():
    if _focus_existing("settings"):
        return

    settings_win = _make_settings_window("settings", "Gaming Status Agent - MQTT Settings", "430x400")

    fields = [
        ("HA_DEVICE_NAME", "HA Device Name"),
        ("MQTT_BROKER", "MQTT Broker IP"),
        ("MQTT_PORT", "MQTT Port"),
        ("MQTT_USER", "MQTT Username"),
        ("MQTT_PASS", "MQTT Password"),
        ("POLL_INTERVAL", "Universal Poller Rate (s)")
    ]
    vars_dict, row = _add_entry_rows(settings_win, fields)

    tls_var = tk.BooleanVar(value=bool(CONFIG.get("MQTT_TLS", False)))
    tk.Checkbutton(settings_win, text="Use TLS (port is usually 8883)",
                   variable=tls_var).grid(row=row, column=1, padx=10, pady=4, sticky="w")
    row += 1

    tk.Label(settings_win, text="CA Cert (optional)").grid(row=row, column=0, padx=15, pady=8, sticky="e")
    ca_var = tk.StringVar(value=str(CONFIG.get("MQTT_CA_CERT", "")))
    tk.Entry(settings_win, textvariable=ca_var, width=32).grid(row=row, column=1, padx=10, pady=8, sticky="w")
    row += 1

    def save_settings():
        # Checked before anything is written: clearing this field moved the
        # sensor to the generic sensor.gsa_user topic and emptied Profile Name,
        # with no indication that either had happened.
        if not _clean_name(vars_dict["HA_DEVICE_NAME"].get()):
            messagebox.showerror(
                "Error",
                "HA Device Name cannot be empty.\n\n"
                "It becomes your Home Assistant sensor name and the MQTT topic.")
            return

        for key, _ in fields:
            val = vars_dict[key].get()
            if key == "MQTT_PORT":
                val = _coerce_int(val, 1883, 1, 65535)
            elif key == "POLL_INTERVAL":
                val = _coerce_int(val, 5, 1, 3600)
            elif key in ("HA_DEVICE_NAME", "MQTT_BROKER", "MQTT_USER"):
                # A trailing space in the broker address fails to connect with
                # a DNS error that gives no hint of the real cause.
                val = _clean_name(val)
            CONFIG[key] = val

        CONFIG["MQTT_TLS"] = bool(tls_var.get())
        CONFIG["MQTT_CA_CERT"] = ca_var.get().strip()

        if not save_config():
            messagebox.showerror("Error", "Could not save settings. See gsa_debug.log for details.")
            return

        OPEN_WINDOWS.pop("settings", None)
        settings_win.destroy()
        restart_services()

    _finish_settings_window(settings_win, row, save_settings)


def open_settings(icon, item):
    ROOT.after(0, show_settings_ui)


def gamertag_fields():
    """(config key, platform name) for each tracked platform that has a gamertag.

    Listed in PLATFORM_ORDER, and only while the platform is switched on: a
    gamertag for something that is not being tracked is never published, so
    showing the field would only invite filling in a box that does nothing.
    """
    return [(LAUNCHER_PROFILE_KEYS[name], name) for name in PLATFORM_ORDER
            if name in LAUNCHER_PROFILE_KEYS and platform_enabled(name)]


def show_gamertags_ui():
    if _focus_existing("gamertags"):
        return

    fields = gamertag_fields()
    # Two rows of padding plus the Save button, so the window fits its contents
    # however many platforms are switched on.
    height = 90 + 42 * max(len(fields), 1)
    gt_win = _make_settings_window("gamertags", "Gaming Status Agent - Gamertags",
                                   f"430x{height}")

    if not fields:
        tk.Label(gt_win, text="No tracked platform uses a gamertag.\n"
                              "Turn one on under Platforms first.",
                 justify="left").grid(row=0, column=0, columnspan=2,
                                      padx=15, pady=20, sticky="w")
        _finish_settings_window(gt_win, 1, lambda: (OPEN_WINDOWS.pop("gamertags", None),
                                                    gt_win.destroy()))
        return

    vars_dict, row = _add_entry_rows(gt_win, fields)

    def save_gamertags():
        for key, _ in fields:
            CONFIG[key] = vars_dict[key].get().strip()

        if not save_config():
            messagebox.showerror("Error", "Could not save settings. See gsa_debug.log for details.")
            return

        OPEN_WINDOWS.pop("gamertags", None)
        gt_win.destroy()
        restart_services()

    _finish_settings_window(gt_win, row, save_gamertags)


def open_gamertags(icon, item):
    ROOT.after(0, show_gamertags_ui)


def show_platforms_ui():
    if _focus_existing("platforms"):
        return

    plat_win = _make_settings_window("platforms", "Gaming Status Agent - Platforms", "430x360")

    tk.Label(plat_win, text="Which platforms should be tracked?",
             font=("", 9, "bold")).grid(row=0, column=0, columnspan=2,
                                        padx=15, pady=(12, 6), sticky="w")

    row = 1
    vars_dict = {}
    for label in PLATFORM_ORDER:
        key = PLATFORM_ENABLE_KEYS[label]
        var = tk.BooleanVar(value=platform_enabled(label))
        vars_dict[key] = var
        tk.Checkbutton(plat_win, text=label, variable=var).grid(
            row=row, column=0, columnspan=2, padx=15, sticky="w")
        row += 1

    def save_platforms():
        for key, var in vars_dict.items():
            CONFIG[key] = bool(var.get())

        if not save_config():
            messagebox.showerror("Error", "Could not save settings. See gsa_debug.log for details.")
            return

        OPEN_WINDOWS.pop("platforms", None)
        plat_win.destroy()
        restart_services()

    _finish_settings_window(plat_win, row, save_platforms)


def open_platforms(icon, item):
    ROOT.after(0, show_platforms_ui)


def show_custom_games_ui():
    if _focus_existing("custom_games"):
        return

    cg_win = tk.Toplevel(ROOT)
    OPEN_WINDOWS["custom_games"] = cg_win
    cg_win.title("Gaming Status Agent - Custom Games")
    cg_win.geometry("540x350")
    cg_win.attributes('-topmost', True)
    cg_win.protocol("WM_DELETE_WINDOW",
                    lambda: (OPEN_WINDOWS.pop("custom_games", None), cg_win.destroy()))

    columns = ('title', 'type', 'target', 'match')
    tree = ttk.Treeview(cg_win, columns=columns, show='headings')
    tree.heading('title', text='Game Title')
    tree.heading('type', text='Match Method')
    tree.heading('target', text='Target Value')
    tree.heading('match', text='Rule')

    tree.column('title', width=130)
    tree.column('type', width=120)
    tree.column('target', width=150)
    tree.column('match', width=110)
    tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def refresh_list():
        for item in tree.get_children():
            tree.delete(item)
        for game in CONFIG.get("CUSTOM_GAMES", []):
            g_type = game.get("type", "Executable (.exe)" if game.get("exe") else "Window Title")
            target = game.get("target", game.get("exe", game.get("window", "")))
            tree.insert('', tk.END, values=(game.get("title", ""), g_type, target, game.get("match", "Starts With")))

    refresh_list()

    def add_game():
        add_win = tk.Toplevel(cg_win)
        add_win.title("Add Custom Game")
        add_win.geometry("380x280")

        tk.Label(add_win, text="Game Title:").pack(pady=2)
        title_entry = tk.Entry(add_win, width=40)
        title_entry.pack()

        tk.Label(add_win, text="Match Method:").pack(pady=2)
        type_var = tk.StringVar(value="Executable (.exe)")
        ttk.Combobox(add_win, textvariable=type_var, values=["Executable (.exe)", "Window Title"], state="readonly", width=37).pack()

        tk.Label(add_win, text="Target Value (e.g., game.exe or Window Name):").pack(pady=2)
        target_entry = tk.Entry(add_win, width=40)
        target_entry.pack()

        tk.Label(add_win, text="Window Match Rule (Only applies to Window Title):").pack(pady=2)
        match_var = tk.StringVar(value="Starts With")
        ttk.Combobox(add_win, textvariable=match_var, values=["Starts With", "Exact Match", "Contains"], state="readonly", width=37).pack()

        def save_new():
            t = title_entry.get().strip()
            g_type = type_var.get()
            target = target_entry.get().strip()
            m = match_var.get()

            if not t:
                messagebox.showerror("Error", "Game Title is required.")
                return
            if not target:
                messagebox.showerror("Error", "Target Value is required.")
                return

            CONFIG.setdefault("CUSTOM_GAMES", []).append({
                "title": t,
                "type": g_type,
                "target": target,
                "match": m
            })
            if not save_config():
                messagebox.showerror("Error", "Could not save. See gsa_debug.log for details.")
                return

            refresh_list()
            add_win.destroy()
            restart_services()

        tk.Button(add_win, text="Save Game", command=save_new).pack(pady=15)
        add_win.transient(cg_win)
        add_win.grab_set()

    def remove_game():
        selected = tree.selection()
        if not selected:
            return
        item_values = tree.item(selected[0])['values']

        CONFIG["CUSTOM_GAMES"] = [
            g for g in CONFIG.get("CUSTOM_GAMES", [])
            if not (str(g.get("title", "")) == str(item_values[0])
                    and str(g.get("target", g.get("exe", g.get("window", "")))) == str(item_values[2]))
        ]

        if not save_config():
            messagebox.showerror("Error", "Could not save. See gsa_debug.log for details.")
            return

        refresh_list()
        restart_services()

    btn_frame = tk.Frame(cg_win)
    btn_frame.pack(pady=10)
    tk.Button(btn_frame, text="Add Game", command=add_game, width=15).pack(side=tk.LEFT, padx=10)
    tk.Button(btn_frame, text="Remove Selected", command=remove_game, width=15).pack(side=tk.LEFT, padx=10)

    cg_win.focus_force()
    cg_win.after_idle(cg_win.attributes, '-topmost', False)


def open_custom_games(icon, item):
    ROOT.after(0, show_custom_games_ui)


def show_diagnostics_ui():
    if _focus_existing("diagnostics"):
        return

    diag_win = tk.Toplevel(ROOT)
    OPEN_WINDOWS["diagnostics"] = diag_win
    diag_win.title("Gaming Status Agent - Diagnostics")
    diag_win.geometry("860x600")
    diag_win.protocol("WM_DELETE_WINDOW",
                      lambda: (OPEN_WINDOWS.pop("diagnostics", None), diag_win.destroy()))

    text = scrolledtext.ScrolledText(diag_win, wrap=tk.NONE, font=("Consolas", 9))
    text.pack(fill=tk.BOTH, expand=True, padx=10, pady=(10, 4))
    text.insert(tk.END, "Collecting diagnostics...")
    text.config(state=tk.DISABLED)

    status = tk.Label(diag_win, text="", anchor="w")
    status.pack(fill=tk.X, padx=12)

    def set_report(report):
        # Arrives from the worker thread, so this runs via ROOT.after.
        if not diag_win.winfo_exists():
            return
        text.config(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert(tk.END, report)
        text.config(state=tk.DISABLED)

    def collect():
        try:
            report = build_diagnostic_report()
        except Exception as e:
            debug_log(f"Diagnostics failed: {e}")
            report = f"Diagnostics failed: {e}"
        ROOT.after(0, lambda: set_report(report))

    def refresh():
        text.config(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        text.insert(tk.END, "Collecting diagnostics...")
        text.config(state=tk.DISABLED)
        status.config(text="")
        threading.Thread(target=collect, daemon=True).start()

    def copy_report():
        try:
            ROOT.clipboard_clear()
            ROOT.clipboard_append(text.get("1.0", tk.END))
            ROOT.update_idletasks()
            status.config(text="Copied to clipboard.")
        except Exception as e:
            status.config(text=f"Could not copy: {e}")

    def save_report():
        path = os.path.join(BASE_DIR, "gsa_diagnostics.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text.get("1.0", tk.END))
            status.config(text=f"Saved to {path}")
        except Exception as e:
            status.config(text=f"Could not save: {e}")

    btn_frame = tk.Frame(diag_win)
    btn_frame.pack(pady=8)
    tk.Button(btn_frame, text="Refresh", command=refresh, width=14).pack(side=tk.LEFT, padx=6)
    tk.Button(btn_frame, text="Copy to Clipboard", command=copy_report, width=18).pack(side=tk.LEFT, padx=6)
    tk.Button(btn_frame, text="Save to File", command=save_report, width=14).pack(side=tk.LEFT, padx=6)

    # Enumerating every process takes a moment; keep the tray responsive.
    threading.Thread(target=collect, daemon=True).start()

    diag_win.focus_force()


def open_diagnostics(icon, item):
    ROOT.after(0, show_diagnostics_ui)


def force_offline(icon, item):
    # Clear tracked Epic games too, or the next poll would republish them.
    with EPIC_LOCK:
        EPIC_GAMES.clear()
    set_ubisoft_game(None)
    if custom_tracker:
        custom_tracker.active_custom_game = None
    publish_global_state("idle", "Offline", "None")


def quit_app(icon, item):
    stop_services()
    icon.stop()
    ROOT.after(0, ROOT.quit)


RUN_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "Gaming Status Agent"


def startup_command():
    """The command line Windows runs at login to start this app."""
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    # Running from source: prefer pythonw so no console window opens at login.
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    interpreter = pythonw if os.path.exists(pythonw) else sys.executable
    return f'"{interpreter}" "{os.path.abspath(__file__)}"'


def startup_enabled(item=None):
    """True if the per-user Run key starts this app at login. The registry is
    the only record of this, so an entry removed in Task Manager shows as off."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH) as key:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
        return True
    except OSError:
        return False


def toggle_startup(icon, item):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_REG_PATH, 0,
                            winreg.KEY_SET_VALUE) as key:
            if startup_enabled():
                winreg.DeleteValue(key, RUN_VALUE_NAME)
                debug_log("Start at login disabled.")
            else:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  startup_command())
                debug_log("Start at login enabled.")
    except OSError as e:
        debug_log(f"Could not change start at login: {e}")


def create_tray_menu():
    # visible= takes a callable, re-evaluated each time the menu is opened, so
    # toggling a platform updates the menu without rebuilding the tray icon.
    return pystray.Menu(
        pystray.MenuItem("MQTT Settings", open_settings),
        pystray.MenuItem("Platforms", open_platforms),
        pystray.MenuItem("Gamertags", open_gamertags,
                         visible=lambda item: bool(gamertag_fields())),
        pystray.MenuItem("Custom Games", open_custom_games,
                         visible=lambda item: platform_enabled("Custom")),
        pystray.MenuItem("Start Gaming Status Agent at Login", toggle_startup,
                         checked=startup_enabled),
        pystray.MenuItem("Run Diagnostics", open_diagnostics),
        pystray.MenuItem("Force Offline", force_offline),
        pystray.MenuItem("Quit", quit_app)
    )


def create_image():
    icon_path = resource_path("gsa_icon.ico")
    if os.path.exists(icon_path):
        try:
            with Image.open(icon_path) as img:
                return img.copy()
        except Exception as e:
            debug_log(f"Could not load tray icon, using fallback: {e}")

    img = Image.new('RGB', (64, 64), color=(35, 35, 35))
    dc = ImageDraw.Draw(img)
    dc.rectangle((12, 20, 52, 44), fill=(0, 120, 215))
    dc.rectangle((18, 28, 24, 36), fill=(255, 255, 255))
    dc.rectangle((40, 28, 46, 36), fill=(255, 255, 255))
    return img


def apply_window_icon():
    """Replace Tk's default feather on every Gaming Status Agent window.

    Nothing set an icon on the Tk side, so Settings, Custom Games and
    Diagnostics all showed the Tcl/Tk logo. iconbitmap(default=...) applies to
    every current and future toplevel on Windows, so one call covers them all.

    Falls back to the same image the tray draws when no .ico is present, because
    a missing file should not put the feather back.
    """
    global _WINDOW_ICON
    icon_path = resource_path("gsa_icon.ico")
    if os.path.exists(icon_path):
        try:
            ROOT.iconbitmap(default=icon_path)
            return
        except Exception as e:
            debug_log(f"Could not apply window icon from {icon_path}: {e}")

    try:
        from PIL import ImageTk
        # Tk does not keep its own reference, so this must outlive the call.
        _WINDOW_ICON = ImageTk.PhotoImage(create_image())
        ROOT.iconphoto(True, _WINDOW_ICON)
    except Exception as e:
        debug_log(f"Could not apply fallback window icon: {e}")


def main():
    _init_logging()
    debug_log(f"=== GAMING STATUS AGENT {GSA_VERSION} LAUNCHED ===")
    apply_window_icon()
    check_initial_config()
    start_services()
    icon = pystray.Icon("Gaming Status Agent", create_image(), "Gaming Status Agent", create_tray_menu())

    threading.Thread(target=icon.run, daemon=True).start()
    ROOT.mainloop()


if __name__ == "__main__":
    main()