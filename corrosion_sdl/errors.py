"""Errors.

The hierarchy has exactly two branches, because the runner only ever needs to
make one decision when something goes wrong:

    Recoverable  ->  this sample failed, the station is still fine.
                     Skip it and carry on with the next one.

    Fatal        ->  the station's state is no longer trusted.
                     Stop, go to the safe state, wait for a person.

Every concrete error below declares which branch it belongs to by what it
inherits from. That means `runner.py` catches two types and knows exactly what
to do -- it never has to inspect an error message to decide.
"""

from __future__ import annotations


class SdlError(Exception):
    """Base class for every error raised by this package."""


class Recoverable(SdlError):
    """This sample failed. The station is fine. Skip and continue."""


class Fatal(SdlError):
    """The station's state is untrusted. Stop and wait for an operator."""


# --------------------------------------------------------------------------- #
# Concrete errors
# --------------------------------------------------------------------------- #

class ArmError(Fatal):
    """A robot arm failed to reach a pose, or stopped unexpectedly.

    Fatal: if an arm did not arrive where it was told to go, we no longer know
    where it is, and the next move could drive it into a filled cell.
    """


class ClampError(Fatal):
    """The cell clamp did not close, did not open, or its switch disagrees.

    Fatal: filling an unsealed cell floods the baseplate. If we cannot confirm
    the seal, we must not proceed.
    """


class PumpError(Recoverable):
    """A fill, drain, or rinse did not complete as commanded.

    Recoverable: a failed fluid operation costs this sample, but the station is
    still in a known state and the next sample can proceed.
    """


class ReservoirError(Fatal):
    """A reservoir is empty, or the waste bottle is full.

    Fatal: continuing would pump air through the cell or overflow the waste
    bottle. Both ruin every remaining sample, so stopping is the cheaper
    outcome.
    """


class InstrumentError(Recoverable):
    """A measurement failed to complete.

    Recoverable: this sample produced no usable data, but nothing is broken.
    """


class StabilityNotReached(InstrumentError):
    """OCP hit its maximum wait without settling.

    Whether this ends the run is a policy decision held in
    `config.ProtocolConfig.abort_on_ocp_timeout`, not here.
    """


class RelayError(Fatal):
    """The cell selector could not connect a cell, or read back the wrong one.

    Fatal by deliberate choice. A relay that silently connects the wrong cell
    does not crash -- it produces a complete, plausible dataset labelled with
    the wrong sample. That is worse than stopping, because nobody notices.
    """


class PoseError(Fatal):
    """A pose is missing from the registry, or is outside joint limits.

    Fatal: we will not guess where something is.
    """


class SequenceError(Fatal):
    """An operation was requested out of order.

    For example: filling a cell that is not closed, running EIS before OCP, or
    measuring a sample already destroyed by PDP. These are programming errors
    in the workflow, and they are caught at the point of the mistake.
    """


class ConfigError(Fatal):
    """The configuration file is missing, malformed, or contains an unknown key.

    Fatal: an unknown key usually means a mis-typed setting name, which would
    otherwise load silently and leave the station running on a default the
    operator believed they had changed.
    """


class StorageError(Fatal):
    """Measurement data could not be written to disk.

    Fatal: an unattended overnight run that cannot save results is destroying
    samples for nothing.
    """
