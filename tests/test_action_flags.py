"""Pins each action's flags, so a new action or a changed flag is a deliberate choice."""

from __future__ import annotations

import inspect

from zelos_extension_ethercat import actions


def test_every_action_has_its_flags():
    actual = {
        name: (obj._action.read_only, obj._action.hidden)
        for name, obj in inspect.getmembers(actions, inspect.isfunction)
        if not name.startswith("_") and hasattr(obj, "_action")
    }
    assert actual == {
        "get_status": (True, False),
        "list_slaves": (True, False),
        "list_pdos": (True, False),
        "read_sdo": (True, False),
        "write_sdo": (False, False),
        "browse_od": (False, False),
        "write_output": (False, False),
        "clear_outputs": (False, False),
        "request_state": (False, False),
        "list_interfaces": (True, True),
        "auto_config": (False, True),
    }
