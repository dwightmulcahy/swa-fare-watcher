// Confirm dialogs, date-mode toggle and copy button (no inline scripts: CSP).
document.addEventListener("submit", (e) => {
  const msg = e.target.dataset && e.target.dataset.confirm;
  if (msg && !window.confirm(msg)) e.preventDefault();
});

document.addEventListener("DOMContentLoaded", () => {
  const radios = document.querySelectorAll('input[name="date_mode"]');
  const sync = () => {
    const mode = document.querySelector('input[name="date_mode"]:checked');
    document.querySelectorAll("[data-mode]").forEach((el) => {
      el.hidden = !mode || el.dataset.mode !== mode.value;
    });
  };
  radios.forEach((r) => r.addEventListener("change", sync));
  if (radios.length) sync();

  // data-when="radioName:value" shows an element only while that radio choice is selected
  const whens = document.querySelectorAll("[data-when]");
  const syncWhen = () => whens.forEach((el) => {
    const [name, value] = el.dataset.when.split(":");
    const picked = document.querySelector(`input[name="${name}"]:checked`);
    el.hidden = !picked || picked.value !== value;
  });
  if (whens.length) {
    document.addEventListener("change", (e) => { if (e.target.type === "radio") syncWhen(); });
    syncWhen();
  }

  // Show/hide a password field. The saved password is never sent to the page, so this only
  // reveals what's being typed now.
  document.querySelectorAll("[data-reveal]").forEach((btn) => {
    const input = document.getElementById(btn.dataset.reveal);
    if (!input) return;
    btn.addEventListener("click", () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      btn.textContent = show ? "Hide" : "Show";
      btn.setAttribute("aria-pressed", String(show));
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
      input.focus();
    });
    // never submit or leave the page with the password showing
    input.form && input.form.addEventListener("submit", () => { input.type = "password"; });
  });

  // Several dates: pick a day, or a day and a "through" day for a range, then Add. Runs of
  // consecutive days show as one chip. The original comma-separated field stays as the hidden
  // value that's submitted.
  document.querySelectorAll("input[data-date-list]").forEach((field) => {
    const MAX = Number(field.dataset.max) || 31;   // the server's limit on dates per watch
    const label = field.closest("label");
    const box = document.createElement("div");
    box.className = "date-list";
    const chips = document.createElement("ul");
    chips.className = "chips";
    chips.setAttribute("aria-live", "polite");
    const mk = (tag, props) => Object.assign(document.createElement(tag), props);
    const from = mk("input", { type: "date" });
    const thru = mk("input", { type: "date" });
    [from, thru].forEach((i) => { if (field.dataset.min) i.min = field.dataset.min; });
    from.setAttribute("aria-label", "Date, or first day of a range");
    thru.setAttribute("aria-label", "Last day of the range (optional)");
    const add = mk("button", { type: "button", className: "btn small", textContent: "Add" });
    const msg = mk("span", { className: "hint", role: "status" });
    const row = mk("div", { className: "date-add" });
    row.append(from, mk("span", { className: "muted small", textContent: "through" }), thru, add);
    box.append(chips, row, msg);
    label.after(box);
    field.type = "hidden";
    const hint = label.querySelector(".hint");
    if (hint) hint.textContent = hint.textContent.replace("YYYY-MM-DD, separated by commas.",
      "Pick a day, or a first and last day for a range, then Add. Add as many as you like.");

    const DAY = 864e5;
    const toDate = (d) => new Date(d + "T00:00:00Z");
    const iso = (t) => new Date(t).toISOString().slice(0, 10);
    const read = () => field.value.split(/[\s,]+/).filter((d) => /^\d{4}-\d{2}-\d{2}$/.test(d));
    const thisYear = String(new Date().getFullYear());
    const opts = { weekday: "short", month: "short", day: "numeric", timeZone: "UTC" };
    const short = new Intl.DateTimeFormat(undefined, opts);
    const long = new Intl.DateTimeFormat(undefined, { ...opts, year: "numeric" });
    const fmt = (d) => (d.startsWith(thisYear) ? short : long).format(toDate(d));
    const runs = (days) => days.reduce((out, d) => {
      const last = out[out.length - 1];
      if (last && toDate(d) - toDate(last[last.length - 1]) === DAY) last.push(d); else out.push([d]);
      return out;
    }, []);
    const draw = () => {
      const days = [...new Set(read())].sort();
      field.value = days.join(", ");
      chips.replaceChildren(...runs(days).map((run) => {
        const first = run[0], last = run[run.length - 1];
        const li = mk("li", { className: "chip date-chip" });
        li.append(run.length === 1 ? fmt(first) : `${fmt(first)} – ${fmt(last)} (${run.length} days)`, " ");
        const x = mk("button", { type: "button", className: "chip-x", textContent: "×" });
        x.setAttribute("aria-label", run.length === 1 ? `Remove ${first}` : `Remove ${first} through ${last}`);
        x.addEventListener("click", () => { field.value = read().filter((v) => !run.includes(v)).join(","); draw(); from.focus(); });
        li.append(x);
        return li;
      }));
      if (!days.length) chips.append(mk("li", { className: "muted small", textContent: "No dates yet" }));
    };
    const addPicked = () => {
      msg.textContent = "";
      if (!from.value) { msg.textContent = "Pick a date first."; from.focus(); return; }
      let [a, b] = [from.value, thru.value || from.value].sort();
      const n = (toDate(b) - toDate(a)) / DAY + 1;
      const added = Array.from({ length: n }, (_, i) => iso(toDate(a).getTime() + i * DAY));
      const total = new Set([...read(), ...added]).size;
      if (total > MAX) { msg.textContent = `That would make ${total} dates; the limit is ${MAX}.`; return; }
      field.value = [...read(), ...added].join(",");
      from.value = thru.value = "";
      draw();
      from.focus();
    };
    // picking a start day suggests it as the range start so the "through" calendar opens near it
    from.addEventListener("change", () => { if (from.value) thru.min = from.value; });
    add.addEventListener("click", addPicked);
    [from, thru].forEach((i) => i.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addPicked(); } }));
    draw();
  });

  // Airport names under the From/To codes, updated as you type
  const nameBoxes = document.querySelectorAll("[data-airport-names]");
  if (nameBoxes.length) {
    let table = null;
    const load = () => table || (table = fetch("/static/airports.json").then((r) => r.json()).catch(() => ({})));
    nameBoxes.forEach((box) => {
      const input = document.querySelector(`input[name="${box.dataset.airportNames}"]`);
      if (!input) return;
      const show = async () => {
        const codes = [...new Set(input.value.toUpperCase().split(/[\s,/]+/).filter((c) => c.length === 3))];
        const t = await load();
        box.replaceChildren(...codes.map((c) => {
          const a = t[c];
          const line = document.createElement("span");
          if (!a) line.className = "unknown";
          line.append(Object.assign(document.createElement("b"), { textContent: c }), `: ${a ? `${a[0]}, ${a[1]}` : "airport not found"}`);
          if (a) line.title = a[2];
          return line;
        }));
      };
      input.addEventListener("input", show);
    });
  }

  // Rolling window: show how far ahead the picked dates are, since that's what's kept
  const aheadHint = document.querySelector("[data-ahead-hint]");
  if (aheadHint) {
    const today = new Date(aheadHint.dataset.today + "T00:00:00Z");
    const ws = document.querySelector('[data-ahead="ws"]'), we = document.querySelector('[data-ahead="we"]');
    const days = (el) => el.value ? Math.round((new Date(el.value + "T00:00:00Z") - today) / 864e5) : null;
    const show = () => {
      const a = days(ws), b = days(we);
      aheadHint.textContent = (a === null || b === null) ? "Pick a start and end date."
        : b < a ? "The end date must be on or after the start date."
        : `Searches ${a}–${b} days ahead, rolling forward each day (${b - a + 1} departure date${b === a ? "" : "s"}).`;
    };
    ws.addEventListener("input", show);
    we.addEventListener("input", show);
    show();
  }

  // Sortable tables: click a column title to sort; click again to reverse. Cells may carry a
  // data-sort value (raw number or ISO date); blanks always sort last.
  document.querySelectorAll("table[data-sortable]").forEach((table) => {
    const body = table.tBodies[0];
    if (!body) return;
    const heads = [...table.tHead.rows[0].cells];
    const key = (row, i) => {
      const c = row.cells[i];
      if (!c) return null;
      const raw = (c.dataset.sort ?? c.textContent).trim();
      if (raw === "" || raw === "—") return null;
      const n = Number(raw.replace(/[$,¢\s]/g, ""));
      return Number.isFinite(n) && /^[\s$\d.,¢-]+$/.test(raw) ? n : raw.toLowerCase();
    };
    heads.forEach((th, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "sort";
      btn.append(...th.childNodes);
      btn.append(Object.assign(document.createElement("span"), { className: "sort-arrow", ariaHidden: "true" }));
      th.append(btn);
      btn.addEventListener("click", () => {
        const dir = th.getAttribute("aria-sort") === "ascending" ? -1 : 1;
        heads.forEach((h) => h.removeAttribute("aria-sort"));
        th.setAttribute("aria-sort", dir === 1 ? "ascending" : "descending");
        const rows = [...body.rows].map((r, n) => [r, key(r, i), n]);
        rows.sort(([, a, x], [, b, y]) => {
          if (a === null || b === null) return a === b ? x - y : a === null ? 1 : -1;
          if (typeof a !== typeof b) return (typeof a === "number" ? -1 : 1) * dir;
          return (a < b ? -1 : a > b ? 1 : x - y) * dir;
        });
        body.append(...rows.map(([r]) => r));
      });
    });
  });

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = document.getElementById(btn.dataset.copy).textContent.trim();
      try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; } catch { btn.textContent = "Select and copy"; }
    });
  });
});

