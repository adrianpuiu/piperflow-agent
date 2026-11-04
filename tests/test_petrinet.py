"""Behavior tests for the Petri net process validator (no LLM required)."""
from piperflow.petrinet import format_report, validate_process

SOUND_SEQUENCE = """
title: Sound sequence
    lane: L
        (start) as start
        [Step A] as a
        [Step B] as b
        (end) as end
    start->a->b->end
"""

SOUND_XOR_BRANCHES = """
title: Sound xor branches
    lane: L
        (start) as start
        <Approved?> as gate
        [Handle approval] as yes
        [Handle rejection] as no
        (end) as end
    start->gate
    gate->yes: yes
    gate->no: no
    yes->end
    no->end
"""

# XOR split feeding a parallel join: only one branch token ever arrives,
# the join waits forever -> deadlock, end unreachable.
DEADLOCK_XOR_TO_AND_JOIN = """
title: Deadlock
    lane: L
        (start) as start
        <Which path?> as gate
        [Path A] as a
        [Path B] as b
        <@parallel Synchronise> as join
        (end) as end
    start->gate
    gate->a: one
    gate->b: two
    a->join
    b->join
    join->end
"""

# Two XOR branches converge directly on one task (implicit AND-join per our
# semantics): each run leaves the other input starved -> deadlock.
DEADLOCK_MISSING_MERGE = """
title: Missing merge gateway
    lane: L
        (start) as start
        <A oferit feedback?> as g1
        (@timer au trecut cateva ore) as wait
        [Solicita feedbackul] as solicita
        <A raspuns?> as g2
        (@message feedback primit) as msg
        [Inregistreaza feedbackul] as inregistreaza
        (end) as end
    start->g1
    g1->inregistreaza: Da
    g1->wait: Nu
    wait->solicita
    solicita->g2
    g2->msg: Da
    msg->inregistreaza
    g2->end: Nu
    inregistreaza->end
"""

FIXED_WITH_MERGE_GATEWAY = """
title: Merge gateway added
    lane: L
        (start) as start
        <A oferit feedback?> as g1
        (@timer au trecut cateva ore) as wait
        [Solicita feedbackul] as solicita
        <A raspuns?> as g2
        (@message feedback primit) as msg
        <Feedback disponibil?> as merge
        [Inregistreaza feedbackul] as inregistreaza
        (end) as end
    start->g1
    g1->merge: Da
    g1->wait: Nu
    wait->solicita
    solicita->g2
    g2->msg: Da
    msg->merge
    g2->end: Nu
    merge->inregistreaza->end
"""

UNCONNECTED_TASK = """
title: Dead activity
    lane: L
        (start) as start
        [Real step] as real
        [Orphan step] as orphan
        (end) as end
    start->real->end
"""

# The user's hand-written viewing lifecycle model (rendered OK in ProcessPiper
# and semantically sound: implicit parallel fork/join around scheduling).
USER_VIZIONARI = """
title: Ciclul de viață al unei vizionări imobiliare
width: 3400
lane: SORIN (asistent virtual)
    [Sună clientul] as suna
    [Stabilește data și ora cu clientul și proprietarul] as stabileste
    [Programează vizionarea în calendarul zilei] as programeaza
    [Reconfirmă vizionarea cu clientul (cu 1 oră înainte)] as reconf_client
    [Reconfirmă vizionarea cu proprietarul (cu 1 oră înainte)] as reconf_prop
    [Primește raportul agentului despre vizionare] as raport
    <@exclusive A dat feedback?> as g_feedback
    [Înregistrează feedbackul clientului] as inreg_fb
    [Actualizează criteriile clientului] as actualizeaza
    [Trimite clientului mesaj de feedback după câteva ore] as trimite_msg
    <@exclusive A răspuns la mesaj?> as g_raspuns
    [Înregistrează feedbackul din răspuns] as inreg_raspuns
    (end) as sfarsit
lane: Agent imobiliar
    [Efectuează vizionarea] as vizionare
    [Raportează lui SORIN cum a decurs vizionarea, părerea clientului și ce mai caută] as raporteaza
lane: Client
    (start) as start
    [Trimite mesaj WhatsApp cu cererea de vizionare] as cere
    [Stabilește data și ora vizionării] as client_stabileste
    [Reconfirmă vizionarea] as client_reconf
    [Participă la vizionare] as client_participa
    [Răspunde la mesajul de feedback] as client_raspunde
lane: Proprietar
    [Stabilește data și ora vizionării] as prop_stabileste
    [Reconfirmă vizionarea] as prop_reconf
    [Asigură accesul în proprietate] as prop_acces
start->cere->suna->stabileste
stabileste->client_stabileste->programeaza
stabileste->prop_stabileste->programeaza
programeaza->reconf_client->client_reconf->reconf_prop
reconf_prop->prop_reconf->vizionare
vizionare->client_participa->prop_acces->raporteaza
raporteaza->raport->g_feedback
g_feedback->inreg_fb: Da
inreg_fb->actualizeaza->sfarsit
g_feedback->trimite_msg: Nu
trimite_msg->client_raspunde->g_raspuns
g_raspuns->inreg_raspuns: Da
inreg_raspuns->sfarsit
g_raspuns->sfarsit: Nu
"""


