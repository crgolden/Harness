"""How a hook tells the master session (Tools/MasterSession) from every other: Start-MasterSession.ps1 sets
MASTER_SESSION=1 in the claude process it launches on the master's machine, and every hook inherits that environment,
which no tool call can change. The Stop gate and the protected-path gate both read it here."""

import os

MASTER_HOST = "WIN-VL9HOKP0RAM"
MASTER_SESSION_VARIABLE = "MASTER_SESSION"


def runs_as(variable, environ=None):
    """True in a process whose launcher set variable to 1 on the master's machine."""
    environ = os.environ if environ is None else environ
    return environ.get(variable) == "1" and str(environ.get("COMPUTERNAME", "")).upper() == MASTER_HOST
