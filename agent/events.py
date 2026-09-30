"""Progress reporting shared by the pipeline, CLI and UI.

Stages call ``reporter.step(stage, message)``; the UI/CLI decide how to show it.
Every event is also kept in memory so it can be saved with the run.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional

STAGES = ["capture", "analyze", "generate", "validate", "preview", "modify"]


@dataclass
class Event:
    stage: str
    message: str
    level: str = "info"  # info | success | warning | error
    ts: float = field(default_factory=time.time)
    data: Optional[dict] = None


class Reporter:
    def __init__(self, sink: Optional[Callable[[Event], None]] = None):
        self.sink = sink
        self.events: list[Event] = []

    def step(self, stage: str, message: str, level: str = "info", data: Optional[dict] = None) -> None:
        ev = Event(stage=stage, message=message, level=level, data=data)
        self.events.append(ev)
        if self.sink:
            try:
                self.sink(ev)
            except Exception:  # UI problems must never break the pipeline
                pass

    def success(self, stage: str, message: str, **kw) -> None:
        self.step(stage, message, "success", **kw)

    def warn(self, stage: str, message: str, **kw) -> None:
        self.step(stage, message, "warning", **kw)

    def error(self, stage: str, message: str, **kw) -> None:
        self.step(stage, message, "error", **kw)

    def as_dicts(self) -> list[dict]:
        return [e.__dict__ for e in self.events]


def console_sink(ev: Event) -> None:
    icon = {"info": "·", "success": "✓", "warning": "!", "error": "✗"}.get(ev.level, "·")
    print(f"[{ev.stage:>8}] {icon} {ev.message}", flush=True)
