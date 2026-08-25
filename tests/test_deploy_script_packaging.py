"""The runbook's in-container commands must actually work in the container.

`scripts/backfill_day_keys.py` shipped as a documented `docker compose run`
command while `Dockerfile.poller` copied only `app/` and `catalog/`, so the
image did not contain the file at all — and even once copied, running it by path
puts `scripts/` on `sys.path` instead of the repo root, and the image installs
only the dependencies from `pyproject.toml` (the `app/` tree arrives in a later
COPY and is reached via WORKDIR), so there is no installed `app` to fall back
on. Both failures surface only on the VPS, mid-deploy, which is the worst place
to find them.
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "docs" / "DEPLOY.md"
DOCKERFILE = ROOT / "Dockerfile.poller"


def _scripts_run_in_the_poller_container() -> set[str]:
    """Every `scripts/*.py` the runbook invokes via docker, line-continuations
    folded so a wrapped command is still matched."""
    text = DEPLOY.read_text().replace("\\\n", " ")
    return set(re.findall(r"docker\s+(?:compose\s+run|exec)\b.*?python\s+(scripts/\S+\.py)",
                          text))


def test_the_runbook_only_names_scripts_that_exist():
    for rel in _scripts_run_in_the_poller_container():
        assert (ROOT / rel).is_file(), f"DEPLOY.md runs {rel}, which does not exist"


def test_the_poller_image_ships_the_scripts_the_runbook_runs():
    referenced = _scripts_run_in_the_poller_container()
    if not referenced:
        return
    copied = re.findall(r"^COPY\s+(\S+)", DOCKERFILE.read_text(), re.M)
    assert any(c.rstrip("/") == "scripts" for c in copied), (
        f"DEPLOY.md runs {sorted(referenced)} inside the poller container, but "
        f"Dockerfile.poller copies only {copied} — the file would be missing.")


def test_the_backfill_bootstraps_its_own_import_path():
    """`parents[1]` is only the repo root while the script sits directly in
    `scripts/`; moving it deeper would break the in-container import silently."""
    script = ROOT / "scripts" / "backfill_day_keys.py"
    assert script.parent.parent == ROOT
    assert "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))" \
        in script.read_text()


def test_the_backfill_runs_as_a_plain_script_from_a_foreign_cwd(tmp_path):
    """Smoke test: argparse alone would pass, so this is guarding the imports at
    module scope, which run before `main`."""
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "backfill_day_keys.py"), "--help"],
        cwd=tmp_path, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
