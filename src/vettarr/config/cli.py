"""``config check | import-env | example``.

Exit codes: 0 fine, 1 the config (or the environment) is not valid or the
write was refused, 2 bad usage.
"""

from __future__ import annotations

import argparse
import os
import sys
from importlib import resources
from pathlib import Path

from vettarr.config.components import components
from vettarr.config.loader import ConfigError, check, default_path, dump, load_text
from vettarr.config.writer import ConfigReadOnly, write_text

EXAMPLE_NAME = "config.example.yaml"


def example_text() -> str:
    """The shipped config.example.yaml: packaged in the wheel, or the repo root in a checkout."""
    try:
        packaged = resources.files("vettarr.config").joinpath(EXAMPLE_NAME)
        if packaged.is_file():
            return packaged.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError):
        pass
    return (Path(__file__).resolve().parents[3] / EXAMPLE_NAME).read_text(encoding="utf-8")


def _check(path: str | None) -> int:
    target = Path(path) if path else default_path()
    try:
        cfg, warnings = check(target)
    except ConfigError as exc:
        print(f"{target}: not valid", file=sys.stderr)
        for line in exc.errors:
            print(f"  {line}", file=sys.stderr)
        return 1
    print(f"{target}: valid")
    started = components(cfg)
    if started:
        print("Would start:")
        width = max(len(name) for name, _ in started)
        for name, why in started:
            print(f"  {name.ljust(width)}  {why}")
    else:
        print("Would start: nothing (no block enables a component)")
    if warnings:
        print("Warnings:")
        for line in warnings:
            print(f"  {line}")
    return 0


def _import_env(output: str | None) -> int:
    from vettarr.config.import_env import document

    env = dict(os.environ)
    try:
        text = dump(document(env))
    except ValueError as exc:
        print(f"import-env: {exc}", file=sys.stderr)
        return 1
    if output:
        try:
            write_text(output, text, env=env)
        except ConfigReadOnly as exc:
            print(str(exc), file=sys.stderr)
            return 1
        except ConfigError as exc:
            print(f"import-env: the generated file is not valid, {output} not written:", file=sys.stderr)
            for line in exc.errors:
                print(f"  {line}", file=sys.stderr)
            return 1
        print(f"wrote {output}")
        return 0
    sys.stdout.write(text)
    try:
        load_text(text, "<import-env>", env)
    except ConfigError as exc:
        print("import-env: the generated file is not valid yet:", file=sys.stderr)
        for line in exc.errors:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vettarr config", description="Work with config.yaml.")
    commands = parser.add_subparsers(dest="command", required=True)
    check_cmd = commands.add_parser("check", help="validate a config file and list what it would start")
    check_cmd.add_argument("path", nargs="?", help="default: $CONFIG_PATH or ./config.yaml")
    import_cmd = commands.add_parser(
        "import-env", help="write the config equivalent to today's environment variables"
    )
    import_cmd.add_argument("-o", "--output", help="write here (keeps a .bak); default: print")
    commands.add_parser("example", help="print config.example.yaml")
    args = parser.parse_args(argv)
    if args.command == "check":
        return _check(args.path)
    if args.command == "import-env":
        return _import_env(args.output)
    sys.stdout.write(example_text())
    return 0
