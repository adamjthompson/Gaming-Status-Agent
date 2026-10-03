# Gaming Status Agent

A Windows system-tray app that reports what you're playing to Home Assistant
over MQTT. It detects games from the following launchers and publishes a single sensor with the game name, the launcher, and when you started playing:

- Amazon Games
- Battle.net
- EA
- Epic
- GOG
- HoYoverse
- Minecraft
- PCSX2 (PlayStation 2 emulator)
- Playnite (including emulated titles launched through Playnite)
- Riot Games
- Roblox
- Rockstar Games
- Steam
- Ubisoft
- Xbox

Home Assistant discovers the sensor automatically. No YAML required. 

A separate Linux version covers the following (see [Linux](#linux)):
- Heroic (Epic, GOG and Amazon)
- Lutris
- PCSX2
- Steam (native and Proton)

---

## Requirements

- Windows, or Linux for `linux/gsa_linux.py` (see [Linux](#linux))
- A Home Assistant instance with the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/)
  set up and a broker it can reach (Mosquitto is the usual choice)
- Python 3.8+ if running from source
 
## Install

**From a release:** download `Gaming Status Agent.exe` from the
[Releases page](../../releases/latest), put it anywhere you like, and run it.
It creates its config file next to itself, so a dedicated folder is tidiest.

**From source:**

```
pip install -r windows/requirements.txt
python windows/gsa_tracker.py
```

### Repository layout

| Folder | Contents |
|---|---|
| `windows/` | The Windows agent (`gsa_tracker.py`), its build script and requirements |
| `linux/` | The Linux agent (`gsa_linux.py`), requirements, systemd service and desktop entry |
| `assets/` | `gsa_icon.ico`, used by both agents |
| `catalog.json` | Ubisoft game names. Stays at the root: released exes download it from there |

On first run a short setup wizard walks you through three pages:

1. **MQTT Settings** — your broker address and login. The device name starts as
   your Windows account name and becomes the Home Assistant sensor.
2. **Platforms** — launchers found on this PC are already switched on. Steam and
   Xbox are marked if found but stay off (see below).
3. **Gamertags** — optional, one per selected platform, plus a **Start at login**
   option that is ticked by default.

**Finish** writes `gsa_config.json` and puts a controller icon in your system
tray. **Cancel** still saves the detected platforms and anything already
entered, so the wizard runs only once; every setting stays editable from the
tray menu.

## The tray menu

| Item | What it does |
|---|---|
| **MQTT Settings** | Broker address, port, credentials, TLS, the device name, poll rate |
| **Platforms** | Which stores to track — Steam and Xbox start switched off |
| **Gamertags** | Your gamertag per store — shown as the sensor's `Profile Name`. Lists only the platforms you're tracking |
| **Custom Games** | Match games Gaming Status Agent doesn't detect on its own, by executable or window title. Appears once **Custom** is switched on under Platforms |
| **Run Diagnostics** | Shows exactly what Gaming Status Agent currently sees and why. Start here when something looks wrong |
| **Force Offline** | Immediately publish Offline, whatever detection thinks |
| **Quit** | Publishes Offline, then exits |

## The sensor

Gaming Status Agent publishes one sensor, `sensor.gsa_<device name>`, whose state is the game
title. Attributes:

| Attribute | Example |
|---|---|
| `Game Title` | `Borderlands 2` |
| `Launcher` | `Epic` |
| `Profile Name` | Your gamertag for that store, or the device name if unset |
| `Start Time` | `2026-09-21 14:09:40` |
| `End Time` | Set when a session ends |
| `Machine` | The computer's name, e.g. `GAMING-PC` |
| `OS` | `Windows` or `Linux` |

If the PC loses power or drops off the network, the broker publishes `Offline`
on Gaming Status Agent's behalf via its Last Will, so the sensor never sticks on a game you
stopped playing hours ago.

---

## How detection works

Gaming Status Agent uses whichever source knows the game's real name, in this order:

1. **Epic** — reads the launcher log for game starts and the local manifests for
   real titles. Exits are detected by watching the game's process, because
   Epic's log does not reliably announce them.
2. **Steam** *(off by default)* — reads the appid Steam itself reports as
   running from its client registry key, and names it from the
   `appmanifest_*.acf` files across every Steam library on the machine.
3. **GOG** — reads GOG Galaxy's registry entries. Matches on the game's
   executable, so it works even when a DRM-free game is started straight from a
   shortcut with Galaxy closed.
4. **Battle.net** — reads Blizzard's uninstall entries for install locations,
   then matches any process running from inside one.
5. **Xbox / Microsoft Store** *(off by default)* — reads installed Store
   packages from the app registry, keeps the ones carrying a game manifest, and
   matches any process running from inside a package folder.
6. **Riot Games, HoYoverse, Minecraft, Roblox and Rockstar Games** *(off by default)* — matched by the
   game's own executable, so lobbies and launchers never count as playing.
   League of Legends and Teamfight Tactics share one executable; the game's
   local Live Client Data API tells them apart once a match has loaded.
   Minecraft Java is recognised by a `javaw.exe` window titled "Minecraft",
   which covers the official launcher, CurseForge, Prism and MultiMC alike.
   Roblox is the player client, however it was started (Bloxstrap included);
   it reports as "Roblox" rather than the experience being played.
   Rockstar Games covers titles bought from Rockstar directly; Steam and Epic
   copies still report under those stores.
7. **PCSX2** *(off by default)* — asks the running emulator for the game's
   title over PINE, PCSX2's control port. See [PCSX2](#pcsx2) below.
8. **Window ancestry** — for everything else, finds a visible window whose
   process descends from a known launcher and uses its title.

Sources that know a game's real name outrank raw window titles, so a
Playnite-launched Epic game reports as **Epic** with its proper name rather than
as Playnite with whatever the window happens to be called.

Every source above is local. Nothing needs an API key, an internet connection or
a public profile, and a private account is detected just as well as a public one.

### Choosing platforms

Right-click the tray icon and pick **Platforms** to switch any source on or off.
Turning one off stops it being detected immediately on **Save & Apply** — no
restart needed.

### Steam and Xbox are off by default

Both have an official Home Assistant integration of their own
([Steam](https://www.home-assistant.io/integrations/steam_online/),
[Xbox](https://www.home-assistant.io/integrations/xbox/)) that reads your status
from the vendor's API. Those are authoritative, cover console and remote play,
and keep working when this PC is switched off, so Gaming Status Agent does not
switch itself on alongside them without being asked.

Turn them on in **Platforms** if you want one sensor covering all PC play, or if
you'd rather not connect an API key and a public profile. If you run both, expect
two entities reporting the same session.

While Steam is off, anything launched through Steam is ignored, Playnite-launched
Steam games included. A **Custom Games** rule still overrides that, because it's
deliberate configuration rather than automatic detection.

### Riot Games, HoYoverse, Minecraft, Roblox and Rockstar Games are opt-in

These are matched by fixed executable names rather than a launcher's own
records, so they stay off until you switch them on under **Platforms**. Once
HoYoverse is on, a Custom Games rule for `genshinimpact.exe` is no longer needed.

### PCSX2

PCSX2 reports the game you're playing over PINE, its built-in control
interface. To use it:

1. In PCSX2, turn on PINE under *Settings → Advanced → PINE*, and leave the
   slot at the default **28011**.
2. Restart PCSX2 once so PINE starts listening.
3. In Gaming Status Agent, switch on **PCSX2** under **Platforms**.

It stays off by default because nothing works until PINE is turned on in
PCSX2. If you changed PCSX2's PINE slot, set `PCSX2_PINE_SLOT` in
`gsa_config.json` to match. A PS2 game launched from Playnite also reports as
**PCSX2** with its proper title, rather than as Playnite with the emulator's
window title.

### What Playnite does and doesn't cover

Playnite hands most store games off to their own client, so those report under
that store. Playnite gets the credit only where it genuinely owns the process (emulated and manually added games) and those are named from the window title,
which for emulators is often the emulator rather than the ROM. Use a **Custom
Games** rule to give those a proper name.

If Playnite is set to close itself when a game starts, the process chain breaks
and the game can't be attributed to it.

---

## Configuration

Most settings live in the tray menu. `gsa_config.json` holds a few extra keys
for less common situations.

### `ENABLE_*`

One key per platform, all editable from **Platforms** in the tray menu. Listed
here because they're handy to set when deploying the same config to several PCs:

| Key | Default |
| --- | --- |
| `ENABLE_EPIC` | `true` |
| `ENABLE_UBISOFT` | `true` |
| `ENABLE_GOG` | `true` |
| `ENABLE_BATTLENET` | `true` |
| `ENABLE_EA` | `true` |
| `ENABLE_AMAZON` | `true` |
| `ENABLE_PLAYNITE` | `true` |
| `ENABLE_CUSTOM` | `false` |
| `ENABLE_STEAM` | `false` |
| `ENABLE_XBOX` | `false` |
| `ENABLE_RIOT` | `false` |
| `ENABLE_HOYOVERSE` | `false` |
| `ENABLE_MINECRAFT` | `false` |
| `ENABLE_ROBLOX` | `false` |
| `ENABLE_ROCKSTAR` | `false` |

A switched-off platform is skipped completely — its registry and manifest scans
never run at startup, and the poller doesn't check it each tick. Turning off
everything you don't use makes the poll measurably cheaper.

Upgrading from a version without these keys keeps the same behaviour: the `true`
defaults above cover everything that used to be detected. `ENABLE_CUSTOM` is the
one exception — it defaults to `false` for a fresh install, but a config that
already has `CUSTOM_GAMES` rules switches it on automatically so those keep
working.

### `CATALOG_URL`

Ubisoft's launcher log identifies games by numeric id, so Gaming Status Agent downloads a
community-maintained id → name catalog. Point this at your own copy if you're
running a fork, or if your network blocks `raw.githubusercontent.com`:

```json
"CATALOG_URL": "[https://example.com/my-catalog.json](https://example.com/my-catalog.json)"
```

Only `http://` and `https://` are accepted; anything else reverts to the
default. The file is cached locally, so a fetch failure isn't fatal.

Catalog format — the top level groups games however you like:

```json
{
  "Assassin's Creed": { "720": "Assassin's Creed II" },
  "Far Cry":          { "370": "Far Cry 3" }
}
```

### `UBISOFT_OVERRIDES`

To fix or add a single Ubisoft game without touching the catalog:

```json
"UBISOFT_OVERRIDES": { "5678": "My Game Name" }
```

Overrides beat both the catalog and the registry.

### `POLL_INTERVAL`

How often the detector runs, in seconds. Default `5`. Lower reacts faster and
costs more CPU.

---

## Troubleshooting

**Start with Run Diagnostics.** It lists every visible window, the process chain
behind it, and whether Gaming Status Agent detected it *and why not* if it didn't, then shows
which source won and what would be published. Copy to Clipboard puts the whole
report on your clipboard for a bug report. It contains no passwords.

`gsa_debug.log` sits next to the app and rotates at 1 MB.

| Symptom | Likely cause |
|---|---|
| No sensor in Home Assistant | Broker address or credentials. Diagnostics shows whether MQTT is connected |
| Sensor stuck on an old game | Use Force Offline. If it recurs, send the diagnostics report |
| A Steam or Xbox game isn't detected | Both start switched off. Turn them on under **Platforms**, or use HA's own Steam/Xbox integration |
| An Xbox game reports an odd name | Its Store display name couldn't be resolved, so the package id was used. Add a Custom Games rule to override it |
| Wrong name for an emulated game | Add a Custom Games rule matching the window title |
| A game isn't detected at all | Diagnostics will show its window and process chain. If its launcher shows as `UNRECOGNISED`, open an issue with that line |
| Ubisoft games named `Unknown Ubisoft Game (1234)` | The catalog didn't have that id. Add a `UBISOFT_OVERRIDES` entry, and consider opening a PR against the catalog |

Newly installed games are picked up on restart. **Save & Apply** in Settings
refreshes without closing Gaming Status Agent.

---

## Privacy

Everything stays between this PC and your broker. Gaming Status Agent makes exactly one
outbound internet request for fetching the Ubisoft name catalog and sends no
telemetry.

Your MQTT password is encrypted at rest with Windows DPAPI, scoped to your
Windows account on this machine. A config file copied to another PC won't
decrypt, and you'll be asked to re-enter it. TLS is available in MQTT Settings
and is off by default, matching the usual home-LAN setup.

These files are written next to the app and contain personal data. They're
excluded by `.gitignore`; don't commit them:

```
gsa_config.json      broker, username, encrypted password, gamertags
gsa_debug.log        what you played and when
gsa_diagnostics.txt  broker address, window titles, process list
gsa_ubi_ids.json     cached game-name catalog
```

## Linux

`linux/gsa_linux.py` is a separate agent for Linux. It publishes the same sensor in
the same format, so Home Assistant and the Gaming Status integration treat it
exactly like the Windows agent.

### What it detects

| Platform | How |
|---|---|
| **Steam**, native and Proton *(off by default, as on Windows)* | Steam starts every game through its `reaper` wrapper with `AppId=<id>`, Proton games included. The title comes from the `appmanifest_*.acf` files in every Steam library. Native, Flatpak and Snap installs of Steam are all found. |
| **Epic, GOG, Amazon Games** via [Heroic](https://heroicgameslauncher.com) | There are no official Linux clients for these stores, so Heroic is the launcher used. Heroic's installed-games lists give each game's folder; a running process inside that folder is that game, reported under its store (Epic, GOG or Amazon Games) with that store's gamertag. Native and Flatpak installs of Heroic are both found. |
| **Lutris** | Reads the game name Lutris gives the game's process. A Steam game started from Lutris still reports as Steam. |
| **PCSX2** | Asks the running emulator for the game's title over PINE, PCSX2's control socket. Native, AppImage and Flatpak builds all work. *Off by default.* PINE is also off in PCSX2 by default: turn it on under *Settings → Advanced → PINE*, restart PCSX2 once so the socket is created, then switch on **PCSX2** under **Platforms**. |
| **Custom** | Rules by process name (use `game.exe` for a Wine or Proton game) or by window title. Window-title rules need an X11 session and `wmctrl`. Wayland doesn't allow listing other apps' windows, so use process-name rules there. |

### Install

```
sudo apt install python3-tk gir1.2-ayatanaappindicator3-0.1   # Debian/Ubuntu; tray icon support
pip install -r linux/requirements-linux.txt
python3 linux/gsa_linux.py
```

Use a clone of this repository, or the `gaming-status-agent-linux` zip from the
[Releases page](../../releases/latest). Keep the `linux` and `assets` folders
side by side.

On first run the agent writes a default config, turns on **Start at login**, and
opens the **MQTT Settings** and **Platforms** windows. Everything is editable
later from the tray menu, which has the same items as the Windows agent.

Files live in `~/.config/gaming-status-agent/` (`gsa_config.json`,
`gsa_debug.log`). If the `keyring` package and a desktop keyring are available,
the MQTT password is stored there rather than in the config file. Otherwise it
is kept in the config file, which only your user can read.

At login the agent can start before KWallet or GNOME Keyring is ready. When
that happens it keeps running and detecting games, checks the keyring again
every 10 seconds, and connects to MQTT once it can read the password. Home
Assistant shows the current game as soon as it connects.

### Running without a tray icon

On a desktop without a system tray, or to run as a service:

```
python3 linux/gsa_linux.py --headless
```

To start it at login with systemd, put the repository (or the release zip's
folder) at `~/gaming-status-agent/`, then:

```
cp linux/gaming-status-agent.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now gaming-status-agent
```

In headless mode, edit `~/.config/gaming-status-agent/gsa_config.json` by hand
and restart the service to apply the changes.

### Diagnostics

`python3 linux/gsa_linux.py --diagnose` prints what the agent finds (Steam libraries,
Heroic installs, PCSX2 PINE sockets, running launchers) and what it would publish right now. The
tray's **Run Diagnostics** shows the same report.

### More than one computer

Give each computer its own device name. Each one then gets its own sensor
(`sensor.gsa_<device name>`), and the `Machine` and `OS` attributes show which
computer a game is running on. Don't give two computers the same device name:
they would share one sensor, and the idle computer would keep overwriting the
one you're playing on.

## Building

Double-click **`windows\build.bat`**, or run it from a Command Prompt. It finds a Python
interpreter, installs PyInstaller if needed, builds, and pauses so you can read
the result.

To do it by hand instead, from a **Command Prompt** in the `windows` folder:

```
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name "Gaming Status Agent" --icon=..\assets\gsa_icon.ico --add-data "..\assets\gsa_icon.ico;." --version-file=version_info.txt gsa_tracker.py
```

The result is `windows\dist\Gaming Status Agent.exe`. Copy it somewhere of its own — it writes
`gsa_config.json` and `gsa_debug.log` next to itself.

`python -m PyInstaller` rather than a bare `pyinstaller` on purpose: pip puts
`pyinstaller.exe` in Python's `Scripts` folder, which is frequently not on PATH,
giving *"pyinstaller is not recognized as an internal or external command"*.
Running it as a module uses the interpreter already on PATH instead. If `python`
itself is not recognised, use `py -m PyInstaller ...` — the Windows Python
Launcher lives in `System32` and is almost always available.

**In PowerShell**, quote the `--add-data` value with single quotes. The `;` is
PyInstaller's source/destination separator, and PowerShell will otherwise treat
it as a command separator:

```
python -m PyInstaller --onefile --windowed --name "Gaming Status Agent" --icon=..\assets\gsa_icon.ico --add-data '..\assets\gsa_icon.ico;.' --version-file=version_info.txt gsa_tracker.py
```

What each flag is for:

| Flag | Why |
|---|---|
| `--onefile` | One self-contained exe rather than a folder |
| `--windowed` | No console window behind the tray icon |
| `--name "Gaming Status Agent"` | Produces `Gaming Status Agent.exe` instead of `gsa_tracker.exe` |
| `--icon=` | The icon Explorer and the taskbar show for the exe itself |
| `--add-data` | Bundles the .ico *inside* the exe, so the tray and windows can load it at runtime |
| `--version-file=` | Embeds the name and version, so Windows shows "Gaming Status Agent" not "Gaming Status Agent.exe" |

Both icon flags are needed: `--icon` brands the executable, `--add-data` makes
the file readable at runtime through `resource_path()`. Omit the second and the
exe looks right in Explorer but falls back to the drawn placeholder once running.

`assets\gsa_icon.ico` is the tray and window icon. Replace it with your own and
rebuild - nothing in the code needs changing, and if the file is missing Gaming Status Agent
falls back to a drawn placeholder rather than showing Tk's default feather.

`version_info.txt` supplies the exe's Windows metadata. Its `FileDescription`
is what Task Manager, Startup Apps and Properties → Details display; drop the
flag and they all fall back to the filename. Keep its version numbers in step
with `GSA_VERSION` in `windows/gsa_tracker.py`, which the diagnostics report prints.

`build\`, `dist\` and `Gaming Status Agent.spec` are build artifacts and are gitignored.

`windows/gsa_diagnose.py` is a command-line front end for the same diagnostics report
(`python gsa_diagnose.py > report.txt`). It's a development convenience and is
not part of the build — the tray menu covers the same ground.

## Releasing

The version lives in **one place**: `GSA_VERSION` in `windows/gsa_tracker.py`.

```python
GSA_VERSION = "1.1.0"
```

`build.bat` regenerates `version_info.txt` from it on every build, so the number
Windows shows in Task Manager can't drift from the one the app reports in its
diagnostics and log. Don't edit `version_info.txt` by hand (it says so at the
top). To regenerate without building: `python make_version_info.py`.

Use three numbers, `MAJOR.MINOR.PATCH`:

| Bump | When |
|---|---|
| **PATCH** — `1.0.0` → `1.0.1` | Detection fixes, a launcher's executable name changed, log-string corrections |
| **MINOR** — `1.0.0` → `1.1.0` | A new launcher, a new tray option, a new config key |
| **MAJOR** — `1.0.0` → `2.0.0` | Existing configs or Home Assistant sensors need changing to keep working |

Windows requires four numeric fields, so `1.1.0` is written into the exe as
`1.1.0.0`. Suffixes like `1.1.0-beta` are rejected by the generator rather than
silently producing a broken resource.

A release then looks like:

1. Edit `GSA_VERSION` and commit.
2. Tag and push it: `git tag v1.3.0 && git push origin v1.3.0`. Creating the
   release from the GitHub Releases page with a new `v1.3.0` tag works too.

The **Release** workflow (`.github/workflows/release.yml`) then builds the exe on
a Windows runner and attaches it to the release, along with a
`gaming-status-agent-linux-v1.3.0.zip` of the Linux agent. Watch it under the
repository's **Actions** tab; it takes a few minutes. It refuses to build if
the tag doesn't match `GSA_VERSION`, so a release can never ship an exe that
reports the wrong version. To rebuild an existing release, run the workflow by
hand from the Actions tab and give it the tag.

Exes are never committed to the repository (`*.exe` is gitignored). Use
`build.bat` for local testing; releases come from the workflow.

Anyone sending a bug report will be quoting that version back to you: the
diagnostics report is headed `Gaming Status Agent 1.1.0 diagnostics`, and `gsa_debug.log`
opens each run with `=== GAMING STATUS AGENT 1.1.0 LAUNCHED ===`.

## Contributing

Ubisoft game ids are the most useful contribution: if you hit an
`Unknown Ubisoft Game (1234)`, the id and the real name are enough for a
catalog PR.

For a detection bug, attach the Run Diagnostics output taken **while the game is
running**. Note that switching to another window to run it minimises the game,
so window sizes in the report won't reflect what Gaming Status Agent sees during play.
