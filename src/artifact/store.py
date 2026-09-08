"""File-backed capability store: one JSON file per artifact under capabilities/.

Human-reviewable and diffable in git, which matters more here than query
power. The interface is small enough that a DB-backed store (per-tenant,
with approval workflow) is a drop-in replacement.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .schema import CapabilityArtifact


class CapabilityStore:
    def __init__(self, root: str | Path = "capabilities"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, name: str, version: int) -> Path:
        return self.root / f"{name}.v{version}.json"

    def save(self, artifact: CapabilityArtifact, path: str | Path | None = None) -> Path:
        p = Path(path) if path else self.path_for(artifact.name, artifact.version)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(artifact.to_json())
        return p

    def load(self, path: str | Path) -> CapabilityArtifact:
        return CapabilityArtifact.from_json(Path(path).read_text())

    def get(self, name: str, version: int | None = None) -> CapabilityArtifact:
        candidates = sorted(self.root.glob(f"{name}.v*.json"))
        if version is not None:
            candidates = [self.path_for(name, version)]
        if not candidates or not candidates[-1].exists():
            raise FileNotFoundError(f"no capability {name!r} v{version or 'latest'} in {self.root}")
        return self.load(candidates[-1])

    def all(self) -> Iterator[CapabilityArtifact]:
        for p in sorted(self.root.glob("*.json")):
            yield self.load(p)

    def catalog(self) -> list[dict]:
        """Agent-facing catalog: what can be invoked, with what, returning what."""
        return [{**a.tool_schema(), "version": a.version, "status": a.status} for a in self.all()]
