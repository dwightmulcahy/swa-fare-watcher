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

  document.querySelectorAll("[data-copy]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const text = document.getElementById(btn.dataset.copy).textContent.trim();
      try { await navigator.clipboard.writeText(text); btn.textContent = "Copied"; } catch { btn.textContent = "Select and copy"; }
    });
  });
});
