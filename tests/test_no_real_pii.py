"""Every email-like string in tracked files must use a synthetic or
institutional domain. A real person's address never belongs in the repo —
when a bug is triggered by someone's actual data, the fixture reproduces
its shape, not its value.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Reserved example domains, obviously fake fixture domains, and the project's
# own or institutional contacts. Extend only for addresses that cannot belong
# to a private person.
ALLOWED_EMAIL_DOMAINS = {
    "b.com",
    "beipsielstadt.de",
    "buergerwecker.de",
    "example.com",
    "example.eu",
    "example.net",
    "example.org",
    "github.com",
    "jakubwaller.eu",
    "kommunix.de",
    "leipzig.de",
    "sub.example.co.uk",
    "x.com",
}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")


def test_tracked_files_contain_only_allowlisted_email_domains():
    tracked = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, check=True, capture_output=True
    ).stdout.decode()
    offenders = []
    for name in tracked.split("\0"):
        if not name:
            continue
        try:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for match in EMAIL_RE.finditer(text):
            if match.group(1).lower() not in ALLOWED_EMAIL_DOMAINS:
                offenders.append(f"{name}: {match.group(0)}")
    assert not offenders, (
        "email addresses with non-allowlisted domains in tracked files -- use "
        "synthetic fixtures (@example.com), or extend ALLOWED_EMAIL_DOMAINS "
        "only for addresses that cannot belong to a private person:\n"
        + "\n".join(offenders)
    )
