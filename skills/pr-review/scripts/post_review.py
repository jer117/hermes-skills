#!/usr/bin/env python3
"""Validate and post an agent's findings on a GitHub pull request.

Modes:

  review   One formal PR review: inline comments on diff lines plus a summary.
           Verdict is COMMENT, or REQUEST_CHANGES when at least one finding
           is labelled **Bug:** or **Security:**. This script cannot approve.
  comment  One plain PR conversation comment that @-mentions a configured
           user and lists every finding with a permalink. No formal review.

Safety checks before anything is posted:

  - gh must be logged in as the configured reviewer login.
  - The PR must be open and its head must still be the reviewed commit.
  - This account must not have posted for this head and mode already.
  - Inline comments must sit on diff lines; others move into the summary.

Configuration (first match wins):

  reviewer login  --reviewer, $PR_REVIEW_LOGIN, config "reviewer_login"
  mention login   --mention,  $PR_REVIEW_MENTION, config "mention_login"

The config file is $PR_REVIEW_CONFIG, or $XDG_CONFIG_HOME/pr-review/config.json
(default ~/.config/pr-review/config.json):

  {"reviewer_login": "my-review-bot", "mention_login": "me"}

Usage:
  post_review.py --check [--repo OWNER/REPO --pr N]
  post_review.py --repo OWNER/REPO --pr N --head SHA --review FILE
                 [--mode review|comment] [--event comment|request-changes]
                 [--reviewer LOGIN] [--mention LOGIN] [--repost] [--dry-run]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

MIN_GH = (2, 48, 0)  # gh api --paginate --slurp
MARKER = "<!-- pr-review-skill:v1 mode={mode} head={sha} -->"
BLOCKING_RE = re.compile(r"\*\*(Bug|Security):\*\*")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")
ALLOWED_TOP = {"body", "comments"}
ALLOWED_COMMENT = {"path", "line", "side", "start_line", "start_side", "body"}
EVENTS = {"comment": "COMMENT", "request-changes": "REQUEST_CHANGES"}


def fail(message):
    sys.exit(f"post_review: {message}")


def note(message):
    print(f"post_review: {message}", file=sys.stderr)


# --- gh -----------------------------------------------------------------------

def plain_gh_output():
    """Stop gh from colouring or paging output it believes goes to a terminal.

    CLICOLOR_FORCE or GH_FORCE_TTY in the user's shell makes `gh api` write
    ANSI-coloured JSON even into a pipe. Changing the process environment
    (rather than passing env=) lets every child gh inherit the fix.
    """
    for name in ("GH_FORCE_TTY", "CLICOLOR_FORCE"):
        os.unsetenv(name)
    os.putenv("NO_COLOR", "1")
    os.putenv("GH_PAGER", "cat")


def run_gh(*args, input=None):
    try:
        return subprocess.run(["gh", *args], input=input, capture_output=True, encoding="utf-8")
    except FileNotFoundError:
        fail("the GitHub CLI (gh) is not installed or not on PATH")


def gh(*args, input=None):
    result = run_gh(*args, input=input)
    if result.returncode != 0:
        fail(f"gh {' '.join(args[:2])} failed:\n{(result.stderr or result.stdout).strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def gh_pages(path):
    # --paginate with --slurp yields a list of pages
    return [item for page in gh("api", path, "--paginate", "--slurp") for item in page]


def gh_version():
    out = run_gh("--version").stdout
    m = re.search(r"gh version (\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def require_gh_version():
    version = gh_version()
    if version is None or version < MIN_GH:
        found = ".".join(map(str, version)) if version else "unknown"
        fail(f"gh {'.'.join(map(str, MIN_GH))} or newer is required (found {found})")
    return version


# --- configuration -------------------------------------------------------------

def config_path():
    explicit = os.environ.get("PR_REVIEW_CONFIG")
    if explicit:
        return Path(explicit).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pr-review" / "config.json"


def load_config():
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}, path
    except (OSError, ValueError) as exc:
        fail(f"cannot read config {path}: {exc}")
    if not isinstance(data, dict):
        fail(f"config {path} must be a JSON object")
    return data, path


def resolve(flag_value, flag_name, env_name, config, config_file, key):
    """Return (value, source) for the first configured value, else (None, None)."""
    candidates = (
        (flag_value, flag_name),
        (os.environ.get(env_name), f"${env_name}"),
        (config.get(key), f"{config_file} ({key})"),
    )
    for value, source in candidates:
        if value:
            if not isinstance(value, str) or not LOGIN_RE.match(value):
                fail(f"{source} is not a valid GitHub login: {value!r}")
            return value, source
    return None, None


def missing_login(what, flag_name, env_name, key, config_file):
    fail(
        f"no {what} configured. Set one of:\n"
        f"  {flag_name} LOGIN\n"
        f"  {env_name}=LOGIN\n"
        f'  {config_file}: {{"{key}": "LOGIN"}}'
    )


# --- diff and review validation -----------------------------------------------

def commentable_lines(patch):
    """Return {"RIGHT": set(new lines), "LEFT": set(old lines)} present in the diff."""
    lines = {"RIGHT": set(), "LEFT": set()}
    old = new = None
    text = patch or ""
    if text.endswith("\n"):
        text = text[:-1]
    # Split on "\n" only, as git does: str.splitlines() also breaks on form
    # feeds and U+2028, which would shift every following line number.
    for raw in text.split("\n"):
        if raw.endswith("\r"):
            raw = raw[:-1]
        m = HUNK_RE.match(raw)
        if m:
            old, new = int(m.group(1)), int(m.group(2))
            continue
        if old is None or raw.startswith("\\"):
            continue
        tag = raw[:1]
        if tag == "+":
            lines["RIGHT"].add(new)
            new += 1
        elif tag == "-":
            lines["LEFT"].add(old)
            old += 1
        else:  # context line: commentable on both sides
            lines["RIGHT"].add(new)
            lines["LEFT"].add(old)
            old += 1
            new += 1
    return lines


def validate(review, diff_lines):
    """Split comments into (postable, moved) and return them with error messages."""
    errors, postable, moved = [], [], []
    if not isinstance(review, dict):
        return ["review file must be a JSON object"], [], []
    extra = set(review) - ALLOWED_TOP
    if extra:
        errors.append(f"unexpected top-level keys {sorted(extra)}; only body and comments are allowed")
    if not isinstance(review.get("body"), str) or not review["body"].strip():
        errors.append("review body must be a non-empty string")
    comments = review.get("comments") or []
    if not isinstance(comments, list):
        return errors + ["comments must be a list"], [], []
    for i, c in enumerate(comments):
        if not isinstance(c, dict):
            errors.append(f"comment {i}: must be an object")
            continue
        bad = set(c) - ALLOWED_COMMENT
        if bad:
            errors.append(f"comment {i}: unexpected keys {sorted(bad)}")
            continue
        if not all(isinstance(c.get(k), t) and not isinstance(c.get(k), bool)
                   for k, t in (("path", str), ("line", int), ("body", str))):
            errors.append(f"comment {i}: path (str), line (int) and body (str) are required")
            continue
        if not c["body"].strip():
            errors.append(f"comment {i}: body is empty")
            continue
        side = c.setdefault("side", "RIGHT")
        if side not in ("RIGHT", "LEFT"):
            errors.append(f"comment {i}: side must be RIGHT or LEFT")
            continue
        valid = diff_lines.get(c["path"], {}).get(side, set())
        span = [c["line"]]
        if "start_line" in c:
            c.setdefault("start_side", side)
            if (c["start_side"] != side or not isinstance(c["start_line"], int)
                    or isinstance(c["start_line"], bool) or c["start_line"] >= c["line"]):
                errors.append(f"comment {i}: start_line must be an int below line, on the same side")
                continue
            span = range(c["start_line"], c["line"] + 1)
        if all(n in valid for n in span):
            postable.append(c)
        else:
            moved.append(c)
    return errors, postable, moved


def finding_lines(comments, link=None):
    """Render comments as a markdown list, optionally linking each location."""
    out = []
    for c in comments:
        first, *rest = c["body"].strip().splitlines() or [""]
        span = f"{c['start_line']}-{c['line']}" if "start_line" in c else str(c["line"])
        where = f"{c['path']}:{span}" + (" (removed line)" if c["side"] == "LEFT" else "")
        loc = f"[`{where}`]({link(c)})" if link and c["side"] == "RIGHT" else f"`{where}`"
        out.append(f"- {loc} {first}")
        out.extend(f"  {line}" for line in rest)
    return out


def review_body(body, outside):
    if outside:
        body += "\n\n### Notes outside the diff\n" + "\n".join(finding_lines(outside))
    return body


def already_posted(repo, pr, login, mode, head):
    """URL of this account's earlier post for this head and mode, else None.

    Only a marker on the final line counts. post() always ends with it, and a
    marker quoted elsewhere in a body (for example from a PR description) must
    not suppress a later review.
    """
    marker = MARKER.format(mode=mode, sha=head)
    items = gh_pages(f"repos/{repo}/pulls/{pr}/reviews?per_page=100")
    items += gh_pages(f"repos/{repo}/issues/{pr}/comments?per_page=100")
    for item in items:
        if (item.get("user") or {}).get("login", "").lower() != login.lower():
            continue
        if (item.get("body") or "").rstrip().endswith(marker):
            return item.get("html_url") or "(existing post)"
    return None


# --- commands ------------------------------------------------------------------

def check(args):
    """Report the effective setup without touching any pull request."""
    config, path = load_config()
    version = gh_version()
    login_result = run_gh("api", "user", "--jq", ".login")
    login = login_result.stdout.strip() if login_result.returncode == 0 else None
    reviewer, reviewer_src = resolve(args.reviewer, "--reviewer", "PR_REVIEW_LOGIN", config, path, "reviewer_login")
    mention, mention_src = resolve(args.mention, "--mention", "PR_REVIEW_MENTION", config, path, "mention_login")

    ok = True
    version_ok = version is not None and version >= MIN_GH
    ok &= version_ok
    print(f"gh version:     {'.'.join(map(str, version)) if version else 'not found'}"
          f"{'' if version_ok else '  (need ' + '.'.join(map(str, MIN_GH)) + '+)'}")
    print(f"gh logged in:   {login or 'no (run gh auth login)'}")
    ok &= login is not None
    print(f"config file:    {path}{'' if path.exists() else ' (not present)'}")
    print(f"reviewer login: {reviewer or 'NOT SET'}{f'  from {reviewer_src}' if reviewer else ''}")
    ok &= reviewer is not None
    if reviewer and login and reviewer.lower() != login.lower():
        print(f"                MISMATCH: gh is logged in as {login}; posting will be refused")
        ok = False
    print(f"mention login:  {mention or 'not set (needed for comment mode)'}"
          f"{f'  from {mention_src}' if mention else ''}")
    if not ok:
        print("setup:          set the reviewer login with --reviewer, $PR_REVIEW_LOGIN, or")
        print(f'                {path}: {{"reviewer_login": "LOGIN", "mention_login": "LOGIN"}}')
        print("                and log gh in as that account (gh auth login)")

    if args.repo and args.pr is not None and login:
        if not REPO_RE.match(args.repo):
            fail(f"--repo must be OWNER/REPO, got {args.repo!r}")
        pr = gh("api", f"repos/{args.repo}/pulls/{args.pr}")
        head = pr["head"]["sha"]
        print(f"pull request:   {args.repo}#{args.pr} {pr['state']}, head {head}, author {pr['user']['login']}")
        for mode in ("review", "comment"):
            prior = already_posted(args.repo, args.pr, login, mode, head)
            print(f"posted ({mode + '):':10}{prior or 'not yet for this head'}")
    print("ready" if ok else "not ready")
    return 0 if ok else 1


def post(args):
    for name in ("repo", "pr", "head", "review"):
        if getattr(args, name) is None:
            fail(f"--{name} is required (or use --check)")
    if not REPO_RE.match(args.repo):
        fail(f"--repo must be OWNER/REPO, got {args.repo!r}")
    if not SHA_RE.match(args.head):
        fail("--head must be the full 40-character head commit SHA")

    config, path = load_config()
    reviewer, _ = resolve(args.reviewer, "--reviewer", "PR_REVIEW_LOGIN", config, path, "reviewer_login")
    if not reviewer:
        missing_login("reviewer login", "--reviewer", "PR_REVIEW_LOGIN", "reviewer_login", path)
    mention = None
    if args.mode == "comment":
        if args.event != "comment":
            fail("--event applies to review mode only")
        mention, _ = resolve(args.mention, "--mention", "PR_REVIEW_MENTION", config, path, "mention_login")
        if not mention:
            missing_login("mention login for comment mode", "--mention", "PR_REVIEW_MENTION", "mention_login", path)

    require_gh_version()
    login = gh("api", "user")["login"]
    if login.lower() != reviewer.lower():
        fail(f"gh is authenticated as {login}, expected {reviewer}; not posting.")

    try:
        review = json.loads(Path(args.review).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read review file {args.review}: {exc}")

    pr = gh("api", f"repos/{args.repo}/pulls/{args.pr}")
    if pr["state"] != "open":
        fail(f"PR #{args.pr} is {pr['state']}; not posting.")
    if pr["head"]["sha"] != args.head:
        fail(f"PR head moved to {pr['head']['sha']} (reviewed {args.head}); re-review before posting.")

    if not args.repost:
        prior = already_posted(args.repo, args.pr, login, args.mode, args.head)
        if prior:
            fail(f"already posted for this head: {prior}\nPass --repost to post again.")

    files = gh_pages(f"repos/{args.repo}/pulls/{args.pr}/files?per_page=100")
    diff_lines = {f["filename"]: commentable_lines(f.get("patch")) for f in files}
    errors, postable, moved = validate(review, diff_lines)
    if errors:
        fail("review file invalid:\n  " + "\n  ".join(errors))

    event = EVENTS[args.event]
    all_text = review["body"] + "".join(c["body"] for c in postable + moved)
    if event == "REQUEST_CHANGES" and not BLOCKING_RE.search(all_text):
        fail("request-changes needs at least one **Bug:** or **Security:** finding; use --event comment.")
    if event == "REQUEST_CHANGES" and pr["user"]["login"].lower() == login.lower():
        note("GitHub does not allow requesting changes on your own PR; posting as COMMENT instead.")
        event = "COMMENT"

    marker = MARKER.format(mode=args.mode, sha=args.head)
    body = review["body"].rstrip()

    if args.mode == "review":
        endpoint = f"repos/{args.repo}/pulls/{args.pr}/reviews"
        payload = {"commit_id": args.head, "event": event,
                   "body": f"{review_body(body, moved)}\n\n{marker}", "comments": postable}
        summary = f"review ({event}): {len(postable)} inline, {len(moved)} moved to summary"
    else:
        def link(c):
            anchor = f"L{c['start_line']}-L{c['line']}" if "start_line" in c else f"L{c['line']}"
            path = urllib.parse.quote(c["path"], safe="/")
            return f"https://github.com/{args.repo}/blob/{args.head}/{path}#{anchor}"
        located = postable + moved
        if located:
            body += "\n\n### Line-level findings\n" + "\n".join(finding_lines(located, link))
        endpoint = f"repos/{args.repo}/issues/{args.pr}/comments"
        payload = {"body": f"@{mention}\n\n{body}\n\n{marker}"}
        summary = f"comment mentioning @{mention}: {len(located)} line-level finding(s)"

    note(summary)
    if args.dry_run:
        print(json.dumps({"endpoint": endpoint, "payload": payload}, indent=2))
        return 0

    result = run_gh("api", endpoint, "--method", "POST", "--input", "-", input=json.dumps(payload))
    if result.returncode != 0 and args.mode == "review" and postable and "HTTP 422" in result.stderr:
        # GitHub rejects the whole review when any inline anchor fails to resolve.
        note("GitHub rejected an inline comment (HTTP 422); retrying with every finding in the summary.")
        payload["body"] = f"{review_body(body, postable + moved)}\n\n{marker}"
        payload["comments"] = []
        result = run_gh("api", endpoint, "--method", "POST", "--input", "-", input=json.dumps(payload))
    if result.returncode != 0:
        fail(f"GitHub rejected the post:\n{(result.stderr or result.stdout).strip()}")
    print(json.loads(result.stdout)["html_url"])
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report gh login and configuration, then exit")
    ap.add_argument("--repo", help="OWNER/REPO")
    ap.add_argument("--pr", type=int)
    ap.add_argument("--head", help="full head SHA the findings were written against")
    ap.add_argument("--review", help="path to the review JSON file")
    ap.add_argument("--mode", choices=("review", "comment"), default="review")
    ap.add_argument("--event", choices=EVENTS, default="comment", help="review mode verdict; never approve")
    ap.add_argument("--reviewer", help="GitHub login gh must be authenticated as")
    ap.add_argument("--mention", help="GitHub login to @-mention in comment mode")
    ap.add_argument("--repost", action="store_true", help="post even if this head was already covered")
    ap.add_argument("--dry-run", action="store_true", help="validate and print the payload; post nothing")
    args = ap.parse_args()
    plain_gh_output()
    return check(args) if args.check else post(args)


if __name__ == "__main__":
    sys.exit(main())
