"""Pydantic AI agent that turns plain English into BPMN diagrams via ProcessPiper."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.models import ModelProfile
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from .petrinet import format_report, validate_process
from .renderer import render_syntax, sanitize_name

DEFAULT_BASE_URL = 'http://192.168.0.206:8000/v1'
DEFAULT_MODEL = 'deepseek-v4.1-flash'

COLOUR_THEMES = (
    'DEFAULT', 'BLUEMOUNTAIN', 'GREENTURTLE', 'GREYWOOF', 'ORANGEPEEL',
    'PURPLERAIN', 'RUBYRED', 'SEAFOAMS', 'SUNFLOWER', 'TEALWATERS',
)


@dataclass
class RenderDeps:
    """Injected per-run rendering options."""

    output_dir: Path
    name: str = 'diagram'  # base file name, without extension
    fmt: str = 'png'  # 'png' or 'svg'
    export_bpmn: bool = False


class DiagramResult(BaseModel):
    """Final answer returned by the agent."""

    title: str
    piperflow_syntax: str = Field(
        description='The final PiperFlow DSL that rendered successfully'
    )
    diagram_files: list[str] = Field(description='Paths of generated diagram files')
    notes: str = Field(
        default='',
        description='Assumptions made where the description was ambiguous',
    )


def make_model(
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> OpenAIChatModel:
    """Build an OpenAI-compatible model (local vLLM/LM Studio/llama.cpp style).

    `supports_forced_tool_choice=False` keeps pydantic-ai from sending
    `tool_choice: "required"` / named-function forcing: llama.cpp-style servers
    reject it when the model's chat template has no tool-call markers. Tool
    calls still work — the model just picks them itself.
    """
    return OpenAIChatModel(
        model or os.environ.get('LLM_MODEL', DEFAULT_MODEL),
        provider=OpenAIProvider(
            base_url=base_url or os.environ.get('LLM_BASE_URL', DEFAULT_BASE_URL),
            api_key=api_key or os.environ.get('LLM_API_KEY', 'local'),
        ),
        profile=ModelProfile(supports_forced_tool_choice=False),
    )


def _lang_rule(language: str | None) -> str:
    if language:
        return (
            f'- Write ALL element names, pool/lane names, titles, footers and edge\n'
            f'  labels in {language}, regardless of the description language —\n'
            f'  never mix languages.'
        )
    return (
        '- Write ALL element names, pool/lane names, titles, footers and edge\n'
        "  labels in the same language as the user's description — never mix\n"
        '  languages.'
    )


def piperflow_grammar(language: str | None = None) -> str:
    """The normative system prompt: DSL grammar + renderer limits + workflow.

    `language` forces the diagram language regardless of the prompt's
    language (None = follow the description, the historical default).
    """
    return f"""\
You are a BPMN process analyst. Convert the user's plain-English process
description into a diagram written in the PiperFlow DSL, then call the
`render_diagram` tool to validate and render it. If the tool reports an error,
fix the DSL and call it again. Never invent DSL constructs outside this grammar.

## PIPERFLOW DSL GRAMMAR

### 1. Header (top of the DSL)
    title: <diagram title>
    colourtheme: <THEME>          # one of: {', '.join(COLOUR_THEMES)}
    width: <pixels>               # optional

### 2. Structure: pools contain lanes, lanes contain elements
    pool: <pool name>
        lane: <lane name>
            <element>
A bare `lane:` outside any pool is allowed (an implicit pool is created).

### 3. Elements — declared inside a lane, each MUST have a unique alias
    (start) as <alias>                  # start event
    (end) as <alias>                    # end event
    (@timer <text>) as <alias>          # timer event
    (@intermediate <text>) as <alias>   # intermediate event
    # NOTE: (@message/@signal/@conditional) events are FORBIDDEN — processpiper
    # 0.8.1 silently displaces every element laid out after them. Model a
    # send/receive/notification as a plain task instead.
    (@link <text>) as <alias>           # link event
    [<task name>] as <alias>            # task (activity)
    [@subprocess <name>] as <alias>     # subprocess
    <gateway text> as <alias>           # exclusive gateway (default)
    <@parallel <text>> as <alias>       # parallel gateway
    <@inclusive <text>> as <alias>      # inclusive gateway
    <@event <text>> as <alias>          # event gateway

