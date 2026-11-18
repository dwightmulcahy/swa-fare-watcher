// Seat preference heat map on the seat watch form (a file, not inline: the CSP allows only
// scripts from this site). Reads its data from <script type="application/json" id="heat-data">.
(() => {
  const D = JSON.parse(document.getElementById("heat-data").textContent);
  const form = document.getElementById("seat-heat").closest("form");
  const grid = document.getElementById("heat-grid"), plane = document.getElementById("heat-plane"),
        win = document.getElementById("heat-window");
  // same scoring as app/seats.py: value of each criterion 0..1, rank i of n weighs base^(n-1-i)
  const value = (s, k, pos) => {
    const f = new Set(s.features);
    switch (k) {
      case "seat_type": return (pos === "window" || pos === "aisle") && f.has(pos) ? 1 : 0;
      case "no_middle": return f.has("middle") ? 0 : 1;
      case "extra_legroom": return {extra: 1, preferred: 0.5, standard: 0}[s.type];
      case "exit_row": return f.has("exit row") ? 1 : 0;
      case "open_front": return f.has("no seat in front") ? 1 : 0;
      case "bulkhead": return f.has("bulkhead seat") ? 1 : 0;
      case "window_view": return f.has("window without window view") ? 0 : 1;
      case "front": return Math.max(0, 1 - (s.row - 1) / 40);
    }
    return 0;
  };
  const prefs = () => {
    const pos = (form.querySelector("input[name=seat_position]:checked") || {}).value || "any";
    const ranked = D.criteria.map((k, i) => [Number(form.querySelector(`[name=rank_${k}]`)?.value || 0), i, k])
      .filter(([r]) => r > 0).sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(([, , k]) => k)
      .filter((k) => !(k === "seat_type" && pos === "any"));
    return {pos, order: ranked};
  };
  const fit = (s, p) => {
    const n = p.order.length;
    let tot = 0, got = 0;
    p.order.forEach((k, i) => { const w = D.base ** (n - 1 - i); tot += w; got += value(s, k, p.pos) * w; });
    return tot ? 100 * got / tot : 0;
  };
  // Southwest colour stops: [score, colour, seat meaning, text colour]
  const STOPS = [[0, "#D5152E", "Worst seat", "#FFFFFF"], [30, "#FFBF27", "Below average", "#111B40"],
                 [50, "#E2E8F0", "Standard seat", "#111B40"], [75, "#60A5FA", "Preferred seat", "#111B40"],
                 [100, "#304CB2", "Best seat", "#FFFFFF"]];
  const rgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const colour = (score) => {
    const i = STOPS.findIndex(([s]) => score <= s);
    if (i <= 0) return STOPS[0][1];
    const [s0, c0] = STOPS[i - 1], [s1, c1] = STOPS[i], t = (score - s0) / (s1 - s0), a = rgb(c0), b = rgb(c1);
    return `rgb(${a.map((v, k) => Math.round(v + (b[k] - v) * t)).join(",")})`;
  };
  const band = (score) => STOPS.reduce((best, st) => Math.abs(st[0] - score) < Math.abs(best[0] - score) ? st : best);
  const tip = document.getElementById("heat-tip");
  let tipFor = null;
  const showTip = (el) => {
    tipFor = el;
    const st = band(Number(el.dataset.fit));
    tip.innerHTML = `<b>Seat ${el.dataset.seat}: ${st[2]}</b> · score ${Math.round(el.dataset.fit)}<br>${el.dataset.desc}`;
    tip.style.background = st[1]; tip.style.color = st[3]; tip.hidden = false;
    const w = tip.parentElement.getBoundingClientRect(), r = el.getBoundingClientRect();
    tip.style.left = `${r.right - w.left + 8}px`; tip.style.top = `${r.top - w.top - 4}px`;
  };
  const isSeat = (e) => e.target.classList && e.target.classList.contains("heat-seat");
  grid.addEventListener("mouseover", (e) => isSeat(e) && showTip(e.target));
  grid.addEventListener("focusin", (e) => isSeat(e) && showTip(e.target));
  ["mouseleave", "focusout"].forEach((ev) => grid.addEventListener(ev, () => { tip.hidden = true; tipFor = null; }));
  grid.addEventListener("scroll", () => {      // follow the seat as the map scrolls; hide it once out of view
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
    const scored = seats.map((s) => ({...s, fit: fit(s, p), free: free.has(s.type)}));
    const best = Math.max(...scored.filter((s) => s.free).map((s) => s.fit), -1);
    const rows = Math.max(...seats.map((s) => s.row));
    grid.textContent = "";
    const head = document.createElement("div"); head.className = "heat-row heat-cols";
    head.innerHTML = "<span></span>" + "ABC".split("").map((c) => `<span>${c}</span>`).join("") +
      "<span></span>" + "DEF".split("").map((c) => `<span>${c}</span>`).join("");
    grid.append(head);
    for (let r = 1; r <= rows; r++) {
      const row = document.createElement("div"); row.className = "heat-row";
      const cells = ["num", ..."ABC", "num", ..."DEF"].map((c, i) => {
        const el = document.createElement("span");
        if (c === "num") { el.className = "heat-num"; el.textContent = i ? "" : r; return el; }
        const s = scored.find((x) => x.row === r && x.col === c);
        if (!s) { el.className = "heat-gap"; return el; }
        el.className = "heat-seat" + (s.free ? "" : " heat-locked") + (s.free && Math.abs(s.fit - best) < 1e-6 && best > 0 ? " heat-best" : "");
        el.style.backgroundColor = colour(s.fit);
        el.tabIndex = 0;
        el.dataset.seat = s.seat; el.dataset.fit = s.fit.toFixed(2);
        const kind = {extra: "Extra Legroom", preferred: "Preferred", standard: "Standard"}[s.type];
        el.dataset.desc = `${kind}, ${s.features.filter((f) => f !== "window without window view").join(", ")}` +
          (s.features.includes("window without window view") ? ", no window view" : "") +
          (s.free ? "" : " · not free for your status at this time");
        el.setAttribute("aria-label", `Seat ${s.seat}: ${band(s.fit)[2]}, score ${Math.round(s.fit)}`);
        return el;
      });
      row.append(...cells); grid.append(row);
    }
  };
  form.addEventListener("change", draw);
  plane.addEventListener("change", draw);
  draw();
})();
