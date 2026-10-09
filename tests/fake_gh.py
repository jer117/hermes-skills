#!/usr/bin/env python3
"""Stand-in for the `gh` CLI used by the tests. Never touches the network.

Reads canned responses from the JSON file named by $FAKE_GH_STATE:

  {"routes": {"user": {...}, "repos/o/r/pulls/1": {...}, ...},
   "version": "2.60.0",          # reported by `gh --version`
   "reject_inline": false,       # POST with non-empty comments -> HTTP 422
   "posted": []}

`gh api PATH [--paginate --slurp] [--jq .field]` returns routes[PATH] (wrapped
in a list of pages for --slurp). `gh api PATH --method POST --input -` appends
{"endpoint", "payload"} to "posted" and returns a fake html_url.
"""

import json
import os
import sys


def emit(text):
    """Print like gh: forced colour reaches pipes, as real gh does."""
    forced = os.environ.get("CLICOLOR_FORCE", "") not in ("", "0") or os.environ.get("GH_FORCE_TTY")
    if forced and text.lstrip().startswith(("{", "[")):
        text = "\x1b[1;37m" + text + "\x1b[m"
    print(text)


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    state_path = os.environ.get("FAKE_GH_STATE")
    with open(state_path) as fh:
        state = json.load(fh)
    args = sys.argv[1:]
    if args == ["--version"]:
        print(f"gh version {state.get('version', '2.60.0')} (fake)")
        return 0
    if not args or args[0] != "api":
        print(f"fake gh: unsupported command {args}", file=sys.stderr)
        return 2
    path = args[1]
    if "--method" in args and args[args.index("--method") + 1] == "POST":
        payload = json.load(sys.stdin)
        if state.get("reject_inline") and payload.get("comments"):
            print("gh: Validation Failed: Line could not be resolved (HTTP 422)", file=sys.stderr)
            return 1
        state["posted"].append({"endpoint": path, "payload": payload})
        with open(state_path, "w") as fh:
            json.dump(state, fh)
        emit(json.dumps({"html_url": f"https://github.test/{path}/{len(state['posted'])}"}))
        return 0
    route = path.split("?", 1)[0]
    if route not in state["routes"]:
        print(f"fake gh: HTTP 404 for {route}", file=sys.stderr)
        return 1
    body = state["routes"][route]
    if "--jq" in args:
        field = args[args.index("--jq") + 1].lstrip(".")
        print(body[field])
        return 0
    if "--slurp" in args:
        body = [body]
    emit(json.dumps(body, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
