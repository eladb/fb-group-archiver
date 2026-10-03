"""Every variable a shell script reads must be one it assigns.

These scripts run `set -u` under systemd, where an unbound variable kills them
mid-flight with nothing but a journal line to show it. That happened twice in
probe-rate-limit.sh within one day:

  * `$LOG_DIR` was referenced in the auto-resume block while only `$LOG` existed.
    The rate-limit window opened, the probe logged "CLEAR", then died before
    resuming the crawl -- which stayed idle for 19 hours.
  * `$COMMENTS` was copied in from watch-comments-finish.sh, where it is defined.
    Here the variable is `$after_comments`. The crawl resumed and the bookkeeping
    after it died.

Neither was caught by testing the block in a harness, because the harness defined
the missing variable itself -- it validated the scaffold, not the script. A static
check over the real file is what actually catches this, so it lives here.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = sorted(ROOT.glob("*.sh")) + sorted((ROOT / "data").glob("*.sh"))

# Supplied by the environment or the shell, not by the script.
EXTERNAL = {
    "HOME", "PATH", "PWD", "USER", "SHELL", "IFS", "BASH_SOURCE", "FUNCNAME",
    "RANDOM", "LINENO", "OPTARG", "OPTIND", "BASH_REMATCH", "PIPESTATUS",
    # Deliberate configuration knobs, read with a default or from EnvironmentFile.
    "PANDAS_LOG_DIR", "PANDAS_ARCHIVE_DIR", "PANDAS_BACKUP_S3",
    "PANDAS_ENCRYPT_RECIPIENT", "PANDAS_BACKUP_MIN_INTERVAL", "PANDAS_DB_KEY",
    "PANDAS_ALLOW_RETIRED", "AWS_PROFILE", "AWS_REGION",
}

# A bare $VAR or ${VAR} is the dangerous form. ${VAR:-default}, ${VAR:?msg} and
# ${VAR:=x} all supply or demand a value explicitly and cannot be the silent
# mid-run abort this test exists to catch -- archive-runner.sh reads five
# configuration knobs that way on purpose.
REF = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)\b|\{([A-Za-z_][A-Za-z0-9_]*)\})")
ASSIGN = re.compile(r"^\s*(?:local\s+|export\s+|declare\s+)?"
                    r"([A-Za-z_][A-Za-z0-9_]*)=")
# `for x in`, `while read x`, and `local a b c` all bind names too.
FOR = re.compile(r"\bfor\s+([A-Za-z_][A-Za-z0-9_]*)\s+in\b")
READ = re.compile(r"\bread\s+(?:-r\s+)?(?:-a\s+)?([A-Za-z_][A-Za-z0-9_]*)")
LOCAL = re.compile(r"^\s*local\s+(.*)$")


def names_bound(text):
    bound = set()
    for line in text.splitlines():
        for rx in (ASSIGN, FOR, READ):
            for m in rx.finditer(line):
                bound.add(m.group(1))
        m = LOCAL.match(line)
        if m:
            for tok in m.group(1).split():
                bound.add(tok.split("=")[0].lstrip("-"))
        # `local a=1 b=2`
        for m2 in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_]*)=", line):
            bound.add(m2.group(1))
    return bound


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_no_unbound_variables(script):
    text = script.read_text()
    if "set -u" not in text:
        pytest.skip(f"{script.name} does not run with set -u")
    referenced = {g for m in REF.finditer(text) for g in m.groups() if g}
    bound = names_bound(text) | EXTERNAL
    missing = sorted(referenced - bound)
    assert not missing, (
        f"{script.name} reads variables it never assigns: {missing}\n"
        f"  Under `set -u` these abort the script at the moment they are reached, "
        f"which may be a branch that only runs once a week.")
