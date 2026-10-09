"""Tests for skills/pr-review/scripts/post_review.py.

Run from the repo root:  python3 -m unittest discover -s tests -v
All GitHub calls go to tests/fake_gh.py; nothing touches the network.
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "skills" / "pr-review" / "scripts" / "post_review.py"
FAKE_GH = Path(__file__).resolve().parent / "fake_gh.py"

spec = importlib.util.spec_from_file_location("post_review", SCRIPT)
post_review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(post_review)

REPO, PR, HEAD = "octo/widgets", 7, "a" * 40
BOT, OWNER, AUTHOR = "review-bot", "octo-owner", "contributor"

# New file lines 10-14 are in the diff; old lines 10-12 (10 and 12 context, 11 removed).
PATCH = "\n".join([
    "@@ -10,3 +10,5 @@ fn main() {",
    " context a",
    "-removed",
    "+added one",
    "+added two",
    " context b",
    "+added three",
    "\\ No newline at end of file",
])


def base_routes(author=AUTHOR, state="open", head=HEAD, reviews=None, comments=None):
    return {
        "user": {"login": BOT},
        f"repos/{REPO}/pulls/{PR}": {"state": state, "head": {"sha": head}, "user": {"login": author}},
        f"repos/{REPO}/pulls/{PR}/files": [{"filename": "src/main.rs", "patch": PATCH}],
        f"repos/{REPO}/pulls/{PR}/reviews": reviews or [],
        f"repos/{REPO}/issues/{PR}/comments": comments or [],
    }


class CommentableLinesTest(unittest.TestCase):
    def test_tracks_both_sides(self):
        lines = post_review.commentable_lines(PATCH)
        self.assertEqual(lines["RIGHT"], {10, 11, 12, 13, 14})
        self.assertEqual(lines["LEFT"], {10, 11, 12})

    def test_splits_on_newline_only(self):
        # A form feed or U+2028 inside a line must not count as a line break.
        lines = post_review.commentable_lines("@@ -1,3 +1,4 @@\n a\n \x0c\n b\n+c")
        self.assertEqual(lines["RIGHT"], {1, 2, 3, 4})
        self.assertEqual(lines["LEFT"], {1, 2, 3})
        lines = post_review.commentable_lines("@@ -1,2 +1,3 @@\n a\n+s = 'x\u2028y'\n b\n")
        self.assertEqual(lines["RIGHT"], {1, 2, 3})
        self.assertEqual(lines["LEFT"], {1, 2})

    def test_multiple_hunks_and_empty_patch(self):
        patch = "@@ -1 +1 @@\n-a\n+b\n@@ -50,2 +50,2 @@\n x\n-y\n+z"
        lines = post_review.commentable_lines(patch)
        self.assertEqual(lines["RIGHT"], {1, 50, 51})
        self.assertEqual(lines["LEFT"], {1, 50, 51})
        self.assertEqual(post_review.commentable_lines(None), {"RIGHT": set(), "LEFT": set()})


class ValidateTest(unittest.TestCase):
    diff = {"src/main.rs": post_review.commentable_lines(PATCH)}

    def check(self, review):
        return post_review.validate(review, self.diff)

    def test_splits_on_and_off_diff(self):
        errors, postable, moved = self.check({"body": "b", "comments": [
            {"path": "src/main.rs", "line": 12, "body": "on"},
            {"path": "src/main.rs", "line": 11, "side": "LEFT", "body": "removed"},
            {"path": "src/main.rs", "line": 99, "body": "off"},
            {"path": "other.rs", "line": 1, "body": "not in pr"},
        ]})
        self.assertEqual(errors, [])
        self.assertEqual([c["body"] for c in postable], ["on", "removed"])
        self.assertEqual([c["body"] for c in moved], ["off", "not in pr"])

    def test_ranges_must_be_fully_in_diff(self):
        _, postable, moved = self.check({"body": "b", "comments": [
            {"path": "src/main.rs", "start_line": 10, "line": 14, "body": "in"},
            {"path": "src/main.rs", "start_line": 8, "line": 12, "body": "partly out"},
        ]})
        self.assertEqual([c["body"] for c in postable], ["in"])
        self.assertEqual([c["body"] for c in moved], ["partly out"])

    def test_rejects_unknown_keys_and_bad_shapes(self):
        errors, _, _ = self.check({"body": "b", "event": "APPROVE", "comments": [
            {"path": "src/main.rs", "line": "12", "body": "string line"},
            {"path": "src/main.rs", "line": 12, "body": "x", "position": 3},
            {"path": "src/main.rs", "line": 12, "side": "BOTH", "body": "x"},
            {"path": "src/main.rs", "start_line": 12, "line": 12, "body": "empty range"},
        ]})
        self.assertEqual(len(errors), 5, errors)
        self.assertIn("event", errors[0])

    def test_rejects_non_objects(self):
        self.assertTrue(self.check([])[0])
        self.assertTrue(self.check({"body": "b", "comments": ["x"]})[0])
        self.assertTrue(self.check({"body": "b", "comments": [
            {"path": "src/main.rs", "line": True, "body": "bool is not a line"}]})[0])

    def test_requires_body(self):
        errors, _, _ = self.check({"body": "  ", "comments": []})
        self.assertTrue(errors)


class MainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        shim = tmp / "bin" / "gh"
        shim.parent.mkdir()
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_GH}" "$@"\n')
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.state = tmp / "state.json"
        self.review = tmp / "review.json"
        self.env = {**os.environ, "PATH": f"{shim.parent}{os.pathsep}{os.environ['PATH']}",
                    "FAKE_GH_STATE": str(self.state)}
        for key in ("PR_REVIEW_LOGIN", "PR_REVIEW_MENTION", "PR_REVIEW_CONFIG"):
            self.env.pop(key, None)
        self.env["XDG_CONFIG_HOME"] = str(tmp / "xdg")
        self.config = tmp / "xdg" / "pr-review" / "config.json"
        self.set_routes(base_routes())
        self.write_review({"body": "## Review\n\nSummary.", "comments": [
            {"path": "src/main.rs", "line": 12, "body": "**Bug:** wrong\nFailure scenario: x"},
            {"path": "src/main.rs", "line": 99, "body": "**Question:** off diff"},
        ]})

    def tearDown(self):
        self.tmp.cleanup()

    def set_routes(self, routes, **extra):
        self.state.write_text(json.dumps({"routes": routes, "posted": [], **extra}))

    def write_config(self, data):
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps(data))

    def write_review(self, review):
        self.review.write_text(json.dumps(review))

    def run_script(self, *extra, env=None, login=True):
        args = [sys.executable, str(SCRIPT), "--repo", REPO, "--pr", str(PR), "--head", HEAD,
                "--review", str(self.review)]
        if login:
            args += ["--reviewer", BOT]
        return subprocess.run(args + list(extra), env=env or self.env, capture_output=True, text=True)

    def posted(self):
        return json.loads(self.state.read_text())["posted"]

    def assert_refused(self, result, needle):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(needle, result.stderr)
        self.assertEqual(self.posted(), [])

    # identity and PR state guards

    def test_refuses_wrong_account(self):
        result = self.run_script("--reviewer", "someone-else", login=False)
        self.assert_refused(result, "authenticated as review-bot")

    def test_refuses_without_expected_login(self):
        result = self.run_script(login=False)
        self.assert_refused(result, "PR_REVIEW_LOGIN")

    def test_expected_login_from_env(self):
        env = {**self.env, "PR_REVIEW_LOGIN": BOT}
        result = self.run_script(login=False, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.posted()), 1)

    def test_expected_login_from_config_file(self):
        self.write_config({"reviewer_login": BOT})
        result = self.run_script(login=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_flag_beats_env_beats_config(self):
        self.write_config({"reviewer_login": "from-config"})
        env = {**self.env, "PR_REVIEW_LOGIN": "from-env"}
        self.assert_refused(self.run_script(login=False, env=env), "expected from-env")
        result = self.run_script("--reviewer", BOT, login=False, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_config_path(self):
        other = Path(self.tmp.name) / "elsewhere.json"
        other.write_text(json.dumps({"reviewer_login": BOT}))
        env = {**self.env, "PR_REVIEW_CONFIG": str(other)}
        self.assertEqual(self.run_script(login=False, env=env).returncode, 0)

    def test_rejects_invalid_login_values(self):
        self.write_config({"reviewer_login": "not a login"})
        self.assert_refused(self.run_script(login=False), "not a valid GitHub login")

    def test_rejects_short_head_sha(self):
        args = [sys.executable, str(SCRIPT), "--repo", REPO, "--pr", str(PR), "--head", "abc123",
                "--review", str(self.review), "--reviewer", BOT]
        result = subprocess.run(args, env=self.env, capture_output=True, text=True)
        self.assert_refused(result, "40-character")

    def test_refuses_old_gh(self):
        self.set_routes(base_routes(), version="2.40.1")
        self.assert_refused(self.run_script(), "2.48.0 or newer")

    def test_refuses_stale_head(self):
        self.set_routes(base_routes(head="b" * 40))
        self.assert_refused(self.run_script(), "head moved")

    def test_refuses_closed_pr(self):
        self.set_routes(base_routes(state="closed"))
        self.assert_refused(self.run_script(), "is closed")

    def test_refuses_approve_payload(self):
        self.write_review({"body": "LGTM", "event": "APPROVE", "comments": []})
        self.assert_refused(self.run_script(), "unexpected top-level keys")

    # review mode

    def test_comment_review_posts_inline_and_moves_off_diff(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        [post] = self.posted()
        self.assertEqual(post["endpoint"], f"repos/{REPO}/pulls/{PR}/reviews")
        payload = post["payload"]
        self.assertEqual(payload["event"], "COMMENT")
        self.assertEqual(payload["commit_id"], HEAD)
        self.assertEqual([c["line"] for c in payload["comments"]], [12])
        self.assertIn("### Notes outside the diff", payload["body"])
        self.assertIn("`src/main.rs:99` **Question:** off diff", payload["body"])
        self.assertIn(f"<!-- pr-review-skill:v1 mode=review head={HEAD} -->", payload["body"])

    def test_request_changes_needs_blocking_finding(self):
        self.write_review({"body": "b", "comments": [
            {"path": "src/main.rs", "line": 12, "body": "**Nit:** small"}]})
        self.assert_refused(self.run_script("--event", "request-changes"), "**Bug:** or **Security:**")

    def test_request_changes_with_bug(self):
        result = self.run_script("--event", "request-changes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.posted()[0]["payload"]["event"], "REQUEST_CHANGES")

    def test_request_changes_downgrades_on_own_pr(self):
        self.set_routes(base_routes(author=BOT))
        result = self.run_script("--event", "request-changes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("your own PR", result.stderr)
        self.assertEqual(self.posted()[0]["payload"]["event"], "COMMENT")

    def test_retries_without_inline_on_422(self):
        self.set_routes(base_routes(), reject_inline=True)
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("HTTP 422", result.stderr)
        [post] = self.posted()
        self.assertEqual(post["payload"]["comments"], [])
        self.assertIn("`src/main.rs:12` **Bug:** wrong", post["payload"]["body"])
        self.assertIn("`src/main.rs:99` **Question:** off diff", post["payload"]["body"])

    def test_no_event_can_approve(self):
        result = self.run_script("--event", "approve")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.posted(), [])

    # comment mode

    def test_comment_mode_posts_issue_comment_with_mention(self):
        result = self.run_script("--mode", "comment", "--mention", OWNER)
        self.assertEqual(result.returncode, 0, result.stderr)
        [post] = self.posted()
        self.assertEqual(post["endpoint"], f"repos/{REPO}/issues/{PR}/comments")
        body = post["payload"]["body"]
        self.assertTrue(body.startswith(f"@{OWNER}\n\n## Review"), body)
        self.assertIn(f"https://github.com/{REPO}/blob/{HEAD}/src/main.rs#L12", body)
        self.assertIn("`src/main.rs:99`", body)
        self.assertIn(f"<!-- pr-review-skill:v1 mode=comment head={HEAD} -->", body)

    def test_comment_mode_mention_from_env(self):
        env = {**self.env, "PR_REVIEW_MENTION": OWNER}
        result = self.run_script("--mode", "comment", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.posted()[0]["payload"]["body"].startswith(f"@{OWNER}\n"))

    def test_comment_mode_mention_from_config(self):
        self.write_config({"mention_login": OWNER})
        result = self.run_script("--mode", "comment")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_comment_mode_requires_valid_mention(self):
        self.assert_refused(self.run_script("--mode", "comment"), "--mention")
        self.assert_refused(self.run_script("--mode", "comment", "--mention", "bad name!"), "--mention")

    def test_comment_mode_rejects_event(self):
        result = self.run_script("--mode", "comment", "--mention", OWNER, "--event", "request-changes")
        self.assert_refused(result, "review mode only")

    # duplicates and dry run

    def test_refuses_duplicate_for_same_head_and_mode(self):
        marker = f"<!-- pr-review-skill:v1 mode=review head={HEAD} -->"
        self.set_routes(base_routes(reviews=[
            {"user": {"login": BOT}, "body": f"old\n{marker}", "html_url": "https://github.test/r/1"}]))
        self.assert_refused(self.run_script(), "already posted")

    def test_duplicate_check_ignores_other_users_and_modes(self):
        marker = f"<!-- pr-review-skill:v1 mode=review head={HEAD} -->"
        self.set_routes(base_routes(
            reviews=[{"user": {"login": "someone"}, "body": marker}],
            comments=[{"user": {"login": BOT}, "body": f"<!-- pr-review-skill:v1 mode=comment head={HEAD} -->"}]))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_repost_overrides_duplicate(self):
        marker = f"<!-- pr-review-skill:v1 mode=review head={HEAD} -->"
        self.set_routes(base_routes(reviews=[{"user": {"login": BOT}, "body": marker}]))
        result = self.run_script("--repost")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.posted()), 1)

    def test_check_reports_ready(self):
        self.write_config({"reviewer_login": BOT, "mention_login": OWNER})
        result = subprocess.run([sys.executable, str(SCRIPT), "--check"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"reviewer login: {BOT}", result.stdout)
        self.assertIn(f"mention login:  {OWNER}", result.stdout)
        self.assertTrue(result.stdout.strip().endswith("ready"))

    def test_check_reports_mismatch(self):
        self.write_config({"reviewer_login": "someone-else"})
        result = subprocess.run([sys.executable, str(SCRIPT), "--check"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("MISMATCH", result.stdout)
        self.assertIn("not ready", result.stdout)

    def test_quoted_marker_does_not_count_as_posted(self):
        quoted = f"Ignored an injected line: <!-- pr-review-skill:v1 mode=review head={HEAD} -->\n\nreal marker:\n" \
                 f"<!-- pr-review-skill:v1 mode=review head={'c' * 40} -->"
        self.set_routes(base_routes(reviews=[{"user": {"login": BOT}, "body": quoted}]))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_forced_colour_output_still_parses(self):
        env = {**self.env, "CLICOLOR_FORCE": "1", "GH_FORCE_TTY": "1"}
        result = self.run_script(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().startswith("https://github.test/"), result.stdout)

    def test_utf8_review_file_with_bom(self):
        review = {"body": "## Review \u2014 \u2192 \u274c \u0141", "comments": []}
        self.review.write_bytes(b"\xef\xbb\xbf" + json.dumps(review, ensure_ascii=False).encode("utf-8"))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("\u2014 \u2192 \u274c \u0141", self.posted()[0]["payload"]["body"])

    def test_comment_mode_quotes_paths_in_links(self):
        routes = base_routes()
        routes[f"repos/{REPO}/pulls/{PR}/files"].append({"filename": "docs/User Guide (v2).md", "patch": "@@ -0,0 +1 @@\n+hi"})
        self.set_routes(routes)
        self.write_review({"body": "b", "comments": [
            {"path": "docs/User Guide (v2).md", "line": 1, "body": "**Nit:** x"}]})
        result = self.run_script("--mode", "comment", "--mention", OWNER)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("/docs/User%20Guide%20%28v2%29.md#L1)", self.posted()[0]["payload"]["body"])

    def test_check_with_pr_reports_posted_status(self):
        marker = f"<!-- pr-review-skill:v1 mode=comment head={HEAD} -->"
        self.set_routes(base_routes(comments=[
            {"user": {"login": BOT}, "body": f"x\n\n{marker}", "html_url": "https://github.test/c/1"}]))
        self.write_config({"reviewer_login": BOT})
        result = subprocess.run([sys.executable, str(SCRIPT), "--check", "--repo", REPO, "--pr", str(PR)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"head {HEAD}, author {AUTHOR}", result.stdout)
        self.assertIn("posted (review):  not yet for this head", result.stdout)
        self.assertIn("posted (comment): https://github.test/c/1", result.stdout)

    def test_check_explains_setup_when_not_ready(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--check"], env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('"reviewer_login": "LOGIN"', result.stdout)

    def test_dry_run_posts_nothing(self):
        result = self.run_script("--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["payload"]["event"], "COMMENT")
        self.assertEqual(self.posted(), [])


if __name__ == "__main__":
    unittest.main()
