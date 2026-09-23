# Gaming Status Agent

A Windows system-tray app that reports what you're playing to Home Assistant
over MQTT. It detects games from Epic, Ubisoft Connect, GOG, Battle.net, EA,
Amazon Games and Playnite — including emulated titles launched through Playnite
— and publishes a single sensor with the game name, the launcher, and when you
started.

Home Assistant discovers the sensor automatically. No YAML required.

---

## Requirements

- Windows
- A Home Assistant instance with the [MQTT integration](https://www.home-assistant.io/integrations/mqtt/)
  set up and a broker it can reach (Mosquitto is the usual choice)
- Python 3.8+ if running from source
 
## Install

**From a release:** download `Gaming Status Agent.exe`, put it anywhere you like, and run it.
It creates its config file next to itself, so a dedicated folder is tidiest. Adding it to your startup items is recommended as well.

**From source:**

```
pip install -r requirements.txt
python gsa_tracker.py
```

On first run Gaming Status Agent writes `gsa_config.json`, shows you the device name it chose
(taken from your Windows account), and puts a controller icon in your system
tray. Right-click it and open **MQTT Settings** to enter your broker address.

## The tray menu

| Item | What it does |
|---|---|
| **MQTT Settings** | Broker address, port, credentials, TLS, the device name, poll rate |
| **Account Settings** | Your gamertag per store — shown as the sensor's `Profile Name` |
| **Custom Games** | Match games Gaming Status Agent doesn't detect on its own, by executable or window title |
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

If the PC loses power or drops off the network, the broker publishes `Offline`
on Gaming Status Agent's behalf via its Last Will, so the sensor never sticks on a game you
stopped playing hours ago.

---

## How detection works

Gaming Status Agent uses whichever source knows the game's real name, in this order:

1. **Epic** — reads the launcher log for game starts and the local manifests for
   real titles. Exits are detected by watching the game's process, because
   Epic's log does not reliably announce them.
2. **GOG** — reads GOG Galaxy's registry entries. Matches on the game's
   executable, so it works even when a DRM-free game is started straight from a
   shortcut with Galaxy closed.
3. **Battle.net** — reads Blizzard's uninstall entries for install locations,
   then matches any process running from inside one.
4. **Window ancestry** — for everything else, finds a visible window whose
   process descends from a known launcher and uses its title.

Sources that know a game's real name outrank raw window titles, so a
Playnite-launched Epic game reports as **Epic** with its proper name rather than
as Playnite with whatever the window happens to be called.

### Steam is deliberately excluded

Home Assistant's own [Steam integration](https://www.home-assistant.io/integrations/steam_online/)
reads what you're playing from the Steam Web API. That's a better source than
anything Gaming Status Agent could determine locally — it's authoritative, and it works even
when this PC is switched off. Gaming Status Agent therefore ignores anything launched through
Steam rather than publishing a competing value.

This applies to Steam games launched via Playnite too. A **Custom Games** rule
still overrides the exclusion, because that's deliberate configuration rather
than automatic detection.

### What Playnite does and doesn't cover

Playnite hands most store games off to their own client, so those report under
that store. Playnite gets the credit only where it genuinely owns the process —
emulated and manually added games — and those are named from the window title,
which for emulators is often the emulator rather than the ROM. Use a **Custom
Games** rule to give those a proper name.

If Playnite is set to close itself when a game starts, the process chain breaks
and the game can't be attributed to it.

---

## Configuration

Most settings live in the tray menu. `gsa_config.json` holds a few extra keys
for less common situations.

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
behind it, and whether Gaming Status Agent detected it *and why not* if it didn't — then shows
which source won and what would be published. Copy to Clipboard puts the whole
report on your clipboard for a bug report. It contains no passwords.

`gsa_debug.log` sits next to the app and rotates at 1 MB.

| Symptom | Likely cause |
|---|---|
| No sensor in Home Assistant | Broker address or credentials. Diagnostics shows whether MQTT is connected |
| Sensor stuck on an old game | Use Force Offline. If it recurs, send the diagnostics report |
| A Steam game isn't detected | Expected — see above. Use HA's Steam integration |
| Wrong name for an emulated game | Add a Custom Games rule matching the window title |
| A game isn't detected at all | Diagnostics will show its window and process chain. If its launcher shows as `UNRECOGNISED`, open an issue with that line |
| Ubisoft games named `Unknown Ubisoft Game (1234)` | The catalog didn't have that id. Add a `UBISOFT_OVERRIDES` entry, and consider opening a PR against the catalog |

Newly installed games are picked up on restart. **Save & Apply** in Settings
refreshes without closing Gaming Status Agent.

---

## Privacy

Everything stays between this PC and your broker. Gaming Status Agent makes exactly one
outbound internet request — fetching the Ubisoft name catalog — and sends no
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

## Building

Double-click **`build.bat`**, or run it from a Command Prompt. It finds a Python
interpreter, installs PyInstaller if needed, builds, and pauses so you can read
the result.

To do it by hand instead, from a **Command Prompt** in the project folder:

```
pip install pyinstaller
python -m PyInstaller --onefile --windowed --name "Gaming Status Agent" --icon=gsa_icon.ico --add-data "gsa_icon.ico;." --version-file=version_info.txt gsa_tracker.py
```

The result is `dist\Gaming Status Agent.exe`. Copy it somewhere of its own — it writes
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
python -m PyInstaller --onefile --windowed --name "Gaming Status Agent" --icon=gsa_icon.ico --add-data 'gsa_icon.ico;.' --version-file=version_info.txt gsa_tracker.py
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

`gsa_icon.ico` is the tray and window icon. Replace it with your own and
rebuild - nothing in the code needs changing, and if the file is missing Gaming Status Agent
falls back to a drawn placeholder rather than showing Tk's default feather.

`version_info.txt` supplies the exe's Windows metadata. Its `FileDescription`
is what Task Manager, Startup Apps and Properties → Details display; drop the
flag and they all fall back to the filename. Keep its version numbers in step
with `GSA_VERSION` in `gsa_tracker.py`, which the diagnostics report prints.

`build\`, `dist\` and `Gaming Status Agent.spec` are build artifacts and are gitignored.

`gsa_diagnose.py` is a command-line front end for the same diagnostics report
(`python gsa_diagnose.py > report.txt`). It's a development convenience and is
not part of the build — the tray menu covers the same ground.

## Releasing

The version lives in **one place**: `GSA_VERSION` in `gsa_tracker.py`.

```python
GSA_VERSION = "1.1.0"
```

`build.bat` regenerates `version_info.txt` from it on every build, so the number
Windows shows in Task Manager can't drift from the one the app reports in its
diagnostics and log. Don't edit `version_info.txt` by hand — it says so at the
top. To regenerate without building: `python make_version_info.py`.

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

1. Edit `GSA_VERSION`.
2. Run `build.bat`.
3. Confirm the exe shows the new version in Properties → Details.
4. Tag it — `git tag v1.1.0 && git push --tags` — and attach `dist\Gaming Status Agent.exe`.

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
