"""v3.6.1 — the cutover interval under concurrency, deterministically.

Why this file exists separately: every other runtime test asks what *one*
operation does. These ask what two of them do to each other, which is a
different kind of test — it needs event control, a second thread or a second
*process*, and an assertion that names the serial outcome rather than "no
exception was raised".

Nothing here waits for a scheduler to be kind. Each interleaving is driven by
explicit events (in-process) or barrier files (cross-process), and the thing
asserted is the final control-plane state compared against the only outcome a
serial execution could have produced. A wait that is *expected* not to fire is
used only as supporting evidence that an operation is blocked, never as the
proof that the outcome is right.

The three claims pinned here, all of them from an independent probe's findings:

  * a pointer-changing operation holds its **whole** read/validate/write/confirm
    interval, so a second one cannot decide on a snapshot that is about to be
    replaced and discard the retained target;
  * the same holds across real, independent **processes**, where a shared
    in-process lock would prove nothing;
  * activation and migration withdrawal coordinate through one shared guard on
    the generation, so neither "withdrawn first" nor "admitted first" can leave
    a pointer at a generation that has been moved away.

Isolation: generations are produced by the real migration operator from a
neutral fixture snapshot with deterministic fake embeddings, under ``tmp_path``.
No real memory home, no real config file, no discovery, no network, no model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from layered_memory_mcp import graph_generation_runtime as runtime
from layered_memory_mcp import graph_migration
from layered_memory_mcp import graph_migration_generation as generation_module
from layered_memory_mcp.graph_generation_runtime import (
    STATUS_CUTOVER_APPLIED,
    STATUS_RESTORE_APPLIED,
    activate_generation,
    resolve_generation_runtime,
    restore_generation,
)

from test_v361_graph_migration import (  # noqa: F401 — fixtures are reused
    build_source,
    fake_embeddings,
    tree_state,
)

#: How long a test may wait for an event that *must* arrive. Generous: it is a
#: deadlock guard, not a timing assumption. Nothing is asserted about how long
#: an operation took.
ARRIVES = 60.0

#: How long a test waits for an event that must **not** arrive while another
#: operation holds the interval. Supporting evidence only — the decisive
#: assertion in every test below is the final serialized state.
BLOCKED = 1.0

#: Thread names, so one patched seam can tell the two operations apart without
#: either of them knowing it is being observed.
ACTIVATOR = "p1-activate"
RESTORER = "p1-restore"
ROLLER = "p1-rollback"


@pytest.fixture
def control_root(tmp_path) -> Path:
    root = tmp_path / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def pointer(control_root) -> Path:
    return control_root / "active.json"


@pytest.fixture
def source(tmp_path) -> Path:
    return build_source(tmp_path / "old-store")


def publish(source: Path, control_root: Path, name: str) -> Path:
    """One real published generation at ``<control root>/<name>/current``.

    Three independent destinations inside one control root is the layout the
    pointer contract allows: the pointer lives beside them, never inside one.
    """
    destination = control_root / name
    report = graph_migration.apply_migration(source=source, destination=destination)
    assert report["status"] == graph_migration.STATUS_APPLIED, report["findings"]
    return destination / "current"


def pointer_body(pointer: Path) -> dict:
    return json.loads(pointer.read_text(encoding="utf-8"))


def relative(control_root: Path, generation: Path) -> str:
    return generation.relative_to(control_root).as_posix()


def run_thread(name: str, call) -> tuple[threading.Thread, dict]:
    """Start ``call`` in a named thread and return it with its result slot."""
    outcome: dict = {}

    def body() -> None:
        try:
            outcome["report"] = call()
        except BaseException as exc:  # noqa: BLE001 — surfaced by the assertions
            outcome["error"] = f"{type(exc).__name__}"

    thread = threading.Thread(target=body, name=name, daemon=True)
    thread.start()
    return thread, outcome


# ---------------------------------------------------------------------------
# One process, two operations, one interval
# ---------------------------------------------------------------------------

def test_a_concurrent_restore_cannot_decide_on_a_superseded_pointer_snapshot(
    source, control_root, pointer, monkeypatch
):
    """The proven defect, as a test: a read/validate/write/confirm interval.

    The starting state is the probe's: ``b`` served, ``a`` retained. An
    activation of ``c`` is held at the moment of its write, and a restore is
    started while it is there. Only one outcome is serializable, because the
    restore cannot have run *before* an activation that has already read,
    validated and decided: activate ``c`` (retaining ``b``), then restore ``b``
    (retaining ``c``).

    What made the old behaviour unserializable was not the final replace — that
    was already atomic — but everything before it: the restore read the pointer
    the activation was about to supersede, and the activation then wrote a
    payload whose retained target was computed from a snapshot that no longer
    existed. ``a`` and then ``b`` disappeared from the recovery chain while both
    operations reported success.
    """
    a = publish(source, control_root, "a")
    b = publish(source, control_root, "b")
    c = publish(source, control_root, "c")
    assert activate_generation(pointer=pointer, generation=a, apply=True)["status"] == (
        STATUS_CUTOVER_APPLIED
    )
    assert activate_generation(pointer=pointer, generation=b, apply=True)["status"] == (
        STATUS_CUTOVER_APPLIED
    )

    at_write = threading.Event()
    may_write = threading.Event()
    restore_has_read = threading.Event()
    real_write = runtime._write_pointer
    real_read = runtime.read_active_pointer

    def write(path: Path, payload):
        if threading.current_thread().name == ACTIVATOR:
            at_write.set()
            assert may_write.wait(ARRIVES), "the activation was never released"
        return real_write(path, payload)

    def read(path: Path):
        if threading.current_thread().name == RESTORER:
            restore_has_read.set()
        return real_read(path)

    monkeypatch.setattr(runtime, "_write_pointer", write)
    monkeypatch.setattr(runtime, "read_active_pointer", read)

    activator, activation = run_thread(
        ACTIVATOR,
        lambda: activate_generation(pointer=pointer, generation=c, apply=True),
    )
    assert at_write.wait(ARRIVES), "the activation never reached its write"
    restorer, restoration = run_thread(
        RESTORER, lambda: restore_generation(pointer=pointer, apply=True)
    )

    # Supporting evidence, not the proof: while the activation holds the
    # interval the restore may not even *read* the pointer it would supersede.
    assert restore_has_read.wait(BLOCKED) is False
    assert pointer_body(pointer)["generation"] == relative(control_root, b)

    may_write.set()
    activator.join(ARRIVES)
    restorer.join(ARRIVES)
    assert not activator.is_alive() and not restorer.is_alive()

    assert activation.get("report", {}).get("status") == STATUS_CUTOVER_APPLIED
    assert restoration.get("report", {}).get("status") == STATUS_RESTORE_APPLIED
    # The decisive assertion: exactly the activate-then-restore serial outcome.
    final = pointer_body(pointer)
    assert final["generation"] == relative(control_root, b)
    assert final["previous_generation"] == relative(control_root, c)
    # And the restore reported the state it actually produced.
    resolution = restoration["report"]["resolution"]
    assert resolution["relative_generation"] == relative(control_root, b)
    assert resolution["previous_generation"] == relative(control_root, c)
    assert resolve_generation_runtime(pointer=pointer).config.root == b


# ---------------------------------------------------------------------------
# Two real processes, one interval
#
# An in-process mutex would satisfy the tests above and protect nothing a
# deployment cares about: the two operators of a control plane are two
# commands, which is two interpreters. These run the real module in real
# subprocesses — the second one through the real CLI entry point — and the
# overlap is established by barrier files rather than assumed.
# ---------------------------------------------------------------------------

#: The working tree's own ``src``, so a subprocess runs the code under test
#: rather than whatever copy of the package happens to be installed.
WORKING_TREE = Path(runtime.__file__).resolve().parents[1]

#: Pauses an activation inside its interval, at the moment of the write, until
#: a barrier file appears. The patched seam is the module's own private write;
#: nothing about the resolution, the proof or the payload is faked.
ACTIVATE_DRIVER = '''\
import json, sys, time
from pathlib import Path
from layered_memory_mcp import graph_generation_runtime as runtime

tree, ready, go, out, pointer, generation = sys.argv[1:7]
assert Path(runtime.__file__).resolve().parents[1] == Path(tree), runtime.__file__
real_write = runtime._write_pointer


def write(path, payload):
    Path(ready).write_text("at-write\\n", encoding="utf-8")
    limit = time.monotonic() + 120.0
    while not Path(go).exists():
        if time.monotonic() > limit:
            raise SystemExit("barrier-timeout")
        time.sleep(0.01)
    return real_write(path, payload)


runtime._write_pointer = write
report = runtime.activate_generation(pointer=pointer, generation=generation, apply=True)
Path(out).write_text(json.dumps(report), encoding="utf-8")
raise SystemExit(runtime.exit_code(report))
'''

#: The real CLI, unpatched, announcing only that it has started. Whatever it
#: does with the pointer it does on its own.
RESTORE_DRIVER = '''\
import sys
from pathlib import Path
from layered_memory_mcp import graph_generation_runtime as runtime

tree, entered, pointer = sys.argv[1:4]
assert Path(runtime.__file__).resolve().parents[1] == Path(tree), runtime.__file__
Path(entered).write_text("entered\\n", encoding="utf-8")
raise SystemExit(runtime.main(["restore", "--pointer", pointer, "--apply"]))
'''


def await_file(path: Path, *, other: subprocess.Popen) -> None:
    """Block until ``path`` appears, failing if the process died instead.

    A barrier, not a delay: it returns as soon as the other process says it has
    arrived, and nothing is asserted about how long that took.
    """
    limit = time.monotonic() + ARRIVES
    while not path.exists():
        if other.poll() is not None:
            raise AssertionError(
                f"{path.name} never appeared; process exited {other.returncode}"
            )
        if time.monotonic() > limit:
            raise AssertionError(f"{path.name} never appeared")
        time.sleep(0.01)


def test_two_real_processes_serialize_the_whole_pointer_interval(
    source, control_root, pointer, tmp_path
):
    """The cross-process half of the interval, with a real overlap.

    The activation is a real subprocess held *inside* its interval, at the
    moment of its write; the restore is a second real subprocess running the
    real CLI verb. The restore is started, and observed to have started, while
    the activation is still holding the interval and the pointer on disk is
    still the one the activation is about to supersede — so the two intervals
    genuinely overlap rather than merely being launched together.

    Only one outcome is serializable. The restore must act on the pointer the
    activation published (retained target ``b``), not on the one it read on the
    way in (retained target ``a``): the second is the interleaving an
    independent probe reproduced, and it loses a generation from the chain.
    """
    a = publish(source, control_root, "a")
    b = publish(source, control_root, "b")
    c = publish(source, control_root, "c")
    activate_generation(pointer=pointer, generation=a, apply=True)
    activate_generation(pointer=pointer, generation=b, apply=True)

    signals = tmp_path / "signals"
    signals.mkdir()
    ready, go, entered = (signals / name for name in ("ready", "go", "entered"))
    out = signals / "activate.json"
    activator = tmp_path / "activate_driver.py"
    restorer = tmp_path / "restore_driver.py"
    activator.write_text(ACTIVATE_DRIVER, encoding="utf-8")
    restorer.write_text(RESTORE_DRIVER, encoding="utf-8")

    environment = {**os.environ, "PYTHONPATH": str(WORKING_TREE)}
    activate_process = subprocess.Popen(
        [
            sys.executable,
            str(activator),
            str(WORKING_TREE),
            str(ready),
            str(go),
            str(out),
            str(pointer),
            str(c),
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        await_file(ready, other=activate_process)
        # The activation is inside its interval and has not written yet.
        assert pointer_body(pointer)["generation"] == relative(control_root, b)

        restore_process = subprocess.Popen(
            [
                sys.executable,
                str(restorer),
                str(WORKING_TREE),
                str(entered),
                str(pointer),
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            await_file(entered, other=restore_process)
            # The overlap, stated: the second process is running, the first is
            # still inside its interval, and the pointer is still the old one.
            assert activate_process.poll() is None
            assert pointer_body(pointer)["generation"] == relative(control_root, b)

            go.write_text("go\n", encoding="utf-8")
            activate_out, activate_err = activate_process.communicate(timeout=ARRIVES)
            restore_out, restore_err = restore_process.communicate(timeout=ARRIVES)
        finally:
            if restore_process.poll() is None:  # pragma: no cover - cleanup only
                restore_process.kill()
    finally:
        if activate_process.poll() is None:  # pragma: no cover - cleanup only
            activate_process.kill()

    assert activate_process.returncode == 0, (activate_out, activate_err)
    assert restore_process.returncode == 0, (restore_out, restore_err)

    activation = json.loads(out.read_text(encoding="utf-8"))
    restoration = json.loads(restore_out)
    assert activation["status"] == STATUS_CUTOVER_APPLIED
    assert activation["resolution"]["relative_generation"] == relative(control_root, c)
    assert restoration["status"] == STATUS_RESTORE_APPLIED

    # The decisive assertion: the restore returned to what the activation
    # published, and retained the activation's generation in turn. Had it
    # decided on the pointer it read on the way in, this would be ``a/current``
    # with ``b/current`` retained, and ``b`` would be out of the chain.
    final = pointer_body(pointer)
    assert final["generation"] == relative(control_root, b)
    assert final["previous_generation"] == relative(control_root, c)
    assert restoration["resolution"]["relative_generation"] == relative(control_root, b)
    assert resolve_generation_runtime(pointer=pointer).usable is True
    # The interval left nothing behind in the control root.
    assert not runtime.pointer_lock_path(pointer).exists()


# ---------------------------------------------------------------------------
# Activation against withdrawal, in both orders
#
# The second proven defect's race. A withdrawal that checked "is anything
# serving this?" and *then* renamed could be overtaken by an activation that
# checked "has this been withdrawn?" and then published a pointer — so the two
# coordinate through one guard on the generation, and each order has exactly
# one safe answer.
# ---------------------------------------------------------------------------

def test_a_withdrawal_inside_the_activation_window_leaves_no_dangling_pointer(
    source, control_root, pointer, monkeypatch
):
    """Withdrawal first: the activation refuses, and the pointer still works.

    The activation is held after its publication proof has passed and before it
    records its admission — the exact window in which the old preflight could be
    overtaken. The withdrawal runs to completion in that window, which it is
    entitled to do: nothing had been admitted yet. The activation must then
    notice that the generation it proved is no longer there and refuse, rather
    than publish a pointer naming a directory that has been renamed away.
    """
    serving = publish(source, control_root, "serving")
    target = publish(source, control_root, "target")
    assert activate_generation(
        pointer=pointer, generation=serving, apply=True
    )["status"] == STATUS_CUTOVER_APPLIED
    before = pointer_body(pointer)

    at_admission = threading.Event()
    may_admit = threading.Event()
    real_owning = runtime.owning_destination

    def owning(generation: Path):
        if threading.current_thread().name == ACTIVATOR and generation == target:
            at_admission.set()
            assert may_admit.wait(ARRIVES), "the activation was never released"
        return real_owning(generation)

    monkeypatch.setattr(runtime, "owning_destination", owning)

    activator, activation = run_thread(
        ACTIVATOR,
        lambda: activate_generation(pointer=pointer, generation=target, apply=True),
    )
    assert at_admission.wait(ARRIVES), "the activation never reached its admission"

    withdrawal = graph_migration.rollback_migration(
        destination=control_root / "target"
    )
    assert withdrawal["status"] == graph_migration.STATUS_ROLLED_BACK
    assert not target.exists()

    may_admit.set()
    activator.join(ARRIVES)
    assert not activator.is_alive()

    report = activation["report"]
    assert report["status"] == runtime.STATUS_CUTOVER_REFUSED
    assert report["exit_code"] == runtime.EXIT_BLOCKED
    assert any(
        finding.startswith(runtime.FINDING_GENERATION_MOVED)
        for finding in report["findings"]
    ), report["findings"]
    # No dangling pointer: the control plane still names what it was serving.
    assert pointer_body(pointer) == before
    resolved = resolve_generation_runtime(pointer=pointer)
    assert resolved.usable is True
    assert resolved.config.root == serving
    # And the withdrawal retained everything, as a withdrawal must.
    assert Path(withdrawal["generation"]["withdrawn_to"]).is_dir()


def test_the_admission_is_durable_before_the_pointer_naming_it_is_published(
    source, control_root, pointer, monkeypatch
):
    """Admission first: the ledger already stops a withdrawal at the write.

    The activation is held at its write — after its admission has been recorded
    and *after* the generation guard has been released, which is deliberate: a
    pointer names two generations in up to two destinations, so holding a guard
    across the write would mean holding two of them at once. What makes the
    early release safe is the one thing asserted here: the record is already
    durable at the moment the pointer has not yet been written, so a withdrawal
    arriving in exactly that window finds it, refuses, and moves nothing.

    The complementary case — a withdrawal that arrives *while* the record is
    being written — is
    :func:`test_a_withdrawal_waits_for_the_guard_while_an_admission_is_recorded`.
    """
    target = publish(source, control_root, "target")

    at_write = threading.Event()
    may_write = threading.Event()
    real_write = runtime._write_pointer

    def write(path: Path, payload):
        if threading.current_thread().name == ACTIVATOR:
            at_write.set()
            assert may_write.wait(ARRIVES), "the activation was never released"
        return real_write(path, payload)

    monkeypatch.setattr(runtime, "_write_pointer", write)

    activator, activation = run_thread(
        ACTIVATOR,
        lambda: activate_generation(pointer=pointer, generation=target, apply=True),
    )
    assert at_write.wait(ARRIVES)

    # The ordering invariant, stated while it is observable: the pointer does
    # not exist yet, and the generation it is about to name is already out of
    # the withdrawal's reach.
    assert not pointer.exists()
    withdrawal = graph_migration.rollback_migration(destination=control_root / "target")

    assert withdrawal["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert generation_module.FINDING_GENERATION_SERVED in withdrawal["findings"]
    assert withdrawal["generation"]["admitted"] is True
    assert target.is_dir()
    assert not (control_root / "target" / "rollback").exists()

    may_write.set()
    activator.join(ARRIVES)
    assert not activator.is_alive()

    assert activation["report"]["status"] == STATUS_CUTOVER_APPLIED
    assert target.is_dir()
    assert resolve_generation_runtime(pointer=pointer).config.root == target


def test_a_withdrawal_waits_for_the_guard_while_an_admission_is_recorded(
    source, control_root, pointer, monkeypatch
):
    """The guard itself: the record and the rename cannot interleave.

    The activation is held *inside* the generation guard, at the moment it
    writes its record. A withdrawal started there cannot check-and-rename past
    it — it produces no report at all while the guard is held — and once it is
    released the record is in the ledger and the only answer left is a refusal
    that moves nothing.
    """
    target = publish(source, control_root, "target")

    at_record = threading.Event()
    may_record = threading.Event()
    real_register = runtime.register_admission

    def register(destination: Path, generation: Path, digest: str, **kwargs):
        if threading.current_thread().name == ACTIVATOR:
            at_record.set()
            assert may_record.wait(ARRIVES), "the activation was never released"
        return real_register(destination, generation, digest, **kwargs)

    monkeypatch.setattr(runtime, "register_admission", register)
    before = tree_state(control_root)

    activator, activation = run_thread(
        ACTIVATOR,
        lambda: activate_generation(pointer=pointer, generation=target, apply=True),
    )
    assert at_record.wait(ARRIVES), "the activation never reached its record"

    roller, withdrawal = run_thread(
        ROLLER,
        lambda: graph_migration.rollback_migration(
            destination=control_root / "target"
        ),
    )
    # Supporting evidence: the withdrawal is waiting on the guard rather than
    # racing the rename. The decisive assertions are below.
    roller.join(BLOCKED)
    assert withdrawal == {}
    assert target.is_dir()
    assert tree_state(control_root) == before
    assert not generation_module.served_lock_path(target.parent).exists()

    may_record.set()
    activator.join(ARRIVES)
    roller.join(ARRIVES)
    assert not activator.is_alive() and not roller.is_alive()

    assert activation["report"]["status"] == STATUS_CUTOVER_APPLIED
    assert withdrawal["report"]["status"] == graph_migration.STATUS_ROLLBACK_REFUSED
    assert (
        generation_module.FINDING_GENERATION_SERVED
        in withdrawal["report"]["findings"]
    )
    assert withdrawal["report"]["generation"]["admitted"] is True
    assert target.is_dir()
    assert not (control_root / "target" / "rollback").exists()
    assert resolve_generation_runtime(pointer=pointer).config.root == target
    assert not generation_module.served_lock_path(target.parent).exists()


# ---------------------------------------------------------------------------
# The control lock itself, measured rather than assumed
#
# Both guards above rest on one primitive. In file mode it unlinks the lock
# file *before* it releases the ``flock`` on it. The reasoning about whether
# that window can produce two simultaneous holders is only reasoning; what
# follows measures it, under exactly the pattern that would expose it — many
# short critical sections in independent processes, so acquisitions and
# releases interleave continuously rather than once.
#
# What this establishes: mutual exclusion across real processes for the
# implementation, and that no lock file survives. What it does not: anything
# about a lock file an outsider
# deletes while it is held. Those are reported as limitations, not as results.
# ---------------------------------------------------------------------------

#: Takes the real control lock repeatedly and records each critical section as
#: one JSON line of ``[entered_ns, left_ns]``, on the system-wide realtime
#: clock so two processes' records are comparable.
CONTENTION_DRIVER = '''\
import json, sys, time
from pathlib import Path
from layered_memory_mcp.graph_migration_generation import control_lock

tree, lock, out, rounds, directory = sys.argv[1:6]
import layered_memory_mcp
assert Path(layered_memory_mcp.__file__).resolve().parents[1] == Path(tree)
sections = []
for _round in range(int(rounds)):
    with control_lock(Path(lock), directory=directory == "directory"):
        entered = time.time_ns()
        time.sleep(0.001)
        sections.append([entered, time.time_ns()])
Path(out).write_text(json.dumps(sections), encoding="utf-8")
'''

#: Enough processes and rounds that acquisition and release interleave
#: continuously; small enough to stay a deterministic unit test.
CONTENDERS = 4
ROUNDS = 15


@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_the_control_lock_excludes_real_processes_across_rapid_handovers(
    control_root, tmp_path, directory
):
    """No two critical sections overlap, and no lock file is left behind.

    The decisive assertion is on the recorded intervals: sorted by entry, each
    one must have finished before the next one began. Two holders at once —
    which is what an inode handed over badly would produce — show up as an
    overlap, not as an exception.
    """
    lock = control_root / ".contended.lock"
    driver = tmp_path / "contention_driver.py"
    driver.write_text(CONTENTION_DRIVER, encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(WORKING_TREE)}
    outputs = [tmp_path / f"sections-{index}.json" for index in range(CONTENDERS)]
    before = tree_state(control_root)

    processes = [
        subprocess.Popen(
            [
                sys.executable,
                str(driver),
                str(WORKING_TREE),
                str(lock),
                str(out),
                str(ROUNDS),
                "directory" if directory else "file",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for out in outputs
    ]
    try:
        results = [process.communicate(timeout=ARRIVES) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:  # pragma: no cover - cleanup only
                process.kill()

    assert [process.returncode for process in processes] == [0] * CONTENDERS, results
    sections = [
        tuple(section)
        for out in outputs
        for section in json.loads(out.read_text(encoding="utf-8"))
    ]
    assert len(sections) == CONTENDERS * ROUNDS
    ordered = sorted(sections)
    overlaps = [
        (earlier, later)
        for earlier, later in zip(ordered, ordered[1:])
        if earlier[1] > later[0]
    ]
    assert overlaps == [], (
        "control_lock allowed overlapping critical "
        f"sections: {overlaps[:3]}"
    )
    # Held only while it is held: the contract the preview tests also rest on.
    assert not lock.exists()
    if directory:
        assert tree_state(control_root) == before


DIRECTORY_HOLDER = '''\
import sys, time
from pathlib import Path
from layered_memory_mcp.graph_migration_generation import control_lock

lock, ready = map(Path, sys.argv[1:3])
with control_lock(lock, directory=True):
    ready.write_text("locked", encoding="utf-8")
    time.sleep(120)
'''


@pytest.mark.parametrize("guard", ["pointer", "generation"])
def test_directory_interval_times_out_then_recovers_after_holder_death(
    control_root, pointer, tmp_path, guard
):
    """A real process holds the unchanged tree; death releases its inode lock."""
    if guard == "pointer":
        lock = runtime.pointer_lock_path(pointer)
    else:
        destination = control_root / "destination"
        destination.mkdir()
        lock = generation_module.served_lock_path(destination)
    ready = tmp_path / "directory-ready"
    before = tree_state(control_root)
    holder = subprocess.Popen(
        [sys.executable, "-c", DIRECTORY_HOLDER, str(lock), str(ready)],
        env={**os.environ, "PYTHONPATH": str(WORKING_TREE)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        await_file(ready, other=holder)
        assert tree_state(control_root) == before
        with pytest.raises(generation_module.ControlLockError, match="^busy$"):
            with generation_module.control_lock(lock, directory=True, timeout=0.05):
                pytest.fail("entered an interval held by another process")
        assert tree_state(control_root) == before
    finally:
        holder.kill()
        holder.communicate(timeout=ARRIVES)

    with generation_module.control_lock(lock, directory=True, timeout=0.05):
        assert tree_state(control_root) == before
    report = restore_generation(pointer=pointer, apply=True)
    assert report["status"] == runtime.STATUS_RESTORE_NOTHING
    assert tree_state(control_root) == before


def test_directory_interval_rejects_reentry_for_a_second_pointer(control_root, pointer):
    """All pointers in one directory share an inode, including on one thread."""
    before = tree_state(control_root)
    with generation_module.control_lock(
        runtime.pointer_lock_path(pointer), directory=True
    ):
        report = restore_generation(pointer=control_root / "other.json", apply=True)
    assert report["status"] == runtime.STATUS_RESTORE_REFUSED
    assert f"{runtime.FINDING_POINTER_LOCK}:reentrant" in report["findings"]
    assert tree_state(control_root) == before


@pytest.mark.parametrize("failure", ["open", "flock", "unsupported"])
def test_directory_interval_errors_refuse_without_changing_the_pointer(
    source, control_root, pointer, monkeypatch, failure
):
    """An unavailable kernel lock never falls through to an unlocked cutover."""
    import fcntl

    first = publish(source, control_root, "first")
    second = publish(source, control_root, "second")
    assert activate_generation(pointer=pointer, generation=first, apply=True)["status"] == (
        STATUS_CUTOVER_APPLIED
    )
    before = tree_state(control_root)

    def unavailable(*args, **kwargs):
        raise OSError("injected lock failure")

    with monkeypatch.context() as patch:
        if failure == "open":
            patch.setattr(generation_module.os, "open", unavailable)
        elif failure == "flock":
            patch.setattr(fcntl, "flock", unavailable)
        else:
            patch.setitem(sys.modules, "fcntl", None)
        report = activate_generation(pointer=pointer, generation=second, apply=True)

    assert report["status"] == runtime.STATUS_CUTOVER_REFUSED
    reason = "unsupported" if failure == "unsupported" else "OSError"
    assert (
        f"{runtime.FINDING_POINTER_LOCK}:"
        f"{generation_module.FINDING_LOCK_PATH_REJECTED}:{reason}"
    ) in report["findings"]
    assert tree_state(control_root) == before
    assert activate_generation(pointer=pointer, generation=second, apply=True)["status"] == (
        STATUS_CUTOVER_APPLIED
    )


def test_two_concurrent_activations_serialize_into_one_recovery_chain(
    source, control_root, pointer, monkeypatch
):
    """Neither activation may compute its retained target from a dead snapshot.

    The same interval, with the second operation also a cutover: whichever runs
    second must retain what the first published, so the chain stays walkable.
    """
    a = publish(source, control_root, "a")
    b = publish(source, control_root, "b")
    c = publish(source, control_root, "c")
    activate_generation(pointer=pointer, generation=a, apply=True)

    at_write = threading.Event()
    may_write = threading.Event()
    real_write = runtime._write_pointer

    def write(path: Path, payload):
        if threading.current_thread().name == ACTIVATOR:
            at_write.set()
            assert may_write.wait(ARRIVES), "the activation was never released"
        return real_write(path, payload)

    monkeypatch.setattr(runtime, "_write_pointer", write)

    first, first_outcome = run_thread(
        ACTIVATOR,
        lambda: activate_generation(pointer=pointer, generation=b, apply=True),
    )
    assert at_write.wait(ARRIVES)
    second, second_outcome = run_thread(
        RESTORER,
        lambda: activate_generation(pointer=pointer, generation=c, apply=True),
    )
    assert pointer_body(pointer)["generation"] == relative(control_root, a)

    may_write.set()
    first.join(ARRIVES)
    second.join(ARRIVES)

    assert first_outcome.get("report", {}).get("status") == STATUS_CUTOVER_APPLIED
    assert second_outcome.get("report", {}).get("status") == STATUS_CUTOVER_APPLIED
    final = pointer_body(pointer)
    assert final["generation"] == relative(control_root, c)
    assert final["previous_generation"] == relative(control_root, b)
    assert final["previous_manifest_sha256"] == first_outcome["report"]["resolution"][
        "manifest_sha256"
    ]
