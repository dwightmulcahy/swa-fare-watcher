// Version label + About dialog (ported from repo-template). Build info comes from the
// <script id="build-info"> JSON the server embeds (app/buildinfo.py); the server start time
// from GET /api/about.

export function readBuildInfo(doc = document) {
  try {
    const b = JSON.parse(doc.getElementById('build-info')?.textContent || '{}');
    return b && typeof b.version === 'string' ? b : null;
  } catch {
    return null;
  }
}

export const buildLabel = (b) => (b.version === 'dev' ? `dev${b.commit ? ' ' + b.commit : ''}` : `v${b.version}`);

const fmt = (iso) => {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
};

const el = (tag, props = {}, ...children) => {
  const n = Object.assign(document.createElement(tag), props);
  n.append(...children);
  return n;
};

const link = (href, text) => el('a', { href, textContent: text, target: '_blank', rel: 'noopener' });

async function serverStartedAt() {
  try {
    const r = await fetch('/api/about', { cache: 'no-store' });
    return r.ok ? (await r.json()).started_at || '' : '';
  } catch {
    return '';
  }
}

export async function openAbout(info, appName = document.title) {
  const started = await serverStartedAt();
  const rows = [
    ['Version', info.version === 'dev' ? 'dev (local build)' : `v${info.version}`],
    ['Commit', info.commit],
    ['Edition', { web: 'Web (Docker / server)', offline: 'Offline' }[info.edition] || info.edition],
    ['Built', info.builtAt && fmt(info.builtAt)],
    ['Server up since', started && fmt(started)],
  ].filter(([, v]) => v);
  const text = [appName, ...rows.map(([k, v]) => `${k}: ${v}`), `Browser: ${navigator.userAgent}`].join('\n');

  const dl = el('dl', { className: 'about-kv' });
  for (const [k, v] of rows) {
    const value = k === 'Commit' && info.repository ? link(`${info.repository}/commit/${v}`, v) : v;
    dl.append(el('dt', { textContent: k }), el('dd', {}, value));
  }
  const links = el('p', { className: 'about-links' });
  if (info.repository) {
    const notes = info.version === 'dev' ? `${info.repository}/commits` : `${info.repository}/releases/tag/v${encodeURIComponent(info.version)}`;
    links.append(link(notes, info.version === 'dev' ? 'Recent changes' : 'Release notes'), ' · ', link(`${info.repository}/issues/new`, 'Report a problem'));
  }
  const status = el('p', { className: 'about-status', role: 'status' });
  const copy = el('button', { type: 'button', textContent: 'Copy details' });
  const close = el('button', { type: 'button', textContent: 'Close', className: 'primary' });
  const dlg = el('dialog', { className: 'about', ariaLabel: `About ${appName}` },
    el('h2', { textContent: appName }), dl, links, status, el('div', { className: 'about-actions' }, copy, close));
  copy.addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(text); status.textContent = 'Copied.'; } catch { status.textContent = "Couldn't copy. Select the text instead."; }
  });
  close.addEventListener('click', () => dlg.close());
  dlg.addEventListener('close', () => dlg.remove());
  document.body.append(dlg);
  dlg.showModal();
  return dlg;
}

export function initVersionLabel(button = document.getElementById('buildVersion'), appName = undefined) {
  const info = readBuildInfo();
  if (!button || !info) return;
  button.textContent = buildLabel(info);
  button.title = 'About this build';
  button.setAttribute('aria-label', `Version ${buildLabel(info)}: show build details`);
  button.hidden = false;
  button.addEventListener('click', () => openAbout(info, appName));
}
