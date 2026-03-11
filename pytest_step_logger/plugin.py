"""pytest_step_logger/plugin.py

Pytest plugin that renders test steps as an interactive Rich live tree.

Execution modes
---------------
Sequential (no xdist)
  A rich.live.Live display spans the full test lifecycle (setup → call →
  teardown).  Fixture names appear under "setup" / "teardown" branches;
  test steps appear in-between, all in real time.

xdist worker process
  No live display.  Steps and fixtures are tracked in _StepRecord objects
  which are JSON-serialised into report.user_properties (teardown report)
  and shipped to the controller via xdist's normal report serialisation.

xdist controller process
  A single persistent Live display covers the whole session:
  • A spinner panel shows every currently-running test with animated dots
    and a live elapsed-time counter (updates 4×/s).
  • As each test finishes its full tree (setup + steps + teardown) is
    printed above the spinner panel — without flickering.

Step tracking
-------------
1. Allure mode  — @allure.step present.
   _discover() pre-builds grey nodes from bytecode.
   allure_commons hookimpl (start_step / stop_step) drives node colours.

2. Plain-function mode  — no @allure.step.
   _discover_plain() pre-builds grey nodes for callables in the test file.
   sys.settrace _Tracer intercepts call/line/exception/return events.
   The 'line' event after 'exception' resets the failure flag (caught exc).

Fixture tracking
----------------
pytest_fixture_setup (hookwrapper) records each fixture setup with timing.
fixturedef.finish is wrapped once per FixtureDef to record teardown timing.
Internal pytest / allure / xdist fixtures are excluded.
Only function-scoped fixture teardowns are added to the teardown branch
(broader scopes are torn down at module/session boundaries and don't belong
to a single test's teardown tree).

Soft-assertion support (pytest-check)
  Before every step the failure count is snapshotted; any growth after the
  step marks it red even without a propagating exception.
"""

import dis
import json
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import pytest
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

_console = Console(file=sys.__stdout__, highlight=False)

# ── Global state ──────────────────────────────────────────────────────────────
_config: Optional[pytest.Config] = None
_xdist_worker: bool = False
_xdist_ctrl: Optional["_XdistController"] = None


# ── Soft-failure helper ───────────────────────────────────────────────────────

def _soft_fail_count() -> int:
    try:
        from pytest_check import check_log
        return len(check_log.get_failures())
    except Exception:
        return 0


# ── Status label helpers ──────────────────────────────────────────────────────

def _lbl_pending(title: str) -> Text:
    return Text.assemble(("○ ", "dim white"), (title, "dim white"))

def _lbl_running(title: str) -> Text:
    return Text.assemble(("▶ ", "bold yellow"), (title, "yellow"))

def _lbl_passed(title: str, elapsed: float) -> Text:
    return Text.assemble(("✔ ", "bold green"), (title, "green"), (f"  {elapsed:.2f}s", "dim"))

def _lbl_failed(title: str, elapsed: float) -> Text:
    return Text.assemble(("✘ ", "bold red"), (title, "red"), (f"  {elapsed:.2f}s", "dim red"))

def _lbl_root(name: str, status: str) -> Text:
    if status == "passed":
        return Text.assemble(("✔ ", "bold green"), (name, "bold green"))
    if status == "failed":
        return Text.assemble(("✘ ", "bold red"), (name, "bold red"))
    return Text.assemble(("⊘ ", "bold yellow"), (name, "bold yellow"))

def _lbl_section(title: str) -> Text:
    return Text(title, style="dim italic")


# ── Step records (JSON-safe, survive xdist wire protocol) ─────────────────────

@dataclass
class _StepRecord:
    title: str
    elapsed: float = 0.0
    status: str = "pending"           # pending | passed | failed
    children: list["_StepRecord"] = field(default_factory=list)


