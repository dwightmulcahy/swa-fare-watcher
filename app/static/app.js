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
