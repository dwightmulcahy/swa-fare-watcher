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
  const fields = () => [...box.querySelectorAll("select, input")];
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

  // ---- auto-save
  let timer = null;
  const schedule = () => { clearTimeout(timer); if (saveUrl) saved.textContent = "Saving…"; timer = setTimeout(save, 700); };
  const save = async () => {
    if (!saveUrl) {
      try { localStorage.setItem(DRAFT, JSON.stringify(fields().map((f) => [f.name || f.id, f.type === "checkbox" ? f.checked : f.value]))); } catch (e) { /* storage off */ }
      saved.textContent = "Draft kept in this browser until you save";
      return;
    }
    const data = new FormData();
    data.append("csrf", form.querySelector("[name=csrf]").value);
    form.querySelectorAll("[name=seat_position]:checked").forEach((r) => data.append(r.name, r.value));
    fields().forEach((f) => { if (!f.name) return; if (f.type === "checkbox") { if (f.checked) data.append(f.name, f.value); } else data.append(f.name, f.value); });
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