def _records_to_json(records: list[_StepRecord]) -> list[dict]:
    return [
        {"title": r.title, "elapsed": r.elapsed, "status": r.status,
         "children": _records_to_json(r.children)}
        for r in records
    ]


def _records_from_json(data: list[dict]) -> list[_StepRecord]:
    return [
        _StepRecord(title=d["title"], elapsed=d["elapsed"], status=d["status"],
                    children=_records_from_json(d.get("children", [])))
        for d in data
    ]


def _build_tree(name: str, status: str,
                setup: list[_StepRecord],
                steps: list[_StepRecord],
                teardown: list[_StepRecord]) -> Tree:
    """Build a Rich Tree from serialised records (used by xdist controller)."""
    tree = Tree(_lbl_root(name, status))
    if setup:
        branch = tree.add(_lbl_section("setup"))
        for r in setup:
            _add_record(branch, r)
    for r in steps:
        _add_record(tree, r)
    if teardown:
        branch = tree.add(_lbl_section("teardown"))
        for r in teardown:
            _add_record(branch, r)
    return tree


def _add_record(parent: Tree, r: _StepRecord) -> None:
    label = (_lbl_passed(r.title, r.elapsed) if r.status == "passed"
             else _lbl_failed(r.title, r.elapsed) if r.status == "failed"
             else _lbl_pending(r.title))
    node = parent.add(label)
    for child in r.children:
        _add_record(node, child)


_PROP_KEY = "step_logger_records"


# ── xdist controller display ──────────────────────────────────────────────────

class _RunningPanel:
    """
    Dynamic Rich renderable shown inside the controller's Live display.

    __rich__ is called on every Live refresh cycle, so elapsed times update
    smoothly (4×/s) and Spinner frames advance with the system clock —
    no background thread or manual state machine needed.
    """

    def __init__(self, running: dict[str, float]) -> None:
        self._running = running  # shared reference — mutated by controller

    def __rich__(self) -> Table | Text:
        if not self._running:
            return Text("")
        tbl = Table.grid(padding=(0, 1))
        for nodeid, start in self._running.items():
            elapsed = time.monotonic() - start
            name = nodeid.split("::")[-1]
            tbl.add_row(
                Spinner("dots", style="cyan"),
                Text(name, style="bold blue"),
                Text(f" {elapsed:.1f}s", style="dim"),
            )
        return tbl


class _XdistController:
    """
    Manages the single persistent Live display for xdist parallel runs.

    Outcomes are tracked per-phase so the tree can be displayed with the
    correct root status when the teardown report arrives (which carries the
    serialised payload).
    """

    def __init__(self) -> None:
        self._running: dict[str, float] = {}   # nodeid → start_time
        self._outcomes: dict[str, str] = {}    # nodeid → "passed"/"failed"/"skipped"
        self._panel = _RunningPanel(self._running)
        self.live = Live(self._panel, console=_console, refresh_per_second=4)
        self.live.start(refresh=True)

    def test_started(self, nodeid: str) -> None:
        self._running[nodeid] = time.monotonic()

    def on_report(self, report: pytest.TestReport) -> None:
        if report.when == "setup" and report.failed:
            self._outcomes[report.nodeid] = "failed"
        elif report.when == "call":
            self._outcomes[report.nodeid] = (
                "passed" if report.passed else "failed" if report.failed else "skipped"
            )
        elif report.when == "teardown":
            self._running.pop(report.nodeid, None)
            status = self._outcomes.pop(
                report.nodeid,
                "skipped" if report.skipped else "failed",
            )
            payload = next(
                (v for k, v in report.user_properties if k == _PROP_KEY), None
            )
            name = report.nodeid.split("::")[-1]
            if payload:
                try:
                    data = json.loads(payload)
                    tree = _build_tree(
                        name, status,
                        _records_from_json(data.get("setup", [])),
                        _records_from_json(data.get("steps", [])),
                        _records_from_json(data.get("teardown", [])),
                    )
                except Exception:
                    tree = Tree(_lbl_root(name, status))
            else:
                tree = Tree(_lbl_root(name, status))
            self.live.console.print()
            self.live.console.print(tree)
            self.live.console.print()

    def stop(self) -> None:
        self.live.stop()


