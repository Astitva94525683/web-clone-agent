// In-page DOM + computed-style extractor.
// Injected by capture/browser.py. Exposes window.__CA with:
//   tag()          -> assigns a stable data-ca id to every element (run once)
//   extract(name)  -> returns {nodes, page} for the current viewport
// Only *visible* elements are returned; the same ids are reused across
// viewports so Python can merge desktop + mobile into one responsive tree.
(() => {
  if (window.__CA) return;

  const SKIP = new Set([
    "SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "LINK", "META", "HEAD", "TITLE",
    "BASE", "OBJECT", "EMBED", "PARAM", "SOURCE", "TRACK", "MAP", "AREA", "DIALOG",
  ]);
  // Computed-style properties we care about (converted to Tailwind in Python).
  const PROPS = [
    "display", "position", "top", "right", "bottom", "left", "z-index",
    "flex-direction", "flex-wrap", "justify-content", "align-items", "align-self",
    "flex-grow", "flex-shrink", "flex-basis", "order",
    "row-gap", "column-gap", "grid-template-columns", "grid-column-start", "grid-column-end", "justify-self",
    "grid-row-start", "grid-row-end",
    "padding-top", "padding-right", "padding-bottom", "padding-left",
    "margin-top", "margin-right", "margin-bottom", "margin-left",
    "width", "height", "max-width", "min-height", "max-height", "box-sizing",
    "font-family", "font-size", "font-weight", "font-style", "line-height", "letter-spacing",
    "text-transform", "text-align", "text-decoration-line", "white-space",
    "color", "background-color", "background-image", "background-size", "background-position",
    "background-repeat",
    "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
    "border-top-style", "border-top-color", "border-bottom-style", "border-bottom-color",
    "border-left-style", "border-left-color", "border-right-style", "border-right-color",
    "border-top-left-radius", "border-top-right-radius", "border-bottom-right-radius",
    "border-bottom-left-radius",
    "box-shadow", "opacity", "overflow-x", "overflow-y", "object-fit", "object-position",
    "aspect-ratio", "backdrop-filter", "list-style-type", "cursor", "transform", "filter",
    "clip", "clip-path",
  ];
  const COLOR_PROPS = new Set([
    "color", "background-color", "border-top-color", "border-bottom-color",
    "border-left-color", "border-right-color",
  ]);

  // --- colour normalisation: any CSS colour (oklch, lab, hsl...) -> rgba() ---
  const cvs = document.createElement("canvas");
  cvs.width = cvs.height = 1;
  const ctx = cvs.getContext("2d", { willReadFrequently: true });
  const colorCache = new Map();
  function normColor(c) {
    if (!c) return null;
    if (colorCache.has(c)) return colorCache.get(c);
    let out = c;
    if (!/^rgba?\(/.test(c)) {
      try {
        ctx.clearRect(0, 0, 1, 1);
        ctx.fillStyle = "rgba(0,0,0,0)";
        ctx.fillStyle = c;
        ctx.fillRect(0, 0, 1, 1);
        const d = ctx.getImageData(0, 0, 1, 1).data;
        out = `rgba(${d[0]}, ${d[1]}, ${d[2]}, ${(d[3] / 255).toFixed(3)})`;
      } catch (e) { out = c; }
    }
    colorCache.set(c, out);
    return out;
  }

  function tag() {
    let n = 0;
    document.body.setAttribute("data-ca", "0");
    const all = document.body.querySelectorAll("*");
    for (const el of all) {
      if (SKIP.has(el.tagName)) continue;
      const svg = el.closest("svg");
      if (svg && svg !== el) continue; // svg internals are serialised with the svg
      el.setAttribute("data-ca", String(++n));
    }
    return n;
  }

  function isCookieBanner(el, cs) {
    if (cs.position !== "fixed" && cs.position !== "sticky") return false;
    const t = (el.innerText || "").slice(0, 600).toLowerCase();
    return /cookie|consent|gdpr|privacy preferences|we use cookies/.test(t) && t.length < 1500;
  }

  function serializeSvg(el, color) {
    try {
      const clone = el.cloneNode(true);
      const r = el.getBoundingClientRect();
      if (!clone.getAttribute("xmlns")) clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      if (!clone.getAttribute("width")) clone.setAttribute("width", String(Math.round(r.width)));
      if (!clone.getAttribute("height")) clone.setAttribute("height", String(Math.round(r.height)));
      // <use href="#sprite"> references break when detached: inline the symbol.
      clone.querySelectorAll("use").forEach((u) => {
        const ref = u.getAttribute("href") || u.getAttribute("xlink:href");
        if (ref && ref.startsWith("#")) {
          const sym = document.querySelector(ref);
          if (sym) {
            const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
            g.innerHTML = sym.innerHTML;
            if (sym.getAttribute("viewBox") && !clone.getAttribute("viewBox"))
              clone.setAttribute("viewBox", sym.getAttribute("viewBox"));
            u.replaceWith(g);
          }
        }
      });
      let html = clone.outerHTML.replace(/currentColor/gi, color || "#000");
      // Resolve the inherited fill so detached SVGs keep their colour.
      if (!/fill=/.test(html.slice(0, 300))) {
        const fill = getComputedStyle(el).fill;
        if (fill && fill !== "none" && !fill.startsWith("url")) {
          html = html.replace("<svg", `<svg fill="${normColor(fill)}"`);
        }
      }
      return html.length < 60000 ? html : null;
    } catch (e) { return null; }
  }

  function readStyles(cs) {
    const s = {};
    for (const p of PROPS) {
      let v = cs.getPropertyValue(p);
      if (COLOR_PROPS.has(p)) v = normColor(v);
      s[p] = v;
    }
    return s;
  }

  // Absolutely positioned ::before/::after boxes with a background (image overlays,
  // decorative shapes) are real visuals but not DOM elements: record them as child nodes.
  function pseudoNode(el, which, id, r, cs, scrollX, scrollY) {
    if (cs.position === "static") return null; // containing block would be some other ancestor
    const ps = getComputedStyle(el, which);
    if (!ps.content || ps.content === "none" || ps.content === "normal") return null;
    if (ps.display === "none" || ps.visibility === "hidden" || parseFloat(ps.opacity) === 0) return null;
    if (ps.position !== "absolute") return null;
    const bg = normColor(ps.backgroundColor) || "";
    const alpha = /rgba\([^)]*,\s*([\d.]+)\)/.exec(bg);
    const hasBg = (ps.backgroundImage && ps.backgroundImage !== "none") || (bg && !(alpha && parseFloat(alpha[1]) === 0));
    if (!hasBg) return null;
    const w = parseFloat(ps.width), h = parseFloat(ps.height);
    if (!(w >= 4 && h >= 4)) return null;
    const bl = parseFloat(cs.borderLeftWidth) || 0, bt = parseFloat(cs.borderTopWidth) || 0;
    const br = parseFloat(cs.borderRightWidth) || 0, bb = parseFloat(cs.borderBottomWidth) || 0;
    let left = parseFloat(ps.left), top = parseFloat(ps.top);
    if (isNaN(left)) left = r.width - bl - br - (parseFloat(ps.right) || 0) - w;
    if (isNaN(top)) top = r.height - bt - bb - (parseFloat(ps.bottom) || 0) - h;
    return {
      id: id + which, tag: "div", parent: id,
      rect: [Math.round(r.left + scrollX + bl + left), Math.round(r.top + scrollY + bt + top), Math.round(w), Math.round(h)],
      s: readStyles(ps), a: {}, kids: [],
    };
  }

  function extract(vpName) {
    const nodes = {};
    const scrollY = window.scrollY, scrollX = window.scrollX;
    const pageW = document.documentElement.scrollWidth;
    const vw = window.innerWidth;
    const all = [document.body, ...document.body.querySelectorAll("[data-ca]")];

    function visit(el) {
      const id = el.getAttribute("data-ca");
      if (id === null) return;
      const cs = getComputedStyle(el);
      if (cs.display === "none" || cs.visibility === "hidden" || cs.visibility === "collapse") return;
      // opacity:0 + pointer-events:none = intentionally hidden (menus, tooltips).
      // opacity:0 alone is usually an unfinished entrance animation -> keep.
      if (parseFloat(cs.opacity) === 0 && cs.pointerEvents === "none") return;
      if (isCookieBanner(el, cs)) return;
      const r = el.getBoundingClientRect();
      const contents = cs.display === "contents";
      if (!contents && r.width < 1 && r.height < 1 && el !== document.body) return;
      if (!contents && (r.right + scrollX <= 0 || r.left + scrollX >= pageW)) return; // off-canvas
      // Parent must be visible too (visibility is inherited, but display:none is not).
      const parentEl = el.parentElement;
      const parentId = el === document.body ? null : (parentEl && parentEl.getAttribute("data-ca"));
      if (el !== document.body && (parentId === null || !(parentId in nodes))) return;

      const s = readStyles(cs);
      if (parseFloat(cs.opacity) === 0) s["opacity"] = "1";

      const tagName = el.tagName.toLowerCase();
      const node = {
        id, tag: tagName, parent: parentId,
        rect: [Math.round(r.left + scrollX), Math.round(r.top + scrollY), Math.round(r.width), Math.round(r.height)],
        s, a: {}, kids: [],
      };
      // attributes
      const a = node.a;
      if (el.id) a.id = el.id.slice(0, 60);
      const cls = el.getAttribute("class");
      if (cls && typeof cls === "string") a.cls = cls.slice(0, 120);
      for (const k of ["alt", "title", "aria-label", "role", "type", "placeholder", "name", "target", "colspan", "rowspan"]) {
        const v = el.getAttribute(k);
        if (v) a[k] = v.slice(0, 200);
      }
      if (tagName === "a" && el.href) a.href = el.href;
      if (tagName === "img") {
        a.src = el.currentSrc || el.src || "";
        a.nw = el.naturalWidth; a.nh = el.naturalHeight;
      }
      if (tagName === "video") { a.src = el.currentSrc || el.src || ""; if (el.poster) a.poster = el.poster; }
      if (tagName === "iframe") a.src = el.src || "";
      if (tagName === "input" || tagName === "textarea" || tagName === "select") {
        if (el.value && tagName !== "select") a.value = String(el.value).slice(0, 80);
        if (tagName === "select" && el.options && el.selectedIndex >= 0) {
          a.selected = String(el.options[el.selectedIndex].text || "").slice(0, 60);
        }
      }
      if (tagName === "svg") {
        a.svg = serializeSvg(el, normColor(cs.color));
      }
      if (tagName === "canvas") a.canvas = true;
      if (tagName === "details" && el.open) a.open = true;
      // ordered children (elements by id + text nodes)
      if (tagName !== "svg") {
        const before = pseudoNode(el, "::before", id, r, cs, scrollX, scrollY);
        const after = pseudoNode(el, "::after", id, r, cs, scrollX, scrollY);
        if (before) node.kids.push(before.id);
        for (const ch of el.childNodes) {
          if (ch.nodeType === 3) {
            const t = ch.textContent.replace(/\s+/g, " ");
            if (t.trim()) node.kids.push({ t });
            else if (t === " " && node.kids.length) node.kids.push({ t: " " });
          } else if (ch.nodeType === 1) {
            const cid = ch.getAttribute("data-ca");
            if (cid !== null) node.kids.push(cid);
          }
        }
        if (after) node.kids.push(after.id);
        for (const pn of [before, after]) if (pn) nodes[pn.id] = pn;
      }
      nodes[id] = node;
    }

    for (const el of all) visit(el);
    // Content of closed <details> (FAQ answers) is not rendered, but it is real content:
    // open each one briefly to record it (the clone keeps it collapsed, like the original).
    for (const det of document.body.querySelectorAll("details:not([open])")) {
      const did = det.getAttribute("data-ca");
      if (did === null || !(did in nodes)) continue;
      det.open = true;
      try {
        for (const el of det.querySelectorAll("[data-ca]")) {
          if (!(el.getAttribute("data-ca") in nodes)) visit(el);
        }
      } finally {
        det.open = false;
      }
    }

    // Drop kid references to elements that were not visible in this viewport.
    for (const n of Object.values(nodes)) {
      n.kids = n.kids.filter((k) => typeof k !== "string" || k in nodes);
    }

    const bodyCs = getComputedStyle(document.body);
    const htmlCs = getComputedStyle(document.documentElement);
    return {
      viewport: vpName,
      width: vw,
      height: window.innerHeight,
      scrollHeight: document.documentElement.scrollHeight,
      nodes,
      page: {
        bodyBg: normColor(bodyCs.backgroundColor),
        htmlBg: normColor(htmlCs.backgroundColor),
        bodyColor: normColor(bodyCs.color),
        bodyFont: bodyCs.fontFamily,
        rootFontSize: htmlCs.fontSize,
      },
    };
  }

  function pageMeta() {
    const q = (s) => document.querySelector(s);
    const icons = Array.from(document.querySelectorAll("link[rel~='icon'], link[rel='apple-touch-icon']"))
      .map((l) => l.href).filter(Boolean);
    const fontFaces = [];
    for (const sheet of Array.from(document.styleSheets)) {
      let rules;
      try { rules = sheet.cssRules; } catch (e) { continue; } // cross-origin sheet
      const base = sheet.href || location.href;
      for (const rule of Array.from(rules || [])) {
        if (rule.type === CSSRule.FONT_FACE_RULE) {
          const st = rule.style;
          fontFaces.push({
            family: st.getPropertyValue("font-family").replace(/["']/g, "").trim(),
            weight: st.getPropertyValue("font-weight") || "400",
            style: st.getPropertyValue("font-style") || "normal",
            src: st.getPropertyValue("src"),
            unicodeRange: st.getPropertyValue("unicode-range") || "",
            base,
          });
        }
      }
    }
    return {
      title: document.title,
      description: (q("meta[name='description']") || {}).content || "",
      lang: document.documentElement.lang || "en",
      icons,
      fontFaces,
      googleFontLinks: Array.from(document.querySelectorAll("link[href*='fonts.googleapis.com']")).map((l) => l.href),
      usedFonts: Array.from(new Set(Array.from(document.fonts || []).filter((f) => f.status === "loaded").map((f) => f.family.replace(/["']/g, "")))),
    };
  }

  window.__CA = { tag, extract, pageMeta };
})();
