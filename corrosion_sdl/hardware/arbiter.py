"""The arbiter: only one thing may happen on the station at a time.

Both arms move, the pumps vibrate and draw current, the clamp motor runs, and
the potentiostat measures microvolts. Every one of those except the last
generates mechanical and electrical noise, and that noise corrupts an OCP or
EIS trace.

So the rule is simple and absolute:

    Exactly one Activity at a time. Everything else waits.

The rule is enforced structurally rather than by convention. Every method on
`Station` acquires the arbiter before touching hardware, so there is no path to
an actuator that skips the check. A caller cannot forget the rule, because
obeying it was never the caller's job.

WHY NOT RUN THE ARMS IN PARALLEL
--------------------------------
It would look faster and would not be. OCP alone runs between ten minutes and an
hour; a full OCP/EIS/PDP sequence is several hours. Overlapping two arm moves
saves a few seconds against that. In exchange it buys workspace collision
checking, motion planning, and the hardest class of bug in robotics -- one that
appears intermittently, at night, with an arm holding a coupon.

If profiling ever shows arm motion is a real bottleneck, the right change is to
allow concurrency between *disjoint workspace zones*, not to remove the arbiter.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

from ..errors import SequenceError
from ..types import Activity, utc_now


class Arbiter:
    """Grants exclusive use of the station to one activity at a time.

    Acquisition is non-blocking and fails fast. A request that arrives while
    something else holds the station raises immediately rather than queueing --
    a pump that starts the instant a measurement ends is still a bug, and a
    queued one would be nearly impossible to find in the data afterwards.

    The arbiter is re-entrant for the *same* activity, so a routine that calls a
    helper doing the same kind of work does not deadlock against itself.

    Example:
        >>> arb = Arbiter()
        >>> with arb.hold(Activity.MEASUREMENT):
        ...     pass   # every motor on the station is guaranteed idle here
    """

    def __init__(self) -> None:
        self._mutex = threading.RLock()
        self._holder: Activity | None = None
        self._depth = 0
        self._since = None

    # -- state ------------------------------------------------------------- #

    @property
    def holder(self) -> Activity | None:
        """Return the activity currently holding the station, or None."""
        with self._mutex:
            return self._holder

    @property
    def is_held(self) -> bool:
        """Return whether anything currently holds the station."""
        return self.holder is not None

    def held_for_s(self) -> float:
        """Return how long the current holder has held it, or 0.0 if free."""
        with self._mutex:
            if self._since is None:
                return 0.0
            return (utc_now() - self._since).total_seconds()

    # -- acquisition ------------------------------------------------------- #

    @contextmanager
    def hold(self, activity: Activity) -> Iterator[None]:
        """Take exclusive use of the station for `activity`.

        Args:
            activity: What is about to happen.

        Raises:
            SequenceError: If a different activity already holds the station.
        """
        with self._mutex:
            if self._holder is not None and self._holder is not activity:
                raise SequenceError(
                    f"{activity.value} was requested while {self._holder.value} "
                    f"has held the station for {self.held_for_s():.1f} s. "
                    "Only one activity may run at a time -- motion, pumps, and "
                    "relays all inject noise that corrupts OCP and EIS."
                )
            self._holder = activity
            self._depth += 1
            if self._depth == 1:
                self._since = utc_now()
        try:
            yield
        finally:
            with self._mutex:
                self._depth -= 1
                if self._depth == 0:
                    self._holder = None
                    self._since = None

    def assert_idle(self) -> None:
        """Raise unless the station is completely idle.

        Used before starting a measurement as a second, independent check that
        nothing is moving.

        Raises:
            SequenceError: If anything holds the station.
        """
        with self._mutex:
            if self._holder is not None:
                raise SequenceError(
                    f"the station is not idle: {self._holder.value} is active"
                )
