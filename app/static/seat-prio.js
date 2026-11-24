// Seat Selection Priorities on the seat watch form: drag (or ↑/↓) to reorder, Restore Defaults,
// rows sorted by priority as they change, and auto-save (an existing watch saves to the server;
// a new one keeps a draft in this browser until it's saved).
(() => {
  const box = document.getElementById("seat-prio");
  if (!box) return;
  const form = box.closest("form"), list = document.getElementById("prio-list"),
        ties = document.getElementById("tie-list"), tieOrder = document.getElementById("tiebreak-order"),
        saved = document.getElementById("prio-saved"), defaults = JSON.parse(box.dataset.defaults),
        saveUrl = box.dataset.save, DRAFT = "swa-seat-prio-draft";
  const fields = () => [...box.querySelectorAll("select, input")].filter((f) => !f.closest("#prio-presets"));
  const changed = () => { form.dispatchEvent(new Event("prio-change")); schedule(); };

  // keep the list in priority order (Off last), stable for equal priorities
  const sortRows = () => {
    const rows = [...list.children];
    const val = (li) => Number(li.querySelector("select").value) || 99;
    rows.map((li, i) => [val(li), i, li]).sort((a, b) => a[0] - b[0] || a[1] - b[1]).forEach(([, , li]) => list.append(li));
  };
  // after a drag or move: number the enabled rows 1, 2, 3 ... in their new order
  const renumber = () => {
    let n = 0;
    [...list.children].forEach((li) => { const sel = li.querySelector("select"); if (sel.value) sel.value = String(++n); });
  };
  const syncTies = () => { tieOrder.value = [...ties.children].map((li) => li.dataset.key).join(","); };

  const dragging = (ol, after) => {
    let drag = null;
    ol.addEventListener("dragstart", (e) => { drag = e.target.closest("li"); e.dataTransfer.effectAllowed = "move"; drag.classList.add("dragging"); });
    ol.addEventListener("dragend", () => { if (drag) drag.classList.remove("dragging"); drag = null; });
    ol.addEventListener("dragover", (e) => {
      if (!drag) return;
      e.preventDefault();
      const over = e.target.closest("li");
      if (!over || over === drag) return;
      const r = over.getBoundingClientRect();
      ol.insertBefore(drag, e.clientY > r.top + r.height / 2 ? over.nextSibling : over);
    });
    ol.addEventListener("drop", (e) => { e.preventDefault(); after(); changed(); });
    ol.addEventListener("click", (e) => {
      const b = e.target.closest("[data-move]");
      if (!b) return;
      const li = b.closest("li"), step = Number(b.dataset.move);
      const sib = step < 0 ? li.previousElementSibling : li.nextElementSibling;
      if (!sib) return;
      ol.insertBefore(li, step < 0 ? sib : sib.nextSibling);
      after(); changed(); b.focus();
    });
  };
  dragging(list, renumber);
  dragging(ties, syncTies);

  list.addEventListener("change", (e) => { if (e.target.matches("select")) { sortRows(); } });
  document.getElementById("prio-defaults").addEventListener("click", () => {
    [...list.children].forEach((li) => { li.querySelector("select").value = String(defaults[li.dataset.key]); });
    sortRows(); changed();
  });

  // the priorities (and window/aisle choice) as form fields, for auto-save and presets
  function formData() {
    const data = new FormData();
    data.append("csrf", form.querySelector("[name=csrf]").value);
    form.querySelectorAll("[name=seat_position]:checked").forEach((r) => data.append(r.name, r.value));
    fields().forEach((f) => { if (!f.name) return; if (f.type === "checkbox") { if (f.checked) data.append(f.name, f.value); } else data.append(f.name, f.value); });
    return data;
  }

  // ---- presets: built-in and saved Seat Selection Priorities
  const pbox = document.getElementById("prio-presets");
  const pick = document.getElementById("preset-pick"), pname = document.getElementById("preset-name"),
        pdel = document.getElementById("preset-delete");
  let presets = JSON.parse(pbox.dataset.presets || "[]");
  ["change", "input"].forEach((ev) => pbox.addEventListener(ev, (e) => e.stopPropagation()));  // not a prefs edit
  const fillPick = (keep) => {
    pick.replaceChildren(new Option("Choose a preset…", ""));
    const groups = [["Ready-made", presets.filter((p) => !p.id)], ["Saved", presets.filter((p) => p.id)]];
    groups.forEach(([label, ps]) => {
      if (!ps.length) return;
      const g = document.createElement("optgroup"); g.label = label;
      ps.forEach((p) => g.append(new Option(p.name, p.name)));
      pick.append(g);
    });
    pick.value = keep || "";
    const cur = presets.find((p) => p.name === pick.value);
    pdel.hidden = !(cur && cur.id);
  };
  const apply = (prefs) => {
    [...list.children].forEach((li) => {
      const v = prefs.priorities[li.dataset.key];
      li.querySelector("select").value = v ? String(v) : "";
    });
    const pos = form.querySelector(`[name=seat_position][value="${prefs.position}"]`);
    if (pos) pos.checked = true;
    box.querySelector("[name=seat_fallback]").checked = !!prefs.fallback;
    box.querySelector("[name=seat_explain]").checked = !!prefs.explain;
    const on = prefs.tiebreak || [];
    [...on, ...[...ties.children].map((li) => li.dataset.key).filter((k) => !on.includes(k))].forEach((k) => {
      const li = ties.querySelector(`[data-key="${k}"]`);
      if (li) { ties.append(li); li.querySelector("input").checked = on.includes(k); }
    });
    syncTies(); sortRows(); changed();
  };
  const post = async (url, extra) => {
    const data = formData();
    Object.entries(extra || {}).forEach(([k, v]) => data.append(k, v));
    const r = await fetch(url, {method: "POST", body: data, credentials: "same-origin"});
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.error || "Couldn't save the preset");
    presets = j.presets;
  };
  pick.addEventListener("change", () => {
    const p = presets.find((x) => x.name === pick.value);
    pdel.hidden = !(p && p.id);
    if (p) { apply(p.prefs); pname.value = p.id ? p.name : ""; saved.textContent = `Loaded "${p.name}"`; }
  });
  document.getElementById("preset-save").addEventListener("click", async () => {
    const name = pname.value.trim();
    if (!name) { pname.focus(); saved.textContent = "Name the preset first"; return; }
    try { await post("/seat-presets", {preset_name: name}); fillPick(name); saved.textContent = `Saved preset "${name}"`; }
    catch (e) { saved.textContent = e.message; }
  });
  pdel.addEventListener("click", async () => {
    const p = presets.find((x) => x.name === pick.value);
    if (!p || !p.id) return;
    try { await post(`/seat-presets/${p.id}/delete`); fillPick(""); pname.value = ""; saved.textContent = `Deleted preset "${p.name}"`; }
    catch (e) { saved.textContent = e.message; }
  });
  fillPick("");

  // ---- auto-save
  let timer = null;
  const schedule = () => { clearTimeout(timer); if (saveUrl) saved.textContent = "Saving…"; timer = setTimeout(save, 700); };
  const save = async () => {
    if (!saveUrl) {
      try { localStorage.setItem(DRAFT, JSON.stringify(fields().map((f) => [f.name || f.id, f.type === "checkbox" ? f.checked : f.value]))); } catch (e) { /* storage off */ }
      saved.textContent = "Draft kept in this browser until you save";
      return;
    }
    const data = formData();
    try {
      const r = await fetch(saveUrl, {method: "POST", body: data, credentials: "same-origin"});
      saved.textContent = r.ok ? "Saved" : "Couldn't save: press Save below";
    } catch (e) { saved.textContent = "Couldn't save: press Save below"; }
  };
  box.addEventListener("change", changed);
  form.addEventListener("change", (e) => { if (e.target.name === "seat_position") schedule(); });
  form.addEventListener("submit", () => { try { localStorage.removeItem(DRAFT); } catch (e) { /* storage off */ } });
  if (!saveUrl) {          // a new watch: bring back an unsaved draft
    try {
      const d = JSON.parse(localStorage.getItem(DRAFT) || "null");
      if (d) {
        const byName = Object.fromEntries(d);
        fields().forEach((f) => { const k = f.name || f.id; if (!(k in byName)) return; if (f.type === "checkbox") f.checked = byName[k]; else f.value = byName[k]; });
        sortRows(); syncTies();
        const order = (tieOrder.value || "").split(",");
        order.forEach((k) => { const li = ties.querySelector(`[data-key="${k}"]`); if (li) ties.append(li); });
        saved.textContent = "Restored your unsaved draft";
        form.dispatchEvent(new Event("prio-change"));
      }
    } catch (e) { /* storage off */ }
  }
})();
