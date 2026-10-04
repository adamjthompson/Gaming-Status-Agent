"""Gaming Status Agent for Linux.

Reports what you're playing to Home Assistant over MQTT, using the same topics,
discovery config and payload as the Windows agent (gsa_tracker.py), so Home
Assistant and the Gaming Status integration treat both the same way.

Detects:
  - Steam games, native and Proton (Steam's reaper names the AppId)
  - Heroic games, reported as Epic, GOG or Amazon Games
  - Lutris games (the game name Lutris hands to the launched process)
  - Custom rules, by process name or (X11 only) window title

    python3 gsa_linux.py              tray icon and settings windows
    python3 gsa_linux.py --headless   no UI, for a systemd user service
    python3 gsa_linux.py --diagnose   print one detection pass and exit
"""
import os
import sys
import json
import re
import glob
import signal
import socket
import struct
import shutil
import logging
import argparse
import threading
import subprocess
from logging.handlers import RotatingFileHandler
from datetime import datetime

import paho.mqtt.client as mqtt
import psutil

GSA_VERSION = "1.1.2"

# --- GLOBALS & PATHS ---
client = None
poller = None
CONFIG = {}
PROFILE_SANITIZED = "user"
ROOT = None
HEADLESS = False

STATE_LOCK = threading.RLock()
GLOBAL_STATE = {
    "status": "idle",
    "game": "Offline",
    "launcher": "None",
    "start_time": "None"
}
OPEN_WINDOWS = {}

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
                          "gaming-status-agent")
CONFIG_FILE = os.path.join(CONFIG_DIR, "gsa_config.json")
DEBUG_LOG_FILE = os.path.join(CONFIG_DIR, "gsa_debug.log")
AUTOSTART_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"),
                              "autostart", "gaming-status-agent.desktop")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MAX_TITLE_LEN = 255
MAX_ANCESTRY_DEPTH = 20
SETTLE_TICKS = 2
KEYRING_SERVICE = "gaming-status-agent"
KEYRING_MARKER = "keyring:"
# In-memory CONFIG flag: the password is in the keyring but could not be read.
KEYRING_PENDING = "_MQTT_PASS_PENDING"
KEYRING_RETRY_SECONDS = 10
MACHINE_NAME = socket.gethostname()

GENERIC_ACCOUNT_NAMES = {"root", "admin", "user", "default", "guest", "deck"}

# Native and Flatpak installs. The first one that exists wins for each launcher.
STEAM_ROOTS = [
    os.path.join(HOME, ".steam", "steam"),
    os.path.join(HOME, ".local", "share", "Steam"),
    os.path.join(HOME, ".var", "app", "com.valvesoftware.Steam", ".local", "share", "Steam"),
    os.path.join(HOME, "snap", "steam", "common", ".local", "share", "Steam"),
]
HEROIC_ROOTS = [
    os.path.join(HOME, ".config", "heroic"),
    os.path.join(HOME, ".var", "app", "com.heroicgameslauncher.hgl", "config", "heroic"),
]

# Steam installs these as apps, but they are tools that run games, not games.
STEAM_TOOL_PREFIXES = ("proton", "steam linux runtime", "steamworks common redistributables")

# Launcher and Wine plumbing processes. Their paths and command lines can point
# into a game folder without being the game.
IGNORE_NAMES = {
    "steam", "steamwebhelper", "reaper", "pressure-vessel-wrap", "pv-bwrap",
    "pressure-vessel-adverb", "srt-bwrap", "steam-runtime-launcher-service",
    "heroic", "legendary", "gogdl", "nile", "lutris", "lutris-wrapper",
    "wineserver", "winedevice.exe", "services.exe", "explorer.exe",
    "plugplay.exe", "svchost.exe", "rpcss.exe", "conhost.exe", "start.exe",
    "tabtip.exe", "winedbg", "umu-run", "python3", "python", "bash", "sh",
}

# Process name -> the label it launches for. Used only for diagnostics: Heroic
# games are matched by install folder and Lutris by its own variables.
LAUNCHER_PROCESSES = {"steam": "Steam", "heroic": "Heroic", "lutris": "Lutris",
                      "pcsx2-qt": "PCSX2", "pcsx2": "PCSX2", "rpcs3": "RPCS3"}

WINE_DRIVE_RE = re.compile(r'^[zZ]:[\\/]')


def _default_device_name():
    raw = (os.environ.get("USER") or os.environ.get("LOGNAME") or "").strip()
    if raw and raw.lower() not in GENERIC_ACCOUNT_NAMES:
        return raw
    return "Gamer"


DEFAULT_CONFIG = {
    "HA_DEVICE_NAME": _default_device_name(),
    "EPIC_PROFILE_NAME": "",
    "GOG_PROFILE_NAME": "",
    "STEAM_PROFILE_NAME": "",
    # Steam defaults to off, as on Windows: Home Assistant's own Steam
    # integration would otherwise be a second, competing source.
    "ENABLE_STEAM": False,
    "ENABLE_EPIC": True,
    "ENABLE_GOG": True,
    "ENABLE_AMAZON": True,
    "ENABLE_LUTRIS": True,
    # Off until asked for: it only works once PINE is switched on in PCSX2.
    "ENABLE_PCSX2": False,
    # Off until asked for: it only works once RPCS3's IPC server is switched on.
    "ENABLE_RPCS3": False,
    "ENABLE_CUSTOM": False,
    "MQTT_BROKER": "192.168.1.xxx",
    "MQTT_PORT": 1883,
    "MQTT_USER": "",
    "MQTT_PASS": "",
    "MQTT_TLS": False,
    "MQTT_CA_CERT": "",
    "POLL_INTERVAL": 5,
    "CUSTOM_GAMES": []
}

LAUNCHER_PROFILE_KEYS = {
    "Epic": "EPIC_PROFILE_NAME",
    "GOG": "GOG_PROFILE_NAME",
    "Steam": "STEAM_PROFILE_NAME",
}

PLATFORM_ENABLE_KEYS = {
    "Steam": "ENABLE_STEAM",
    "Epic": "ENABLE_EPIC",
    "GOG": "ENABLE_GOG",
    "Amazon Games": "ENABLE_AMAZON",
    "Lutris": "ENABLE_LUTRIS",
    "PCSX2": "ENABLE_PCSX2",
    "RPCS3": "ENABLE_RPCS3",
    "Custom": "ENABLE_CUSTOM"
}

PLATFORM_ORDER = ("Amazon Games", "Epic", "GOG", "Lutris", "PCSX2", "RPCS3", "Steam",
                  "Custom")

PLATFORM_NOTES = {
    "Amazon Games": "via Heroic",
    "Epic": "via Heroic",
    "GOG": "via Heroic",
    "Steam": "native and Proton",
    "PCSX2": "needs PINE enabled in PCSX2",
    "RPCS3": "experimental; needs IPC enabled in RPCS3",
}

# Filled in at startup and on every restart.
STEAM_BY_APPID = {}
STEAM_SCAN_INFO = {"root": "", "libraries": [], "games": 0}
# [(install dir with trailing slash, title, launcher label)]
HEROIC_GAMES = []
HEROIC_SCAN_INFO = {"root": "", "Epic": 0, "GOG": 0, "Amazon Games": 0}


