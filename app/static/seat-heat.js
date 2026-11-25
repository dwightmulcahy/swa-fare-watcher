// Seat preference heat map on the seat watch form (a file, not inline: the CSP allows only
// scripts from this site). Reads its data from <script type="application/json" id="heat-data">.
// Classifies and ranks seats exactly like app/seats.py (a browser test compares the two).
(() => {
  const D = JSON.parse(document.getElementById("heat-data").textContent);
  const form = document.getElementById("seat-heat").closest("form");
  const grid = document.getElementById("heat-grid"), plane = document.getElementById("heat-plane"),
        win = document.getElementById("heat-window"), tip = document.getElementById("heat-tip");
  const CATS = Object.keys(D.categories);

  const attrs = (s) => {
    const f = new Set(s.features);
    const pos = ["window", "aisle", "middle"].find((x) => f.has(x)) || null;
    return {pos, exit: f.has("exit row"), extra: s.type === "extra", open: f.has("no seat in front"),
            bulk: f.has("bulkhead seat"), recline: f.has("limited recline") ? false : null,
            neighbor: f.has("rest of its side of the row empty") ? 2 : f.has("empty seat next to it") ? 1 : 0,
            view: pos !== "window" ? null : !f.has("window without window view")};
  };
  const matches = (a, cat) => {
    const extra = a.extra && !a.bulk, std = !a.extra && !a.exit && !a.bulk;
    return {exit_window_open: a.exit && a.pos === "window" && a.open, exit_window: a.exit && a.pos === "window",
            extra_window: extra && a.pos === "window", bulkhead_window: a.bulk && a.pos === "window",
            exit_aisle: a.exit && a.pos === "aisle", extra_aisle: extra && a.pos === "aisle",
            bulkhead_aisle: a.bulk && a.pos === "aisle", std_window: std && a.pos === "window",
            std_aisle: std && a.pos === "aisle", middle: a.pos === "middle"}[cat];
  };
  const prefs = () => {
    const pri = {};
    CATS.forEach((k) => { const v = Number(form.querySelector(`[name=prio_${k}]`)?.value || 0); pri[k] = v || null; });
    const order = (form.querySelector("#tiebreak-order")?.value || D.tiebreaks.join(",")).split(",");
    return {pri, pos: (form.querySelector("input[name=seat_position]:checked") || {}).value || "any",
            tie: order.filter((k) => form.querySelector(`[name=tb_${k}]`)?.checked),
            fallback: !!form.querySelector("[name=seat_fallback]")?.checked};
  };
  const priority = (s, p) => {
    const a = attrs(s);
    let best = [null, null];
    CATS.forEach((c) => { const v = p.pri[c]; if (v && matches(a, c) && (best[0] === null || v < best[0])) best = [v, c]; });
    return best;
  };
  const tiebreak = (s, p) => {
    const a = attrs(s);
    return p.tie.map((k) => k === "position" ? ((p.pos !== "window" && p.pos !== "aisle") || a.pos === p.pos ? 0 : 1)
      : k === "legroom" ? (a.extra || a.open ? 0 : 1) : k === "forward" ? Math.floor((s.row - 1) / 10)
      : k === "view" ? (a.view === false ? 1 : 0) : k === "recline" ? (a.recline === false ? 1 : 0)
      : k === "neighbor" ? 2 - a.neighbor : 0);
  };
  const key = (s, p) => [priority(s, p)[0] ?? D.max + 1, ...tiebreak(s, p), s.row, s.col];
  const cmp = (x, y) => { for (let i = 0; i < x.length; i++) { if (x[i] < y[i]) return -1; if (x[i] > y[i]) return 1; } return 0; };
  const fit = (pr) => pr === null ? 0 : Math.round(1000 * (D.max - pr) / (D.max - 1)) / 10;

  // Southwest colour stops: [score, colour, text colour]; priority 1 = 100 ... last = 0
  const STOPS = [[0, "#D5152E", "#FFFFFF"], [30, "#FFBF27", "#111B40"], [50, "#E2E8F0", "#111B40"],
                 [75, "#60A5FA", "#111B40"], [100, "#304CB2", "#FFFFFF"]];
  const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const colour = (score) => {
    const i = STOPS.findIndex(([s]) => score <= s);
    if (i <= 0) return STOPS[0][1];
    const [s0, c0] = STOPS[i - 1], [s1, c1] = STOPS[i], t = (score - s0) / (s1 - s0), a = rgb(c0), b = rgb(c1);
    return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * t)).join(",")})`;
  };
  const ink = (score) => STOPS.reduce((b, st) => Math.abs(st[0] - score) < Math.abs(b[0] - score) ? st : b)[2];

  let tipFor = null;
  const showTip = (el) => {
    tipFor = el;
    tip.innerHTML = `<b>Seat ${el.dataset.seat}: ${el.dataset.label}</b><br>${el.dataset.desc}`;
    tip.style.background = el.dataset.off ? "#5B6170" : colour(Number(el.dataset.fit));
    tip.style.color = el.dataset.off ? "#FFFFFF" : ink(Number(el.dataset.fit));
    tip.hidden = false;
    const w = tip.parentElement.getBoundingClientRect(), r = el.getBoundingClientRect();
    tip.style.left = `${r.right - w.left + 8}px`; tip.style.top = `${r.top - w.top - 4}px`;
  };
  const isSeat = (e) => e.target.classList && e.target.classList.contains("heat-seat");
  grid.addEventListener("mouseover", (e) => isSeat(e) && showTip(e.target));
  grid.addEventListener("focusin", (e) => isSeat(e) && showTip(e.target));
  ["mouseleave", "focusout"].forEach((ev) => grid.addEventListener(ev, () => { tip.hidden = true; tipFor = null; }));
  grid.addEventListener("scroll", () => {
    if (!tipFor || tip.hidden) return;
    const g = grid.getBoundingClientRect(), r = tipFor.getBoundingClientRect();
    if (r.bottom < g.top || r.top > g.bottom) { tip.hidden = true; return; }
    showTip(tipFor);
  });
  document.querySelector(".heat-scale").style.background =
    `linear-gradient(90deg, ${STOPS.map(([s, c]) => `${c} ${s}%`).join(", ")})`;

  const draw = () => {
    const p = prefs(), seats = D.maps[plane.value];
    const status = (form.querySelector("[name=seat_status]") || {}).value || "none";
    const free = new Set(D.status[status][win.checked ? "window" : "booking"]);
    // your seat: the real map says which (selected); on a plane layout, the seat(s) from the last check
    const real = seats.some((s) => s.selected);
    const mine = new Map((D.mine || []).map((m) => [m.seat, m.flight]));
    const scored = seats.map((s) => {
      const [pr, cat] = priority(s, p);
      const own = real ? !!s.selected : mine.has(s.seat);
      return {...s, pr, cat, fit: fit(pr), free: free.has(s.type), own, ownLeg: real ? "" : mine.get(s.seat) || ""};
    });
    const eligible = scored.filter((s) => s.free && s.available !== false && (s.pr !== null || p.fallback))
      .sort((x, y) => cmp(key(x, p), key(y, p)));
    const pick = eligible.length ? eligible[0].seat : null;
    // only the rows the plane has: Southwest's 737-700 numbers 1-9 then 14 on (no rows 10-13)
    const rows = [...new Set(seats.map((s) => s.row))].sort((a, b) => a - b);
    grid.textContent = "";
    const head = document.createElement("div"); head.className = "heat-row heat-cols";
    head.innerHTML = "<span></span>" + "ABC".split("").map((c) => `<span>${c}</span>`).join("") +
      "<span></span>" + "DEF".split("").map((c) => `<span>${c}</span>`).join("");
    grid.append(head);
    for (const r of rows) {
      const row = document.createElement("div"); row.className = "heat-row";
      row.append(...["num", ..."ABC", "num", ..."DEF"].map((c, i) => {
        const el = document.createElement("span");
        if (c === "num") { el.className = "heat-num"; el.textContent = i ? "" : r; return el; }
        const s = scored.find((x) => x.row === r && x.col === c);
        if (!s) { el.className = "heat-gap"; return el; }
        el.className = "heat-seat" + (s.pr === null ? " heat-offcat" : "") + (s.free ? "" : " heat-locked") +
          (s.available === false && !s.own ? " heat-taken" : "") + (s.own ? " heat-mine" : "") +
          (s.seat === pick ? " heat-best" : "");
        if (s.pr !== null) el.style.backgroundColor = colour(s.fit); else el.dataset.off = "1";
        el.tabIndex = 0;
        el.dataset.seat = s.seat; el.dataset.fit = s.fit.toFixed(2); el.dataset.priority = s.pr ?? "";
        el.dataset.label = s.pr === null ? (p.fallback ? "not in your priorities (fallback only)" : "not in your priorities")
          : `Priority ${s.pr} · ${D.categories[s.cat]}`;
        const kind = {extra: "Extra Legroom", preferred: "Preferred", standard: "Standard"}[s.type];
        el.dataset.desc = `${kind}, ${s.features.filter((f) => f !== "window without window view" && f !== "limited recline").join(", ")}` +
          (s.features.includes("window without window view") ? ", no window view" : "") +
          (s.features.includes("limited recline") ? ", little or no recline" : "") +
          (s.free ? "" : " · not free for your status at this time") +
          (s.own ? ` · your seat${s.ownLeg ? ` (${s.ownLeg})` : ""}` : s.available === false ? " · taken" : "") +
          (s.seat === pick ? " · the seat it would pick" : "");
        el.setAttribute("aria-label", `Seat ${s.seat}: ${el.dataset.label}${s.own ? ", your seat" : ""}`);
        return el;
      }));
      grid.append(row);
    }
  };
  form.addEventListener("change", draw);
  form.addEventListener("prio-change", draw);
  plane.addEventListener("change", draw);
  draw();
})();
