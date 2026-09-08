"""Playwright (sync) implementation of the Surface protocol.

Perception = per-frame ARIA snapshot (what a screen reader sees) + an enumerated
list of addressable elements with several candidate locators each. We prefer
the accessibility layer over the DOM because it is the one representation that
also exists for native desktop apps, and because legacy markup (tables, <font>,
framesets) is far noisier than its accessibility projection.
"""

from __future__ import annotations

import hashlib
import re
import time

from playwright.sync_api import Browser, BrowserContext, Frame, Page, Playwright, sync_playwright
from playwright.sync_api import Locator as PWLocator
from playwright.sync_api import TimeoutError as PWTimeout

from artifact.schema import ActionType, Condition, ConditionKind, Locator, LocatorStrategy

from .base import ElementInfo, Perception, ResolveError

ENUMERATE_JS = r"""
(maxItems) => {
  const out = [];
  const seen = new Set();
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const xpathOf = el => {
    const parts = [];
    for (; el && el.nodeType === 1; el = el.parentNode) {
      let i = 1; for (let s = el.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === el.tagName) i++;
      parts.unshift(el.tagName.toLowerCase() + '[' + i + ']');
    }
    return '/' + parts.join('/');
  };
  const implicitRole = el => {
    const t = el.tagName.toLowerCase(), ty = (el.getAttribute('type') || '').toLowerCase();
    if (t === 'a' && el.hasAttribute('href')) return 'link';
    if (t === 'button' || (t === 'input' && ['button','submit','reset','image'].includes(ty))) return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') return ty === 'checkbox' ? 'checkbox' : ty === 'radio' ? 'radio' : 'textbox';
    return '';
  };
  const labelFor = el => {
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return norm(l.textContent); }
    const wrap = el.closest('label'); if (wrap) return norm(wrap.textContent);
    return '';
  };
  const adjacentLabel = el => {
    const cell = el.closest('td,th'); if (!cell) return '';
    let p = cell.previousElementSibling;
    while (p && !norm(p.textContent)) p = p.previousElementSibling;
    return p ? norm(p.textContent) : '';
  };
  // returns [name, source]; source 'a11y' = browsers compute this as the accessible name,
  // 'adjacent' = our heuristic from the neighbouring label cell (NOT in the a11y tree)
  const accName = el => {
    const t = el.tagName.toLowerCase(), ty = (el.getAttribute('type') || '').toLowerCase();
    const a11y = norm(el.getAttribute('aria-label')) || labelFor(el) ||
      (t === 'input' && ['submit','button','reset'].includes(ty) ? norm(el.value) : '') ||
      ((t === 'a' || t === 'button') ? norm(el.textContent) : '') ||
      norm(el.getAttribute('placeholder')) || norm(el.getAttribute('title'));
    if (a11y) return [a11y, 'a11y'];
    const adj = adjacentLabel(el);
    return adj ? [adj, 'adjacent'] : ['', ''];
  };
  const controls = document.querySelectorAll('a[href],button,input,select,textarea,[role],[onclick],[tabindex]');
  for (const el of controls) {
    if (out.length >= maxItems) break;
    if (!visible(el) || seen.has(el)) continue; seen.add(el);
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const r = el.getBoundingClientRect();
    const attrs = {};
    for (const a of ['name','type','placeholder','href','aria-label','value','title']) { const v = el.getAttribute(a); if (v) attrs[a] = v; }
    if (el.tagName.toLowerCase() === 'select') attrs.options = Array.from(el.options).map(o => o.text).join(' | ');
    const lf = labelFor(el); if (lf) attrs.label_for = lf;
    const [name, src] = accName(el); if (src) attrs.name_source = src;
    out.push({ kind: 'control', tag: el.tagName.toLowerCase(), role: el.getAttribute('role') || implicitRole(el),
      name, text: norm(el.textContent), attrs, xpath: xpathOf(el),
      bbox: [r.x, r.y, r.width, r.height] });
  }
  // value cells: leaf-ish table cells with short text whose left neighbour is a label cell
  for (const cell of document.querySelectorAll('td')) {
    if (out.length >= maxItems) break;
    if (!visible(cell) || cell.querySelector('td,input,select,a,button')) continue;
    const text = norm(cell.textContent); if (!text || text.length > 80) continue;
    let p = cell.previousElementSibling; while (p && !norm(p.textContent)) p = p.previousElementSibling;
    const label = p ? norm(p.textContent) : '';
    if (!label || label === text) continue;
    const r = cell.getBoundingClientRect();
    out.push({ kind: 'text', tag: 'td', role: 'cell', name: label, text, attrs: {}, xpath: xpathOf(cell),
      label_adjacent: label, bbox: [r.x, r.y, r.width, r.height] });
  }
  return out;
}
"""