# Loop that returns to a gateway (never to a task): the reachable marking set
# is bounded and the process is sound, but the trace language is infinite —
# witnesses must stay shortest-path, never loop unfoldings.
LOOP_PROCESS = """
title: Loop through gateway
    lane: L
        (start) as start
        <Ready?> as entry
        [Do work] as work
        <Again?> as again
        [Publish result] as publish
        (end) as end
    start->entry
    entry->work
    work->again
    again->entry: Nu
    again->publish: Da
    publish->end
"""

# Two labelled ends (success / failure): the validator must emit one shortest
# witness PER end event — the overall-shortest path ends at the failure end
# and would misrepresent the process if reported alone.
MULTI_END = """
title: Multi end
    lane: L
        (start) as start
        <Approved?> as gate
        [Deploy release] as deploy
        [Roll back deployment] as rollback
        (end) as ok
        (end) as ko
    start->gate
    gate->deploy: yes
    gate->rollback: no
    deploy->ok
    rollback->ko
"""


def test_multi_end_process_yields_one_witness_per_end() -> None:
    r = validate_process(MULTI_END)
    assert r.verdict == 'SOUND', format_report(r)
    completions = [w for w in r.witnesses if w.kind == 'completion']
    assert sorted(w.end for w in completions) == ['ko', 'ok']
    by_end = {w.end: w for w in completions}
    assert by_end['ok'].labels == ['Deploy release']
    assert by_end['ko'].labels == ['Roll back deployment']
    assert 'WITNESS (completion -> ok): Deploy release' in format_report(r)


def test_sequential_process_is_sound() -> None:
    r = validate_process(SOUND_SEQUENCE)
    assert r.verdict == 'SOUND', format_report(r)
    assert r.can_complete and r.always_terminates
    assert not r.deadlocks and not r.dead_tasks


def test_xor_branches_are_sound() -> None:
    r = validate_process(SOUND_XOR_BRANCHES)
    assert r.verdict == 'SOUND', format_report(r)


def test_xor_split_into_parallel_join_deadlocks() -> None:
    r = validate_process(DEADLOCK_XOR_TO_AND_JOIN)
    assert r.verdict == 'UNSOUND'
    assert not r.can_complete
    assert r.deadlocks  # starvation deadlock found
    assert not r.always_terminates


def test_missing_merge_gateway_deadlocks() -> None:
    r = validate_process(DEADLOCK_MISSING_MERGE)
    assert r.verdict == 'UNSOUND', format_report(r)
    assert r.deadlocks
    # the starved implicit AND-join is named in the error, with its fix
    assert any(
        'starved implicit AND-join' in e and 'Inregistreaza feedbackul' in e
        for e in r.errors
    )


def test_merge_gateway_fixes_the_process() -> None:
    r = validate_process(FIXED_WITH_MERGE_GATEWAY)
    assert r.verdict == 'SOUND', format_report(r)


def test_unconnected_task_is_rejected() -> None:
    r = validate_process(UNCONNECTED_TASK)
    assert r.verdict == 'INVALID'
    assert any('orphan' in e.lower() for e in r.errors)


def test_user_viewing_model_is_sound() -> None:
    r = validate_process(USER_VIZIONARI)
    assert r.verdict == 'SOUND', format_report(r)
    assert r.can_complete and r.always_terminates


def test_completion_witness_is_the_shortest_task_sequence() -> None:
    r = validate_process(SOUND_SEQUENCE)
    assert r.verdict == 'SOUND', format_report(r)
    assert len(r.witnesses) == 1
    assert r.witnesses[0].kind == 'completion'
    assert r.witnesses[0].labels == ['Step A', 'Step B']


def test_deadlock_witness_shows_how_the_process_gets_stuck() -> None:
    r = validate_process(DEADLOCK_XOR_TO_AND_JOIN)
    assert r.verdict == 'UNSOUND', format_report(r)
    deadlocks = [w for w in r.witnesses if w.kind == 'deadlock']
    assert deadlocks  # one counterexample per stuck marking
    # Each counterexample ends on one XOR branch; the parallel join never fires.
    assert all(w.labels and w.labels[-1] in ('Path A', 'Path B') for w in deadlocks)
    assert all('Synchronise' not in w.labels for w in deadlocks)
    assert not any(w.kind == 'completion' for w in r.witnesses)


def test_completion_witness_does_not_unfold_loops() -> None:
    r = validate_process(LOOP_PROCESS)
    assert r.verdict == 'SOUND', format_report(r)
    completions = [w for w in r.witnesses if w.kind == 'completion']
    assert len(completions) == 1
    assert completions[0].labels == ['Do work', 'Publish result']


def test_invalid_reports_have_no_witnesses() -> None:
    r = validate_process(UNCONNECTED_TASK)
    assert r.verdict == 'INVALID'
    assert r.witnesses == []


def test_format_report_prints_witnesses() -> None:
    out = format_report(validate_process(SOUND_SEQUENCE))
    assert 'WITNESS (completion -> end): Step A -> Step B' in out
