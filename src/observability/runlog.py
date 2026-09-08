"""Structured per-run evidence under evidence/<run_id>/.

    run_meta.json          goal/mode/params, start/end, summary
    steps/NNN.json         one record per step: what, why, locator used, result, timing
    steps/NNN.png          screenshot after the step
    final_state.json       ReplayResult / DiscoveryResult
    failure/               a11y snapshot + screenshot at the point of failure
    handoff/               intervention request, operator notes, before/after diffs

Everything written passes through the Redactor.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from safety.redactor import Redactor


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class RunLog:
    def __init__(
        self,
        mode: str,
        root: str | Path = "evidence",
        run_id: str | None = None,
        redactor: Redactor | None = None,
        label: str | None = None,
    ):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        self.run_id = run_id or f"{stamp}_{mode}_{(label or uuid.uuid4().hex[:6])}"
        self.dir = Path(root) / self.run_id
        (self.dir / "steps").mkdir(parents=True, exist_ok=True)
        self.redactor = redactor or Redactor()
        self.mode = mode
        self.started = time.time()
        self.events: list[dict[str, Any]] = []
        self.step_count = 0
        self.meta: dict[str, Any] = {"run_id": self.run_id, "mode": mode, "started_at": now_iso()}
        self._flush_meta()

    # -- writing -------------------------------------------------------------
    def _write(self, rel: str, data: Any) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.redactor.obj(data), indent=2, default=str))
        return p

    def _flush_meta(self) -> None:
        self._write("run_meta.json", self.meta)

    def set_meta(self, **kv: Any) -> None:
        self.meta.update(kv)
        self._flush_meta()

    def step(self, index: int | None, record: dict[str, Any], screenshot: bytes | None = None) -> None:
        self.step_count += 1
        index = index if index is not None else self.step_count
        record = {"index": index, "timestamp": now_iso(), **record}
        self.events.append(self.redactor.obj(record))
        self._write(f"steps/{index:03d}.json", record)
        if screenshot:
            (self.dir / "steps" / f"{index:03d}.png").write_bytes(screenshot)

    def event(self, kind: str, **data: Any) -> None:
        rec = {"kind": kind, "timestamp": now_iso(), **data}
        self.events.append(self.redactor.obj(rec))
        self._write("events.json", self.events)

    def failure_snapshot(self, a11y: str, screenshot: bytes | None, detail: dict[str, Any]) -> None:
        (self.dir / "failure").mkdir(exist_ok=True)
        (self.dir / "failure" / "a11y_snapshot.txt").write_text(self.redactor.text(a11y))
        if screenshot:
            (self.dir / "failure" / "screenshot.png").write_bytes(screenshot)
        self._write("failure/detail.json", detail)

    def artifact_file(self, rel: str, data: Any) -> Path:
        return self._write(rel, data)

    def binary(self, rel: str, data: bytes) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def finish(self, final_state: Any, **summary: Any) -> Path:
        self.meta.update({"finished_at": now_iso(), "duration_s": round(time.time() - self.started, 2), **summary})
        self._flush_meta()
        payload = final_state.model_dump(mode="json") if hasattr(final_state, "model_dump") else final_state
        return self._write("final_state.json", payload)
