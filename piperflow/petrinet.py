"""Petri-net based validation for PiperFlow process models.

Builds a Place/Transition Petri net from the DSL and answers the questions:

* can the process complete?   — a marking holding only end-event tokens is reachable
* must it terminate?          — every reachable marking can still reach such a marking
* is it deadlock-free?        — every deadlock is a proper end marking
* are there dead activities?  — tasks never fired in any reachable state

Mapping semantics (deliberate, documented):

* a task-like element with multiple OUTGOING edges is an implicit AND-split
  (every branch receives a token);
* a task-like element with multiple INCOMING edges is an implicit AND-join —
  all incoming branches must arrive before it runs. Use an explicit exclusive
  gateway when you want an XOR merge;
* exclusive / inclusive / event gateways are XOR (a plain place): first
  arriving token passes, exactly one outgoing branch is taken;
* parallel gateways map to fork/join transitions;
* timer / message / intermediate events are pass-through steps — time is not
  modelled, so the analysis is conservative for timers.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque
from dataclasses import dataclass, field

EVENT_TAGS = {
    '@timer': 'timer',
    '@message': 'message',
    '@intermediate': 'intermediate',
    '@signal': 'signal',
    '@conditional': 'conditional',
    '@link': 'link',
}
TASK_LIKE = {
    'task', 'subprocess', 'timer', 'message',
    'intermediate', 'signal', 'conditional', 'link',
}
GW_XOR_LIKE = {'xor', 'inclusive', 'event_gw'}

_STATE_CAP = 200_000


# --------------------------------------------------------------------------- parsing


@dataclass
class Element:
    alias: str
    name: str
    kind: str


def _parse_declaration(line: str):
    m = re.fullmatch(r'\[@subprocess\s+([^\]]+)\]\s+as\s+(\w+)', line)
    if m:
        return 'subprocess', m.group(1).strip(), m.group(2)
    m = re.fullmatch(r'\((.+)\)\s+as\s+(\w+)', line)
    if m:
        inner, alias = m.group(1).strip(), m.group(2)
        low = inner.lower()
        if low == 'start' or low.startswith('start '):
            return 'start', inner, alias
        if low == 'end' or low.startswith('end '):
            return 'end', inner, alias
        if inner.startswith('@'):
            tag = inner.split(None, 1)[0].lower()
            kind = EVENT_TAGS.get(tag)
            if kind is None:
                return None
            rest = inner.split(None, 1)
            name = rest[1] if len(rest) > 1 else alias
            return kind, name, alias
        return 'intermediate', inner or alias, alias
    for tag, kind in (('@parallel', 'parallel'), ('@inclusive', 'inclusive'), ('@event', 'event_gw')):
        m = re.fullmatch(rf'<{tag}\s+([^>]+)>\s+as\s+(\w+)', line)
        if m:
            return kind, m.group(1).strip(), m.group(2)
    m = re.fullmatch(r'<([^>]+)>\s+as\s+(\w+)', line)
    if m:
        return 'xor', m.group(1).strip(), m.group(2)
    m = re.fullmatch(r'\[([^\]]+)\]\s+as\s+(\w+)', line)
    if m:
        return 'task', m.group(1).strip(), m.group(2)
    return None


def parse_process(dsl: str):
    """Parse DSL -> (elements, edges, errors, warnings).

    edges are (source_alias, target_alias) pairs; parallel duplicate edges
    are collapsed. Edge labels and connection sides are ignored (they do not
    influence control flow).
    """
    elements: dict[str, Element] = {}
    edges: list[tuple[str, str]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for lineno, raw in enumerate(dsl.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        low = line.lower()
        if low.startswith(('title:', 'colourtheme:', 'colortheme:', 'width:', 'footer:', 'lane:', 'pool:')):
            continue
        decl = _parse_declaration(line)
        if decl is not None:
            kind, name, alias = decl
            if alias in elements:
                errors.append(f'line {lineno}: duplicate alias "{alias}"')
                continue
            if kind in ('inclusive', 'event_gw'):
                warnings.append(
                    f'"{alias}": {kind} gateway analysed as XOR (conservative)')
            elements[alias] = Element(alias, name, kind)
            continue
        if '->' in line:
            prev: str | None = None
            for part in line.split('->'):
                part = part.strip()
                if ':' in part:
                    part = part.split(':', 1)[0].strip()
                part = re.sub(r'-\([^)]*\)$', '', part).strip()
                if prev is not None:
                    edges.append((prev, part))
                prev = part
            continue
        errors.append(f'line {lineno}: unrecognized line "{line}"')
    return elements, edges, errors, warnings


# --------------------------------------------------------------------------- net building


@dataclass
class Net:
    places: set[str] = field(default_factory=set)
    transitions: dict[str, tuple[dict[str, int], dict[str, int], str | None]] = field(default_factory=dict)
    initial: dict[str, int] = field(default_factory=dict)
    end_places: set[str] = field(default_factory=set)
    tid_name: dict[str, str] = field(default_factory=dict)  # labeled transitions only
    end_alias: dict[str, str] = field(default_factory=dict)  # end place id -> alias


def build_net(elements: dict[str, Element], edges: list[tuple[str, str]]):
    """Wire elements into a P/T net. Returns (net, errors)."""
    errors: list[str] = []
    succ: dict[str, set[str]] = defaultdict(set)
    pred: dict[str, set[str]] = defaultdict(set)
    for s, d in edges:
        if s not in elements:
            errors.append(f'connection references undeclared alias "{s}"')
            continue
        if d not in elements:
            errors.append(f'connection references undeclared alias "{d}"')
            continue
        if s == d:
            errors.append(f'self-connection on "{s}"')
            continue
        succ[s].add(d)
        pred[d].add(s)

    starts = [a for a, e in elements.items() if e.kind == 'start']
    ends = [a for a, e in elements.items() if e.kind == 'end']
    if len(starts) != 1:
        errors.append(f'exactly one start event is required, found {len(starts)}')
    if not ends:
        errors.append('at least one end event is required')
    for alias, el in elements.items():
        if el.kind == 'start':
            if pred[alias]:
                errors.append(f'start event "{alias}" has incoming connections')
            if len(succ[alias]) > 1:
                errors.append(f'start event "{alias}" has multiple outgoing connections')
        elif el.kind == 'end':
            if succ[alias]:
                errors.append(f'end event "{alias}" has outgoing connections')
        else:
            if not pred[alias]:
                errors.append(f'"{alias}" ({el.name}) has no incoming connection')
            if not succ[alias]:
                errors.append(f'"{alias}" ({el.name}) has no outgoing connection')
    if errors:
        return None, errors

    net = Net()
    gw_place: dict[str, str] = {}
    in_place: dict[str, str] = {}

    for alias, el in elements.items():
        k = el.kind
        if k == 'start':
            p = f'in[{alias}]'
            net.places.add(p)
            net.initial[p] = 1
            in_place[alias] = p
        elif k == 'end':
            p = f'end[{alias}]'
            net.places.add(p)
            net.end_places.add(p)
            net.end_alias[p] = alias
            in_place[alias] = p
        elif k in TASK_LIKE:
            in_place[alias] = f'in[{alias}]'
            net.places.add(in_place[alias])
        else:  # xor-like and parallel gateways
            gw_place[alias] = f'gw[{alias}]'
            net.places.add(gw_place[alias])

    # labeled transitions for task-like elements (AND-join semantics: the
    # input place accumulates one token per incoming edge and the transition
    # consumes all of them).
    for alias, el in elements.items():
        if el.kind in TASK_LIKE:
            pre = defaultdict(int, {in_place[alias]: len(pred[alias])})
            net.transitions[f'task:{alias}'] = (pre, defaultdict(int), el.name)
            net.tid_name[f'task:{alias}'] = el.name

    # silent join/fork transitions for parallel gateways
    for alias, el in elements.items():
        if el.kind == 'parallel' and len(pred[alias]) > 1:
            net.transitions[f'join:{alias}'] = (defaultdict(int), defaultdict(int), None)
        if el.kind == 'parallel' and len(succ[alias]) > 1:
            net.transitions[f'fork:{alias}'] = (defaultdict(int), defaultdict(int), None)

    def consume_place(s: str, d: str) -> str:
        """The place a token must land in for flow s->d (d's input)."""
        k = elements[d].kind
        if k == 'parallel' and len(pred[d]) > 1:
            return f'joinin[{s}->{{{d}}}]'
        if k in TASK_LIKE:
            return in_place[d]
        if k == 'end':
            return in_place[d]
        return gw_place[d]  # xor-like or parallel with single input

    def producer_transition_post(s: str, d: str):
        """Wire s's producing side for edge s->d.

        Returns None when s's own transition posts the token directly into
        d's consume place; otherwise returns the bare producer place (start
        event or gateway place) that needs a silent pass transition.
        """
        k = elements[s].kind
        if k in TASK_LIKE:
            net.transitions[f'task:{s}'][1][consume_place(s, d)] += 1
            return None
        if k == 'parallel':
            if len(succ[s]) > 1:
                pre, post, _ = net.transitions[f'fork:{s}']
                pre[gw_place[s]] = 1
                post[consume_place(s, d)] += 1
                return None
            return gw_place[s]  # single-output parallel: bare place
        return gw_place.get(s) or in_place[s]  # start / xor-like: bare place

    for s, d in edges:
        if s not in elements or d not in elements or s == d:
            continue
        cp = consume_place(s, d)
        net.places.add(cp)
        # parallel join: collect one token per incoming edge, emit one token
        if elements[d].kind == 'parallel' and len(pred[d]) > 1:
            pre, post, _ = net.transitions[f'join:{d}']
            pre[cp] = 1
            post[gw_place[d]] = 1
        src = producer_transition_post(s, d)
        if src is not None:
            tid = f'pass:{s}->{{{d}}}'
            if tid not in net.transitions:
                net.transitions[tid] = ({}, {}, None)
            net.transitions[tid][0][src] = 1
            net.transitions[tid][1][cp] = 1

    return net, errors


# --------------------------------------------------------------------------- analysis


@dataclass
class Witness:
    """A concrete task sequence over the reachable state space (bounded).

    Reconstructed from first-discovery BFS parents, so it is a *shortest*
    witness. Only labeled task transitions appear (silent fork/join/pass
    steps are omitted) — this is the behavior an author recognises. One
    witness per interesting state, never an enumeration of the trace
    language: loops make it infinite even when the marking set is bounded.
    """

    kind: str  # 'completion' | 'deadlock'
    labels: list[str] = field(default_factory=list)
    # alias of the end event reached (completion witnesses, one per end)
    end: str | None = None


@dataclass
class AnalysisReport:
    verdict: str  # SOUND | UNSOUND | INVALID | INCONCLUSIVE
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    can_complete: bool = False
    always_terminates: bool = False
    deadlocks: list[str] = field(default_factory=list)
    livelock_states: int = 0
    dead_tasks: list[str] = field(default_factory=list)
    states: int = 0
    places: int = 0
    transitions: int = 0
    # shortest completion trace + one trace per deadlock marking (max 5)
    witnesses: list[Witness] = field(default_factory=list)

    def summary_line(self) -> str:
        if self.verdict == 'INVALID':
            return f'INVALID structure ({len(self.errors)} error(s)): {self.errors[0]}'
        if self.verdict == 'INCONCLUSIVE':
            return f'INCONCLUSIVE: state space exceeds {_STATE_CAP} markings'
        parts = [f'{self.states} states', 'can complete' if self.can_complete else 'CANNOT complete']
        parts.append('always terminates' if self.always_terminates else 'MAY NOT terminate')
        if self.deadlocks:
            parts.append(f'{len(self.deadlocks)} deadlock(s)')
        if self.dead_tasks:
            parts.append(f'dead tasks: {", ".join(self.dead_tasks)}')
        head = 'SOUND' if self.verdict == 'SOUND' else 'UNSOUND'
        return f'{head} ({"; ".join(parts)})'


def _canonical(m: dict[str, int]) -> tuple:
    return tuple(sorted((p, c) for p, c in m.items() if c > 0))


def _enabled(m: dict[str, int], transitions) -> bool:
    for pre, _post, _label in transitions.values():
        if all(m.get(p, 0) >= w for p, w in pre.items()):
            return True
    return False


def _is_end_marking(m: tuple, end_places: set[str]) -> bool:
    return bool(m) and all(p in end_places for p, _c in m)


def analyze(net: Net, state_cap: int = _STATE_CAP) -> AnalysisReport:
    report = AnalysisReport(
        verdict='UNSOUND', places=len(net.places), transitions=len(net.transitions),
        warnings=[],
    )
    init = _canonical(net.initial)
    seen = {init}
    order: list[tuple] = [init]  # BFS discovery order: deterministic + shortest witnesses
    parents: dict[tuple, tuple[tuple, str]] = {}  # marking -> (predecessor marking, fired tid)
    succ_m: dict[tuple, list[tuple[str, tuple]]] = {init: []}
    queue = deque([init])
    truncated = False
    fired: set[str] = set()

    while queue:
        m = queue.popleft()
        md = dict(m)
        for tid, (pre, post, label) in net.transitions.items():
            if all(md.get(p, 0) >= w for p, w in pre.items()):
                m2 = dict(md)
                for p, w in pre.items():
                    m2[p] -= w
                for p, w in post.items():
                    m2[p] = m2.get(p, 0) + w
                c2 = _canonical(m2)
                succ_m[m].append((tid, c2))
                if label is not None:
                    fired.add(tid)
                if c2 not in seen:
                    seen.add(c2)
                    order.append(c2)
                    parents[c2] = (m, tid)
                    succ_m[c2] = []
                    queue.append(c2)
                    if len(seen) > state_cap:
                        truncated = True
                        queue.clear()
                        break
        if truncated:
            break

    report.states = len(seen)
    if truncated:
        report.verdict = 'INCONCLUSIVE'
        return report

    end_markings = [m for m in order if _is_end_marking(m, net.end_places)]
    deadlocks = [m for m in order if not _enabled(dict(m), net.transitions)]
    improper = [m for m in deadlocks if not _is_end_marking(m, net.end_places)]

    # backward traversal: which markings can still reach an end marking?
    preds_m: dict[tuple, list[tuple]] = defaultdict(list)
    for m, outs in succ_m.items():
        for _tid, m2 in outs:
            preds_m[m2].append(m)
    reach_end = set(end_markings)
    dq = deque(end_markings)
    while dq:
        m = dq.popleft()
        for pm in preds_m.get(m, []):
            if pm not in reach_end:
                reach_end.add(pm)
                dq.append(pm)
    livelocks = [m for m in seen if m not in reach_end]

    dead_tasks = sorted(
        net.tid_name[tid] for tid in net.tid_name if tid not in fired
    )

    report.can_complete = bool(end_markings)
    report.always_terminates = report.can_complete and not livelocks
    report.deadlocks = [_fmt_marking(m) for m in improper[:5]]

    def witness_labels(target: tuple) -> list[str]:
        """Walk first-discovery parents back to the initial marking."""
        tids: list[str] = []
        m = target
        while m in parents:
            prev, tid = parents[m]
            if tid in net.tid_name:
                tids.append(net.tid_name[tid])
            m = prev
        tids.reverse()
        return tids

    witnesses: list[Witness] = []
    seen_end_places: set[str] = set()
    for m in end_markings:
        # one shortest witness per distinct end event (max 5), discovery order
        endp = next((p for p, _c in m if p in net.end_places), None)
        if endp is None or endp in seen_end_places:
            continue
        seen_end_places.add(endp)
        witnesses.append(
            Witness(kind='completion', labels=witness_labels(m), end=net.end_alias.get(endp, endp))
        )
        if len(seen_end_places) >= 5:
            break
    witnesses.extend(Witness(kind='deadlock', labels=witness_labels(m)) for m in improper[:5])
    report.witnesses = witnesses
    report.livelock_states = len(livelocks)
    report.dead_tasks = dead_tasks
    if not report.can_complete:
        report.errors.append('no reachable end marking: the process can never complete')
    if improper:
        starved = sorted({
            p[3:-1] for m in improper for p, _c in m
            if p.startswith('in[') and p.endswith(']')
        })
        detail = ''
        if starved:
            name_of = {tid.split(':', 1)[1]: n for tid, n in net.tid_name.items()}
            names = ', '.join(name_of.get(a, a) for a in starved)
            detail = (
                f' — starved implicit AND-join at: {names}; a task with several '
                'incoming connections waits for ALL of them, so route merges and '
                'loop back-edges through an exclusive gateway instead'
            )
        report.errors.append(
            f'{len(improper)} deadlock marking(s) that are not proper ends, '
            f'e.g. {_fmt_marking(improper[0])}{detail}'
        )
    if livelocks:
        report.errors.append(
            f'{len(livelocks)} reachable state(s) can never reach an end (livelock/infinite loop)'
        )
    if dead_tasks:
        report.errors.append(f'dead activities (never executed): {", ".join(dead_tasks)}')
    report.verdict = 'SOUND' if not report.errors else 'UNSOUND'
    return report


def _fmt_marking(m: tuple) -> str:
    return '{' + ', '.join(f'{p}: {c}' for p, c in m) + '}'


def _fmt_witness(w: Witness) -> str:
    return ' -> '.join(w.labels) or '(no tasks fired)'


# --------------------------------------------------------------------------- entry point


def validate_process(dsl: str, state_cap: int = _STATE_CAP) -> AnalysisReport:
    """Parse, build and analyse a PiperFlow process. Never raises for bad DSL —
    problems come back as an INVALID/UNSOUND report."""
    elements, edges, errors, warnings = parse_process(dsl)
    if not elements:
        return AnalysisReport(verdict='INVALID', errors=errors or ['no elements found'], warnings=warnings)
    net, build_errors = build_net(elements, edges)
    errors = errors + build_errors
    if net is None:
        return AnalysisReport(verdict='INVALID', errors=errors, warnings=warnings)
    report = analyze(net, state_cap)
    report.errors = errors + report.errors
    report.warnings = warnings + report.warnings
    if errors:
        report.verdict = 'INVALID'
    return report


def format_report(r: AnalysisReport) -> str:
    lines = [f'Petri net verdict: {r.verdict} — {r.summary_line()}']
    lines.append(f'states: {r.states} | places: {r.places} | transitions: {r.transitions}')
    for e in r.errors:
        lines.append(f'ERROR: {e}')
    for w in r.warnings:
        lines.append(f'WARNING: {w}')
    for witness in r.witnesses:
        end_suffix = f' -> {witness.end}' if witness.end else ''
        lines.append(f'WITNESS ({witness.kind}{end_suffix}): {_fmt_witness(witness)}')
    if r.verdict == 'SOUND':
        lines.append('The process can run and always terminates properly.')
    return '\n'.join(lines)
