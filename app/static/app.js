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

  function render(s) {
    const active = s.state === "running" || s.state === "queued";
    box.hidden = !active;
    box.classList.toggle("queued", s.state === "queued");
    if (active) {
      box.querySelector(".live-state").textContent = s.state === "running" ? "Searching:" : "Queued:";
      box.querySelector(".live-phase").textContent =
        s.state === "running" ? (s.phase || "starting…") : "waiting for the browser to be free";
      const counted = s.state === "running" && s.total && s.done;
      box.querySelector(".live-count").textContent = counted ? `search ${s.done} of ${s.total}` : "";
      box.querySelector(".live-bar span").style.width = counted ? `${Math.round((100 * s.done) / s.total)}%` : "0";
      // the watch being searched: show its live phase in its row
      document.querySelectorAll("[data-watch-status]").forEach((cell) => {
        if (s.state === "running" && String(s.watch_id) === cell.dataset.watchStatus) {
          cell.innerHTML = '<span class="pill">Running now</span><br><span class="muted"></span>';
          cell.querySelector(".muted").textContent = s.phase || "";
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