# ── Per-test state ────────────────────────────────────────────────────────────

@dataclass
class _Step:
    uuid: str
    title: str
    started: float
    node: Tree
    record: _StepRecord
    soft_fails_before: int = field(default_factory=_soft_fail_count)


class _Context:
    def __init__(self, name: str) -> None:
        self.name = name
        self.tree = Tree(Text(f"⟳ {name}", style="bold blue"))
        self.setup_branch: Optional[Tree] = None
        self.teardown_branch: Optional[Tree] = None
        self.stack: list[_Step] = []
        self.pending: dict[str, deque[Tree]] = {}
        self.live: Optional[Live] = None
        self.records: list[_StepRecord] = []          # test-body steps
        self.record_stack: list[_StepRecord] = []
        self.setup_records: list[_StepRecord] = []    # fixture setups
        self.teardown_records: list[_StepRecord] = [] # fixture teardowns
        self.setup_fixture_names: set[str] = set()    # fixtures set up this test
        self.td_segment_start: float = 0.0            # reset per fixture in post_finalizer


_ctx: Optional[_Context] = None
_active: bool = False


# ── Fixture filter ────────────────────────────────────────────────────────────

_INTERNAL_MODULES = frozenset({
    "_pytest", "pytest", "allure", "allure_pytest", "allure_commons",
    "pytest_check", "xdist", "pluggy",
})


def _is_internal_fixture(fixturedef) -> bool:
    module: str = getattr(getattr(fixturedef, "func", None), "__module__", "") or ""
    root = module.split(".")[0]
    return root in _INTERNAL_MODULES or fixturedef.argname.startswith("_")


# ── Step discovery ────────────────────────────────────────────────────────────

def _allure_title(func) -> Optional[str]:
    if not callable(func) or not hasattr(func, "__wrapped__"):
        return None
    for cell in getattr(func, "__closure__", None) or []:
        try:
            val = cell.cell_contents
        except ValueError:
            continue
        t = getattr(val, "title", None)
        if isinstance(t, str):
            return t
    return None


def _discover(func, parent: Tree, pending: dict, seen: set) -> None:
    fid = id(func)
    if fid in seen:
        return
    seen.add(fid)
    try:
        code = getattr(func, "__wrapped__", func).__code__
        globs = getattr(func, "__globals__", {})
        instrs = list(dis.get_instructions(code))
    except Exception:
        return
    for instr in instrs:
        if instr.opname != "LOAD_GLOBAL":
            continue
        name = instr.argval
        if not isinstance(name, str):
            continue
        obj = globs.get(name)
        if not callable(obj):
            continue
        title = _allure_title(obj)
        if title is None:
            continue
        node = parent.add(_lbl_pending(title))
        pending.setdefault(title, deque()).append(node)
        _discover(obj, node, pending, seen)


def _discover_plain(func, parent: Tree, pending: dict, seen: set, test_file: str) -> None:
    fid = id(func)
    if fid in seen:
        return
    seen.add(fid)
    try:
        code = getattr(func, "__wrapped__", func).__code__
        globs = getattr(func, "__globals__", {})
        instrs = list(dis.get_instructions(code))
    except Exception:
        return
    for instr in instrs:
        if instr.opname != "LOAD_GLOBAL":
            continue
        name = instr.argval
        if not isinstance(name, str) or name.startswith("_"):
            continue
        obj = globs.get(name)
        if not callable(obj) or _allure_title(obj) is not None:
            continue
        if getattr(getattr(obj, "__code__", None), "co_filename", None) != test_file:
            continue
        node = parent.add(_lbl_pending(name))
        pending.setdefault(name, deque()).append(node)
        _discover_plain(obj, node, pending, seen, test_file)


