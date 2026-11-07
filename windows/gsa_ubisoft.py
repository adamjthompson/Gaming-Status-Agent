"""Ubisoft Connect achievements and library, read from the launcher's local
cache. Read-only: nothing here ever writes to, moves or locks a Ubisoft file.

Ubisoft keeps this data only on the PC (there is no public profile to query):

  ownership  %LOCALAPPDATA%\\Ubisoft Game Launcher\\cache\\ownership\\<user GUID>
             protobuf after a 264-byte header; records hold game IDs.
  defs       <cache root>\\cache\\achievements\\<gameId>_<hash>
             a zip with <locale>_loc.txt ("id<TAB>name<TAB>description" lines)
             and one <id>.png icon per achievement. Found under both
             %ProgramData%\\Ubisoft\\Ubisoft Game Launcher and the launcher's
             install folder.
  unlocks    %LOCALAPPDATA%\\Ubisoft Game Launcher\\spool\\<user GUID>\\...\\<gameId>.spool
             records of {achievement id, unix seconds}; full history, rewritten
             the moment an achievement unlocks.
  names      <install dir>\\cache\\configuration\\configurations
             protobuf records of {game id, YAML}; a fallback for games the
             community catalog doesn't name.

Copies of one game bought from different stores (e.g. "Skull and Bones" and
"Skull and Bones (Steam)") have separate IDs but the same achievements; they
are merged into one library entry. The format was worked out by the community
(see UbiSlot by BalTor02, MIT) and isn't documented by Ubisoft, so every
parser tolerates junk and returns what it could read.

Standard library only, and no Windows-only imports, so it can be tested off
Windows against sample files.
"""

import glob
import io
import os
import re
import time
import zipfile
from datetime import datetime, timezone

OWNERSHIP_HEADER = 264
RECENT_LIMIT = 10
# Store suffixes the community catalog appends to a game's other-store copy.
_STORE_SUFFIX_RE = re.compile(r"\s*\((?:steam|uplay(?: connect)?|ubisoft connect)\)\s*$",
                              re.IGNORECASE)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_ICON_BYTES = 256 * 1024


# --- low-level readers -------------------------------------------------------
def read_bytes(path, attempts=3):
    """Read a file read-only, retrying briefly if Ubisoft Connect has it open
    for writing (a sharing violation surfaces as PermissionError)."""
    for i in range(attempts):
        try:
            with open(path, "rb") as f:
                return f.read()
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.3)
    return b""


def _varint(data, pos):
    result = shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("bad varint")
    raise ValueError("truncated varint")


def _skip(data, pos, wire):
    if wire == 0:
        _, pos = _varint(data, pos)
    elif wire == 1:
        pos += 8
    elif wire == 2:
        n, pos = _varint(data, pos)
        pos += n
    elif wire == 5:
        pos += 4
    else:
        raise ValueError(f"wire type {wire}")
    if pos > len(data):
        raise ValueError("field past end")
    return pos


def parse_spool(data):
    """[(achievement_id, unix_seconds)] from a .spool file."""
    records = []
    pos = 0
    while pos < len(data):
        if data[pos] != 0x0A:
            break
        pos += 1
        length, pos = _varint(data, pos)
        end = pos + length
        if end > len(data):
            break
        ach_id = ts = None
        while pos < end:
            field = data[pos]
            pos += 1
            if field == 0x0A:
                flen, pos = _varint(data, pos)
                fend = pos + flen
                if fend > end:
                    break
                if pos < fend and data[pos] == 0x08:
                    ach_id, _ = _varint(data, pos + 1)
                pos = fend
            elif field == 0x10:
                ts, pos = _varint(data, pos)
            else:
                _, pos = _varint(data, pos)
        if ach_id is not None and ts is not None:
            records.append((ach_id, ts))
        pos = end
    return records


