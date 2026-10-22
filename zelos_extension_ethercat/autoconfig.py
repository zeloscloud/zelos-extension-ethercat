"""The NICs this machine has, for the config form, and a slave scan of one named NIC.

A scan opens a raw socket and runs SOEM `config_init`, which needs CAP_NET_RAW and
broadcasts INIT to every slave on the NIC, dropping a segment another master runs there
out of OP: only on an interface the user names, never on one of this extension's
connections. It runs in a child process killed at a deadline, as the CAN extension
scans gs_usb: a stuck pcap open must not sit in the live extension.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
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


def propose(form: list[dict[str, Any]]) -> dict[str, Any]:
    """The auto-configure result, from the interface list alone: the form's connections
    as entered, or the demo when it has none. Never scans: a scan resets whatever segment
    hangs off the NIC, so only an interface the user names is scanned."""
    found = interfaces()
    listed = ", ".join(f"{i['name']} ({i['detail']})" if i["detail"] else i["name"] for i in found)
    message = (
        f"Interfaces: {listed or 'none found'}. Set a connection's Interface to the NIC the "
        "segment hangs off; Scan Interface lists the slaves on one. On Linux the extension "
        "needs CAP_NET_RAW and CAP_NET_ADMIN."
    )
    if not form:
        form = [{"name": "demo", "interface": "demo"}]
        message = "Added a demo connection. " + message
    return {"status": "success", "config": {"connections": form}, "message": message}


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