# ── Tracer for plain (non-allure) functions ───────────────────────────────────

class _Tracer:
    """
    sys.settrace tracker for plain function calls.

    call_stack entries: [func_name, node, t0, had_exc, soft_before, record]

    Caught-exception fix: 'line' after 'exception' in the same frame resets
    had_exc (the except-clause ran, so the step continued normally).

    Soft-assertion detection: snapshot before vs. count after.
    """

    def __init__(self, ctx: _Context, test_file: str, test_name: str) -> None:
        self._ctx = ctx
        self._test_file = test_file
        self._test_name = test_name
        self._call_stack: list[list] = []  # [name, node, t0, had_exc, soft_before, record]

    def __call__(self, frame, event, _arg):
        if frame.f_code.co_filename != self._test_file:
            return None

        func_name = frame.f_code.co_name
        if func_name == self._test_name or func_name.startswith("<"):
            return self

        if event == "call":
            obj = frame.f_globals.get(func_name)
            if obj is not None and _allure_title(obj) is not None:
                return None  # allure hooks handle this

            q = self._ctx.pending.get(func_name)
            if q:
                node = q.popleft()
                node.label = _lbl_running(func_name)
            else:
                parent_node = self._call_stack[-1][1] if self._call_stack else self._ctx.tree
                node = parent_node.add(_lbl_running(func_name))

            record = _StepRecord(title=func_name)
            parent_rec = self._call_stack[-1][5] if self._call_stack else None
            if parent_rec is not None:
                parent_rec.children.append(record)
            else:
                self._ctx.records.append(record)

            self._call_stack.append(
                [func_name, node, time.monotonic(), False, _soft_fail_count(), record]
            )
            if self._ctx.live:
                self._ctx.live.refresh()

        elif event == "exception":
            if self._call_stack and self._call_stack[-1][0] == func_name:
                self._call_stack[-1][3] = True

        elif event == "line":
            # Exception was caught — a line executed after the except clause
            if self._call_stack and self._call_stack[-1][0] == func_name:
                if self._call_stack[-1][3]:
                    self._call_stack[-1][3] = False

        elif event == "return":
            if self._call_stack and self._call_stack[-1][0] == func_name:
                name, node, started, had_exc, soft_before, record = self._call_stack.pop()
                elapsed = time.monotonic() - started
                failed = had_exc or (_soft_fail_count() > soft_before)
                record.elapsed = elapsed
                record.status = "failed" if failed else "passed"
                node.label = _lbl_failed(name, elapsed) if failed else _lbl_passed(name, elapsed)
                if self._ctx.live:
                    self._ctx.live.refresh()

        return self


# ── Allure listener ───────────────────────────────────────────────────────────

def _register_allure_listener() -> None:
    try:
        import allure_commons
    except ImportError:
        return

    class _Listener:
        @allure_commons.hookimpl
        def start_step(self, uuid: str, title: str, _params=None) -> None:
            if not _active or _ctx is None:
                return
            title = title or "step"
            q = _ctx.pending.get(title)
            if q:
                node = q.popleft()
            else:
                parent_node = _ctx.stack[-1].node if _ctx.stack else _ctx.tree
                node = parent_node.add(_lbl_running(title))
            node.label = _lbl_running(title)

            record = _StepRecord(title=title)
            if _ctx.record_stack:
                _ctx.record_stack[-1].children.append(record)
            else:
                _ctx.records.append(record)
            _ctx.record_stack.append(record)

            _ctx.stack.append(
                _Step(uuid=uuid, title=title, started=time.monotonic(), node=node, record=record)
            )
            if _ctx.live:
                _ctx.live.refresh()

        @allure_commons.hookimpl
        def stop_step(self, uuid: str, exc_type, _exc_val=None, _exc_tb=None) -> None:
            if not _active or _ctx is None:
                return
            info = next((s for s in reversed(_ctx.stack) if s.uuid == uuid), None)
            if info is None:
                return
            _ctx.stack.remove(info)
            if _ctx.record_stack and _ctx.record_stack[-1] is info.record:
                _ctx.record_stack.pop()

            elapsed = time.monotonic() - info.started
            soft_failed = _soft_fail_count() > info.soft_fails_before
            failed = exc_type is not None or soft_failed
            info.record.elapsed = elapsed
            info.record.status = "failed" if failed else "passed"
            info.node.label = _lbl_failed(info.title, elapsed) if failed else _lbl_passed(info.title, elapsed)
            if _ctx.live:
                _ctx.live.refresh()

    try:
        allure_commons.plugin_manager.register(_Listener(), name="pytest_step_logger")
    except Exception:
        pass