def parse_ownership(data, offset=OWNERSHIP_HEADER):
    """Every candidate game ID in an ownership file. The records also carry
    unrelated numbers, so callers keep only IDs they can name or that have
    achievement data."""
    ids = set()
    payload = data[offset:]
    pos = 0
    while pos < len(payload):
        start = pos
        try:
            tag, pos = _varint(payload, pos)
            fnum, wire = tag >> 3, tag & 7
            if fnum == 1 and wire == 2:
                n, pos = _varint(payload, pos)
                if pos + n > len(payload):
                    break
                rec = payload[pos:pos + n]
                pos += n
                rpos = 0
                while rpos < len(rec):
                    rtag, rpos = _varint(rec, rpos)
                    rf, rw = rtag >> 3, rtag & 7
                    if rf in (1, 2) and rw == 0:
                        v, rpos = _varint(rec, rpos)
                        ids.add(v)
                    else:
                        rpos = _skip(rec, rpos, rw)
            else:
                pos = _skip(payload, pos, wire)
        except ValueError:
            pos = start + 1
    return ids


def parse_configurations(data):
    """{game id: YAML text} from the launcher's configurations cache."""
    games = {}
    pos = 0
    while pos < len(data):
        start = pos
        try:
            tag, pos = _varint(data, pos)
            fnum, wire = tag >> 3, tag & 7
            if fnum == 1 and wire == 2:
                n, pos = _varint(data, pos)
                rec = data[pos:pos + n]
                pos += n
                gid, texts, rpos = None, [], 0
                while rpos < len(rec):
                    rtag, rpos = _varint(rec, rpos)
                    rf, rw = rtag >> 3, rtag & 7
                    if rf == 1 and rw == 0:
                        gid, rpos = _varint(rec, rpos)
                    elif rw == 2:
                        ln, rpos = _varint(rec, rpos)
                        texts.append(rec[rpos:rpos + ln].decode("utf-8", "replace"))
                        rpos += ln
                    else:
                        rpos = _skip(rec, rpos, rw)
                if gid is not None:
                    games[gid] = "\n".join(texts)
            else:
                pos = _skip(data, pos, wire)
        except ValueError:
            pos = start + 1
    return games


def name_from_yaml(text):
    """root/name from a configuration YAML, resolving a localization key
    (name: l1 -> localizations/default/l1). None when there's no usable name."""
    m = re.search(r"^\s*name:\s*(.+?)\s*$", text, re.MULTILINE)
    if not m:
        return None
    raw = m.group(1).strip().strip("'\"")
    if "localizations:" in text:
        loc = re.search(r"^\s*" + re.escape(raw) + r":\s*(.+?)\s*$",
                        text.split("localizations:", 1)[1], re.MULTILINE)
        if loc:
            return loc.group(1).strip().strip("'\"") or None
    # A bare localization key that didn't resolve isn't a name.
    if re.fullmatch(r"l\d+", raw, re.IGNORECASE):
        return None
    return raw or None


# --- locating the cache --------------------------------------------------------
def find_paths(local_appdata, program_data, install_dir=None):
    """Folders/files that exist, as lists, so a missing one is simply empty."""
    roots = []
    if local_appdata:
        roots.append(os.path.join(local_appdata, "Ubisoft Game Launcher"))
    if program_data:
        roots.append(os.path.join(program_data, "Ubisoft", "Ubisoft Game Launcher"))
    if install_dir:
        roots.append(install_dir)
    seen, unique = set(), []
    for r in roots:
        key = os.path.normcase(os.path.normpath(r))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    paths = {"ownership": [], "achievements": [], "spool": [], "configurations": []}
    for r in unique:
        for kind, rel in (("ownership", ("cache", "ownership")),
                          ("achievements", ("cache", "achievements")),
                          ("spool", ("spool",))):
            p = os.path.join(r, *rel)
            if os.path.isdir(p):
                paths[kind].append(p)
        cfg = os.path.join(r, "cache", "configuration", "configurations")
        if os.path.isfile(cfg):
            paths["configurations"].append(cfg)
    return paths


