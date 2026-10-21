#!/usr/bin/env python3
"""Zelos EtherCAT Extension - CLI entry point.

Modes:

1. App mode (default): configuration comes from the Zelos App
2. Demo mode: a simulated segment, no hardware
3. CLI: `scan` lists interfaces or the slaves on one; `trace` runs one segment

Examples:
    uv run main.py                      # app mode
    uv run main.py demo                 # simulated EK1100 + EL1004 + 2x EL2004 + EL3004
    uv run main.py scan                 # network interfaces
    uv run main.py scan eth1            # slaves on eth1
    uv run main.py trace eth1 --cycle-time 5
"""

from __future__ import annotations

import logging
import time

import rich_click as click

from zelos_extension_ethercat import ACTION_PREFIX as _ACTION_PREFIX
from zelos_extension_ethercat.cli import app as app_mode

#: Re-exported so a packaging-time action inventory - which reads this entry
#: module - sees the same namespace the live registration uses. See the
#: definition in `zelos_extension_ethercat/__init__.py`.
ACTION_PREFIX = _ACTION_PREFIX

# UTC ISO 8601 with ms, matching the SDK's Rust tracing lines in the same log stream
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03dZ %(levelname)5s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


@click.group(invoke_without_command=True)
@click.option("--demo", is_flag=True, help="Run in demo mode with the simulated segment")
@click.pass_context
def cli(ctx: click.Context, demo: bool) -> None:
    """Zelos EtherCAT Extension - the agent as EtherCAT master on a dedicated NIC.

    Without a subcommand this starts in app mode using the configuration saved
    by the Zelos App. Use --demo or the 'demo' subcommand for a simulated
    segment, 'scan' to look at a NIC, and 'trace' for direct CLI access.
    """
    if ctx.invoked_subcommand is None:
        app_mode.run_app_mode(demo=demo)


@cli.command()
def demo() -> None:
    """Run the built-in demo: one connection `demo` on a simulated segment
    (EK1100, EL1004, 2x EL2004, EL3004) with outputs enabled. No hardware required."""
    app_mode.run_app_mode(demo=True)


@cli.command()
@click.argument("interface", required=False)
def scan(interface: str | None) -> None:
    """List network interfaces, or the slaves on INTERFACE (needs CAP_NET_RAW on Linux)."""
    from zelos_extension_ethercat import autoconfig

    if interface is None:
        found = autoconfig.interfaces()
        if not found:
            raise click.ClickException("no network interfaces found")
        for iface in found:
            click.echo(f"{iface['name']:<16} {iface['detail']}")
        return
    try:
        slaves = autoconfig.scan(interface)
    except RuntimeError as e:
        raise click.ClickException(f"scan of {interface} failed: {e}") from e
    if not slaves:
        raise click.ClickException(f"no EtherCAT slaves answered on {interface}")
    click.echo(f"{len(slaves)} slaves on {interface}")
    for s in slaves:
        click.echo(
            f"  {s['position']:>3}  {s['name']:<24} vendor {s['vendor_id']}  "
            f"product {s['product_code']}  rev {s['revision']}"
        )


@cli.command()
@click.argument("interface")
@click.option("--cycle-time", type=float, default=10.0, show_default=True, help="Cycle time in ms")
@click.option(
    "--enable-outputs", is_flag=True, help="Allow the write_output action to drive outputs"
)
@click.option("--name", default="", help="Connection name; defaults to the interface")
def trace(interface: str, cycle_time: float, enable_outputs: bool, name: str) -> None:
    """Run one segment from the command line: master on INTERFACE (or `demo`).

    \b
    Examples:
        uv run main.py trace eth1
        uv run main.py trace eth1 --cycle-time 5 --enable-outputs
    """
    # One connection through the same resolution as app mode.
    app_mode.run_config(
        {
            "connections": [
                {
                    "name": name,
                    "interface": interface,
                    "cycle_time": cycle_time,
                    "enable_outputs": enable_outputs,
                }
            ],
            "advanced": {},
        }
    )


if __name__ == "__main__":
    cli()