_register_allure_listener()


# ── Pytest hooks ──────────────────────────────────────────────────────────────

def pytest_configure(config: pytest.Config) -> None:
    global _config, _xdist_worker
    _config = config
    _xdist_worker = hasattr(config, "workerinput")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--step-log",
        action="store_true",
        default=False,
        help="Render test steps as an interactive Rich live tree.",
    )


def pytest_sessionstart(session: pytest.Session) -> None:
    """Create the controller-side live display when running with xdist."""
    global _xdist_ctrl
    if _xdist_worker:
        return
    config = session.config
    if not config.getoption("--step-log", default=False):
        return
    # dsession is registered by xdist on the controller only when -n > 0
    if config.pluginmanager.has_plugin("dsession"):
        _xdist_ctrl = _XdistController()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    global _xdist_ctrl
    if _xdist_ctrl is not None:
        _xdist_ctrl.stop()
        _xdist_ctrl = None


def pytest_runtest_logstart(nodeid: str, location: tuple) -> None:
    """Fires on the controller when a worker picks up a test."""
    if _xdist_ctrl is not None:
        _xdist_ctrl.test_started(nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item):
    """
    Create the per-test context and start the live display.
    pytest_fixture_setup fires inside this wrapper for each fixture.
    """
    global _ctx

    if not item.config.getoption("--step-log", default=False):
        yield
        return

    if _xdist_ctrl is not None:
        yield  # controller has no per-test context
        return

    _ctx = _Context(item.name)

    if not _xdist_worker:
        live = Live(_ctx.tree, console=_console, refresh_per_second=10)
        live.start(refresh=True)
        _ctx.live = live

    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_fixture_setup(fixturedef, request):
    """Track fixture setup timing and patch finish() for teardown tracking."""
    if _ctx is None or _xdist_ctrl is not None:
        yield
        return

    if _is_internal_fixture(fixturedef):
        yield
        return

    name = fixturedef.argname

    # Lazily add "setup" branch on first fixture
    if _ctx.setup_branch is None:
        _ctx.setup_branch = _ctx.tree.add(_lbl_section("setup"))

    node = _ctx.setup_branch.add(_lbl_running(name))
    record = _StepRecord(title=name)
    _ctx.setup_records.append(record)
    t0 = time.monotonic()

    if _ctx.live:
        _ctx.live.refresh()

    outcome = yield

    elapsed = time.monotonic() - t0
    try:
        outcome.get_result()
        failed = False
    except Exception:
        failed = True

    record.elapsed = elapsed
    record.status = "failed" if failed else "passed"
    node.label = _lbl_failed(name, elapsed) if failed else _lbl_passed(name, elapsed)

    if not failed:
        # Remember this fixture so pytest_fixture_post_finalizer can match it
        _ctx.setup_fixture_names.add(name)

    if _ctx.live:
        _ctx.live.refresh()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item):
    """Record teardown-phase start time so per-fixture elapsed times are accurate."""
    if _ctx is not None and item.config.getoption("--step-log", default=False):
        _ctx.td_segment_start = time.monotonic()
    yield