# --- readers over the whole cache ------------------------------------------------
def read_owned_ids(paths):
    owned = set()
    for d in paths["ownership"]:
        for path in glob.glob(os.path.join(d, "**", "*"), recursive=True):
            if not os.path.isfile(path):
                continue
            try:
                data = read_bytes(path)
            except OSError:
                continue
            if len(data) > OWNERSHIP_HEADER:
                owned |= parse_ownership(data)
    return owned


def read_config_names(paths):
    names = {}
    for cfg in paths["configurations"]:
        try:
            games = parse_configurations(read_bytes(cfg))
        except OSError:
            continue
        for gid, text in games.items():
            name = name_from_yaml(text)
            if name:
                names.setdefault(gid, name[:255])
    return names


def _archives(paths):
    """{game id: newest definitions zip}."""
    best = {}
    for d in paths["achievements"]:
        for path in glob.glob(os.path.join(d, "*")):
            m = re.match(r"(\d+)_", os.path.basename(path))
            if not m or not os.path.isfile(path):
                continue
            gid = int(m.group(1))
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if gid not in best or mtime > best[gid][1]:
                best[gid] = (path, mtime)
    return {gid: p for gid, (p, _) in best.items()}


def read_definitions(paths):
    """{game id: {"archive", "achievements": {ach id: (name, description)}}}."""
    defs = {}
    for gid, archive in _archives(paths).items():
        try:
            with zipfile.ZipFile(io.BytesIO(read_bytes(archive))) as z:
                locs = [n for n in z.namelist() if n.lower().endswith("_loc.txt")]
                pick = next((n for n in locs if n.lower() == "en-us_loc.txt"),
                            locs[0] if locs else None)
                if not pick:
                    continue
                text = z.read(pick).decode("utf-8-sig", "replace")
        except (OSError, zipfile.BadZipFile, KeyError):
            continue
        achievements = {}
        for line in text.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0].strip().isdigit():
                achievements[int(parts[0].strip())] = (
                    parts[1].strip()[:255], "\t".join(parts[2:]).strip()[:500])
        if achievements:
            defs[gid] = {"archive": archive, "achievements": achievements}
    return defs


def read_icon(archive, ach_id):
    """The PNG bytes for one achievement icon, or None (missing, too large, or
    not a PNG)."""
    try:
        with zipfile.ZipFile(io.BytesIO(read_bytes(archive))) as z:
            name = next((n for n in z.namelist() if n.lower() == f"{ach_id}.png"), None)
            if not name or z.getinfo(name).file_size > MAX_ICON_BYTES:
                return None
            data = z.read(name)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None
    return data if data.startswith(_PNG_SIGNATURE) else None


def spool_files(paths):
    """{game id: [spool paths]} -- one game can have files in several user
    folders."""
    files = {}
    for d in paths["spool"]:
        for path in glob.glob(os.path.join(d, "**", "*.spool"), recursive=True):
            base = os.path.splitext(os.path.basename(path))[0]
            if base.isdigit():
                files.setdefault(int(base), []).append(path)
    return files


def read_unlocks(paths, errors=None):
    """{game id: {achievement id: earliest unix seconds}}."""
    unlocks = {}
    for gid, files in spool_files(paths).items():
        for path in files:
            try:
                records = parse_spool(read_bytes(path))
            except (OSError, ValueError) as e:
                if errors is not None:
                    errors.append(f"{os.path.basename(path)}: {e}")
                continue
            game = unlocks.setdefault(gid, {})
            for ach_id, ts in records:
                if ach_id not in game or ts < game[ach_id]:
                    game[ach_id] = ts
    return {gid: u for gid, u in unlocks.items() if u}


# --- building the payload ---------------------------------------------------------
def _iso(ts):
    try:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _norm(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())


def base_title(title):
    return _STORE_SUFFIX_RE.sub("", title).strip() or title


