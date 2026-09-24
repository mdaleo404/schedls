import argparse
import subprocess


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="schedls",
        description="Inspect and manage Linux scheduled jobs.",
    )

    parser.add_argument(
        "--version",
        action="version",
        version="schedls 0.0.1",
    )

    subparsers = parser.add_subparsers(dest="command")

    calendar = subparsers.add_parser(
        "calendar",
        help="Validate and inspect a systemd calendar expression",
    )
    calendar.add_argument("expression")

    args = parser.parse_args()

    if args.command == "calendar":
        result = subprocess.run(
            ["systemd-analyze", "calendar", args.expression],
            check=False,
        )
        return result.returncode

    parser.print_help()
    return 0
