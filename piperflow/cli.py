"""CLI entry point: turn a plain-English process description into a BPMN diagram."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from pydantic_ai.usage import UsageLimits

from .agent import DEFAULT_BASE_URL, DEFAULT_MODEL, RenderDeps, build_agent
from .renderer import sanitize_name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='piperflow-agent',
        description='Generate a BPMN diagram from a plain-English process description.',
    )
    parser.add_argument('text', nargs='?', help='Plain-English process description')
    parser.add_argument('-f', '--file', type=Path, help='Read the description from a text file')
    parser.add_argument('-o', '--output-dir', type=Path, default=Path('output'))
    parser.add_argument('--name', default='diagram', help='Base name for output files')
    parser.add_argument('--svg', action='store_true', help='Emit SVG instead of PNG')
    parser.add_argument('--bpmn', action='store_true', help='Also export .bpmn (BPMN XML)')
    parser.add_argument('--model', default=os.environ.get('LLM_MODEL', DEFAULT_MODEL))
    parser.add_argument(
        '--base-url', default=os.environ.get('LLM_BASE_URL', DEFAULT_BASE_URL)
    )
    parser.add_argument('--api-key', default=os.environ.get('LLM_API_KEY', 'local'))
    parser.add_argument(
        '--language',
        default=os.environ.get('LLM_LANGUAGE', ''),
        help="Force the diagram language (e.g. 'English'); default follows the description",
    )
    args = parser.parse_args(argv)

    if args.file:
        description = args.file.read_text(encoding='utf-8').strip()
    elif args.text:
        description = args.text.strip()
    else:
        parser.error('provide the process description as TEXT or via --file PATH')
        return 2

    agent = build_agent(
        base_url=args.base_url,
        model=args.model,
        api_key=args.api_key,
        language=args.language or None,
    )
    deps = RenderDeps(
        output_dir=args.output_dir,
        name=args.name,
        fmt='svg' if args.svg else 'png',
        export_bpmn=args.bpmn,
    )
    result = agent.run_sync(
        description,
        deps=deps,
        usage_limits=UsageLimits(request_limit=80),
    )

    out = result.output
    dsl_path = args.output_dir / f'{sanitize_name(args.name)}.piperflow'
    dsl_path.write_text(out.piperflow_syntax + '\n', encoding='utf-8')
    print('=' * 70)
    print(f'Title: {out.title}')
    if out.notes:
        print(f'Notes: {out.notes}')
    print('=' * 70)
    print(out.piperflow_syntax)
    print('=' * 70)
    print(f'Syntax saved: {dsl_path}')
    for f in out.diagram_files:
        print(f'Generated: {f}')
    usage = result.usage
    print(
        f'Requests: {usage.requests} | '
        f'Input tokens: {usage.input_tokens} | Output tokens: {usage.output_tokens}'
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
