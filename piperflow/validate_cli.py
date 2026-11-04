"""CLI: validate a PiperFlow process file with Petri net analysis."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .petrinet import format_report, validate_process


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='piperflow-validate',
        description='Check that a PiperFlow process can run and terminate '
        '(Petri net reachability analysis).',
    )
    parser.add_argument('file', type=Path, help='PiperFlow DSL file to validate')
    parser.add_argument(
        '--max-states', type=int, default=200_000,
        help='state-space exploration cap (default: 200000)',
    )
    args = parser.parse_args(argv)

    report = validate_process(args.file.read_text(encoding='utf-8'), args.max_states)
    print(format_report(report))
    return 0 if report.verdict == 'SOUND' else 1


if __name__ == '__main__':
    raise SystemExit(main())
