"""Checks every skill against the Agent Skills spec (agentskills.io) and the
quirks of the agents this repo targets. Standard library only, so the
frontmatter checks are line-based rather than a full YAML parse.
"""

import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = sorted(p.parent for p in (ROOT / "skills").glob("*/SKILL.md"))

# Strict validators (skills-ref, claude.ai upload) reject any other top-level key.
SPEC_KEYS = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Folders that installs copy; anything else in a skill directory is noise.
SKILL_CONTENTS = {"SKILL.md", "scripts", "references", "assets"}


def published_files():
    """Files git would publish; falls back to a plain walk outside a checkout."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                             cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ROOT / line for line in out.splitlines() if (ROOT / line).exists()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if ".git" not in p.parts and p.is_file()]


def frontmatter(skill_dir):
    text = (skill_dir / "SKILL.md").read_text()
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        raise AssertionError(f"{skill_dir.name}: SKILL.md must start with a --- frontmatter block")
    block = match.group(1)
    top, continued, key = {}, {}, None
    for line in block.splitlines():
        m = re.match(r"^([A-Za-z][\w-]*):\s?(.*)$", line)
        if m:
            key = m.group(1)
            top[key] = m.group(2).strip()
            continued[key] = []
        elif key and line.strip():
            continued[key].append(line.strip())
    return block, top, continued, text[match.end():]


class SkillMetadataTest(unittest.TestCase):
    def test_there_are_skills(self):
        self.assertTrue(SKILLS, "no skills/<name>/SKILL.md found")

    def test_frontmatter(self):
        for skill in SKILLS:
            with self.subTest(skill=skill.name):
                block, top, continued, body = frontmatter(skill)
                for key in ("name", "description", "compatibility"):
                    # Keeps the length checks honest: no block scalars or wrapped values.
                    self.assertFalse(continued.get(key), f"keep {key} on one line")
                self.assertLessEqual(set(top), SPEC_KEYS, "only spec keys at top level; put the rest under metadata")
                self.assertEqual(top.get("name"), skill.name, "name must match the directory name")
                self.assertRegex(skill.name, NAME_RE)
                self.assertLessEqual(len(skill.name), 64)
                description = top.get("description", "")
                self.assertTrue(description, "description is required")
                self.assertLessEqual(len(description), 1024)
                if not description.startswith(("'", '"')):
                    # ": " or " #" inside an unquoted value breaks YAML parsing.
                    self.assertNotRegex(description, r": | #", "quote the description")
                self.assertLessEqual(len(top.get("compatibility", "")), 500)
                self.assertNotRegex(block, r":\s*\[", "write lists one item per line, not [a, b]")
                self.assertLess(len(body.splitlines()), 500, "keep SKILL.md under 500 lines")

    def test_skill_directories_hold_only_installable_files(self):
        files = published_files()
        for skill in SKILLS:
            with self.subTest(skill=skill.name):
                names = {p.relative_to(skill).parts[0] for p in files if skill in p.parents}
                self.assertLessEqual(names, SKILL_CONTENTS)
                self.assertFalse([p for p in files if skill in p.parents and p.is_symlink()],
                                 "no symlinks in skills")

    def test_no_stray_skill_files(self):
        # A published SKILL.md elsewhere (e.g. a test fixture) would install as another skill.
        stray = [p for p in published_files()
                 if p.name == "SKILL.md" and p.parent.parent != ROOT / "skills"]
        self.assertEqual(stray, [])

    def test_marketplace_lists_every_skill(self):
        manifest = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        self.assertFalse(manifest["name"].startswith(("claude-", "anthropic-")))
        listed = {Path(s).name for plugin in manifest["plugins"] for s in plugin.get("skills", [])}
        self.assertEqual(listed, {s.name for s in SKILLS})
        for plugin in manifest["plugins"]:
            for path in plugin.get("skills", []):
                self.assertTrue((ROOT / path / "SKILL.md").is_file(), path)


if __name__ == "__main__":
    unittest.main()
