"""Generated ArduPilot DataFlash logs with known, injected faults.

Used by the validation benchmark (``flightlens validate``), the demo
(``flightlens demo``) and the tests. ``dfwriter`` packs records with
:mod:`struct`, independently of the reader, so reading back what it writes is a
genuine cross-check.
"""