// ---------- live run status (all signed-in pages) ----------
(() => {
  const box = document.getElementById("live");
  if (!box) return;
  const reloadOnFinish = document.body.hasAttribute("data-reload-on-finish");
  let wasActive = false;
  let timer = null;

  // A pause counts down every second between status polls (the server sends when it ends)
  let tick = null;
  const phaseText = (s) => {
    if (s.state !== "running") return "waiting for the browser to be free";
    if (s.until) {
      const left = Math.max(0, Math.ceil(s.until - Date.now() / 1000));
      return left > 0 ? `Pausing before the next search: ${fmtLeft(left)}` : "Starting the next search…";
    }
    return s.phase || "starting…";
  };
  const fmtLeft = (n) => (n >= 60 ? `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}` : `${n} s`);

  function render(s) {
    clearInterval(tick);
    if (s.state === "running" && s.until) {
      tick = setInterval(() => {
        const t = phaseText(s);
        box.querySelector(".live-phase").textContent = t;
        document.querySelectorAll(`[data-watch-status="${s.watch_id}"] .muted`).forEach((el) => { el.textContent = t; });
      }, 1000);
    }
    const active = s.state === "running" || s.state === "queued";
    box.hidden = !active;
    box.classList.toggle("queued", s.state === "queued");
    if (active) {
      box.querySelector(".live-state").textContent = s.state === "running" ? "Searching:" : "Queued:";
      box.querySelector(".live-phase").textContent = phaseText(s);
      const counted = s.state === "running" && s.total && s.done;
      box.querySelector(".live-count").textContent = counted ? `search ${s.done} of ${s.total}` : "";
      box.querySelector(".live-bar span").style.width = counted ? `${Math.round((100 * s.done) / s.total)}%` : "0";
      // the watch being searched: show its live phase in its row
      document.querySelectorAll("[data-watch-status]").forEach((cell) => {
        if (s.state === "running" && String(s.watch_id) === cell.dataset.watchStatus) {
          cell.innerHTML = '<span class="pill">Running now</span><br><span class="muted"></span>';
          cell.querySelector(".muted").textContent = phaseText(s);
        }
      });
    }
    if (wasActive && !active && reloadOnFinish) location.reload();
    wasActive = active;
    return active;
  }

  async function poll() {
    let active = false;
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      if (r.ok) active = render(await r.json());
    } catch { /* offline for a moment: try again later */ }
    // quick while something is happening; slow otherwise (catches scheduled runs starting)
    timer = setTimeout(poll, active ? 2500 : 15000);
  }
  poll();
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) { clearTimeout(timer); poll(); }
  });
})();

