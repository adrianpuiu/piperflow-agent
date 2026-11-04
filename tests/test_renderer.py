"""Behavior tests for the renderer wrapper (no LLM required)."""
from pathlib import Path

import pytest

from piperflow.renderer import render_syntax, sanitize_name

VALID_DSL = """
title: Renderer Test
colourtheme: GREENTURTLE
    lane: User
        (start) as start
        [Do Work] as do_work
        (end) as end

    start->do_work->end

    footer: test
"""

INVALID_DSL = """
title: Broken
    lane: User
        (start) as start
        [Do Work] as do_work

    start->do_work->nonexistent_alias
"""


def test_render_syntax_creates_image(tmp_path: Path) -> None:
    out = tmp_path / 'test.png'
    _code, path = render_syntax(VALID_DSL, out)
    assert path == out
    assert out.exists() and out.stat().st_size > 0


def test_render_syntax_rejects_undefined_alias(tmp_path: Path) -> None:
    with pytest.raises(Exception, match='(?i).*(nonexistent_alias|not found|unknown).*'):
        render_syntax(INVALID_DSL, tmp_path / 'broken.png')


def test_render_syntax_refuses_misrendered_message_events(tmp_path: Path) -> None:
    # processpiper 0.8.1 renders these "successfully" with displaced elements;
    # the wrapper must refuse loudly instead of shipping a broken image.
    dsl = VALID_DSL.replace('[Do Work] as do_work', '(@message Notify) as do_work')
    with pytest.raises(Exception, match='(?i).*(not renderable|displaces).*'):
        render_syntax(dsl, tmp_path / 'broken.png')

def test_sanitize_name_strips_unsafe_characters() -> None:
    assert sanitize_name('../../etc/passwd') == 'etc_passwd'
    assert sanitize_name('My Process!!') == 'My_Process'
    assert sanitize_name('') == 'diagram'
    assert sanitize_name('...___') == 'diagram'