def build_library(names, owned, defs, unlocks, config_names=None):
    """Merge everything into the library payload published to Home Assistant,
    plus the icons it needs: [(game id, achievement id, archive path)] for each
    game's most recent unlocks (what Home Assistant shows).

    names        {str id: title} from the catalog/registry/overrides
    owned        set of IDs from the ownership cache
    defs         read_definitions() output
    unlocks      read_unlocks() output
    config_names {int id: title} from the launcher's configuration cache
    """
    config_names = config_names or {}

    def title_for(gid):
        return names.get(str(gid)) or config_names.get(gid) or f"Ubisoft game {gid}"

    # Group store copies by base title; split a group whose achievement lists
    # differ in size, since then they aren't really the same set.
    groups = {}
    for gid in set(defs) | set(unlocks):
        groups.setdefault(_norm(base_title(title_for(gid))), []).append(gid)
    clusters = []
    for ids in groups.values():
        by_total = {}
        for gid in ids:
            total = len(defs[gid]["achievements"]) if gid in defs else None
            by_total.setdefault(total, []).append(gid)
        unknown = by_total.pop(None, [])
        if len(by_total) <= 1:
            clusters.append(ids)  # one known size (or none): all one game
        else:
            # Several sizes: keep them apart, attaching definition-less IDs to
            # the largest set.
            sized = sorted(by_total.values(), key=len, reverse=True)
            sized[0] = sized[0] + unknown
            clusters.extend(sized)

    # Owned copies with no achievement data of their own (e.g. the Ubisoft
    # store copy of a game whose Steam copy holds the achievements) still
    # belong to the game, so a launch under that ID matches it. They're only
    # listed in "variants", never used for counts.
    extra_variants = {}
    cluster_keys = {}
    for i, ids in enumerate(clusters):
        cluster_keys.setdefault(_norm(base_title(title_for(ids[0]))), i)
    for gid in owned:
        if gid in defs or gid in unlocks:
            continue
        title = names.get(str(gid)) or config_names.get(gid)
        i = cluster_keys.get(_norm(base_title(title))) if title else None
        if i is not None:
            extra_variants.setdefault(i, set()).add(gid)

    games, icons = [], []
    for index, ids in enumerate(clusters):
        # Primary: a copy without a store suffix, preferring one with
        # definitions, then the lowest ID.
        ids.sort(key=lambda g: (bool(_STORE_SUFFIX_RE.search(title_for(g))), g not in defs, g))
        primary = ids[0]
        def_src = next((g for g in ids if g in defs), None)
        achievements = defs[def_src]["achievements"] if def_src is not None else {}
        merged = {}
        for gid in ids:
            for ach_id, ts in unlocks.get(gid, {}).items():
                if achievements and ach_id not in achievements:
                    continue
                if ach_id not in merged or ts < merged[ach_id]:
                    merged[ach_id] = ts
        newest = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)
        recent = []
        for ach_id, ts in newest[:RECENT_LIMIT]:
            name, desc = achievements.get(ach_id, (f"Achievement {ach_id}", ""))
            recent.append({"id": ach_id, "name": name, "description": desc,
                           "unlocked_at": _iso(ts)})
            if def_src is not None:
                icons.append((primary, ach_id, defs[def_src]["archive"]))
        games.append({
            "id": primary,
            "title": base_title(title_for(primary)),
            "variants": sorted(set(ids) | extra_variants.get(index, set())),
            "owned": any(g in owned for g in ids),
            "achievements_total": len(achievements) if achievements else None,
            "achievements_earned": len(merged),
            "last_unlock": _iso(newest[0][1]) if newest else None,
            "recent": recent,
        })
    games.sort(key=lambda g: g["title"].lower())

    named_owned = {g for g in owned if str(g) in names or g in config_names or g in defs}
    return {
        "version": 1,
        "owned_game_count": len(named_owned),
        "games": games,
    }, icons
