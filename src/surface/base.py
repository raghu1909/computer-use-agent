"""The surface seam.

Everything above this module (discovery loop, recorder, replay engine, handoff)
talks to a Surface. Everything below it knows how to see and touch one kind of
screen. A desktop surface (AT-SPI / UIA / pywinauto) implements the same
protocol: perceive() returns an accessibility tree plus a list of addressable
elements with candidate locators; act() performs one primitive; resolve()
turns a Locator into a live handle. The artifact never references Playwright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from artifact.schema import ActionType, Condition, Locator, LocatorStrategy


@dataclass
class ElementInfo:
    ref: str  # short handle for the LLM: e12
    frame: str | None
    tag: str
    role: str
    name: str  # accessible name (label, aria-label, value, adjacent label cell)
    text: str  # visible text (trimmed)
    kind: str  # "control" | "text"
    attrs: dict[str, str] = field(default_factory=dict)
    xpath: str = ""
    css: str | None = None
    label_adjacent: str | None = None  # label cell text when this is a value cell
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)

    def candidate_locators(self) -> list[Locator]:
        """Ordered fallback chain: semantics first, structure last."""
        chain: list[Locator] = []
        f = self.frame
        if self.kind == "control":
            if self.role and self.name and self.attrs.get("name_source") == "a11y":
                chain.append(
                    Locator(
                        strategy=LocatorStrategy.ROLE,
                        value=self.role,
                        name=self.name,
                        frame=f,
                        rationale="accessible role+name; survives markup and styling changes",
                    )
                )
            if self.name and self.attrs.get("name_source") == "adjacent":
                chain.append(
                    Locator(
                        strategy=LocatorStrategy.LABEL_ADJACENT,
                        value=self.name,
                        frame=f,
                        rationale="control located relative to its label cell (legacy table form, "
                        "no <label>); layout-agnostic within the row",
                    )
                )
            if self.attrs.get("aria-label") or self.attrs.get("label_for"):
                chain.append(
                    Locator(
                        strategy=LocatorStrategy.LABEL,
                        value=self.attrs.get("aria-label") or self.attrs["label_for"],
                        frame=f,
                        rationale="explicit label association",
                    )
                )
            if self.attrs.get("placeholder"):
                chain.append(Locator(strategy=LocatorStrategy.PLACEHOLDER, value=self.attrs["placeholder"], frame=f))
            if self.tag in ("a", "button") and self.text:
                chain.append(
                    Locator(
                        strategy=LocatorStrategy.TEXT,
                        value=self.text,
                        frame=f,
                        rationale="link/button text is operator-facing and rarely changes",
                    )
                )
            if self.attrs.get("name"):
                chain.append(
                    Locator(
                        strategy=LocatorStrategy.CSS,
                        value=f'{self.tag}[name="{self.attrs["name"]}"]',
                        frame=f,
                        rationale="form field name is part of the server contract",
                    )
                )
        else:
            if self.label_adjacent:
                chain.append(
                    Locator(
                        strategy=LocatorStrategy.LABEL_ADJACENT,
                        value=self.label_adjacent,
                        frame=f,
                        rationale="value cell located relative to its label cell; layout-agnostic",
                    )
                )
            elif self.text:
                chain.append(Locator(strategy=LocatorStrategy.TEXT, value=self.text, frame=f))
        if self.xpath:
            chain.append(
                Locator(
                    strategy=LocatorStrategy.XPATH,
                    value=self.xpath,
                    frame=f,
                    rationale="structural fallback; brittle, last resort",
                )
            )
        return chain


@dataclass
class Perception:
    url: str
    title: str
    frames: list[dict[str, Any]]  # {name, url, a11y}
    elements: list[ElementInfo]
    screenshot: bytes
    fingerprint: str  # hash of the a11y trees — used for stuck/drift detection

    def element(self, ref: str) -> ElementInfo | None:
        return next((e for e in self.elements if e.ref == ref), None)

    def a11y_text(self) -> str:
        parts = []
        for f in self.frames:
            hdr = f"### frame {f['name'] or '(main)'} — {f['url']}"
            parts.append(f"{hdr}\n{f['a11y']}")
        return "\n\n".join(parts)


class ResolveError(Exception):
    def __init__(self, locators: list[Locator], attempts: list[str]):
        self.locators = locators
        self.attempts = attempts
        super().__init__("no locator resolved:\n  " + "\n  ".join(attempts))


class Surface(Protocol):
    def perceive(self) -> Perception: ...
    def resolve(self, locators: list[Locator], timeout_ms: int) -> tuple[Any, Locator, list[str]]: ...
    def act(self, action: ActionType, handle: Any, value: str | None, timeout_ms: int) -> str | None: ...
    def navigate(self, url: str, timeout_ms: int) -> None: ...
    def check(self, cond: Condition) -> tuple[bool, str]: ...
    def wait_for(self, cond: Condition) -> tuple[bool, str]: ...
    def screenshot(self) -> bytes: ...
    def current_url(self) -> str: ...
    def a11y_dump(self) -> str: ...
    def drain_dialogs(self) -> list[str]: ...  # unexpected modal dialogs auto-dismissed since the last call
    def close(self) -> None: ...
