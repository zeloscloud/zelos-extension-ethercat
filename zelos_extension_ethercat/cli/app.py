"""App mode: run from the Zelos App's saved config, or demo mode.

Startup problems are one logger.error line plus exit 1; tracebacks are for bugs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from typing import Any

import zelos_sdk
from zelos_sdk.extensions import ConfigValidationError, load_config
from zelos_sdk.hooks.logging import TraceLoggingHandler

from zelos_extension_ethercat import ACTION_PREFIX, actions
from zelos_extension_ethercat.constants import (
    DEFAULT_PREFIX,
    LOG_SOURCE_NAME,
    RESERVED_CONNECTION_NAMES,
    name_error,
    trace_layout,
)
from zelos_extension_ethercat.master import DEMO_INTERFACE, Segment, SegmentError

logger = logging.getLogger(__name__)

#: Defaults of the schema's `advanced` object: settings shared by every connection.
ADVANCED_DEFAULTS: dict[str, Any] = {
    "prefix": DEFAULT_PREFIX,
    "log_level": "INFO",
    "diagnostics_interval": 1.0,
}
#: Defaults of one `connections[]` entry.
CONNECTION_DEFAULTS: dict[str, Any] = {
    "name": "",
    "interface": "",
    "cycle_time": 10.0,
    "enable_outputs": False,
    "restore_outputs_after_recovery": False,
}

#: The demo's one connection.
DEMO_NAME = "demo"

#: Seconds a stop waits for the connections to finish before giving up.
SHUTDOWN_TIMEOUT = 3.0
#: Seconds the forced exit gives the trace sources to flush.
FORCED_FLUSH_TIMEOUT = 0.5


def resolve_advanced(config: dict[str, Any]) -> dict[str, Any]:
    """`advanced` over its defaults; an absent `prefix` is the default, an empty one clears it."""
    return {**ADVANCED_DEFAULTS, **(config.get("advanced") or {})}


def _exit_on(error: str | None) -> None:
    """Exit with a one-line reason when ``error`` is set."""
    if error:
        logger.error(error)
        sys.exit(1)


def apply_log_level(level_name: str) -> None:
    """Apply the configured log level, falling back to INFO on a bad value."""
    level = getattr(logging, level_name.upper(), None)
    if not isinstance(level, int):
        logger.warning("Invalid log level '%s', using INFO", level_name)
        level = logging.INFO
    logging.getLogger().setLevel(level)


def resolve_connections(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Each `connections[]` entry over its defaults, named (the interface when no name),
    legal trace names, unique interfaces and names; or exit."""
    entries = config.get("connections") or []
    if not entries:
        _exit_on("No connections configured: add one under Connections")
    resolved: list[dict[str, Any]] = []
    for i, entry in enumerate(entries):
        connection = {**CONNECTION_DEFAULTS, **entry}
        interface = str(connection["interface"] or "").strip()
        if not interface:
            _exit_on(f"connections[{i}] has no interface")
        name = str(connection["name"] or "").strip() or zelos_sdk.sanitize_name(
            interface, kind="source"
        )
        _exit_on(name_error(name, "connection Name", RESERVED_CONNECTION_NAMES))
        if name in [c["name"] for c in resolved]:
            _exit_on(f"Duplicate connection name '{name}'; set a distinct name on one")
        if interface in [c["interface"] for c in resolved]:
            _exit_on(
                f"Interface '{interface}' is used by two connections; one master per interface"
            )
        cycle = float(connection["cycle_time"])
        # 5 cycles inside the 500 ms output watchdog and stall guard.
        if not 0.1 <= cycle <= 100:
            _exit_on(f"connections[{i}] cycle_time {cycle:g} ms is outside 0.1..100")
        connection.update(name=name, interface=interface, cycle_time=cycle)
        resolved.append(connection)
    return resolved


def init_sdk(prefix: str) -> zelos_sdk.TraceSource | None:
    """Init the SDK and route logs to the trace; return the shared prefix source.

    Same layout rule as the data: with a prefix, one source named after it
    carries every connection plus the logs (`<prefix>/log`); cleared, each
    connection owns a source and the logs get LOG_SOURCE_NAME. Register actions
    BEFORE calling this: init advertises them to the agent.
    """
    # First, so init() reuses it instead of creating a source named after the namespace.
    source = zelos_sdk.init_global_source(prefix or LOG_SOURCE_NAME)
    zelos_sdk.init(name=ACTION_PREFIX, actions=True)
    logging.getLogger().addHandler(TraceLoggingHandler(source))
    return source if prefix else None