def pytest_fixture_post_finalizer(fixturedef, request) -> None:
    """
    Fires after each fixture's finish() completes.  Used to record teardown
    timing without patching fixturedef.finish (which is captured by
    functools.partial before pytest_fixture_setup even runs).

    Only function-scoped yield fixtures are shown: broader scopes tear down
    outside the per-test boundary, and non-yield fixtures have no teardown code.
    """
    import inspect

    if _ctx is None or _xdist_ctrl is not None:
        return
    if _is_internal_fixture(fixturedef):
        return
    if fixturedef.scope != "function":
        return
    if not inspect.isgeneratorfunction(fixturedef.func):
        return  # no teardown code
    if fixturedef.argname not in _ctx.setup_fixture_names:
        return

    # Discard now so duplicate finish() calls (pytest clears cache twice) are ignored
    _ctx.setup_fixture_names.discard(fixturedef.argname)

    elapsed = time.monotonic() - _ctx.td_segment_start
    _ctx.td_segment_start = time.monotonic()  # next fixture starts now

    if _ctx.teardown_branch is None:
        _ctx.teardown_branch = _ctx.tree.add(_lbl_section("teardown"))

    td_record = _StepRecord(title=fixturedef.argname, elapsed=elapsed, status="passed")
    _ctx.teardown_records.append(td_record)
    _ctx.teardown_branch.add(_lbl_passed(fixturedef.argname, elapsed))

    if _ctx.live:
        _ctx.live.refresh()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item):
    """Track test body execution; allure/tracer hooks fire here."""
    global _active

    if not item.config.getoption("--step-log", default=False):
        yield
        return

    if _ctx is None:
        yield
        return

    _active = True
    func = getattr(item, "function", None)
    tracer = None

    if func is not None:
        _discover(func, _ctx.tree, _ctx.pending, seen=set())
        if not _ctx.pending:
            test_file = func.__code__.co_filename
            _discover_plain(func, _ctx.tree, _ctx.pending, seen=set(), test_file=test_file)
            tracer = _Tracer(_ctx, test_file, func.__code__.co_name)

    prev_trace = sys.gettrace()
    if tracer is not None:
        sys.settrace(tracer)

    yield  # ← test body runs; allure hooks / tracer fire here

    sys.settrace(prev_trace)
    _active = False


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call):
    outcome = yield
    # Workers serialise ALL records (setup + steps + teardown) in the
    # teardown report, which is always the last report for a test.
    if not _xdist_worker or call.when != "teardown" or _ctx is None:
        return
    if not item.config.getoption("--step-log", default=False):
        return
    try:
        report = outcome.get_result()
        payload = {
            "setup":    _records_to_json(_ctx.setup_records),
            "steps":    _records_to_json(_ctx.records),
            "teardown": _records_to_json(_ctx.teardown_records),
        }
        report.user_properties.append((_PROP_KEY, json.dumps(payload)))
    except Exception:
        pass  # never break the test run over display errors


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    global _ctx, _active

    # ── xdist controller ──────────────────────────────────────────────────────
    if _xdist_ctrl is not None:
        _xdist_ctrl.on_report(report)
        return

    if _ctx is None:
        return

    # ── sequential mode / xdist worker ───────────────────────────────────────
    if report.when == "call":
        # Update root label immediately so the user sees pass/fail while
        # teardown fixtures are still running.
        status = "passed" if report.passed else "failed" if report.failed else "skipped"
        _ctx.tree.label = _lbl_root(_ctx.name, status)
        if _ctx.live:
            _ctx.live.refresh()

    elif report.when == "setup" and (report.failed or report.skipped):
        status = "failed" if report.failed else "skipped"
        _ctx.tree.label = _lbl_root(_ctx.name, status)
        if _ctx.live:
            _ctx.live.refresh()

    elif report.when == "teardown":
        # All phases done — freeze the display and clean up.
        if _ctx.live:
            _ctx.live.stop()
        _ctx = None
        _active = False