ERROR_PAGE_RE = re.compile(
    r"\b(HTTP\s*5\d\d|HTTP\s*4\d\d|Internal (Server|Application) Error|"
    r"Service Unavailable|Bad Gateway|Application Error)\b",
    re.I,
)


class BrowserSurface:
    def __init__(
        self,
        headless: bool = True,
        slow_mo: int = 0,
        viewport: tuple[int, int] = (1280, 900),
        cdp_port: int | None = None,
    ):
        self._pw: Playwright = sync_playwright().start()
        args = [f"--remote-debugging-port={cdp_port}"] if cdp_port else []
        self.browser: Browser = self._pw.chromium.launch(headless=headless, slow_mo=slow_mo, args=args)
        self.context: BrowserContext = self.browser.new_context(viewport={"width": viewport[0], "height": viewport[1]})
        self.page: Page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.cdp_port = cdp_port
        self._console_errors: list[str] = []
        self.page.on("pageerror", lambda e: self._console_errors.append(str(e)))

    # -- perception ----------------------------------------------------------
    def _frame_key(self, f: Frame) -> str | None:
        if f == self.page.main_frame:
            return None
        return f.name or f.url

    def _frames(self) -> list[Frame]:
        return [f for f in self.page.frames if f.url not in ("", "about:blank") or f == self.page.main_frame]

    def a11y_dump(self) -> str:
        parts = []
        for f in self._frames():
            try:
                snap = f.locator("body").aria_snapshot(timeout=2000)
            except Exception as e:  # frame navigating / detached
                snap = f"(unavailable: {e.__class__.__name__})"
            parts.append(f"### frame {self._frame_key(f) or '(main)'} — {f.url}\n{snap}")
        return "\n\n".join(parts)

    def perceive(self, max_elements: int = 120) -> Perception:
        self._settle()
        frames_out, elements = [], []
        n = 0
        for f in self._frames():
            key = self._frame_key(f)
            try:
                a11y = f.locator("body").aria_snapshot(timeout=2000)
                raw = f.evaluate(ENUMERATE_JS, max_elements)
            except Exception as e:
                a11y, raw = f"(unavailable: {e.__class__.__name__})", []
            frames_out.append({"name": key, "url": f.url, "a11y": a11y})
            for item in raw:
                n += 1
                elements.append(
                    ElementInfo(
                        ref=f"e{n}",
                        frame=key,
                        tag=item["tag"],
                        role=item["role"],
                        name=item["name"],
                        text=item["text"],
                        kind=item["kind"],
                        attrs=item.get("attrs", {}),
                        xpath=item["xpath"],
                        label_adjacent=item.get("label_adjacent"),
                        bbox=tuple(item["bbox"]),
                    )
                )
        fp = hashlib.sha1("\n".join(str(f["a11y"]) for f in frames_out).encode()).hexdigest()[:16]
        return Perception(
            url=self.page.url,
            title=self.page.title(),
            frames=frames_out,
            elements=elements,
            screenshot=self.screenshot(),
            fingerprint=fp,
        )

    def _settle(self, timeout_ms: int = 5000) -> None:
        try:
            self.page.wait_for_load_state("load", timeout=timeout_ms)
            for f in self.page.frames:
                f.wait_for_load_state("load", timeout=timeout_ms)
        except PWTimeout:
            pass

    def screenshot(self) -> bytes:
        try:
            return self.page.screenshot(full_page=False)
        except Exception:
            return b""

    def current_url(self) -> str:
        return self.page.url

    # -- locating ------------------------------------------------------------
    def _frame_for(self, key: str | None) -> Frame:
        if key is None:
            return self.page.main_frame
        for f in self.page.frames:
            if f.name == key or key in f.url:
                return f
        raise LookupError(f"frame {key!r} not present (have {[f.name or f.url for f in self.page.frames]})")

    def _build(self, loc: Locator) -> PWLocator:
        f = self._frame_for(loc.frame)
        s = loc.strategy
        if s == LocatorStrategy.ROLE:
            return f.get_by_role(loc.value, name=loc.name, exact=loc.exact)  # type: ignore[arg-type]
        if s == LocatorStrategy.LABEL:
            return f.get_by_label(loc.value, exact=loc.exact)
        if s == LocatorStrategy.PLACEHOLDER:
            return f.get_by_placeholder(loc.value, exact=loc.exact)
        if s == LocatorStrategy.TEXT:
            return f.get_by_text(loc.value, exact=loc.exact)
        if s == LocatorStrategy.LABEL_ADJACENT:
            lbl = loc.value.replace("'", "\\'")
            cell = f"//td[normalize-space()='{lbl}']/following-sibling::td[1]"
            control = f.locator(f"xpath=({cell}//input|{cell}//select|{cell}//textarea)[1]")
            value_cell = f.locator(
                f"xpath=//td[normalize-space()='{lbl}']/following-sibling::td[normalize-space()!=''][1]"
            )
            return control.or_(value_cell)
        if s == LocatorStrategy.CSS:
            return f.locator(loc.value)
        if s == LocatorStrategy.XPATH:
            return f.locator(f"xpath={loc.value}")
        if s == LocatorStrategy.COORDINATE:
            raise NotImplementedError("coordinate locators are acted on directly, not resolved")
        raise ValueError(s)

    def resolve(self, locators: list[Locator], timeout_ms: int = 5000) -> tuple[PWLocator, Locator, list[str]]:
        attempts: list[str] = []
        deadline = time.time() + timeout_ms / 1000
        per_try = max(500, int(timeout_ms / max(1, len(locators))))
        while True:
            for loc in locators:
                try:
                    h = self._build(loc)
                    h.first.wait_for(state="visible", timeout=per_try)
                    count = h.count()
                    if count != 1:
                        attempts.append(f"{loc.describe()} -> matched {count} elements (need exactly 1)")
                        continue
                    attempts.append(f"{loc.describe()} -> OK")
                    return h, loc, attempts
                except (PWTimeout, LookupError) as e:
                    attempts.append(f"{loc.describe()} -> {e.__class__.__name__}")
                except Exception as e:  # invalid selector etc.
                    attempts.append(f"{loc.describe()} -> {e.__class__.__name__}: {str(e).splitlines()[0][:120]}")
            if time.time() > deadline:
                raise ResolveError(locators, attempts)

    # -- acting --------------------------------------------------------------
    def act(
        self, action: ActionType, handle: PWLocator | None, value: str | None, timeout_ms: int = 5000
    ) -> str | None:
        if action == ActionType.CLICK:
            assert handle is not None
            handle.click(timeout=timeout_ms)
        elif action == ActionType.TYPE:
            assert handle is not None
            handle.fill(value or "", timeout=timeout_ms)
        elif action == ActionType.SELECT:
            assert handle is not None
            handle.select_option(label=value, timeout=timeout_ms)
        elif action == ActionType.PRESS:
            if handle is not None:
                handle.press(value or "Enter", timeout=timeout_ms)
            else:
                self.page.keyboard.press(value or "Enter")
        elif action == ActionType.EXTRACT:
            assert handle is not None
            return handle.inner_text(timeout=timeout_ms).strip()
        elif action in (ActionType.WAIT, ActionType.ASSERT, ActionType.NAVIGATE):
            pass
        else:
            raise ValueError(action)
        self._settle()
        return None

    def click_at(self, x: float, y: float) -> None:
        self.page.mouse.click(x, y)

    def navigate(self, url: str, timeout_ms: int = 15000) -> None:
        self.page.goto(url, timeout=timeout_ms, wait_until="load")

    # -- conditions ----------------------------------------------------------
    def _all_text(self, frame_key: str | None = None) -> str:
        frames = [self._frame_for(frame_key)] if frame_key is not None else self._frames()
        chunks = []
        for f in frames:
            try:
                chunks.append(f.locator("body").inner_text(timeout=1000))
            except Exception:
                pass
        return "\n".join(chunks)

    def check(self, cond: Condition) -> tuple[bool, str]:
        k = cond.kind
        if k == ConditionKind.URL_CONTAINS:
            ok = cond.value in self.page.url
            return ok, f"url={self.page.url}"
        if k in (ConditionKind.TEXT_PRESENT, ConditionKind.TEXT_ABSENT):
            try:
                text = self._all_text(cond.frame)
            except LookupError as e:
                return (k == ConditionKind.TEXT_ABSENT), str(e)
            present = cond.value.lower() in text.lower()
            return (
                present if k == ConditionKind.TEXT_PRESENT else not present
            ), f"text {'found' if present else 'not found'}: {cond.value!r}"
        if k in (ConditionKind.ELEMENT_PRESENT, ConditionKind.ELEMENT_ABSENT):
            assert cond.locator is not None
            try:
                n = self._build(cond.locator).count()
            except (LookupError, Exception):
                n = 0
            return (n > 0 if k == ConditionKind.ELEMENT_PRESENT else n == 0), f"{cond.locator.describe()} matched {n}"
        if k == ConditionKind.HTTP_ERROR_PAGE:
            blob = f"{self.page.title()}\n{self._all_text()[:2000]}"
            m = ERROR_PAGE_RE.search(blob)
            return bool(m), (f"error page signature: {m.group(0)!r}" if m else "no error-page signature")
        raise ValueError(k)

    def wait_for(self, cond: Condition) -> tuple[bool, str]:
        deadline = time.time() + cond.timeout_ms / 1000
        while True:
            ok, obs = self.check(cond)
            if ok or time.time() > deadline:
                return ok, obs
            time.sleep(0.2)

    def close(self) -> None:
        try:
            self.context.close()
            self.browser.close()
        finally:
            self._pw.stop()