def platform_enabled(launcher_name):
    key = PLATFORM_ENABLE_KEYS.get(launcher_name)
    if not key:
        return True
    return bool(CONFIG.get(key, DEFAULT_CONFIG.get(key, True)))


def _clean_name(value):
    return value.strip() if isinstance(value, str) else ""


def profile_for_launcher(launcher_name):
    key = LAUNCHER_PROFILE_KEYS.get(launcher_name)
    gamertag = _clean_name(CONFIG.get(key)) if key else ""
    return gamertag or _clean_name(CONFIG.get("HA_DEVICE_NAME")) or "Unknown"


# --- LOGGING ---
_logger = logging.getLogger("gsa")


def _init_logging():
    _logger.setLevel(logging.INFO)
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        handler = RotatingFileHandler(
            DEBUG_LOG_FILE, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("[%(asctime)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        _logger.addHandler(handler)
    except Exception:
        _logger.addHandler(logging.NullHandler())
    if HEADLESS:
        # journalctl picks up stderr from a systemd user service.
        _logger.addHandler(logging.StreamHandler(sys.stderr))


def debug_log(message):
    try:
        _logger.info(message)
    except Exception:
        pass


# --- HELPERS ---
def sanitize_topic_part(name):
    """MQTT topics cannot contain / + #, and an empty segment breaks discovery."""
    cleaned = re.sub(r'[^a-z0-9_]', '_', str(name).strip().lower().replace(" ", "_"))
    cleaned = cleaned.strip("_")
    return cleaned or "user"


def get_state_topic():
    return f"homeassistant/sensor/gsa_{PROFILE_SANITIZED}/state"


def get_config_topic():
    return f"homeassistant/sensor/gsa_{PROFILE_SANITIZED}/config"


def _coerce_int(value, default, low, high):
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return result if low <= result <= high else default


def _keyring():
    try:
        import keyring
        return keyring
    except Exception:
        return None


def encrypt_secret(plaintext):
    """Store the password in the Secret Service keyring when one is available.

    The config file then holds only a marker. Without a keyring the password
    stays in the file, which save_config() keeps readable by this user only.
    """
    if not plaintext:
        return ""
    kr = _keyring()
    if kr:
        try:
            kr.set_password(KEYRING_SERVICE, "MQTT_PASS", plaintext)
            return KEYRING_MARKER
        except Exception as e:
            debug_log(f"Keyring unavailable ({e}); MQTT password kept in the config file.")
    return plaintext


def decrypt_secret(stored, rediscover=False):
    """The MQTT password, or None if it is in the keyring and the keyring
    cannot be read yet.

    At login a systemd user service can start before KWallet or GNOME Keyring
    is up. keyring then picks a backend that cannot read anything, and keeps
    it, so a retry passes rediscover=True to choose the backend again.
    """
    if not isinstance(stored, str):
        return ""
    if stored != KEYRING_MARKER:
        return stored
    kr = _keyring()
    if not kr:
        debug_log("Config refers to the keyring, but python keyring is not installed.")
        return None
    try:
        if rediscover:
            # keyring caches the list of usable backends on first use, so
            # init_backend() alone keeps choosing from the list made before
            # the desktop keyring was up. Clear it so discovery really reruns.
            backends = kr.backend.get_all_keyring
            if hasattr(backends, "reset"):
                backends.reset()
            kr.core.init_backend()
        return kr.get_password(KEYRING_SERVICE, "MQTT_PASS")
    except Exception as e:
        debug_log(f"Could not read MQTT password from the keyring: {e}")
        return None


def load_config():
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

    device_name = _clean_name(merged.get("HA_DEVICE_NAME"))
    if not device_name:
        device_name = DEFAULT_CONFIG["HA_DEVICE_NAME"]
        debug_log(f"HA_DEVICE_NAME is blank; falling back to {device_name!r}.")
    merged["HA_DEVICE_NAME"] = device_name

    merged["MQTT_PORT"] = _coerce_int(merged.get("MQTT_PORT"), 1883, 1, 65535)
    merged["POLL_INTERVAL"] = _coerce_int(merged.get("POLL_INTERVAL"), 5, 1, 3600)
    merged["MQTT_TLS"] = bool(merged.get("MQTT_TLS"))

    for enable_key in PLATFORM_ENABLE_KEYS.values():
        raw = merged.get(enable_key, DEFAULT_CONFIG[enable_key])
        if isinstance(raw, str):
            raw = raw.strip().lower() not in ("", "0", "false", "no", "off")
        merged[enable_key] = bool(raw)
    password = decrypt_secret(merged.get("MQTT_PASS", ""))
    # Kept in memory only: save_config() writes the marker back rather than
    # erasing a password it could not read, and start_services() waits for it.
    merged[KEYRING_PENDING] = password is None
    merged["MQTT_PASS"] = password or ""

    if not isinstance(merged.get("CUSTOM_GAMES"), list):
        debug_log("CUSTOM_GAMES is not a list; ignoring it.")
        merged["CUSTOM_GAMES"] = []
    merged["CUSTOM_GAMES"] = [g for g in merged["CUSTOM_GAMES"] if isinstance(g, dict)]
    return merged


def save_config(config=None):
    """Write config atomically, readable by this user only."""
    source = CONFIG if config is None else config
    to_write = dict(source)
    pending = to_write.pop(KEYRING_PENDING, False)
    if pending and not source.get("MQTT_PASS"):
        to_write["MQTT_PASS"] = KEYRING_MARKER
    else:
        to_write["MQTT_PASS"] = encrypt_secret(source.get("MQTT_PASS", ""))

    tmp_path = CONFIG_FILE + ".tmp"
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
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


def _normalised_dir(path):
    """Absolute, symlink-resolved directory with a trailing slash, so /a/Game
    does not match /a/Game 2."""
    return os.path.join(os.path.realpath(os.path.expanduser(path)), "")


def wine_to_unix(arg):
    r"""Turn Wine's Z:\home\me\Game\game.exe into /home/me/Game/game.exe.

    Z: is Wine's view of the Linux root. Other drive letters live inside the
    prefix and cannot be resolved without it, so they are returned unchanged.
    """
    if not isinstance(arg, str) or not WINE_DRIVE_RE.match(arg):
        return arg
    return "/" + arg[3:].replace("\\", "/")


def _read_json(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        debug_log(f"Could not read {path}: {e}")
        return None


# --- STEAM ---
def _read_vdf_pairs(path):
    """Flat "key" "value" pairs from a Valve KeyValues file. Nesting is ignored,
    which is enough for appmanifest name/installdir and library paths."""
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return re.findall(r'"([^"]*)"\s+"([^"]*)"', f.read())
    except OSError:
        return []


def find_steam_root():
    for root in STEAM_ROOTS:
        if os.path.isdir(os.path.join(root, "steamapps")):
            return os.path.realpath(root)
    return ""


def get_steam_library_paths(steam_root):
    libraries = [steam_root]
    vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
    for key, value in _read_vdf_pairs(vdf):
        if key == "path" and value:
            path = os.path.realpath(value.replace("\\\\", "\\"))
            if path not in libraries:
                libraries.append(path)
    return [lib for lib in libraries if os.path.isdir(os.path.join(lib, "steamapps"))]


def get_steam_mapping():
    """appid string -> game name, from every library's appmanifest files."""
    mapping = {}
    steam_root = find_steam_root()
    libraries = get_steam_library_paths(steam_root) if steam_root else []
    for lib in libraries:
        for acf in glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf")):
            pairs = dict(_read_vdf_pairs(acf))
            appid, name = pairs.get("appid"), _clean_name(pairs.get("name"))
            if not appid or not name:
                continue
            if name.lower().startswith(STEAM_TOOL_PREFIXES):
                continue
            mapping[appid] = name
    STEAM_SCAN_INFO.update({"root": steam_root, "libraries": libraries, "games": len(mapping)})
    debug_log(f"Steam: {len(mapping)} games in {len(libraries)} libraries.")
    return mapping


def steam_appid_from_cmdline(cmdline):
    """The AppId from Steam's game wrapper: reaper SteamLaunch AppId=NNN -- ...

    Every Steam game launch goes through reaper, native and Proton alike, so
    this is the one signal that covers both.
    """
    if "SteamLaunch" not in cmdline:
        return None
    for arg in cmdline:
        if arg.startswith("AppId="):
            appid = arg[6:]
            return appid if appid.isdigit() and appid != "0" else None
    return None


def find_steam_game(snapshot):
    global STEAM_BY_APPID
    for info in snapshot.values():
        appid = steam_appid_from_cmdline(info["cmdline"])
        if not appid:
            continue
        name = STEAM_BY_APPID.get(appid)
        if not name:
            # Installed since startup: rescan once rather than wait for a restart.
            STEAM_BY_APPID = get_steam_mapping()
            name = STEAM_BY_APPID.get(appid)
        if name:
            return name
        # A tool AppId (Proton, runtime) was skipped on purpose; anything else
        # is a game whose manifest could not be read.
        debug_log(f"Steam AppId {appid} is running but has no readable manifest.")
    return None


# --- HEROIC (Epic, GOG, Amazon) ---
def find_heroic_root():
    for root in HEROIC_ROOTS:
        if os.path.isdir(root):
            return root
    return ""


def _titles_from_library(data, list_key):
    """app_name -> title from a Heroic store_cache library file."""
    titles = {}
    if isinstance(data, dict):
        for entry in data.get(list_key) or []:
            if isinstance(entry, dict) and entry.get("app_name") and entry.get("title"):
                titles[str(entry["app_name"])] = str(entry["title"])
    return titles


def get_heroic_games():
    """[(install dir, title, launcher label)] for every game Heroic installed."""
    games = []
    root = find_heroic_root()
    counts = {"Epic": 0, "GOG": 0, "Amazon Games": 0}
    if not root:
        HEROIC_SCAN_INFO.update({"root": "", **counts})
        return games

    def add(path, title, label):
        if path and title and os.path.isdir(os.path.expanduser(path)):
            games.append((_normalised_dir(path), str(title), label))
            counts[label] += 1

    # Epic, through legendary. installed.json carries the title itself.
    if platform_enabled("Epic"):
        data = _read_json(os.path.join(root, "legendaryConfig", "legendary", "installed.json"))
        if isinstance(data, dict):
            for app in data.values():
                if isinstance(app, dict):
                    add(app.get("install_path"), app.get("title"), "Epic")

    # GOG, through gogdl. Titles come from Heroic's library cache.
    if platform_enabled("GOG"):
        titles = _titles_from_library(
            _read_json(os.path.join(root, "store_cache", "gog_library.json")), "games")
        data = _read_json(os.path.join(root, "gog_store", "installed.json"))
        installed = data.get("installed", []) if isinstance(data, dict) else []
        for app in installed:
            if isinstance(app, dict):
                path = app.get("install_path")
                title = titles.get(str(app.get("appName"))) or (
                    os.path.basename(os.path.normpath(path)) if path else None)
                add(path, title, "GOG")

    # Amazon, through nile.
    if platform_enabled("Amazon Games"):
        titles = _titles_from_library(
            _read_json(os.path.join(root, "store_cache", "nile_library.json")), "library")
        data = _read_json(os.path.join(root, "nile_config", "nile", "installed.json"))
        for app in data if isinstance(data, list) else []:
            if isinstance(app, dict):
                path = app.get("path")
                title = titles.get(str(app.get("id"))) or (
                    os.path.basename(os.path.normpath(path)) if path else None)
                add(path, title, "Amazon Games")

    # Longest path first, so a game installed inside another's folder wins.
    games.sort(key=lambda g: len(g[0]), reverse=True)
    HEROIC_SCAN_INFO.update({"root": root, **counts})
    debug_log(f"Heroic ({root}): {counts}")
    return games


# Heroic's files that list installed games and their titles, relative to its
# config folder. A change to any of them means a game was installed, moved or
# removed while the agent was running.
HEROIC_LIST_FILES = (
    ("legendaryConfig", "legendary", "installed.json"),
    ("gog_store", "installed.json"),
    ("store_cache", "gog_library.json"),
    ("nile_config", "nile", "installed.json"),
    ("store_cache", "nile_library.json"),
)
_HEROIC_SIGNATURE = [None]


def heroic_signature():
    """Modification times of Heroic's game lists, across every install
    location, so a change (or Heroic being installed) is noticed cheaply."""
    sig = []
    for root in HEROIC_ROOTS:
        for parts in HEROIC_LIST_FILES:
            try:
                sig.append(os.stat(os.path.join(root, *parts)).st_mtime_ns)
            except OSError:
                sig.append(None)
    return tuple(sig)


def refresh_heroic_games():
    """Rebuild HEROIC_GAMES if Heroic's lists changed since the last check."""
    global HEROIC_GAMES
    sig = heroic_signature()
    if sig == _HEROIC_SIGNATURE[0]:
        return
    first = _HEROIC_SIGNATURE[0] is None
    _HEROIC_SIGNATURE[0] = sig
    HEROIC_GAMES = get_heroic_games()
    if not first:
        debug_log("Heroic's game lists changed; installed games reloaded.")


def process_paths(info):
    """Every filesystem path a process points at: its exe and any absolute
    command-line argument, with Wine Z: paths converted. Under Wine or Proton
    the exe is the Wine loader, so the game's path is only in the arguments."""
    paths = []
    if info["exe"]:
        paths.append(info["exe"])
    for arg in info["cmdline"]:
        arg = wine_to_unix(arg)
        if isinstance(arg, str) and arg.startswith("/"):
            paths.append(arg)
    return paths


def find_heroic_game(snapshot):
    """(title, label) of a running game from a Heroic install folder."""
    refresh_heroic_games()
    if not HEROIC_GAMES:
        return None
    for info in snapshot.values():
        if info["name"] in IGNORE_NAMES:
            continue
        for path in process_paths(info):
            for install_dir, title, label in HEROIC_GAMES:
                if path.startswith(install_dir) and platform_enabled(label):
                    return title, label
    return None


# --- LUTRIS ---
def _read_environ(pid):
    try:
        return psutil.Process(pid).environ()
    except (psutil.Error, OSError):
        return {}


def lutris_game_from_wrapper(cmdline):
    """Older Lutris runs games through lutris-wrapper, whose first argument is
    the game name: [python3] /usr/bin/lutris-wrapper "Game Name" ..."""
    for i, arg in enumerate(cmdline):
        if os.path.basename(arg) == "lutris-wrapper" and i + 1 < len(cmdline):
            return cmdline[i + 1].strip() or None
    return None


def find_lutris_game(snapshot):
    """Current Lutris sets GAME_NAME and LUTRIS_GAME_UUID on the game's
    environment. Reading environ is relatively costly, so only processes that
    descend from Lutris are checked."""
    lutris_pids = {pid for pid, info in snapshot.items() if info["name"] == "lutris"}
    for pid, info in snapshot.items():
        title = lutris_game_from_wrapper(info["cmdline"])
        if title:
            return title
        if not lutris_pids or info["name"] in IGNORE_NAMES:
            continue
        if not any(a in lutris_pids for a in ancestor_pids(pid, snapshot)):
            continue
        env = _read_environ(pid)
        if env.get("LUTRIS_GAME_UUID") and env.get("GAME_NAME"):
            return env["GAME_NAME"].strip() or None
    return None


# --- EMULATORS (PINE) ---
# PINE is the IPC protocol PCSX2 defines and RPCS3 also speaks. Each message is
# a little-endian u32 total size followed by an opcode; each reply is the size,
# a result byte (0 = OK) and the answer. On Linux each emulator listens on a
# Unix socket in $XDG_RUNTIME_DIR, or inside its Flatpak's runtime folder. The
# server is off by default in both emulators and needs a restart once enabled.
PINE_EMULATORS = {
    "PCSX2": {"procs": ("pcsx2",), "socket": "pcsx2.sock", "flatpak": "net.pcsx2.PCSX2",
              "setup": "Is PINE switched on in PCSX2's Advanced settings?"},
    "RPCS3": {"procs": ("rpcs3",), "socket": "rpcs3.sock", "flatpak": "net.rpcs3.RPCS3",
              "setup": "Is the IPC server switched on in RPCS3's Advanced settings?"},
}
PINE_MSG_TITLE = 0x0B
PINE_MSG_STATUS = 0x0F
PINE_STATUS_SHUTDOWN = 2
PINE_TIMEOUT = 0.5


def _recv_exact(sock, n):
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            raise ConnectionError("PINE socket closed")
        data += chunk
    return data


def _pine_request(sock, opcode):
    sock.sendall(struct.pack("<IB", 5, opcode))
    size = struct.unpack("<I", _recv_exact(sock, 4))[0]
    if size < 5 or size > 64 * 1024:
        raise ValueError(f"bad PINE reply size {size}")
    body = _recv_exact(sock, size - 4)
    if body[0] != 0:
        raise ValueError("PINE request failed")
    return body[1:]


def _pine_title(sock):
    """Ask an open PINE connection for the running game's title, or None."""
    sock.settimeout(PINE_TIMEOUT)
    status = struct.unpack("<I", _pine_request(sock, PINE_MSG_STATUS)[:4])[0]
    if status == PINE_STATUS_SHUTDOWN:
        return None
    reply = _pine_request(sock, PINE_MSG_TITLE)
    length = struct.unpack("<I", reply[:4])[0]
    title = reply[4:4 + length].split(b"\0", 1)[0].decode("utf-8", "replace").strip()
    return title or None


# The last error logged per emulator, so a persistent failure is logged once.
_PINE_LAST_ERROR = {}


def _log_pine_error(label, message):
    if message and message != _PINE_LAST_ERROR.get(label):
        debug_log(message)
    _PINE_LAST_ERROR[label] = message


def pine_socket_paths(label):
    """Native and Flatpak socket locations. The default slot is <name>.sock;
    any other slot number is appended as <name>.sock.<slot>."""
    emu = PINE_EMULATORS[label]
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    dirs = [runtime,
            os.path.join(runtime, ".flatpak", emu["flatpak"], "xdg-run"),
            os.path.join(runtime, "app", emu["flatpak"])]
    paths = []
    for d in dirs:
        paths.extend(glob.glob(os.path.join(d, emu["socket"])))
        paths.extend(glob.glob(os.path.join(d, emu["socket"] + ".*")))
    return paths


def pine_query(path):
    """The running game's title from one PINE socket, or None."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(PINE_TIMEOUT)
        sock.connect(path)
        return _pine_title(sock)


def find_pine_game(label, snapshot):
    emu = PINE_EMULATORS[label]
    # A stale socket is left behind after a crash, so only ask while it runs.
    if not any(info["name"].startswith(emu["procs"]) for info in snapshot.values()):
        return None
    paths = pine_socket_paths(label)
    if not paths:
        _log_pine_error(label, f"{label} is running but has no PINE socket. {emu['setup']}")
        return None
    for path in paths:
        try:
            title = pine_query(path)
        except (OSError, ValueError, struct.error) as e:
            _log_pine_error(label, f"{label} PINE query on {path} failed: {e}")
            continue
        _log_pine_error(label, None)
        if title:
            return title
    return None


# --- CUSTOM ---
def get_window_titles():
    """Window titles, from wmctrl on X11. Wayland gives no way to list other
    applications' windows, so title rules are unavailable there."""
    if os.environ.get("XDG_SESSION_TYPE") == "wayland" and not os.environ.get("DISPLAY"):
        return None
    if not shutil.which("wmctrl"):
        return None
    try:
        out = subprocess.run(["wmctrl", "-l"], capture_output=True, text=True, timeout=3).stdout
    except Exception:
        return None
    # Columns: window id, desktop, host, title. The title may contain spaces.
    return [parts[3] for parts in (line.split(None, 3) for line in out.splitlines())
            if len(parts) == 4]


def running_exe_names(snapshot):
    """Process names, plus the Windows exe name of each Wine/Proton process so
    a rule for game.exe matches either way."""
    names = set()
    for info in snapshot.values():
        names.add(info["name"])
        if info["cmdline"]:
            first = info["cmdline"][0].replace("\\", "/")
            names.add(os.path.basename(first).lower())
    return names


def match_custom_game(game, running_exes, titles):
    title = game.get("title", "Unknown Custom Game")
    g_type = game.get("type", "")
    target = str(game.get("target", "")).strip().lower()
    match_type = game.get("match", "Starts With")
    if not target:
        return None
    if g_type in ("Executable (.exe)", "Process Name"):
        return title if target in running_exes else None
    if g_type == "Window Title" and titles:
        lowered = [t.lower() for t in titles]
        if match_type == "Exact Match":
            return title if target in lowered else None
        if match_type == "Contains":
            return title if any(target in t for t in lowered) else None
        if match_type == "Starts With":
            return title if any(t.startswith(target) for t in lowered) else None
    return None


def find_custom_game(snapshot):
    games = CONFIG.get("CUSTOM_GAMES", [])
    if not games:
        return None
    running = running_exe_names(snapshot)
    titles = None
    if any(g.get("type") == "Window Title" for g in games):
        titles = get_window_titles() or []
    for game in games:
        try:
            match = match_custom_game(game, running, titles)
        except Exception as e:
            debug_log(f"Skipping malformed custom game entry: {e}")
            continue
        if match:
            return match
    return None


# --- PROCESSES ---
def snapshot_processes():
    """pid -> {name, exe, cmdline, ppid, created}, one enumeration per poll."""
    snapshot = {}
    for proc in psutil.process_iter(['pid', 'ppid', 'name', 'exe', 'cmdline', 'create_time']):
        info = proc.info
        snapshot[info['pid']] = {
            "name": (info.get('name') or "").lower(),
            "exe": info.get('exe') or "",
            "cmdline": [a for a in (info.get('cmdline') or []) if isinstance(a, str)],
            "ppid": info.get('ppid') or 0,
            "created": info.get('create_time') or 0,
        }
    return snapshot


def ancestor_pids(pid, snapshot):
    info = snapshot.get(pid)
    seen = {pid}
    depth = 0
    while info and info["ppid"] and info["ppid"] not in seen and depth < MAX_ANCESTRY_DEPTH:
        parent = snapshot.get(info["ppid"])
        # A parent created after its child is a reused PID, not the real parent.
        if not parent or (parent["created"] and info["created"]
                          and parent["created"] > info["created"]):
            return
        yield info["ppid"]
        seen.add(info["ppid"])
        info = parent
        depth += 1


def resolve_sources(snapshot):
    """Every enabled source as (label, title or None), in priority order.

    Steam leads: its AppId is exact, and a Steam game started from Lutris or
    Heroic is still a Steam game. Heroic's install-folder match is next, then
    Lutris, then the emulators, then Custom rules as the catch-all. The poller and
    diagnostics both walk this list, so the report never disagrees with what
    is published.
    """
    resolved = []

    def run(label, fn):
        try:
            return fn()
        except Exception as e:
            debug_log(f"{label} detection failed this tick: {e}")
            return None

    if platform_enabled("Steam"):
        resolved.append(("Steam", run("Steam", lambda: find_steam_game(snapshot))))

    heroic = run("Heroic", lambda: find_heroic_game(snapshot))
    for label in ("Epic", "GOG", "Amazon Games"):
        if platform_enabled(label):
            resolved.append((label, heroic[0] if heroic and heroic[1] == label else None))

    if platform_enabled("Lutris"):
        resolved.append(("Lutris", run("Lutris", lambda: find_lutris_game(snapshot))))
    for label in PINE_EMULATORS:
        if platform_enabled(label):
            resolved.append((label, run(label, lambda l=label: find_pine_game(l, snapshot))))
    if platform_enabled("Custom"):
        resolved.append(("Custom", run("Custom", lambda: find_custom_game(snapshot))))
    return resolved


# --- MQTT ---
def _payload(game_title, launcher, start_time, end_time, profile):
    return {
        "Profile Name": profile,
        "Game Title": game_title,
        "Launcher": launcher,
        "Start Time": start_time,
        "End Time": end_time,
        "Machine": MACHINE_NAME,
        "OS": "Linux"
    }


def publish_global_state(status, game_title, launcher_name, wait=False):
    global GLOBAL_STATE
    game_title = str(game_title)[:MAX_TITLE_LEN]

    with STATE_LOCK:
        effective_launcher = launcher_name if status == "playing" else "None"
        if (GLOBAL_STATE["game"] == game_title
                and GLOBAL_STATE["status"] == status
                and GLOBAL_STATE["launcher"] == effective_launcher):
            return

        start_time = GLOBAL_STATE["start_time"]
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
        payload = _payload(game_title, effective_launcher, start_time, end_time,
                           profile_for_launcher(effective_launcher))

    debug_log(f"Attempting to publish via MQTT: {game_title} on {launcher_name}")
    _publish_payload(payload, wait=wait)


def _offline_payload():
    return _payload("Offline", "None", "None", "None", profile_for_launcher("None"))


def _publish_payload(payload, wait=False):
    if not client:
        return
    try:
        # QoS 1: a retained QoS 0 state message is dropped outright if the
        # link is down, which leaves Home Assistant showing a stale game.
        info = client.publish(get_state_topic(), json.dumps(payload), qos=1, retain=True)
    except Exception as e:
        debug_log(f"MQTT publish failed: {e}")
        return
    # publish() only queues the message. A non-zero rc means it was not even
    # handed to the network (usually no connection yet); _on_connect republishes
    # the current state once connected. Delivery is logged by _on_publish.
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        debug_log(f"MQTT publish not sent yet ({mqtt.error_string(info.rc)}); "
                  "the current state is re-sent on reconnect.")
        return
    if not wait:
        debug_log(f"MQTT publish queued (message {info.mid}).")
        return
    try:
        info.wait_for_publish(timeout=2)
    except Exception as e:
        debug_log(f"MQTT publish failed while waiting: {e}")
        return
    if not info.is_published():
        debug_log("MQTT publish not confirmed by the broker within 2s.")


def _on_publish(mqtt_client, userdata, mid, *args):
    # paho 1.6 passes (mid); 2.x adds (reason_code, properties).
    debug_log(f"MQTT publish delivered (message {mid}).")


def republish_current_state():
    with STATE_LOCK:
        state = dict(GLOBAL_STATE)
    if state["status"] != "playing":
        _publish_payload(_offline_payload())
        return
    _publish_payload(_payload(state["game"], state["launcher"], state["start_time"], "None",
                              profile_for_launcher(state["launcher"])))


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
    return getattr(rc, 'value', rc) == 0


def _on_connect(mqtt_client, userdata, *args):
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
    rc = args[1] if len(args) >= 2 else (args[0] if args else None)
    if not _rc_is_success(rc):
        debug_log(f"MQTT connection lost ({rc}); paho will retry automatically.")


# --- POLLER ---
class GamePoller(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()
        self.last_desired = None
        self.pending_state = None
        self.pending_ticks = 0

    def stop(self):
        self.stop_event.set()

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as e:
                debug_log(f"Poller iteration failed: {e}")
            self.stop_event.wait(CONFIG.get("POLL_INTERVAL", 5))

    def poll_once(self):
        desired = ("idle", "Offline", "None")
        for label, title in resolve_sources(snapshot_processes()):
            if title:
                desired = ("playing", title, label)
                break

        if desired != self.last_desired:
            self.last_desired = desired
            debug_log(f"Poller state: {desired[1]} on {desired[2]}" if desired[0] == "playing"
                      else "Poller state: no game running.")

        # A new state must hold for SETTLE_TICKS polls before it is published,
        # so a launcher briefly naming the wrong game never reaches HA.
        if desired == self.pending_state:
            self.pending_ticks += 1
        else:
            self.pending_state = desired
            self.pending_ticks = 1
        if self.pending_ticks >= SETTLE_TICKS:
            publish_global_state(*desired)


# --- SERVICES ---
# Set by stop_services() so a keyring wait from the previous run gives up
# instead of connecting a client nobody will stop.
SERVICES_STOP = threading.Event()
# Serialises creating the MQTT client (possibly from the keyring wait) against
# stop_services() taking it down.
CLIENT_LOCK = threading.Lock()


def stop_services():
    global client, poller
    debug_log("Stopping all background services...")
    SERVICES_STOP.set()
    if poller:
        poller.stop()
        poller.join(timeout=2)
        poller = None
    with CLIENT_LOCK:
        old_client, client = client, None
    if old_client:
        try:
            # client is already None, so publish through the old one directly.
            publish_global_state("idle", "Offline", "None")
            old_client.publish(get_state_topic(), json.dumps(_offline_payload()),
                               qos=1, retain=True).wait_for_publish(timeout=2)
            old_client.loop_stop()
            old_client.disconnect()
        except Exception as e:
            debug_log(f"Error during MQTT shutdown: {e}")


def scan_installed_games():
    global STEAM_BY_APPID
    STEAM_BY_APPID = get_steam_mapping() if platform_enabled("Steam") else {}
    # Forget the last signature so a restart (e.g. a platform switched on)
    # always rereads Heroic's lists.
    _HEROIC_SIGNATURE[0] = None
    refresh_heroic_games()


def start_services():
    global poller, CONFIG, PROFILE_SANITIZED, SERVICES_STOP
    debug_log("Starting Gaming Status Agent services...")

    CONFIG = load_config()
    PROFILE_SANITIZED = sanitize_topic_part(CONFIG.get("HA_DEVICE_NAME", "User"))
    disabled = [name for name in PLATFORM_ORDER if not platform_enabled(name)]
    if disabled:
        debug_log(f"Platforms switched off: {', '.join(disabled)}")

    SERVICES_STOP = threading.Event()
    if CONFIG.get(KEYRING_PENDING):
        # Connecting without the password would be refused, and would never be
        # retried with it, so the connection waits until the keyring answers.
        threading.Thread(target=_await_keyring_then_connect, args=(SERVICES_STOP,),
                         daemon=True).start()
    else:
        _connect_mqtt(SERVICES_STOP)

    scan_installed_games()
    poller = GamePoller()
    poller.start()
    debug_log("Game poller started successfully")


def _await_keyring_then_connect(stop_event):
    debug_log(f"MQTT password is in the keyring, which is not available yet; "
              f"retrying every {KEYRING_RETRY_SECONDS}s before connecting.")
    while not stop_event.wait(KEYRING_RETRY_SECONDS):
        password = decrypt_secret(KEYRING_MARKER, rediscover=True)
        if password is not None:
            CONFIG["MQTT_PASS"] = password
            CONFIG.pop(KEYRING_PENDING, None)
            debug_log("MQTT password read from the keyring; connecting.")
            _connect_mqtt(stop_event)
            return


def _connect_mqtt(stop_event):
    global client
    with CLIENT_LOCK:
        if stop_event.is_set():
            return
        new_client = _build_mqtt_client()
        if _configure_and_connect(new_client):
            client = new_client


# Why MQTT is not connecting, for diagnostics. Empty when nothing is wrong.
MQTT_SETUP_ERROR = ""


def _configure_and_connect(client):
    """Set up and start the client. False if it must not connect at all."""
    global MQTT_SETUP_ERROR
    MQTT_SETUP_ERROR = ""
    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_publish = _on_publish

    user = CONFIG.get("MQTT_USER", "")
    password = CONFIG.get("MQTT_PASS", "")
    if user and password:
        client.username_pw_set(user, password)

    if CONFIG.get("MQTT_TLS"):
        try:
            client.tls_set(ca_certs=CONFIG.get("MQTT_CA_CERT", "").strip() or None)
            debug_log("MQTT TLS enabled.")
        except Exception as e:
            # Connecting anyway would send the login unencrypted to a broker
            # the user asked to reach over TLS.
            MQTT_SETUP_ERROR = f"TLS setup failed, not connecting: {e}"
            debug_log(f"MQTT {MQTT_SETUP_ERROR}. Fix the CA certificate or turn TLS off.")
            return False

    # If this machine sleeps or crashes, the broker publishes Offline for us.
    try:
        client.will_set(get_state_topic(), json.dumps(_offline_payload()), qos=1, retain=True)
    except Exception as e:
        debug_log(f"Could not set MQTT last will: {e}")

    try:
        client.reconnect_delay_set(min_delay=1, max_delay=60)
        client.connect_async(CONFIG.get("MQTT_BROKER", "localhost"),
                             CONFIG.get("MQTT_PORT", 1883), 60)
        client.loop_start()
    except Exception as e:
        debug_log(f"MQTT setup failed (check IP/Port): {e}")
    return True


RESTART_LOCK = threading.Lock()


def restart_services():
    def worker():
        with RESTART_LOCK:
            try:
                stop_services()
                start_services()
            except Exception as e:
                debug_log(f"Restart failed: {e}")
    threading.Thread(target=worker, daemon=True).start()


# --- DIAGNOSTICS ---
def build_diagnostic_report():
    out = [f"Gaming Status Agent for Linux {GSA_VERSION}",
           f"Config: {CONFIG_FILE}",
           f"Device: {CONFIG.get('HA_DEVICE_NAME')}  ->  sensor.gsa_{PROFILE_SANITIZED}",
           f"Machine: {MACHINE_NAME}",
           f"Broker: {CONFIG.get('MQTT_BROKER')}:{CONFIG.get('MQTT_PORT')}"
           f"{' (TLS)' if CONFIG.get('MQTT_TLS') else ''}",
           f"MQTT: {MQTT_SETUP_ERROR or ('connected' if client and client.is_connected() else 'not connected')}",
           f"Session: {os.environ.get('XDG_SESSION_TYPE') or 'unknown'}",
           ""]

    out.append("Platforms:")
    for name in PLATFORM_ORDER:
        out.append(f"  {name}: {'on' if platform_enabled(name) else 'off'}")
    out.append("")

    out.append(f"Steam root: {STEAM_SCAN_INFO['root'] or 'not found'}")
    for lib in STEAM_SCAN_INFO["libraries"]:
        out.append(f"  library: {lib}")
    out.append(f"  games named: {STEAM_SCAN_INFO['games']}")
    out.append(f"Heroic config: {HEROIC_SCAN_INFO['root'] or 'not found'}")
    out.append(f"  installed: Epic {HEROIC_SCAN_INFO['Epic']}, GOG {HEROIC_SCAN_INFO['GOG']}, "
               f"Amazon {HEROIC_SCAN_INFO['Amazon Games']}")

    snapshot = snapshot_processes()
    running = sorted({LAUNCHER_PROCESSES[i["name"]] for i in snapshot.values()
                      if i["name"] in LAUNCHER_PROCESSES})
    out.append(f"Launchers running: {', '.join(running) or 'none'}")

    reapers = [steam_appid_from_cmdline(i["cmdline"]) for i in snapshot.values()]
    reapers = [a for a in reapers if a]
    if reapers:
        out.append(f"Steam AppIds running: {', '.join(sorted(set(reapers)))}")

    if CONFIG.get(KEYRING_PENDING):
        out.append(f"MQTT password: waiting for the keyring (retrying every {KEYRING_RETRY_SECONDS}s)")
    for label, emu in PINE_EMULATORS.items():
        if platform_enabled(label):
            sockets = pine_socket_paths(label)
            out.append(f"{label} PINE sockets: " + (", ".join(sockets) if sockets else
                       f"none. {emu['setup']} Restart {label} after switching it on."))
    if platform_enabled("Custom"):
        titles = get_window_titles()
        out.append("Window-title rules: " + ("available (wmctrl)" if titles is not None else
                   "unavailable (needs X11 and wmctrl); process-name rules still work"))
    out.append("")

    out.append("Detection, in priority order:")
    winner = None
    for label, title in resolve_sources(snapshot):
        out.append(f"  {label}: {title or '-'}")
        if title and not winner:
            winner = (title, label)
    out.append("")
    out.append(f"Would publish: {winner[0]} on {winner[1]}" if winner else
               "Would publish: Offline (no game detected)")
    with STATE_LOCK:
        out.append(f"Currently published: {GLOBAL_STATE['game']} on {GLOBAL_STATE['launcher']}")
    return "\n".join(out)


# --- AUTOSTART ---
def startup_command():
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def startup_enabled(item=None):
    return os.path.exists(AUTOSTART_FILE)


def set_startup(enabled):
    try:
        if enabled:
            os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
            with open(AUTOSTART_FILE, 'w', encoding='utf-8') as f:
                f.write("[Desktop Entry]\nType=Application\nName=Gaming Status Agent\n"
                        f"Exec={startup_command()}\nX-GNOME-Autostart-enabled=true\n"
                        "NoDisplay=false\nTerminal=false\n")
        elif os.path.exists(AUTOSTART_FILE):
            os.remove(AUTOSTART_FILE)
        return True
    except Exception as e:
        debug_log(f"Could not update autostart: {e}")
        return False


# --- GUI & TRAY ---
def _gui():
    """Imported lazily so --headless and --diagnose need neither Tk nor a display."""
    import tkinter as tk
    from tkinter import messagebox, ttk, scrolledtext
    return tk, messagebox, ttk, scrolledtext


def _focus_existing(name):
    tk = _gui()[0]
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


def _make_settings_window(key, title):
    tk = _gui()[0]
    win = tk.Toplevel(ROOT)
    OPEN_WINDOWS[key] = win
    win.title(title)
    win.resizable(False, False)
    win.protocol("WM_DELETE_WINDOW", lambda: (OPEN_WINDOWS.pop(key, None), win.destroy()))
    return win


def _add_entry_rows(win, fields, row=0):
    tk = _gui()[0]
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
    """Add the Save button. No size is ever set on these windows: Tk fits each
    one to its contents, so none has dead space or a cut-off button however
    many rows it holds."""
    tk = _gui()[0]
    tk.Button(win, text="Save & Apply", command=on_save, width=20).grid(
        row=row, column=0, columnspan=2, padx=15, pady=15)
    win.focus_force()


def _save_and_close(key, win):
    messagebox = _gui()[1]
    if not save_config():
        messagebox.showerror("Error", f"Could not save settings. See {DEBUG_LOG_FILE}.", parent=win)
        return
    OPEN_WINDOWS.pop(key, None)
    win.destroy()
    restart_services()


MQTT_FIELDS = [
    ("HA_DEVICE_NAME", "HA Device Name"),
    ("MQTT_BROKER", "MQTT Broker IP"),
    ("MQTT_PORT", "MQTT Port"),
    ("MQTT_USER", "MQTT Username"),
    ("MQTT_PASS", "MQTT Password"),
    ("POLL_INTERVAL", "Poll Rate (s)")
]


def show_settings_ui():
    if _focus_existing("settings"):
        return
    tk, messagebox, _, _ = _gui()
    win = _make_settings_window("settings", "Gaming Status Agent - MQTT Settings")
    vars_dict, row = _add_entry_rows(win, MQTT_FIELDS)

    tls_var = tk.BooleanVar(value=bool(CONFIG.get("MQTT_TLS", False)))
    tk.Checkbutton(win, text="Use TLS (port is usually 8883)",
                   variable=tls_var).grid(row=row, column=1, padx=10, pady=4, sticky="w")
    row += 1
    tk.Label(win, text="CA Cert (optional)").grid(row=row, column=0, padx=15, pady=8, sticky="e")
    ca_var = tk.StringVar(value=str(CONFIG.get("MQTT_CA_CERT", "")))
    tk.Entry(win, textvariable=ca_var, width=32).grid(row=row, column=1, padx=10, pady=8, sticky="w")
    row += 1

    def save():
        if not _clean_name(vars_dict["HA_DEVICE_NAME"].get()):
            messagebox.showerror("Error", "HA Device Name cannot be empty.\n\n"
                                 "It becomes your Home Assistant sensor name and the MQTT topic.",
                                 parent=win)
            return
        for key, _ in MQTT_FIELDS:
            val = vars_dict[key].get()
            if key == "MQTT_PORT":
                val = _coerce_int(val, 1883, 1, 65535)
            elif key == "POLL_INTERVAL":
                val = _coerce_int(val, 5, 1, 3600)
            elif key in ("HA_DEVICE_NAME", "MQTT_BROKER", "MQTT_USER"):
                val = _clean_name(val)
            CONFIG[key] = val
        CONFIG["MQTT_TLS"] = bool(tls_var.get())
        CONFIG["MQTT_CA_CERT"] = ca_var.get().strip()
        if CONFIG["MQTT_PASS"]:
            CONFIG.pop(KEYRING_PENDING, None)
        _save_and_close("settings", win)

    _finish_settings_window(win, row, save)


def gamertag_fields():
    return [(LAUNCHER_PROFILE_KEYS[name], name) for name in PLATFORM_ORDER
            if name in LAUNCHER_PROFILE_KEYS and platform_enabled(name)]


def show_gamertags_ui():
    if _focus_existing("gamertags"):
        return
    tk = _gui()[0]
    fields = gamertag_fields()
    win = _make_settings_window("gamertags", "Gaming Status Agent - Gamertags")
    if not fields:
        tk.Label(win, text="No tracked platform uses a gamertag.\n"
                           "Turn one on under Platforms first.",
                 justify="left").grid(row=0, column=0, columnspan=2, padx=15, pady=20, sticky="w")
        return
    vars_dict, row = _add_entry_rows(win, fields)

    def save():
        for key, _ in fields:
            CONFIG[key] = vars_dict[key].get().strip()
        _save_and_close("gamertags", win)

    _finish_settings_window(win, row, save)


def show_platforms_ui():
    if _focus_existing("platforms"):
        return
    tk = _gui()[0]
    win = _make_settings_window("platforms", "Gaming Status Agent - Platforms")
    tk.Label(win, text="Which platforms should be tracked?",
             font=("", 10, "bold")).grid(row=0, column=0, columnspan=2,
                                         padx=15, pady=(12, 6), sticky="w")
    vars_dict = {}
    row = 1
    for label in PLATFORM_ORDER:
        var = tk.BooleanVar(value=platform_enabled(label))
        vars_dict[label] = var
        text = f"{label}  ({PLATFORM_NOTES[label]})" if label in PLATFORM_NOTES else label
        tk.Checkbutton(win, text=text, variable=var).grid(
            row=row, column=0, columnspan=2, padx=15, sticky="w")
        row += 1

    def save():
        for label, var in vars_dict.items():
            CONFIG[PLATFORM_ENABLE_KEYS[label]] = bool(var.get())
        _save_and_close("platforms", win)

    _finish_settings_window(win, row, save)


CUSTOM_TYPES = ["Process Name", "Window Title"]


def show_custom_games_ui():
    if _focus_existing("custom_games"):
        return
    tk, messagebox, ttk, _ = _gui()
    win = tk.Toplevel(ROOT)
    OPEN_WINDOWS["custom_games"] = win
    win.title("Gaming Status Agent - Custom Games")
    win.geometry("560x360")
    win.protocol("WM_DELETE_WINDOW", lambda: (OPEN_WINDOWS.pop("custom_games", None), win.destroy()))

    columns = ('title', 'type', 'target', 'match')
    tree = ttk.Treeview(win, columns=columns, show='headings')
    for col, text, width in (('title', 'Game Title', 140), ('type', 'Match Method', 120),
                             ('target', 'Target Value', 160), ('match', 'Rule', 100)):
        tree.heading(col, text=text)
        tree.column(col, width=width)
    tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def refresh_list():
        for item in tree.get_children():
            tree.delete(item)
        for game in CONFIG.get("CUSTOM_GAMES", []):
            tree.insert('', tk.END, values=(game.get("title", ""), game.get("type", ""),
                                            game.get("target", ""), game.get("match", "Starts With")))

    refresh_list()

    def add_game():
        add_win = tk.Toplevel(win)
        add_win.title("Add Custom Game")
        add_win.geometry("400x300")

        tk.Label(add_win, text="Game Title:").pack(pady=2)
        title_entry = tk.Entry(add_win, width=40)
        title_entry.pack()
        tk.Label(add_win, text="Match Method:").pack(pady=2)
        type_var = tk.StringVar(value=CUSTOM_TYPES[0])
        ttk.Combobox(add_win, textvariable=type_var, values=CUSTOM_TYPES,
                     state="readonly", width=37).pack()
        tk.Label(add_win, text="Target (process name, game.exe for Wine, or window title):").pack(pady=2)
        target_entry = tk.Entry(add_win, width=40)
        target_entry.pack()
        tk.Label(add_win, text="Window Match Rule (Window Title only, X11 only):").pack(pady=2)
        match_var = tk.StringVar(value="Starts With")
        ttk.Combobox(add_win, textvariable=match_var, values=["Starts With", "Exact Match", "Contains"],
                     state="readonly", width=37).pack()

        def save_new():
            t, target = title_entry.get().strip(), target_entry.get().strip()
            if not t or not target:
                messagebox.showerror("Error", "Game Title and Target are required.", parent=add_win)
                return
            CONFIG.setdefault("CUSTOM_GAMES", []).append(
                {"title": t, "type": type_var.get(), "target": target, "match": match_var.get()})
            if not save_config():
                messagebox.showerror("Error", f"Could not save. See {DEBUG_LOG_FILE}.", parent=add_win)
                return
            refresh_list()
            add_win.destroy()
            restart_services()

        tk.Button(add_win, text="Save Game", command=save_new).pack(pady=15)
        add_win.transient(win)
        add_win.grab_set()

    def remove_game():
        selected = tree.selection()
        if not selected:
            return
        values = tree.item(selected[0])['values']
        CONFIG["CUSTOM_GAMES"] = [
            g for g in CONFIG.get("CUSTOM_GAMES", [])
            if not (str(g.get("title", "")) == str(values[0])
                    and str(g.get("target", "")) == str(values[2]))
        ]
        if not save_config():
            messagebox.showerror("Error", f"Could not save. See {DEBUG_LOG_FILE}.", parent=win)
            return
        refresh_list()
        restart_services()

    btn_frame = tk.Frame(win)
    btn_frame.pack(pady=10)
    tk.Button(btn_frame, text="Add Game", command=add_game, width=15).pack(side=tk.LEFT, padx=10)
    tk.Button(btn_frame, text="Remove Selected", command=remove_game, width=15).pack(side=tk.LEFT, padx=10)
    win.focus_force()


def show_diagnostics_ui():
    if _focus_existing("diagnostics"):
        return
    tk, _, _, scrolledtext = _gui()
    win = tk.Toplevel(ROOT)
    OPEN_WINDOWS["diagnostics"] = win
    win.title("Gaming Status Agent - Diagnostics")
    win.geometry("640x520")
    win.protocol("WM_DELETE_WINDOW", lambda: (OPEN_WINDOWS.pop("diagnostics", None), win.destroy()))
    text = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("monospace", 9))
    text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def refresh():
        text.config(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        try:
            text.insert(tk.END, build_diagnostic_report())
        except Exception as e:
            text.insert(tk.END, f"Diagnostics failed: {e}")
        text.config(state=tk.DISABLED)

    tk.Button(win, text="Refresh", command=refresh, width=15).pack(pady=(0, 10))
    refresh()


def _on_ui(fn):
    return lambda icon, item: ROOT.after(0, fn)


def force_offline(icon, item):
    if poller:
        poller.last_desired = None
    publish_global_state("idle", "Offline", "None")


def toggle_startup(icon, item):
    set_startup(not startup_enabled())


def quit_app(icon, item):
    stop_services()
    icon.stop()
    ROOT.after(0, ROOT.quit)


def create_image():
    from PIL import Image, ImageDraw
    icon_path = os.path.join(BASE_DIR, os.pardir, "assets", "gsa_icon.ico")
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


def create_tray_menu():
    import pystray
    return pystray.Menu(
        pystray.MenuItem("MQTT Settings", _on_ui(show_settings_ui)),
        pystray.MenuItem("Platforms", _on_ui(show_platforms_ui)),
        pystray.MenuItem("Gamertags", _on_ui(show_gamertags_ui),
                         visible=lambda item: bool(gamertag_fields())),
        pystray.MenuItem("Custom Games", _on_ui(show_custom_games_ui),
                         visible=lambda item: platform_enabled("Custom")),
        pystray.MenuItem("Start Gaming Status Agent at Login", toggle_startup,
                         checked=startup_enabled),
        pystray.MenuItem("Run Diagnostics", _on_ui(show_diagnostics_ui)),
        pystray.MenuItem("Force Offline", force_offline),
        pystray.MenuItem("Quit", quit_app)
    )


def run_tray():
    global ROOT, CONFIG
    import pystray
    tk = _gui()[0]
    ROOT = tk.Tk()
    ROOT.withdraw()
    try:
        from PIL import ImageTk
        ROOT._gsa_icon = ImageTk.PhotoImage(create_image())
        ROOT.iconphoto(True, ROOT._gsa_icon)
    except Exception as e:
        debug_log(f"Could not apply window icon: {e}")

    first_run = not os.path.exists(CONFIG_FILE)
    if first_run:
        # Write the defaults now so a closed window does not bring this back
        # every launch; every setting stays editable from the tray.
        CONFIG = load_config()
        save_config()
        set_startup(True)
    start_services()

    icon = pystray.Icon("gaming-status-agent", create_image(), "Gaming Status Agent",
                        create_tray_menu())
    threading.Thread(target=icon.run, daemon=True).start()
    if first_run:
        ROOT.after(300, show_platforms_ui)
        ROOT.after(400, show_settings_ui)
    ROOT.mainloop()


def run_headless():
    if not os.path.exists(CONFIG_FILE):
        global CONFIG
        CONFIG = load_config()
        save_config()
        debug_log(f"Wrote a default config to {CONFIG_FILE}. Set MQTT_BROKER and the "
                  "ENABLE_* switches there, then restart.")
    start_services()
    done = threading.Event()

    def shutdown(signum, frame):
        done.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while not done.wait(1):
        pass
    stop_services()


def run_diagnose():
    global CONFIG, PROFILE_SANITIZED
    CONFIG = load_config()
    PROFILE_SANITIZED = sanitize_topic_part(CONFIG.get("HA_DEVICE_NAME", "User"))
    scan_installed_games()
    print(build_diagnostic_report())


def main():
    global HEADLESS
    parser = argparse.ArgumentParser(description="Gaming Status Agent for Linux")
    parser.add_argument("--headless", action="store_true", help="run without tray or windows")
    parser.add_argument("--diagnose", action="store_true", help="print one detection pass and exit")
    args = parser.parse_args()

    if args.diagnose:
        run_diagnose()
        return
    HEADLESS = args.headless
    _init_logging()
    debug_log(f"=== GAMING STATUS AGENT (LINUX) {GSA_VERSION} LAUNCHED ===")
    if HEADLESS:
        run_headless()
    else:
        run_tray()


if __name__ == "__main__":
    main()