### 4. Connections — after all element declarations, one chain per line
    <alias>-><alias>-><alias>                  # chain
    <alias>-><alias>: <label>                  # labelled edge
    <alias>-(bottom, top)-><alias>: <label>    # explicit sides: top|bottom|left|right

### 5. Footer
    footer: <text>

## RULES
{_lang_rule(language)}
- Aliases are lowercase_with_underscores and unique across the whole diagram.
- Every alias used in a connection must be declared in a lane first.
- Exactly one start event; at least one end event; every gateway branch must
  eventually reach an end event.
- EVERY element (events and gateways included) except the single start event
  must have at least one incoming connection. Chain recurring triggers into the
  main flow, e.g. `keep_request->timer_7days->clone_listing`; a dangling
  `(@timer ...)` with no incoming connection breaks the layout engine.
- Prefer ONE pool with multiple lanes. Use a second pool only when truly
  needed, and connect across pools in forward direction only (from an earlier
  pool to a later pool, never backward).
- Model actors/roles as lanes; use one pool per system or organisation when the
  description mentions several.
- Keep pool and lane names SHORT (aim for <= 20 characters, no subtitles in
  parentheses): long names wrap onto a second vertical line that renders
  faint and overlapping. Prefer "Support" over "Support (first line)".
- Processes longer than ~12 elements must NOT be drawn as one diagram: model
  the main flow with `[@subprocess Name]` blocks (e.g. Preparation,
  Application Loop, Interview Cycle) and say in the notes that each
  subprocess should be generated separately — long linear chains render as
  huge unreadable diagrams with overlapping loop connectors.
- Use exclusive gateways `<...?>` for decisions (phrase as a question, e.g.
  `<Approved?>`), `<@inclusive ...>` for inclusive OR-branches,
  `[@subprocess ...]` for grouped sub-activities.
- For concurrent work, always emit TWO `<@parallel ...>` gateways: a split
  gateway before the parallel activities and a join gateway after them —
  never two outgoing edges from a plain task.
- Label the outgoing edges of decision gateways with the branch condition
  (e.g. `result_found->log_error: not found`).
- Loops: route the back edge into a decision gateway, NEVER into a task — a
  task with two incoming connections is an implicit AND-join and starves
  (deadlock). Create a dedicated entry gateway before the repeated work if
  none exists, and close the loop with a labelled edge like
  `done?->entry: not done`.
- ProcessPiper allows at most 4 incoming+outgoing connections COMBINED per
  element. Keep every gateway and task at 4 or fewer total connections; when a
  decision gateway would exceed it, chain two gateways and split the branches
  across them.

## WORKED EXAMPLE

    title: Sample Test Process
    colourtheme: GREENTURTLE
        lane: End User
            (start) as start
            [Enter Keyword] as enter_keyword
            (end) as end
        pool: System Search
            lane: Database System
                [Login] as login
                [Search Records] as search_records
                <Result Found?> as result_found
                [Display Result] as display_result
                [Logout] as logout
            lane: Log System
                [Log Error] as log_error

        start->login->enter_keyword->search_records->result_found->display_result->logout->end
        result_found->log_error->display_result

        footer: Generated by ProcessPiper

## LOOP EXAMPLE (back edge to a gateway, never to a task)

    title: Loop Example
    lane: Team
        (start) as start
        <Ready?> as ready
        [Do Work] as do_work
        <Done?> as done
        [Publish Result] as publish_result
        (end) as end
    start->ready
    ready->do_work
    do_work->done
    done->ready: not done
    done->publish_result: done
    publish_result->end

