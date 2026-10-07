"""Independent, reversible arm births, gesture changes and retirement."""
from dataclasses import dataclass

EXTENSION_STEPS = 12
ARM_SECONDS = .24


def gesture(pose):
    return ("waiting" if pose in {"needs_you", "stopped"} else "done" if pose == "done"
            else "droop" if pose in {"stale", "error"} else "working")


@dataclass
class Arm:
    row: tuple
    shown: str
    wanted: str
    extension: float
    at: float
    retiring: bool = False


class ArmPlayer:
    def __init__(self):
        self.arms = {}

    def sample(self, rows, now):
        current = {(row[0], row[1]): row for row in rows}
        for key, arm in list(self.arms.items()):
            delta = min(1, max(0, now-arm.at)/ARM_SECONDS)
            arm.at = now
            if arm.retiring or arm.shown != arm.wanted:
                arm.extension = max(0, arm.extension-delta)
                if arm.extension == 0:
                    if arm.retiring and key not in current:
                        del self.arms[key]
                        continue
                    arm.shown = arm.wanted
            else:
                arm.extension = min(1, arm.extension+delta)
            arm.retiring = key not in current
            if key in current:
                arm.row = current[key]
                arm.wanted = gesture(arm.row[3])
        for key, row in current.items():
            if key not in self.arms:
                state = gesture(row[3])
                self.arms[key] = Arm(row, state, state, 0, now)
        rows, states = [], []
        for key, arm in sorted(self.arms.items()):
            rows.append(arm.row)
            # Cubic easing has zero speed at each endpoint, including the root.
            t = arm.extension
            eased = t*t*(3-2*t)
            states.append((arm.row[0], arm.row[1], arm.shown, round(eased*EXTENSION_STEPS)))
        return tuple(rows), tuple(states)

    @property
    def retiring(self):
        return any(arm.retiring for arm in self.arms.values())
