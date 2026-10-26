"""The NICs this machine has, for the config form, and a slave scan of named NICs.

A scan opens a raw socket and runs SOEM `config_init`, which needs CAP_NET_RAW and
broadcasts INIT to every slave on the NIC, dropping a segment another master runs there
out of OP: only on an interface the user names (the form's, or the CLI's), never on one
of this extension's running connections. It runs in a child process killed at a
deadline: a stuck pcap open must not sit in the live extension.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

#: Hard deadline on one interface's scan.
SCAN_SECONDS = 5.0
_SYS_CLASS_NET = Path("/sys/class/net")
#: Never an EtherCAT NIC: no link partner can be a slave.
_SKIP_PREFIXES = (
    "lo",
    "docker",
    "br-",
    "veth",
    "virbr",
    "tailscale",
    "wg",
    "tun",
    "tap",
    "vcan",
    "can",
)


def interfaces() -> list[dict[str, str]]:
    """Candidate NICs as `{name, detail}`: pysoem's adapter list, minus the virtual and
    loopback ones; on Linux `detail` carries the link state and driver from sysfs."""
    try:
        import pysoem

        adapters = pysoem.find_adapters()
    except Exception:
        return []
    found = []
    for adapter in adapters:
        name = adapter.name
        if name.startswith(_SKIP_PREFIXES):
            continue
        notes = []
        if sys.platform == "linux" and (_SYS_CLASS_NET / name).is_dir():
            if (_SYS_CLASS_NET / name / "type").read_text().strip() != "1":  # ARPHRD_ETHER
                continue
            with contextlib.suppress(OSError):
                notes.append((_SYS_CLASS_NET / name / "operstate").read_text().strip())
            driver = _SYS_CLASS_NET / name / "device" / "driver"
            if driver.exists():
                notes.append(driver.resolve().name)
        elif adapter.desc:
            desc = (
                adapter.desc.decode(errors="replace")
                if isinstance(adapter.desc, bytes)
                else str(adapter.desc)
            )
            notes.append(desc)
        found.append({"name": name, "detail": ", ".join(n for n in notes if n and n != "unknown")})
    return found


def scan(interface: str, timeout: float = SCAN_SECONDS) -> list[dict[str, Any]]:
    """The slaves on `interface`, scanned in a child process.

    :raises RuntimeError: the scan failed or did not answer in time; the message is one line
    """
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        [sys.executable, "-P", __file__, interface],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=flags,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=0.5)
        raise RuntimeError(f"no answer within {timeout:g} s") from None
    if proc.returncode:
        lines = err.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"exit code {proc.returncode}")
    return json.loads(out)


def propose(form: list[dict[str, Any]], owners: dict[str, str]) -> dict[str, Any]:
    """The auto-configure result: each interface the form names scanned (`owners`, an
    interface a running connection holds, skipped), the connections kept as entered; the
    demo when the form has none. Only named interfaces: a scan resets whatever segment
    hangs off the NIC."""
    from zelos_extension_ethercat.master import DEMO_INTERFACE

    if not form:
        return {
            "status": "success",
            "config": {"connections": [{"name": "demo", "interface": DEMO_INTERFACE}]},
            "message": f"Added a demo connection. {_interfaces_note()}",
        }
    named = [str(c.get("interface") or "").strip() for c in form]
    chosen = list(dict.fromkeys(i for i in named if i and i != DEMO_INTERFACE and i not in owners))
    held = list(dict.fromkeys(i for i in named if i in owners))
    unset = "" in named
    if not chosen and not held:
        return {"status": "success", "config": {"connections": form}, "message": _interfaces_note()}
    # In parallel: each scan may take its full deadline, inside the action's 30 s.
    with ThreadPoolExecutor(max(len(chosen), 1)) as pool:
        results = dict(zip(chosen, pool.map(_try_scan, chosen), strict=True))
    found = [(i, r) for i, r in results.items() if isinstance(r, list) and r]
    lines = [
        f"{i}: {len(r)} slave{'s' * (len(r) > 1)} ({', '.join(s['name'] for s in r)})"
        for i, r in found
    ]
    lines += [f"{i}: no slaves" for i, r in results.items() if r == []]
    lines += [f"{i}: scan failed: {r}" for i, r in results.items() if isinstance(r, str)]
    lines += [f"{i}: in use by connection '{owners[i]}', not scanned" for i in held]
    message = "; ".join(lines) + "."
    if unset:
        message += " Choose an Interface for each connection without one."
    if found:
        message += (
            " Scanned slaves stay in INIT until a master starts. Review, then save and start."
        )
        return {"status": "success", "config": {"connections": form}, "message": message}
    return {"status": "error", "message": message}


def _try_scan(interface: str) -> list[dict[str, Any]] | str:
    """`scan`, its failure as the one-line reason."""
    try:
        return scan(interface)
    except RuntimeError as e:
        return str(e)


def _interfaces_note() -> str:
    found = interfaces()
    listed = ", ".join(f"{i['name']} ({i['detail']})" if i["detail"] else i["name"] for i in found)
    return (
        f"Interfaces: {listed or 'none found'}. Pick the one the segment hangs off in a "
        "connection's Interface dropdown, then Auto-configure lists its slaves. On Linux the "
        "extension needs CAP_NET_RAW and CAP_NET_ADMIN."
    )


def _scan_main() -> None:
    """Child process: open, config_init, print the slaves as JSON, close."""
    import pysoem

    master = pysoem.Master()
    master.open(sys.argv[1])
    try:
        count = master.config_init()
        slaves = [
            {
                "position": pos,
                "name": s.name,
                "vendor_id": f"0x{s.man:08x}",
                "product_code": f"0x{s.id:08x}",
                "revision": f"0x{s.rev:08x}",
            }
            for pos, s in enumerate(master.slaves, 1)
        ]
        print(json.dumps(slaves if count > 0 else []))
    finally:
        master.close()


if __name__ == "__main__":
    _scan_main()