## WORKFLOW
1. Write the complete DSL for the user's description.
2. Call `validate_process` with the full DSL text: it builds a Petri net and
   checks the process can actually run and terminate. Fix every reported
   error (deadlocks, dead activities, unreachable ends) and re-validate until
   the verdict is SOUND.
3. Call `render_diagram` with the validated DSL text.
4. If render returns an error, correct the DSL and call again.
5. Reply only with the final DiagramResult, containing the DSL that passed
   validation and rendered successfully (never untested DSL) plus the file
   paths from the tool result.
"""


def build_agent(
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    language: str | None = None,
) -> Agent[RenderDeps, DiagramResult]:
    """Create the BPMN diagram agent bound to an OpenAI-compatible endpoint.

    `language` forces the diagram language (e.g. 'English'); None follows
    the description's language.
    """
    agent: Agent[RenderDeps, DiagramResult] = Agent(
        make_model(base_url, model, api_key),
        name='bpmn_diagram_agent',
        instructions=piperflow_grammar(language),
        deps_type=RenderDeps,
        output_type=DiagramResult,
    )

    @agent.tool
    def validate_process_tool(ctx: RunContext[RenderDeps], piperflow_syntax: str) -> str:
        """Validate a process model with Petri net analysis: check the process
        can run and terminate (no deadlocks, no dead activities, no livelocks).

        Args:
            piperflow_syntax: Complete PiperFlow DSL text to validate.
        """
        return format_report(validate_process(piperflow_syntax))

    @agent.tool(retries=6)
    def render_diagram(ctx: RunContext[RenderDeps], piperflow_syntax: str) -> str:
        """Validate and render PiperFlow DSL to a BPMN diagram image file.

        Args:
            piperflow_syntax: Complete PiperFlow DSL text: header, pools, lanes,
                elements with aliases, connections, footer.
        """
        output_file = (
            ctx.deps.output_dir / f'{sanitize_name(ctx.deps.name)}.{ctx.deps.fmt}'
        )
        try:
            _code, path = render_syntax(
                piperflow_syntax, output_file, export_bpmn=ctx.deps.export_bpmn
            )
        except NameError as exc:
            raise ModelRetry(
                f'ProcessPiper rejected the syntax: {exc}. The alias in the error '
                'is used in a connection but was never declared as an element '
                'inside a lane. Declare it or fix the alias name, then call '
                'render_diagram again.'
            ) from exc
        except (KeyError, IndexError, UnboundLocalError) as exc:
            raise ModelRetry(
                f'ProcessPiper layout engine crashed ({type(exc).__name__}: {exc}). '
                'Typical causes: an element other than the single start event with '
                'no incoming connection (including dangling @timer events), or a '
                'backward cross-pool connection. Restructure: keep one pool with '
                'multiple lanes, ensure every element has an incoming connection, '
                'and chain timers into the main flow.'
            ) from exc
        except Exception as exc:
            hint = ''
            if 'incoming and outgoing connections' in str(exc):
                hint = (
                    ' ProcessPiper allows at most 4 incoming+outgoing connections '
                    'COMBINED per element. Split the overloaded gateway into two '
                    'chained gateways (each routing part of the branches) and keep '
                    'the loop back-edge on a gateway that stays at 4 or fewer.'
                )
            raise ModelRetry(
                f'ProcessPiper rejected the syntax: {exc}.{hint} '
                'Fix the DSL and call render_diagram again.'
            ) from exc

        files = [str(path)]
        if ctx.deps.export_bpmn:
            bpmn = path.with_suffix('.bpmn')
            if bpmn.exists():
                files.append(str(bpmn))
        check = validate_process(piperflow_syntax)
        return (
            f'Successfully rendered: {", ".join(files)}\n'
            f'Petri net check: {check.summary_line()}'
        )

    return agent
