"""check-brief-model.py refuses a brief that names a stale model."""

import subprocess
import textwrap
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "gates" / "check-brief-model.py"


def _run(tmp_path: Path, brief: str, config: str, profile: str = "worker") -> subprocess.CompletedProcess:
    home = tmp_path / "hermes"
    cfg = home / "profiles" / profile / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(config)
    brief_path = tmp_path / "brief.md"
    brief_path.write_text(brief)
    return subprocess.run(
        ["python3", str(SCRIPT), "--brief", str(brief_path), "--profile", profile, "--home", str(home)],
        capture_output=True,
        text=True,
    )


LIVE = textwrap.dedent(
    """\
    model:
      provider: opencode-go
      default: mimo-v2.6-flash
    """
)


def test_stale_model_is_refused(tmp_path):
    brief = "provider: opencode-go\nmodel: deepseek-v4.1-flash\n"
    result = _run(tmp_path, brief, LIVE)
    assert result.returncode == 1
    assert "model mismatch" in result.stdout
    assert "mimo-v2.6-flash" in result.stdout


def test_live_model_passes(tmp_path):
    brief = "provider: opencode-go\nmodel: mimo-v2.6-flash\n"
    result = _run(tmp_path, brief, LIVE)
    assert result.returncode == 0
    assert result.stdout.startswith("PASS")


def test_missing_model_line_is_refused(tmp_path):
    result = _run(tmp_path, "provider: opencode-go\n", LIVE)
    assert result.returncode == 1
    assert "no model" in result.stdout
