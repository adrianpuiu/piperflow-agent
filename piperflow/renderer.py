"""Thin, quiet wrapper around processpiper's text-to-diagram renderer."""
from __future__ import annotations

import logging
import re
from pathlib import Path

# processpiper's ProcessMap calls `logging.basicConfig(level=DEBUG)` when it
# renders, which floods the console with PIL/httpx/openai debug records.
# Pinning these loggers' own levels stops the noise whatever root says.
for _name in (
    'processpiper',
    'PIL',
    'PIL.PngImagePlugin',
    'httpx',
    'httpcore',
    'openai',
):
    logging.getLogger(_name).setLevel(logging.WARNING)


def sanitize_name(name: str) -> str:
    """Turn an arbitrary diagram name into a safe file base name."""
    cleaned = re.sub(r'[^\w.-]+', '_', name.strip()).strip('._')
    return cleaned or 'diagram'

# processpiper 0.8.1 (latest) silently misplaces every element laid out after
# a message/signal/conditional event — the diagram renders "successfully" with
# wrong coordinates. Refuse loudly instead of shipping a broken image.
_MISRENDERED_EVENTS = ('message', 'signal', 'conditional')


def _check_known_misrenders(syntax: str) -> None:
    for line in syntax.splitlines():
        stripped = line.strip()
        for kind in _MISRENDERED_EVENTS:
            if stripped.startswith(f'(@{kind}'):
                raise ValueError(
                    f'(@{kind} ...) events are not renderable: processpiper '
                    f'0.8.1 silently displaces the elements that follow them. '
                    'Model the send/receive as a plain task, or use '
                    '(@intermediate ...)/(@timer ...)/(@link ...), which lay out correctly.'
                )

def render_syntax(
    syntax: str, output_file: str | Path, *, export_bpmn: bool = False
) -> tuple[str, Path]:
    """Render PiperFlow DSL to an image file.

    Returns ``(generated_python_code, output_path)``.
    Raises whatever processpiper raises on invalid DSL.
    """
    _check_known_misrenders(syntax)
    from processpiper.text2diagram import render
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    code, _img = render(syntax, str(output_file), export_to_bpmn=export_bpmn)
    return code, output_file
