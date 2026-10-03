"""Gaming Status Agent diagnostic - command line front end.

The same report is available from the tray icon under "Run Diagnostics"; this
script is for when you would rather have it in a terminal or redirect it to a
file. Run it on the Windows machine while a game is running:

    python gsa_diagnose.py
    python gsa_diagnose.py > report.txt

The report is built by gsa_tracker.build_diagnostic_report(), so it reflects
the real detection code rather than a second copy of it. Nothing is published
and no files are changed. The MQTT password is never included.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gsa_tracker as gsa


def main():
    # The tray app loads these at startup; a standalone run has to do it itself.
    # rebuild_active_tables() in particular: without it the ancestry scan reads
    # an empty launcher table and the report says nothing is detected.
    gsa.CONFIG = gsa.load_config()
    gsa.PROFILE_SANITIZED = gsa.sanitize_topic_part(gsa.CONFIG.get("HA_DEVICE_NAME", "User"))
    gsa.rebuild_active_tables()

    # Each scan is skipped when its platform is off, matching start_services(),
    # so the report shows what the running agent would actually see.
    if gsa.platform_enabled("GOG"):
        gsa.GOG_BY_PATH, gsa.GOG_BY_NAME = gsa.get_gog_mapping()
    if gsa.platform_enabled("Battle.net"):
        gsa.BATTLENET_BY_DIR = gsa.get_battlenet_mapping()
    if gsa.platform_enabled("Steam"):
        gsa.STEAM_BY_DIR, gsa.STEAM_BY_APPID = gsa.get_steam_mapping()
    if gsa.platform_enabled("Xbox"):
        gsa.XBOX_BY_DIR = gsa.get_xbox_mapping()

    # Ubisoft's id -> name map lives inside the log handler at runtime, but the
    # registry scan behind it fills the counts the report prints.
    if gsa.platform_enabled("Ubisoft"):
        try:
            gsa.get_ubisoft_info()
        except Exception as e:
            print(f"(Ubisoft catalog/registry scan failed: {e})")

    report = gsa.build_diagnostic_report()
    try:
        print(report)
    except UnicodeEncodeError:
        # Some Windows consoles cannot encode game titles such as "LEGO(R)".
        print(report.encode("ascii", "replace").decode("ascii"))


if __name__ == "__main__":
    main()