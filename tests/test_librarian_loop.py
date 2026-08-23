"""The built-in librarian's always-on scheduled loop (app.librarian_engine.
run_librarian_loop) and its wiring into app/main.py's lifespan (ADR-0010
phase 2, independent review advisory E).

No real sleeps anywhere in this file: `run_librarian` itself is always
mocked, and every test that needs the loop to run more than one cycle
patches `asyncio.sleep` (or, for the lifespan test, patches the background
coroutines themselves) with a deterministic fake -- either instant, or one
that raises a private, non-`Exception` sentinel after N calls so the
infinite `while True` loop can be broken and asserted on without ever
actually waiting.
"""

import asyncio

import pytest

import app.librarian_engine as librarian_engine_module
import app.main as main_module


class _FakeSettings:
    def __init__(self, *, librarian_enabled: bool, librarian_interval_secs: int = 1) -> None:
        self.librarian_enabled = librarian_enabled
        self.librarian_interval_secs = librarian_interval_secs


class _StopLoop(BaseException):
    """Deliberately NOT an Exception subclass -- it must escape
    run_librarian_loop's `except Exception` untouched, the same way a real
    asyncio.CancelledError must (see test_loop_does_not_swallow_cancellation
    below). Used here purely to break the infinite `while True` loop after a
    scripted number of cycles so a test can assert on exactly how many ran.
    """


# --- (a) LIBRARIAN_ENABLED=false -> returns immediately, run_librarian never called ---


async def test_loop_disabled_returns_immediately_without_calling_run_librarian(monkeypatch):
    monkeypatch.setattr(librarian_engine_module, "get_settings", lambda: _FakeSettings(librarian_enabled=False))
    calls = []

    async def fake_run_librarian(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(librarian_engine_module, "run_librarian", fake_run_librarian)

    async def fake_sleep(seconds):
        raise AssertionError("sleep must never be reached when the loop is disabled")

    monkeypatch.setattr(librarian_engine_module.asyncio, "sleep", fake_sleep)

    await librarian_engine_module.run_librarian_loop()  # must return, not hang

    assert calls == []


# --- (b) enabled -> calls run_librarian each cycle ---


async def test_loop_enabled_calls_run_librarian_once_per_cycle(monkeypatch):
    monkeypatch.setattr(librarian_engine_module, "get_settings", lambda: _FakeSettings(librarian_enabled=True, librarian_interval_secs=0))
    calls = []

    async def fake_run_librarian(*args, **kwargs):
        calls.append(1)

    monkeypatch.setattr(librarian_engine_module, "run_librarian", fake_run_librarian)

    sleep_calls = {"n": 0}

    async def fake_sleep(seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 3:
            raise _StopLoop()

    monkeypatch.setattr(librarian_engine_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await librarian_engine_module.run_librarian_loop()

    assert len(calls) == 3  # one run_librarian call per cycle, three cycles before the test stopped it


# --- (c) a run raising Exception is logged and the loop CONTINUES ---


async def test_loop_survives_run_librarian_raising_and_continues_to_next_cycle(monkeypatch, caplog):
    monkeypatch.setattr(librarian_engine_module, "get_settings", lambda: _FakeSettings(librarian_enabled=True, librarian_interval_secs=0))
    calls = []

    async def flaky_run_librarian(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated cycle failure")

    monkeypatch.setattr(librarian_engine_module, "run_librarian", flaky_run_librarian)

    sleep_calls = {"n": 0}

    async def fake_sleep(seconds):
        sleep_calls["n"] += 1
        if sleep_calls["n"] >= 2:
            raise _StopLoop()

    monkeypatch.setattr(librarian_engine_module.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await librarian_engine_module.run_librarian_loop()

    # the first cycle's RuntimeError was caught (not propagated) and the
    # loop reached a second cycle -- it did not die
    assert len(calls) == 2


# --- (d) CancelledError is NOT swallowed ---


async def test_loop_cancellation_is_not_swallowed(monkeypatch):
    """Mirrors app/room_sweeper.py's run_sweeper docstring: `except
    Exception` must never catch asyncio.CancelledError, so app/main.py's
    lifespan shutdown (which calls `.cancel()` on the task) actually stops
    the loop. Exercised the same way the real lifespan cancels it: as a real
    asyncio.Task, cancelled mid-flight while `run_librarian` is "running".
    """
    monkeypatch.setattr(librarian_engine_module, "get_settings", lambda: _FakeSettings(librarian_enabled=True, librarian_interval_secs=0))
    started = asyncio.Event()

    async def slow_run_librarian(*args, **kwargs):
        started.set()
        await asyncio.sleep(1000)  # a cancellable await point, standing in for real work

    monkeypatch.setattr(librarian_engine_module, "run_librarian", slow_run_librarian)

    task = asyncio.create_task(librarian_engine_module.run_librarian_loop())
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --- lifespan wiring (app/main.py) ---


async def test_lifespan_starts_and_cleanly_cancels_both_background_tasks(monkeypatch):
    """No shipped test exercised app/main.py's lifespan wiring at all.
    Mocks both background coroutines (the room sweeper and the librarian
    loop) so this test needs no real DB work or real sleeps -- it only
    proves the shape: both tasks actually start, and exiting the lifespan
    context cancels both cleanly (the `async with` block completing without
    hanging or raising IS the proof -- app/main.py wraps each task's await
    in `contextlib.suppress(asyncio.CancelledError)`).
    """
    sweeper_started = asyncio.Event()
    librarian_started = asyncio.Event()

    async def fake_run_sweeper():
        sweeper_started.set()
        await asyncio.sleep(1000)

    async def fake_run_librarian_loop():
        librarian_started.set()
        await asyncio.sleep(1000)

    monkeypatch.setattr(main_module, "run_sweeper", fake_run_sweeper)
    monkeypatch.setattr(main_module, "run_librarian_loop", fake_run_librarian_loop)

    async def _enter_wait_and_exit_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            await sweeper_started.wait()
            await librarian_started.wait()
        # Reaching here means exiting the `async with` (the lifespan's
        # `finally:` block, which cancels both tasks and awaits each under
        # `contextlib.suppress(asyncio.CancelledError)`) completed cleanly.

    # The whole enter-wait-exit cycle is wrapped in one timeout: if either
    # task's cancellation were swallowed or mishandled on the way out, the
    # exit would hang forever instead of completing -- this turns that into
    # a clean test failure (TimeoutError) rather than hanging the suite.
    await asyncio.wait_for(_enter_wait_and_exit_lifespan(), timeout=5)
