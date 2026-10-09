#!/usr/bin/env python3
"""Refuse a dispatch brief whose model line does not match the live profile config.

Reads model.provider and model.default from the named profile's config.yaml.
Does not remember a model. Does not edit the brief or the config.

Exit 0: the brief's `provider:` and `model:` lines match the live config.
Exit 1: mismatch, missing field, or unreadable config.
Exit 2: usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # stdlib fallback is not enough for this config shape
    yaml = None


def _profile_config(profile: str, home: Path) -> Path:
    return home / "profiles" / profile / "config.yaml"


def _brief_values(text: str) -> tuple[str | None, str | None]:
    provider = None
    model = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("provider:") and provider is None:
            provider = stripped.split(":", 1)[1].strip()
        elif stripped.startswith("model:") and model is None:
            model = stripped.split(":", 1)[1].strip()
        if provider and model:
            break
    return provider, model


def check(brief: Path, profile: str, home: Path) -> tuple[int, str]:
    cfg_path = _profile_config(profile, home)
    if not brief.is_file():
        return 1, f"FAIL brief missing: {brief}"
    if not cfg_path.is_file():
        return 1, f"FAIL config missing: {cfg_path}"
    if yaml is None:
        return 1, "FAIL PyYAML is not installed; cannot read config.yaml"
    data = yaml.safe_load(cfg_path.read_text()) or {}
    model_block = data.get("model") or {}
    live_provider = str(model_block.get("provider") or "")
    live_model = str(model_block.get("default") or "")
    brief_provider, brief_model = _brief_values(brief.read_text())
    problems = []
    if not brief_provider:
        problems.append("brief has no provider: line")
    elif brief_provider != live_provider:
        problems.append(
            f"provider mismatch: brief={brief_provider!r} config={live_provider!r}"
        )
    if not brief_model:
        problems.append("brief has no model: line")
    elif brief_model != live_model:
        problems.append(
            f"model mismatch: brief={brief_model!r} config={live_model!r}"
        )
    if problems:
        return 1, "FAIL " + "; ".join(problems) + f" ({cfg_path})"
    return 0, f"PASS {profile} provider={live_provider} model={live_model}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", required=True, help="path to the dispatch brief")
    parser.add_argument("--profile", required=True, help="profile name, e.g. frida")
    parser.add_argument(
        "--home",
        default=str(Path.home() / ".hermes"),
        help="Hermes home that contains profiles/ (default: ~/.hermes)",
    )
    args = parser.parse_args(argv)
    code, message = check(Path(args.brief), args.profile, Path(args.home))
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