def build_segment(
    connection: dict[str, Any],
    prefix: str,
    shared: zelos_sdk.TraceSource | None,
    advanced: dict[str, Any],
) -> Segment:
    """One connection on the prefix's layout: the shared source, or its own."""
    source_name, event_prefix = trace_layout(prefix, connection["name"])
    source = shared if shared is not None else zelos_sdk.TraceSource(source_name)
    return Segment(
        name=connection["name"],
        interface=connection["interface"],
        cycle_time_ms=float(connection["cycle_time"]),
        enable_outputs=bool(connection["enable_outputs"]),
        source=source,
        event_prefix=event_prefix,
        diagnostics_interval=float(advanced["diagnostics_interval"]),
        restore_outputs=bool(connection["restore_outputs_after_recovery"]),
    )


async def run_until_stopped(connections: dict[str, Segment]) -> None:
    """Run every connection until SIGTERM/SIGINT or one of them ends.

    A connection that fails to come up ends the run with exit 1. Past
    SHUTDOWN_TIMEOUT the process exits anyway: `os._exit`, since a cycle thread stuck
    in a GIL-holding SOEM call would otherwise hold the interpreter's exit.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = {asyncio.create_task(c.run(stop), name=name): name for name, c in connections.items()}
    stopping = asyncio.create_task(stop.wait())
    await asyncio.wait([*tasks, stopping], return_when=asyncio.FIRST_COMPLETED)
    logger.info("Shutting down...")
    stop.set()
    _, pending = await asyncio.wait(tasks, timeout=SHUTDOWN_TIMEOUT)
    for task in pending:
        task.cancel()
    if pending:
        logger.error(f"{len(pending)} connection(s) did not stop in {SHUTDOWN_TIMEOUT:g}s; exiting")
        flushed = _flush_traces(connections)
        logger.warning(f"Forced exit; trace flush {'completed' if flushed else 'timed out'}")
        logging.shutdown()
        os._exit(1)
    failed = [name for task, name in tasks.items() if task.exception() is not None]
    for task, name in tasks.items():
        error = task.exception()
        if error is not None and not isinstance(error, SegmentError):
            logger.error("[%s] %s: %s", name, type(error).__name__, error)
    if failed:
        sys.exit(1)


def _flush_traces(connections: dict[str, Segment]) -> bool:
    """Flush every trace source, waiting at most FORCED_FLUSH_TIMEOUT; True if it finished.

    Runs in a daemon thread: a flush has no timeout and must not hold up the exit.
    """
    sources = {id(c.source): c.source for c in connections.values()}
    sources[0] = zelos_sdk.init_global_source()  # logs; already exists, so name is ignored
    done = threading.Event()

    def flush() -> None:
        for source in sources.values():
            source.flush()
        done.set()

    threading.Thread(target=flush, daemon=True).start()
    return done.wait(FORCED_FLUSH_TIMEOUT)


def run_config(config: dict[str, Any], demo: bool = False) -> None:
    """Resolve an app-shaped config (connections[], advanced) and serve it; `demo`
    (the CLI's `demo` / `--demo`) replaces the connections with the simulated segment."""
    advanced = resolve_advanced(config)
    apply_log_level(advanced["log_level"])
    prefix = str(advanced["prefix"] or "").strip()
    _exit_on(name_error(prefix, "Prefix"))

    if demo:
        logger.info("Demo mode: simulated EK1100 + EL1004 + 2x EL2004 + EL3004 segment")
        resolved = [
            {
                **CONNECTION_DEFAULTS,
                "name": DEMO_NAME,
                "interface": DEMO_INTERFACE,
                "enable_outputs": True,
            }
        ]
    else:
        resolved = resolve_connections(config)

    # Actions register before init(): registered later they may never be advertised.
    actions.register_actions(zelos_sdk.actions_registry)
    shared = init_sdk(prefix)
    logger.info(f"Trace prefix: {prefix}" if prefix else "Trace prefix cleared")

    connections = {c["name"]: build_segment(c, prefix, shared, advanced) for c in resolved}
    actions.set_connections(connections)
    asyncio.run(run_until_stopped(connections))


def run_app_mode(demo: bool = False) -> None:
    """Run the extension with configuration from the Zelos App.

    Args:
        demo: If True, run the simulated segment as the only connection
    """
    try:
        config = load_config()
    except ConfigValidationError as e:
        logger.error("Invalid configuration: %s", "; ".join(m.strip(" •") for m in e.errors))
        sys.exit(1)
    run_config(config, demo)
