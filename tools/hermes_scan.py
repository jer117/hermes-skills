#!/usr/bin/env python3
"""Run Hermes Agent's install-time security scan on skill directories.

Hermes scans every skill installed from a third-party repo, and blocks a
"community" install on any high or critical finding unless the user passes
--force. This runs the same scanner so a change that would block installs
fails here first.

Usage: hermes_scan.py PATH/TO/skills_guard.py SKILL_DIR [SKILL_DIR ...]

Get skills_guard.py from NousResearch/hermes-agent (tools/skills_guard.py).
"""

import importlib.util
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    spec = importlib.util.spec_from_file_location("skills_guard", sys.argv[1])
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    blocked = False
    for skill_dir in sys.argv[2:]:
        result = guard.scan_skill(Path(skill_dir), source="community")
        print(guard.format_scan_report(result))
        allowed, reason = guard.should_allow_install(result, force=False)
        print()
        blocked |= not allowed
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
