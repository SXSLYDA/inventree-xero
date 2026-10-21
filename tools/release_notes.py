"""Print one version's section of CHANGELOG.md (used by CI for GitHub release notes).

    python tools/release_notes.py 1.5.0                 # this repo's CHANGELOG.md
    python tools/release_notes.py 1.5.0 path/CHANGELOG.md

The section is everything after the "## 1.5.0 ..." heading up to the next "## " heading.
Exits with an error if the section is missing or empty, so a release never goes out
without notes.
"""
import pathlib
import re
import sys


def section(text, version):
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^## " + re.escape(version) + r"(\s|$)", line):
            start = i + 1
            break
    if start is None:
        return None
    out = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        out.append(line)
    return "\n".join(out).strip()


def main(argv):
    if len(argv) not in (2, 3):
        sys.exit("usage: release_notes.py X.Y.Z [CHANGELOG.md]")
    version = argv[1].lstrip("v")
    if len(argv) == 3:
        path = pathlib.Path(argv[2])
    else:
        path = pathlib.Path(__file__).resolve().parent.parent / "CHANGELOG.md"
    notes = section(path.read_text(encoding="utf-8"), version)
    if notes is None:
        sys.exit(f"CHANGELOG.md has no '## {version}' section")
    if not notes:
        sys.exit(f"CHANGELOG.md's '## {version}' section is empty")
    sys.stdout.reconfigure(encoding="utf-8")
    print(notes)


if __name__ == "__main__":
    main(sys.argv)
