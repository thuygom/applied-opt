#!/usr/bin/env python3
"""Run Applied Opt's read-only source review without importing the target."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys


_BOOTSTRAP = (
    "import runpy,sys; "
    "sys.path.insert(0,sys.argv.pop(1)); "
    "runpy.run_module('applied_opt.cli',run_name='__main__')"
)


def framework_sources() -> list[Path]:
    """Explicit ``APPLIED_OPT_SRC``, this checkout's ``src``, then a copy vendored into the installed skill."""
    script = Path(__file__).resolve()
    candidates = [script.parents[3] / "src", script.parents[1] / "vendor"]
    if os.environ.get("APPLIED_OPT_SRC"):
        candidates.insert(0, Path(os.environ["APPLIED_OPT_SRC"]).expanduser())
    return candidates


def cli_prefix() -> list[str]:
    """Use a known source layout, otherwise an isolated install."""
    script = Path(__file__).resolve()
    for source in framework_sources():
        if (source / "applied_opt" / "cli.py").is_file():
            return [sys.executable, "-B", "-I", "-c", _BOOTSTRAP, str(source.resolve())]
    probe = subprocess.run(
        [sys.executable, "-B", "-I", "-c",
         "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('applied_opt.cli') else 1)"],
        cwd=script.parent, capture_output=True, check=False, timeout=10,
    )
    if probe.returncode == 0:
        return [sys.executable, "-B", "-I", "-m", "applied_opt.cli"]
    raise RuntimeError(
        "Applied Opt is unavailable. Use this skill inside its framework checkout, set "
        "APPLIED_OPT_SRC to a checkout's src directory, vendor src/applied_opt into the "
        "skill's vendor/ directory, or install the trusted package into this Python."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="Python source file or project directory to inspect")
    parser.add_argument("--format", choices=("json", "markdown", "sarif"), default="json")
    parser.add_argument("--base", help="Review a Git change scope relative to this revision")
    parser.add_argument("--output", type=Path, help="Optional report artifact path")
    parser.add_argument("--policy", type=Path, help="Optional Applied Opt policy file")
    parser.add_argument("--fail-on", choices=("high", "medium", "low", "none"), default="none")
    args = parser.parse_args(argv)
    target = args.target.expanduser().resolve()
    if not target.exists():
        parser.error("target does not exist")
    try:
        command = cli_prefix() + ["review", str(target), "--format", args.format, "--fail-on", args.fail_on]
        if args.base is not None:
            command += ["--base", args.base]
        if args.output is not None:
            command += ["--output", str(args.output.expanduser().resolve())]
        if args.policy is not None:
            command += ["--policy", str(args.policy.expanduser().resolve())]
        return subprocess.run(command, cwd=Path(__file__).resolve().parent,
                              check=False, timeout=120).returncode
    except subprocess.TimeoutExpired:
        print("Applied Opt review exceeded its 120 second wrapper limit.", file=sys.stderr)
        return 124
    except (OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
