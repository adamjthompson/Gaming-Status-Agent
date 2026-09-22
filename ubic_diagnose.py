"""Ubic diagnostic - command line front end.

The same report is available from the tray icon under "Run Diagnostics"; this
script is for when you would rather have it in a terminal or redirect it to a
file. Run it on the Windows machine while a game is running:

    python ubic_diagnose.py
    python ubic_diagnose.py > report.txt

The report is built by ubic_tracker.build_diagnostic_report(), so it reflects
the real detection code rather than a second copy of it. Nothing is published
and no files are changed. The MQTT password is never included.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ubic_tracker as u


def main():
    # The tray app loads these at startup; a standalone run has to do it itself.
    u.CONFIG = u.load_config()
    u.PROFILE_SANITIZED = u.sanitize_topic_part(u.CONFIG.get("HA_DEVICE_NAME", "User"))
    u.GOG_BY_PATH, u.GOG_BY_NAME = u.get_gog_mapping()
    u.BATTLENET_BY_DIR = u.get_battlenet_mapping()

    report = u.build_diagnostic_report()
    try:
        print(report)
    except UnicodeEncodeError:
        # Some Windows consoles cannot encode game titles such as "LEGO(R)".
        print(report.encode("ascii", "replace").decode("ascii"))


if __name__ == "__main__":
    main()