// ---------- log page: live tail ----------
(() => {
  const table = document.getElementById("log-table");
  if (!table) return;
  const toggle = document.getElementById("log-live");
  const tbody = table.tBodies[0];
  const admin = table.dataset.admin === "1";
  const params = new URLSearchParams(location.search);
  const lastId = () => Math.max(0, ...[...tbody.querySelectorAll("tr[data-id]")].map((r) => +r.dataset.id));
  const cell = (text, cls) => { const td = document.createElement("td"); if (cls) td.className = cls; td.textContent = text; return td; };

  function row(r) {
    const tr = document.createElement("tr");
    tr.dataset.id = r.id;
    tr.className = `lvl-${r.level} fresh`;
    const t = (r.ts || "").replace("T", " ");
    tr.append(cell(t.slice(0, 16), "nowrap small"));
    tr.lastChild.append(Object.assign(document.createElement("span"), { className: "muted", textContent: t.slice(16, 19) }));
    const lvl = document.createElement("td");
    lvl.append(Object.assign(document.createElement("span"), {
      className: `pill ${{ error: "bad", warn: "warn" }[r.level] || ""}`, textContent: r.level }));
    tr.append(lvl, cell(r.source, "small"));
    if (admin) tr.append(cell(r.username || "—", "small"));
    const msg = document.createElement("td");
    msg.className = "msg";
    const lines = (r.message || "").split("\n");
    if (lines.length > 1) {
      const d = document.createElement("details");
      d.append(Object.assign(document.createElement("summary"), { textContent: lines[0] }),
               Object.assign(document.createElement("pre"), { textContent: lines.slice(1).join("\n") }));
      msg.append(d);
    } else msg.textContent = r.message;
    if (r.shot_url) {
      const d = Object.assign(document.createElement("details"), { className: "shot" });
      const a = Object.assign(document.createElement("a"), { href: r.shot_url, target: "_blank", rel: "noopener" });
      a.append(Object.assign(document.createElement("img"), { src: r.shot_url, alt: "What the browser showed", loading: "lazy" }));
      d.append(Object.assign(document.createElement("summary"), { textContent: "Screenshot" }), a);
      msg.append(d);
    }
    tr.append(msg);
    return tr;
  }

  async function tail() {
    if (toggle.checked && !document.hidden) {
      params.set("after", lastId());
      try {
        const r = await fetch(`/api/log?${params}`, { cache: "no-store" });
        if (r.ok) {
          const { rows } = await r.json();
          if (rows.length) tbody.querySelector(".empty-row")?.remove();
          rows.reverse().forEach((x) => tbody.prepend(row(x)));  // rows come newest first
        }
      } catch { /* retry next tick */ }
    }
    setTimeout(tail, 3000);
  }
  setTimeout(tail, 3000);
})();
