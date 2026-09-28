"""Merge the desktop and mobile extractions into one responsive tree and
split it into page sections.

Every element keeps two style records: ``d`` (desktop) and ``m`` (mobile).
A record is ``None`` when the element is hidden at that width, which is how
responsive behaviour (hamburger menus, hidden columns) is detected.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterator, Optional, Union

Kid = Union["Node", str]
INHERITABLE = {"color", "font-family", "font-size", "font-weight", "font-style", "line-height",
               "letter-spacing", "text-transform", "text-align", "white-space"}


@dataclass
class Node:
    id: str
    tag: str
    d: Optional[dict]  # {"rect": [...], "s": {...}, "a": {...}} at desktop, or None if hidden
    m: Optional[dict]  # same at mobile
    parent: Optional["Node"] = None
    kids: list = field(default_factory=list)  # Node | str (text)

    # ---- convenience -------------------------------------------------------
    @property
    def rec(self) -> dict:
        return self.d or self.m or {}

    @property
    def rect(self) -> list:
        return self.rec.get("rect", [0, 0, 0, 0])

    @property
    def attrs(self) -> dict:
        a = dict((self.m or {}).get("a", {}))
        a.update((self.d or {}).get("a", {}))
        return a

    def style(self, vp: str = "d") -> Optional[dict]:
        r = self.d if vp == "d" else self.m
        return r["s"] if r else None

    @property
    def elements(self) -> list["Node"]:
        return [k for k in self.kids if isinstance(k, Node)]

    def walk(self) -> Iterator["Node"]:
        yield self
        for k in self.elements:
            yield from k.walk()

    def text(self, limit: int = 100000) -> str:
        parts: list[str] = []
        size = 0
        for k in self.kids:
            if isinstance(k, str):
                parts.append(k)
                size += len(k)
            else:
                t = k.text(limit - size)
                if t:
                    parts.append(" " + t + " ")
                    size += len(t)
            if size >= limit:
                break
        return re.sub(r"\s+", " ", "".join(parts)).strip()[:limit]

    def own_text(self) -> str:
        return re.sub(r"\s+", " ", "".join(k for k in self.kids if isinstance(k, str))).strip()

    def depth_to(self, ancestor: "Node") -> int:
        n, d = self, 0
        while n is not None and n is not ancestor:
            n, d = n.parent, d + 1
        return d


def _order(kid_id: str) -> float:
    """Document-order key of a kid id. Pseudo-element ids ("12::before" / "12::after") sort
    first / last among the children of element 12."""
    num_, _, pseudo = kid_id.partition("::")
    if not pseudo:
        return float(num_)
    return float(num_) + 0.5 if pseudo == "before" else float("inf")


def _merge_kids(dk: list, mk: list, d_ids: set) -> list:
    """Merge two ordered kid lists (element ids are numbered in document order)."""
    base = list(dk)
    extra = [k for k in mk if isinstance(k, str) and k not in d_ids]
    for e in extra:
        eid = _order(e)
        pos = len(base)
        for i, k in enumerate(base):
            if not isinstance(k, dict) and _order(k) > eid:
                pos = i
                break
        base.insert(pos, e)
    return base


def build_tree(desktop: dict, mobile: dict) -> Node:
    dn, mn = desktop["nodes"], mobile["nodes"]
    ids = set(dn) | set(mn)
    nodes: dict[str, Node] = {}
    for i in ids:
        drec, mrec = dn.get(i), mn.get(i)
        tag = (drec or mrec)["tag"]
        nodes[i] = Node(id=i, tag=tag, d=drec, m=mrec)
    for i, node in nodes.items():
        drec, mrec = dn.get(i), mn.get(i)
        dk = drec["kids"] if drec else []
        mk = mrec["kids"] if mrec else []
        if drec and mrec:
            d_elem_ids = {k for k in dk if not isinstance(k, dict)}
            raw = _merge_kids(dk, mk, d_elem_ids)
        else:
            raw = dk or mk
        kids: list = []
        for k in raw:
            if isinstance(k, dict):
                kids.append(k["t"])
            elif k in nodes:
                child = nodes[k]
                child.parent = node
                kids.append(child)
        node.kids = kids
    return nodes["0"]


# ---------------------------------------------------------------------------
# Section segmentation
# ---------------------------------------------------------------------------
HEADER_HINT = re.compile(r"(^|[-_ ])(header|navbar|nav|topbar|masthead)([-_ ]|$)", re.I)
FOOTER_HINT = re.compile(r"(^|[-_ ])(footer)([-_ ]|$)", re.I)


@dataclass
class Section:
    index: int
    node: Node
    kind: str  # header | footer | section
    name: str = ""

    @property
    def rect(self) -> list:
        return self.node.rect


def _visible_elements(n: Node) -> list[Node]:
    """Visible (desktop) element children; zero-height wrappers are replaced by
    their first sizeable descendant (common for fixed headers)."""
    out = []
    for k in n.elements:
        if k.d is None:  # only visible on mobile (e.g. drawer) - ignore at this level
            continue
        if k.rect[3] < 2:
            sub = _first_sized(k, 3)
            if sub is not None:
                out.append(sub)
            continue
        out.append(k)
    return out


def _first_sized(n: Node, depth: int) -> Optional[Node]:
    for k in n.elements:
        if k.d is None:
            continue
        if k.rect[3] >= 20 and k.rect[2] >= 200:
            return k
        if depth > 0:
            got = _first_sized(k, depth - 1)
            if got is not None:
                return got
    return None


def _descend(n: Node) -> Node:
    """Skip wrappers that have a single meaningful child covering most of them."""
    for _ in range(12):
        kids = _visible_elements(n)
        big = [k for k in kids if k.rect[3] >= 0.6 * max(n.rect[3], 1)]
        if len(kids) == 1:
            n = kids[0]
            continue
        if len(big) == 1 and all(k is big[0] or k.rect[3] < 4 for k in kids):
            n = big[0]
            continue
        break
    return n


def _content_children(n: Node) -> list[Node]:
    """Visible children minus narrow decorative rails/borders (no text, <15% width)."""
    return [c for c in _visible_elements(n)
            if not (c.rect[2] < 0.15 * max(n.rect[2], 1) and not c.text(10))]


def _stacked(children: list[Node], parent: Node) -> bool:
    tall = [c for c in children if c.rect[3] > 40]
    return all(c.rect[2] >= 0.5 * parent.rect[2] for c in tall)


def _synthetic(group: list[Node]) -> Node:
    """Wrap consecutive sibling nodes into one virtual section node."""
    par = group[0].parent

    def rec_for(vp: str) -> Optional[dict]:
        recs = [(g.d if vp == "d" else g.m) for g in group]
        recs = [r for r in recs if r]
        if not recs:
            return None
        x0 = min(r["rect"][0] for r in recs)
        y0 = min(r["rect"][1] for r in recs)
        x1 = max(r["rect"][0] + r["rect"][2] for r in recs)
        y1 = max(r["rect"][1] + r["rect"][3] for r in recs)
        base = ((par.d if vp == "d" else par.m) or {}).get("s", {}) if par else {}
        s = {k: v for k, v in base.items() if k in INHERITABLE}
        s["display"] = "block"
        return {"rect": [x0, y0, x1 - x0, y1 - y0], "s": s, "a": {}}

    return Node(id="g" + group[0].id, tag="div", d=rec_for("d"), m=rec_for("m"), parent=par, kids=list(group))


SPLIT_HEIGHT = 2200


def _group(candidates: list[Node], header_like) -> list[Node]:
    """Group small siblings (lead-in headings, logo rows) with the next big block."""
    grouped: list[Node] = []
    pending: list[Node] = []

    def flush():
        if pending:
            grouped.append(pending[0] if len(pending) == 1 else _synthetic(list(pending)))
            pending.clear()

    for i, c in enumerate(candidates):
        h = c.rect[3]
        edge = c.tag in ("header", "footer", "nav") or (i == 0 and header_like(c))
        if h < 8 and not c.text(20):
            continue  # divider lines
        if edge:
            flush()
            grouped.append(c)
            continue
        if h < 260:
            pending.append(c)
            if sum(p.rect[3] for p in pending) > 520:
                flush()
            continue
        if pending and sum(p.rect[3] for p in pending) < 400 and all(p.parent is c.parent for p in pending):
            pending.append(c)
            flush()
        else:
            flush()
            grouped.append(c)
    flush()
    return grouped


def _split_tall(n: Node, header_like, depth: int = 0) -> list[Node]:
    if n.rect[3] < SPLIT_HEIGHT or depth > 1 or n.tag in ("header", "footer", "nav") or n.id.startswith("g"):
        return [n]
    chain = [n]
    inner = n
    for _ in range(6):
        kids = _visible_elements(inner)
        if len(kids) == 1:
            inner = kids[0]
            chain.append(inner)
        else:
            break
    kids = _content_children(inner)
    if len([k for k in kids if k.rect[3] >= 150]) < 2 or not _stacked(kids, inner):
        return [n]
    parts = _group(sorted(kids, key=lambda k: k.rect[1]), lambda c: False)
    if len(parts) < 2:
        return [n]
    # Carry the wrapper's background + horizontal padding into each part.
    for vp in ("d", "m"):
        bg = next((c.style(vp).get("background-color") for c in chain if c.style(vp)
                   and c.style(vp).get("background-color") not in (None, "rgba(0, 0, 0, 0)")), None)
        pad_l = next((c.style(vp).get("padding-left") for c in chain if c.style(vp)
                      and c.style(vp).get("padding-left") not in (None, "0px")), None)
        pad_r = next((c.style(vp).get("padding-right") for c in chain if c.style(vp)
                      and c.style(vp).get("padding-right") not in (None, "0px")), None)
        for p in parts:
            st = p.style(vp)
            if st is None:
                continue
            if bg and st.get("background-color") in (None, "rgba(0, 0, 0, 0)"):
                st["background-color"] = bg
            if pad_l and st.get("padding-left") in (None, "0px"):
                st["padding-left"] = pad_l
            if pad_r and st.get("padding-right") in (None, "0px"):
                st["padding-right"] = pad_r
    out: list[Node] = []
    for p in parts:
        out.extend(_split_tall(p, header_like, depth + 1))
    return out


def _table_safe(n: Node) -> Node:
    """Table rows can't be rendered on their own: wrap them in <table><tbody> using the
    styles of the real table they came from (keeps table layout valid for React)."""
    rows = [n] if n.tag == "tr" else (n.elements if n.id.startswith("g") and n.elements
                                      and all(k.tag == "tr" for k in n.elements) else None)
    if n.tag in ("tbody", "thead", "tfoot"):
        rows = [n]
    if not rows:
        return n
    table = n.parent
    while table is not None and table.tag != "table":
        table = table.parent
    if table is None:
        return n

    def rec(vp: str, src: Node) -> Optional[dict]:
        base = table.d if vp == "d" else table.m
        own = src.d if vp == "d" else src.m
        if base is None or own is None:
            return own
        return {"rect": own["rect"], "s": dict(base["s"]), "a": {}}

    if n.tag in ("tbody", "thead", "tfoot"):
        body = n
    else:
        body = Node(id="b" + n.id, tag="tbody", d={"rect": n.rect, "s": {"display": "table-row-group"}, "a": {}},
                    m={"rect": (n.m or n.d)["rect"], "s": {"display": "table-row-group"}, "a": {}} if n.m else None,
                    parent=table, kids=list(rows))
    return Node(id="t" + n.id, tag="table", d=rec("d", n), m=rec("m", n), parent=table.parent, kids=[body])


def find_sections(body: Node, viewport_w: int, max_sections: int = 18) -> list[Section]:
    page_h = max(body.rect[3], 1)
    root = _descend(body)

    def expand(nodes: list[Node], depth: int = 0) -> list[Node]:
        out: list[Node] = []
        for k in nodes:
            k2 = _descend(k) if k.tag not in ("header", "footer", "nav") else k
            inner = _content_children(k2)
            is_wrapper = (k2.tag == "main" or k2.rect[3] >= 0.45 * page_h) and k2.tag not in ("header", "footer", "nav")
            wide = all(c.rect[2] >= 0.5 * viewport_w for c in inner if c.rect[3] > 40 or c.text(10))
            if depth < 4 and is_wrapper and len(inner) >= 2 and _stacked(inner, k2) and wide:
                out.extend(expand(inner, depth + 1))
            else:
                out.append(k)
        return out

    candidates = expand(_visible_elements(root))
    # never drop content: only purely decorative narrow blocks are discarded
    candidates = [c for c in candidates if c.rect[2] >= 0.5 * viewport_w or c.tag in ("header", "nav", "footer")
                  or c.text(10) or any(k.tag in ("img", "svg", "video") for k in c.walk())]

    def header_like(c: Node) -> bool:
        pos = (c.style("d") or {}).get("position", "")
        return c.tag in ("header", "nav") or (pos in ("fixed", "sticky") and c.rect[1] < 10 and c.rect[3] < 160)

    candidates.sort(key=lambda c: (c.rect[1], 0 if header_like(c) else 1, -c.rect[3]))
    # Drop overlays (absolutely positioned toasts, badges) that sit inside another block.
    kept: list[Node] = []
    for c in candidates:
        if kept and not header_like(c) and c.tag != "footer":
            prev = kept[-1]
            py0, py1 = prev.rect[1], prev.rect[1] + prev.rect[3]
            if py0 <= c.rect[1] and c.rect[1] + c.rect[3] <= py1 and not header_like(prev):
                continue
        kept.append(c)
    candidates = kept

    grouped = _group(candidates, header_like)
    # Very tall blocks become several sections: smaller components are more reliable to generate.
    split: list[Node] = []
    for g in grouped:
        split.extend(_split_tall(g, header_like))
    grouped = split

    if len(grouped) > max_sections:
        keep = set(id(n) for n in sorted(grouped, key=lambda n: -n.rect[3])[:max_sections])
        grouped = [n for n in grouped if id(n) in keep]

    grouped = [_table_safe(n) for n in grouped]

    # The header is normally the first block; a thin announcement strip above a real
    # <header>/<nav> must not take its place.
    header_idx = 0
    if len(grouped) > 1 and grouped[0].rect[3] < 80 and grouped[0].tag not in ("header", "nav") \
            and (grouped[1].tag in ("header", "nav") or grouped[1].attrs.get("role") == "banner"):
        header_idx = 1

    sections: list[Section] = []
    for i, n in enumerate(grouped):
        hint = " ".join([n.tag, n.attrs.get("id", ""), n.attrs.get("cls", ""), n.attrs.get("role", "")])
        kind = "section"
        n_links = len([a for a in n.walk() if a.tag == "a"])
        if i == header_idx and (n.tag in ("header", "nav") or HEADER_HINT.search(hint) or n.attrs.get("role") == "banner"):
            kind = "header"
        elif i == header_idx and n.rect[1] < 20 and n.rect[3] < 160 and n_links >= 3:
            kind = "header"
        elif n.tag == "footer" or (i == len(grouped) - 1 and (
                FOOTER_HINT.search(hint) or n.attrs.get("role") == "contentinfo"
                or re.search(r"©|copyright|all rights reserved", n.text(3000), re.I))):
            kind = "footer"
        sections.append(Section(index=i, node=n, kind=kind))
    return sections


# ---------------------------------------------------------------------------
# Heuristic naming (used for mock mode and as a hint for the LLM)
# ---------------------------------------------------------------------------
KEYWORDS = [
    ("Pricing", r"pricing|per month|/mo\b|plans?\b"),
    ("Testimonials", r"testimonial|what (our )?customers|loved by|reviews?\b"),
    ("Faq", r"\bfaq\b|frequently asked|questions"),
    ("Features", r"features?|why choose|what you get|capabilities"),
    ("Logos", r"trusted by|used by|companies|partners|backed by|powering"),
    ("Stats", r"\b\d+[%kKmM+]"),
    ("Contact", r"contact|get in touch|reach us"),
    ("Newsletter", r"newsletter|subscribe"),
    ("Team", r"our team|meet the"),
    ("Blog", r"\bblog\b|latest posts|articles|news"),
    ("Cta", r"get started|start (your )?free|sign up|try .* free|book a demo"),
]


def heuristic_name(sec: Section, first_content: bool) -> str:
    if sec.kind == "header":
        return "Navbar"
    if sec.kind == "footer":
        return "Footer"
    n = sec.node
    has_h1 = any(k.tag == "h1" for k in n.walk())
    if first_content and not has_h1 and n.rect[3] < 80:
        return "TopBar"  # announcement / contact strip above the navbar
    if first_content or has_h1:
        return "Hero"
    heading = ""
    for k in n.walk():
        if k.tag in ("h2", "h3"):
            heading = k.text(120)
            break
    # The section heading is the strongest signal; fall back to the body text.
    for source in (heading.lower(), n.text(400).lower()):
        for name, pat in KEYWORDS:
            if source and re.search(pat, source):
                return name
    return "Content"


def assign_names(sections: list[Section]) -> None:
    used: dict[str, int] = {}
    first_content_done = False
    for s in sections:
        first = not first_content_done and s.kind == "section"
        name = heuristic_name(s, first)
        if s.kind == "section" and name != "TopBar":
            first_content_done = True
        used[name] = used.get(name, 0) + 1
        s.name = name if used[name] == 1 else f"{name}{used[name]}"
