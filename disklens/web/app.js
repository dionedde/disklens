'use strict';
/* DiskLens UI - vanilla JS + ECharts, talks to the local Python API.
   Texts are written in Portuguese and translated with t() (catalog: disklens/locales.json). */

const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ------------------------------------------------------------------ i18n */
const I18N = { lang: 'pt', locale: 'pt-BR', table: {}, langs: {} };
function t(s, p) {
  let o = I18N.table[s] ?? s;
  if (p) o = o.replace(/\{(\w+)\}/g, (m, k) => (k in p ? p[k] : m));
  return o;
}
const LOC = () => I18N.locale;

function fmt(b) {
  if (b == null) return '—';
  const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0, v = b;
  while (Math.abs(v) >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  if (i === 0) return v.toFixed(0) + ' B';
  return v.toLocaleString(LOC(), { maximumFractionDigits: v < 10 ? 2 : 1, minimumFractionDigits: v < 10 ? 1 : 0 }) + ' ' + u[i];
}
const fmtN = n => (n ?? 0).toLocaleString(LOC());
const fmtDate = ts => ts ? new Date(ts * 1000).toLocaleString(LOC(), { dateStyle: 'short', timeStyle: 'short' }) : '—';
const pct = (a, b) => b ? (a / b * 100).toLocaleString(LOC(), { maximumFractionDigits: 1 }) + '%' : '—';
function ago(ts) {
  if (!ts) return '—';
  const d = (Date.now() / 1000 - ts) / 86400;
  const rtf = new Intl.RelativeTimeFormat(LOC(), { numeric: 'auto' });
  if (d < 1) return rtf.format(0, 'day');
  if (d < 30) return rtf.format(-Math.floor(d), 'day');
  if (d < 365) return rtf.format(-Math.floor(d / 30), 'month');
  return rtf.format(-Math.floor(d / 365), 'year');
}
function dur(s) { s = Math.round(s); const m = Math.floor(s / 60); return m ? `${m}min ${String(s % 60).padStart(2, '0')}s` : `${s}s`; }

async function api(path, body) {
  const r = await fetch('/api/' + path, {
    method: body ? 'POST' : 'GET',
    headers: { 'X-DiskLens-Token': TOKEN, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
function toast(msg) {
  const el = $('#toast'); el.textContent = msg; el.hidden = false;
  clearTimeout(toast._t); toast._t = setTimeout(() => el.hidden = true, 3500);
}
const cssVar = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const ICON = {
  dir: '<svg class="ico" width="15" height="15" viewBox="0 0 16 16"><path fill="#e8b84a" d="M1 3.5A1.5 1.5 0 0 1 2.5 2h3.6l1.5 1.6h5.9A1.5 1.5 0 0 1 15 5.1v7.4a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 1 12.5z"/></svg>',
  file: '<svg class="ico" width="15" height="15" viewBox="0 0 16 16"><path fill="none" stroke="currentColor" stroke-width="1.2" d="M3.5 1.5h6l3 3v10h-9z M9.5 1.5v3h3"/></svg>',
};
const FLAG = { DENIED: 1, HARDLINK: 4, HARDLINK_DUP: 8, CLOUD: 16, COMPRESSED: 32 };

const S = {
  state: null, volume: null, snap: null, cats: {}, safety: {}, tab: 'overview',
  dir: 1, view: 'treemap', colorBy: 'cat', levels: 2, selFile: null, dirData: null, treeData: null,
  files: { q: '', ext: '', cat: '', min: 0, under: null, underPath: '' },
  wasRunning: false, chart: null,
};

/* ------------------------------------------------------------------ state & header */
async function refreshState(selectNewest) {
  S.state = await api('state');
  if (!S.volume) S.volume = S.state.system_drive;
  renderPickers(selectNewest);
  renderAdmin();
  renderScan();
}

function snapsForVolume() {
  return S.state.snapshots.filter(s => s.status === 'done' && s.volume === S.volume);
}

function renderPickers(selectNewest) {
  const vs = $('#volSel');
  vs.innerHTML = S.state.volumes.map(v =>
    `<option value="${v.volume}" ${v.volume === S.volume ? 'selected' : ''}>${v.volume} ${esc(v.label || '')} · ${t('{free} livres de {total}', { free: fmt(v.free), total: fmt(v.total) })}</option>`).join('');
  const snaps = snapsForVolume();
  const ss = $('#snapSel');
  if (!snaps.length) { ss.innerHTML = `<option>${t('nenhum ainda')}</option>`; ss.disabled = true; }
  else {
    ss.disabled = false;
    if (selectNewest || !S.snap || !snaps.find(s => s.id === S.snap.id)) S.snap = snaps[0];
    ss.innerHTML = snaps.map(s => `<option value="${s.id}" ${s.id === S.snap.id ? 'selected' : ''}>${fmtDate(s.finished)} · ${t('{size} usados', { size: fmt(s.used) })}${s.compacted ? ' ' + t('(só pastas)') : ''}</option>`).join('');
  }
  if (!snaps.length) S.snap = null;
}

function renderAdmin() {
  const bar = $('#adminBar');
  if (S.state.admin) { bar.hidden = true; return; }
  bar.hidden = false;
  bar.innerHTML = `<span>⚠ ${t('Rodando <b>sem permissão de administrador</b>: pastas protegidas (outros usuários, partes do Windows) não são lidas e o espaço delas aparece como “Não contabilizado”.')}</span>
    <button class="btn small" id="elevBtn">${t('Reabrir como administrador')}</button>`;
  $('#elevBtn').onclick = async () => {
    const r = await api('elevate', {}).catch(e => ({ ok: false }));
    toast(r.ok ? t('Abrindo como administrador…') : t('Permissão não concedida.'));
  };
}

function renderScan() {
  const box = $('#scanBox');
  const st = S.state.scan;
  const running = S.state.running;
  if (running && st) {
    const vol = S.state.volumes.find(v => v.volume === st.volume);
    const p = vol ? Math.min(99, st.bytes / vol.used * 100) : 0;
    const paused = st.paused;
    box.innerHTML = `
      <div class="scan ${paused ? 'paused' : ''}">
        <div class="scan-line"><span class="dot"></span><b>${paused ? t('Pausado') : t('Escaneando')} ${esc(st.volume)}</b>
          <span class="muted">${t('{n} arquivos', { n: fmtN(st.files) })} · ${fmt(st.bytes)} · ${dur(st.elapsed || 0)} · ${t('CPU de outros programas: {pct}%', { pct: st.cpu_other ?? 0 })}</span></div>
        <div class="progress"><i style="width:${p.toFixed(1)}%"></i></div>
        ${paused ? `<div class="pausenote">⏸ ${esc(st.pause_reason || t('Computador ocupado'))}. ${t('Retoma sozinho quando o PC estiver livre.')}
             <button class="btn small" id="forceBtn">${t('Continuar mesmo assim')}</button></div>`
               : `<div class="scan-sub">${esc(st.step || st.current || '')}</div>`}
      </div>
      <button class="btn small" id="cancelBtn">${t('Cancelar')}</button>`;
    $('#cancelBtn').onclick = () => api('scan/control', { action: 'cancel' }).then(() => toast(t('Cancelando…')));
    const fb = $('#forceBtn');
    if (fb) fb.onclick = () => api('scan/control', { action: 'force' }).then(() => toast(t('Continuando com prioridade baixa.')));
  } else {
    const last = snapsForVolume()[0];
    box.innerHTML = `<span class="muted" style="font-size:12.5px">${last ? t('Último scan: {when}', { when: fmtDate(last.finished) }) : ''}</span>
      <button class="btn primary" id="scanBtn">${t('Escanear {drive} agora', { drive: esc(S.volume) })}</button>`;
    $('#scanBtn').onclick = startScan;
  }
  if (running) startPolling();
}

async function startScan() {
  try { await api('scan', { volume: S.volume }); }
  catch (e) { toast(e.message); return; }
  S.wasRunning = true;
  await refreshState(false);
  if (!S.snap) render();
}

let pollTimer = null;
function startPolling() {
  if (pollTimer) return;
  S.wasRunning = true;
  pollTimer = setInterval(async () => {
    try { S.state = await api('state'); } catch { return; }
    renderScan();
    if (!S.state.running) {
      clearInterval(pollTimer); pollTimer = null;
      if (S.wasRunning) {
        S.wasRunning = false;
        const st = S.state.scan || {};
        if (st.state === 'done') { toast(t('Scan concluído!')); S.dir = 1; }
        else if (st.state === 'cancelled') toast(t('Scan cancelado.'));
        else if (st.state === 'error') toast(t('Erro no scan: {error}', { error: st.error || '' }));
        await refreshState(true);
        render();
      }
    }
  }, 1000);
}

/* ------------------------------------------------------------------ tabs */
function setTab(tab) {
  S.tab = tab;
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  render();
}

function render() {
  disposeChart();
  const main = $('#main');
  main.classList.toggle('full', S.tab === 'explore');
  if (S.tab === 'timeline') return renderTimeline();
  if (S.tab === 'cleanup') return renderCleanup();
  if (!S.snap) return renderEmpty();
  if (S.tab === 'overview') return renderOverview();
  if (S.tab === 'explore') return renderExplore();
  if (S.tab === 'files') return renderFiles();
  if (S.tab === 'types') return renderTypes();
}

function disposeChart() {
  if (S.chart) { S.chart.dispose(); S.chart = null; }
  if (S.chart2) { S.chart2.dispose(); S.chart2 = null; }
  if (S.chart3) { S.chart3.dispose(); S.chart3 = null; }
}
function loading() { $('#main').innerHTML = '<div class="spinner"></div>'; }
function showError(e) { $('#main').innerHTML = `<div class="empty"><h1>${t('Algo deu errado')}</h1><p>${esc(e.message)}</p></div>`; }

function renderEmpty() {
  const running = S.state.running;
  $('#main').innerHTML = `<div class="empty">
    <h1>${running ? t('Primeiro scan em andamento…') : t('Ainda não há um retrato do disco {drive}', { drive: esc(S.volume) })}</h1>
    <p>${t('O DiskLens lê todas as pastas do disco e monta um mapa do que ocupa espaço, do nível macro (categorias) até cada arquivo.')}</p>
    <ul>
      <li>${t('Leva de 1 a 5 minutos e roda com <b>prioridade mínima</b>.')}</li>
      <li>${t('Se você abrir um jogo ou o processador ficar ocupado, o scan <b>pausa sozinho</b> e continua depois.')}</li>
      <li>${t('Nada é apagado ou alterado: o DiskLens só lê.')}</li>
    </ul>
    ${running ? '' : `<p><button class="btn primary big" id="emptyScan">${t('Escanear {drive} agora', { drive: esc(S.volume) })}</button></p>`}
  </div>`;
  const b = $('#emptyScan'); if (b) b.onclick = startScan;
}

const catColor = c => (S.cats[c] || {}).color || '#a0a7b4';
const catLabel = c => (S.cats[c] || {}).label || c;

/* ------------------------------------------------------------------ overview */
async function renderOverview() {
  loading();
  let o;
  try { o = await api(`overview?snap=${S.snap.id}`); } catch (e) { return showError(e); }
  S.cats = o.categories; S.safety = o.safety;
  const s = o.snapshot;
  const total = s.total, used = s.used, free = s.free;
  const unacc = Math.max(0, used - s.scanned);
  const cats = Object.entries(o.cat_stats).filter(([, v]) => v.alloc > 0).sort((a, b) => b[1].alloc - a[1].alloc);

  const hid = o.hidden;
  const hidItems = hid ? hid.items.filter(i => !i.in_scan && i.bytes > 0) : [{ key: 'unknown', label: t('Não contabilizado'), bytes: unacc }];
  const segs = cats.map(([c, v]) => `<div style="width:${v.alloc / total * 100}%;background:${catColor(c)}" title="${esc(catLabel(c))}: ${fmt(v.alloc)}" data-cat="${c}"></div>`).join('')
    + hidItems.map(i => `<div style="width:${i.bytes / total * 100}%;background:${HID[i.key] || HID.unknown}" title="${esc(i.label)}: ${fmt(i.bytes)}" data-cat="__hidden"></div>`).join('');
  const legend = cats.map(([c, v]) => `<span><i class="sw" style="background:${catColor(c)}"></i>${esc(catLabel(c))} <b>${fmt(v.alloc)}</b></span>`).join('')
    + hidItems.map(i => `<span><i class="sw" style="background:${HID[i.key] || HID.unknown}"></i>${esc(i.label)} <b>${fmt(i.bytes)}</b></span>`).join('')
    + `<span><i class="sw" style="background:var(--free);border:1px solid var(--line)"></i>${t('Livre')} <b>${fmt(free)}</b></span>`;

  const callouts = [];
  callouts.push(hid ? hiddenCard(hid, s) : `<div class="callout warn" id="unaccCard">
    <h3>${t('Não contabilizado: {size} ({pct} do usado)', { size: fmt(unacc), pct: pct(unacc, used) })}</h3>
    <div class="muted">${t('Este snapshot é anterior à medição de áreas ocultas. Faça um novo scan para ver o detalhamento.')}</div></div>`);
  callouts.push(reclaimCard(o.reclaim, used));
  const tips = tipsCard(hid, o, s);
  if (tips) callouts.push(tips);
  callouts.push(`<div class="callout"><h3>${t('Sobre este snapshot')}</h3>
    <div>${t('{files} arquivos em {dirs} pastas · escaneado em {time}', { files: fmtN(s.nfiles), dirs: fmtN(s.ndirs), time: dur(s.finished - s.started) })}${s.seconds_paused > 1 ? ' ' + t('(pausado por {time} para não atrapalhar você)', { time: dur(s.seconds_paused) }) : ''}.</div>
    <div class="muted" style="margin-top:4px">${t('Os tamanhos mostram o <b>espaço real ocupado</b>: arquivos “somente online” do OneDrive contam zero, arquivos comprimidos contam o tamanho comprimido e hard links do Windows contam uma vez só. Por isso os números podem diferir do Explorer.')}</div></div>`);

  const cards = cats.map(([c, v]) => {
    const items = (o.tops[c] || []).map(it => `
      <li data-dir="${it.id}" ${it.fid != null ? `data-fid="${it.fid}"` : ''}>
        <span class="lbl">${it.kind === 'file' ? '📄 ' : ''}${esc(it.label)}</span><span class="v">${fmt(it.alloc)}</span>
        <span class="pth" title="${esc(it.path)}">&lrm;${esc(it.path)}&lrm;</span></li>`).join('');
    return `<div class="card cat-card" style="--c:${catColor(c)}">
      <div class="hd"><h3>${esc(catLabel(c))}</h3><span class="sz">${fmt(v.alloc)}</span></div>
      <div class="sub">${t('{pct} do espaço usado · {n} arquivos', { pct: pct(v.alloc, used), n: fmtN(v.n) })}</div>
      <ul class="items">${items || '<li class="muted">—</li>'}</ul></div>`;
  }).join('');

  $('#main').innerHTML = `
    <div class="ov-head"><h1>${t('Disco {drive}', { drive: esc(s.volume) })}</h1>
      <span class="muted">${t('{total} no total · <b>{used}</b> usados ({pct}) · <b>{free}</b> livres · retrato de {when}', { total: fmt(total), used: fmt(used), pct: pct(used, total), free: fmt(free), when: fmtDate(s.finished) })}</span></div>
    <div class="volbar">${segs}</div>
    <div class="legend">${legend}</div>
    <div class="callouts">${callouts.join('')}</div>
    <h2 style="font-size:16px;margin:4px 0 10px">${t('Por categoria: onde está cada GB')}</h2>
    <div class="cards">${cards}</div>`;

  $('#main').querySelectorAll('.items li[data-dir]').forEach(li => li.onclick = () =>
    goExplore(+li.dataset.dir, li.dataset.fid != null ? +li.dataset.fid : null));
  $('#main').querySelectorAll('.volbar div[data-cat]').forEach(d => d.onclick = () => {
    const c = d.dataset.cat;
    if (c === '__hidden') return $('#unaccCard').scrollIntoView({ behavior: 'smooth' });
    const card = [...document.querySelectorAll('.cat-card')].find(x => x.querySelector('h3').textContent === catLabel(c));
    if (card) { card.scrollIntoView({ behavior: 'smooth', block: 'center' }); card.animate([{ outline: `3px solid ${catColor(c)}` }, { outline: '3px solid transparent' }], 1400); }
  });
}

/* hidden space (phase 2) */
const HID = {
  shadow: 'repeating-linear-gradient(45deg,#9c755f 0 6px,#bf9f8a 6px 12px)',
  reserve: 'repeating-linear-gradient(45deg,#5f77a8 0 6px,#8fa2c7 6px 12px)',
  ntfs: 'repeating-linear-gradient(45deg,#6f7c89 0 6px,#a3adb8 6px 12px)',
  unknown: 'var(--unacc)',
};
const GB = 1024 ** 3;

function hiddenCard(h, s) {
  const gap = h.gap || 1;
  const shown = h.items.filter(i => !i.in_scan && i.bytes > 0);
  const mini = shown.map(i => `<div style="width:${i.bytes / gap * 100}%;background:${HID[i.key] || HID.unknown}" title="${esc(i.label)}: ${fmt(i.bytes)}"></div>`).join('');
  const rows = h.items.map(i => `<div class="hid-row">
      <i class="sw" style="background:${HID[i.key] || HID.unknown}"></i>
      <div>
        <div class="hid-h"><b>${esc(i.label)}</b>
          <span class="v">${i.in_scan ? t('já contado nas pastas') : (i.exact ? '' : '≈ ') + fmt(i.bytes)}</span>
          ${i.key === 'unknown' ? '' : i.exact ? `<span class="chip ok">${t('medido')}</span>` : `<span class="chip">${t('estimado')}</span>`}</div>
        <div class="muted">${esc(i.desc)}</div>
        ${i.action ? `<div class="hid-act">→ ${esc(i.action)}</div>` : ''}
        ${safetyHtml(i.safety)}
      </div></div>`).join('');
  const notes = (h.notes || []).map(n => `<li>${esc(n)}</li>`).join('');
  const adminTip = h.admin ? '' : `<div class="hid-note">ⓘ ${t('Rodando como administrador, o DiskLens também mede os <b>pontos de restauração</b>, o tamanho exato da <b>$MFT</b> e lê quase todas as pastas protegidas, deixando o “Não identificado” perto de zero.')}${notes ? `<ul>${notes}</ul>` : ''}</div>`;
  return `<div class="callout wide" id="unaccCard">
    <h3>${t('Espaço que nenhuma pasta mostra: {size} ({pct} do usado)', { size: fmt(h.gap), pct: pct(h.gap, s.used) })}</h3>
    <div class="muted">${t('O Windows diz que {used} estão em uso, mas a soma de todos os arquivos legíveis dá {scanned}. A diferença se explica assim:', { used: fmt(s.used), scanned: fmt(s.scanned) })}</div>
    <div class="minibar">${mini}</div>
    ${rows}${adminTip}</div>`;
}

function reclaimCard(r, used) {
  if (!r || !r.total) return '';
  const items = r.items.map(it => `<li data-dir="${it.id}"><span class="lbl">${esc(it.label)}</span><span class="v">${fmt(it.alloc)}</span>
      <span class="pth" title="${esc(it.path)}">&lrm;${esc(it.path)}&lrm;</span></li>`).join('');
  return `<div class="callout ok"><h3>${t('Dá para liberar com segurança: {size}', { size: fmt(r.total) })}</h3>
    <div class="muted">${t('Caches, temporários, relatórios de erro e lixeira: são recriados automaticamente ou não fazem falta. O DiskLens não apaga nada; clique para ver onde estão e use a ferramenta indicada em cada item.')}</div>
    <ul class="items" style="margin-top:6px">${items}</ul></div>`;
}

function tipsCard(h, o, s) {
  if (!h) return '';
  const tips = [];
  const p = h.pagefile;
  if (p && p.allocated > 2 * GB && p.allocated > 2 * p.peak) {
    tips.push(`<li><b>${t('pagefile.sys ocupa {size}, mas o maior uso desde que o PC foi ligado ({boot}) foi {peak}.', { size: fmt(p.allocated), boot: fmtDate(p.boot_time), peak: fmt(p.peak) })}</b>
      ${p.auto ? t('O tamanho é gerenciado pelo Windows, que aumenta o arquivo quando a memória aperta (jogos, muitos apps abertos) e costuma devolver o espaço só depois de reiniciar.') + ' ' : ''}
      ${t('Se precisar de espaço, reiniciar já pode ajudar. Também dá para fixar um tamanho menor (por exemplo {size}) em Sistema → Configurações avançadas → Desempenho → Memória virtual.', { size: fmt(Math.max(p.peak * 2, 4 * GB)) })}</li>`);
  }
  const hib = [].concat(...Object.values(o.tops)).find(x => x.kind === 'file' && /\\hiberfil\.sys$/i.test(x.path));
  if (hib && hib.alloc > GB && h.hibernation && h.hibernation.enabled) {
    tips.push(`<li><b>${t('hiberfil.sys ocupa {size}', { size: fmt(hib.alloc) })}</b> ${t('(proporcional aos {ram} de RAM). Se você não usa a hibernação, rode no Prompt como administrador:', { ram: fmt(h.hibernation.ram) })}
      <span class="mono">powercfg /h /type reduced</span> ${t('(mantém a Inicialização Rápida e reduz o arquivo pela metade) ou')} <span class="mono">powercfg /h off</span> ${t('(libera tudo).')}</li>`);
  }
  const sh = h.shadow;
  if (sh && sh.storage && sh.storage.used > GB) {
    const rp = (sh.restore_points || []).slice(-4).reverse().map(r => `${fmtDate(r.created)}: ${esc(r.desc)}`).join('<br>');
    tips.push(`<li><b>${t('Pontos de restauração ocupam {size}', { size: fmt(sh.storage.used) })}</b> ${t('({n} cópias).', { n: sh.copies })}${rp ? `<br><span class="muted">${t('Mais recentes:')}<br>${rp}</span>` : ''}</li>`);
  }
  if (!tips.length) return '';
  return `<div class="callout"><h3>${t('Dicas para este disco')}</h3><ul class="tips">${tips.join('')}</ul></div>`;
}

function goExplore(dir, fid) {
  S.dir = dir; S.selFile = fid ?? null;
  setTab('explore');
}

/* ------------------------------------------------------------------ explore */
const AGE = [
  [7, '#e15759', 'última semana'], [30, '#f28e2b', 'último mês'], [90, '#edc948', 'últimos 3 meses'],
  [365, '#8cd17d', 'último ano'], [Infinity, '#4e79a7', 'mais antigo'],
];
function ageColor(mt) {
  if (!mt) return '#a0a7b4';
  const d = (Date.now() / 1000 - mt) / 86400;
  return AGE.find(a => d <= a[0])[1];
}
function shade(hex, k) {           // k in [0,1): mix toward white/black for nested levels
  const n = parseInt(hex.slice(1), 16); let r = n >> 16, g = (n >> 8) & 255, b = n & 255;
  const dark = matchMedia('(prefers-color-scheme: dark)').matches;
  const to = dark ? 0 : 255;
  r = Math.round(r + (to - r) * k); g = Math.round(g + (to - g) * k); b = Math.round(b + (to - b) * k);
  return `rgb(${r},${g},${b})`;
}

function textOn(rgb) {               // dark text on light tiles, white on dark ones
  const [r, g, b] = rgb.match(/\d+/g).map(Number);
  return (0.299 * r + 0.587 * g + 0.114 * b) > 165 ? '#1d2330' : '#ffffff';
}
function toSeries(node, depth) {
  const base = S.colorBy === 'age' ? ageColor(node.mtime) : catColor(node.cat);
  const color = node.kind === 'dir' ? shade(base, Math.min(0.45, depth * 0.14)) : shade(base, node.kind === 'files' ? 0.3 : 0.6);
  const tc = textOn(color);
  const o = {
    name: node.name, value: node.value, id: `${node.kind}:${node.id}:${node.name}`, raw: node, itemStyle: { color },
    label: { color: tc, textShadowBlur: tc === '#ffffff' ? 3 : 0 }, upperLabel: { color: tc, textShadowBlur: tc === '#ffffff' ? 3 : 0 },
  };
  if (node.children && node.children.length) o.children = node.children.map(c => toSeries(c, depth + 1));
  return o;
}

async function renderExplore() {
  $('#main').innerHTML = `<div class="explore">
    <div class="ex-toolbar">
      <div class="crumbs" id="crumbs"></div>
      <div class="seg" id="viewSeg"><button data-v="treemap">Treemap</button><button data-v="sunburst">Sunburst</button></div>
      <div class="seg" id="colorSeg"><button data-c="cat">${t('Cor: categoria')}</button><button data-c="age">${t('Cor: idade')}</button></div>
      <label class="muted" style="font-size:13px">${t('Níveis')} <select id="lvlSel"><option>1</option><option>2</option><option>3</option></select></label>
    </div>
    <div class="ex-body">
      <div class="chartwrap"><div class="chart" id="chart"></div><div class="agelegend" id="ageLegend"></div></div>
      <aside class="side" id="side"><div class="spinner"></div></aside>
    </div></div>`;
  const syncSeg = () => {
    document.querySelectorAll('#viewSeg button').forEach(b => b.classList.toggle('on', b.dataset.v === S.view));
    document.querySelectorAll('#colorSeg button').forEach(b => b.classList.toggle('on', b.dataset.c === S.colorBy));
    $('#lvlSel').value = String(S.levels);
    $('#ageLegend').innerHTML = S.colorBy === 'age'
      ? t('Última modificação dentro da pasta:') + ' ' + AGE.map(a => `<span><i class="sw" style="background:${a[1]}"></i> ${t(a[2])}</span>`).join('')
      : `<span>${t('Clique em uma pasta para entrar nela · passe o mouse para ver detalhes')}</span>`;
  };
  syncSeg();
  document.querySelectorAll('#viewSeg button').forEach(b => b.onclick = () => { S.view = b.dataset.v; syncSeg(); drawChart(); });
  document.querySelectorAll('#colorSeg button').forEach(b => b.onclick = () => { S.colorBy = b.dataset.c; syncSeg(); drawChart(); });
  $('#lvlSel').onchange = e => { S.levels = +e.target.value; loadDir(S.dir); };
  disposeChart();
  S.chart = echarts.init($('#chart'));
  S.chart.on('click', p => {
    const n = p.data && p.data.raw; if (!n) return;
    if (n.kind === 'dir') loadDir(n.id);
    else if (n.kind === 'files') { const el = $('#fileList'); if (el) el.scrollIntoView({ behavior: 'smooth' }); }
  });
  await loadDir(S.dir, S.selFile);
}

async function loadDir(id, fid) {
  S.dir = id; S.selFile = fid ?? null;
  try {
    const [tr, d] = await Promise.all([api(`tree?snap=${S.snap.id}&dir=${id}&levels=${S.levels}`), api(`dir?snap=${S.snap.id}&dir=${id}`)]);
    S.cats = tr.categories; S.treeData = tr.tree; S.dirData = d;
  } catch (e) { toast(e.message); return; }
  renderCrumbs(); drawChart(); renderSide();
}

function renderCrumbs() {
  const c = S.dirData.crumbs;
  $('#crumbs').innerHTML = c.map((x, i) => i === c.length - 1
    ? `<b>${esc(x.name)}</b>` : `<button data-id="${x.id}">${esc(x.name)}</button><span class="sep">›</span>`).join('');
  $('#crumbs').querySelectorAll('button').forEach(b => b.onclick = () => loadDir(+b.dataset.id));
}

function drawChart() {
  if (!S.chart || !S.treeData) return;
  const tree = S.treeData;
  const data = (tree.children || []).map(c => toSeries(c, 0));
  const text = cssVar('--text'), bg = cssVar('--panel');
  const tooltip = {
    confine: true,
    formatter: p => {
      const n = p.data && p.data.raw; if (!n) return '';
      const extra = n.kind === 'dir' ? `${t('{files} arquivos · {dirs} pastas', { files: fmtN(n.nfiles), dirs: fmtN(n.ndirs) })}<br>` :
        n.kind === 'files' ? t('Arquivos soltos diretamente nesta pasta. Maiores:') + '<br>' +
          (n.top || []).map(f => `&nbsp;• ${esc(f.name)}: ${fmt(f.alloc)}`).join('<br>') + '<br>'
          : t('Soma das pastas pequenas demais para desenhar') + '<br>';
      return `<b>${esc(n.name)}</b><br>${t('{size} · {pct} de {parent}', { size: fmt(n.value), pct: pct(n.value, tree.value), parent: esc(tree.name) })}<br>${extra}
        <span style="color:${catColor(n.cat)}">●</span> ${esc(catLabel(n.cat))}<br>${t('Última modificação: {when}', { when: ago(n.mtime) })}`;
    },
  };
  let opt;
  if (S.view === 'treemap') {
    opt = {
      tooltip,
      series: [{
        type: 'treemap', data, roam: false, nodeClick: false, breadcrumb: { show: false },
        left: 0, right: 0, top: 0, bottom: 0, squareRatio: 0.7, leafDepth: S.levels,
        label: { show: true, formatter: p => `${p.name}\n${fmt(p.value)}`, fontSize: 12, color: '#fff', overflow: 'truncate', textShadowColor: 'rgba(0,0,0,.45)', textShadowBlur: 3 },
        upperLabel: { show: true, height: 22, color: '#fff', fontWeight: 600, formatter: p => `${p.name}   ${fmt(p.value)}`, textShadowColor: 'rgba(0,0,0,.4)', textShadowBlur: 3 },
        itemStyle: { borderColor: bg, borderWidth: 1, gapWidth: 1 },
        levels: [
          { itemStyle: { borderColor: bg, borderWidth: 3, gapWidth: 3 } },
          { itemStyle: { borderColor: 'rgba(0,0,0,.18)', borderWidth: 2, gapWidth: 1 } },
          { itemStyle: { borderColor: 'rgba(0,0,0,.12)', borderWidth: 1, gapWidth: 1 } },
        ],
      }],
    };
  } else {
    opt = {
      tooltip,
      title: { text: fmt(tree.value), subtext: tree.name, left: 'center', top: 'middle', textStyle: { color: text, fontSize: 18 }, subtextStyle: { color: cssVar('--muted') } },
      series: [{
        type: 'sunburst', data, radius: ['16%', '96%'], nodeClick: false, sort: undefined,
        itemStyle: { borderColor: bg, borderWidth: 1.5 },
        label: { rotate: 'radial', color: '#fff', fontSize: 11, minAngle: 8, formatter: p => p.name.length > 18 ? p.name.slice(0, 17) + '…' : p.name, textShadowColor: 'rgba(0,0,0,.4)', textShadowBlur: 2 },
        emphasis: { focus: 'ancestor' },
      }],
    };
  }
  S.chart.setOption(opt, true);
}

function safetyHtml(key) {
  if (!key) return '';
  return `<span class="safety ${esc(key)}">${esc(S.safety[key] || key)}</span>`;
}

function renderSide() {
  const d = S.dirData, dir = d.dir;
  const total = dir.alloc || 1;
  const file = S.selFile != null ? d.files.find(f => f.fid === S.selFile) : null;
  let info;
  if (file) {
    const rule = file.rule || d.rule;
    const flags = [];
    if (file.flags & FLAG.CLOUD) flags.push(`<span class="chip">☁ ${t('Somente online: não ocupa espaço')}</span>`);
    if (file.flags & FLAG.COMPRESSED) flags.push(`<span class="chip">${t('Comprimido: {from} → {to}', { from: fmt(file.logical), to: fmt(file.alloc) })}</span>`);
    if (file.flags & FLAG.HARDLINK_DUP) flags.push(`<span class="chip">${t('Hard link: já contado em outro lugar')}</span>`);
    else if (file.flags & FLAG.HARDLINK) flags.push(`<span class="chip">${t('Hard link (contado uma vez)')}</span>`);
    info = `<div class="card info">
      <div class="kicker">${t('Arquivo')}</div><h2>${esc(file.name)}</h2>
      <div class="path">${esc(dir.path)}</div>
      <div class="bignum">${fmt(file.alloc)}</div>
      <div class="muted">${file.logical !== file.alloc ? t('tamanho lógico {size}', { size: fmt(file.logical) }) + ' · ' : ''}${t('modificado em {when} ({ago})', { when: fmtDate(file.mtime), ago: ago(file.mtime) })}</div>
      <div class="chips"><span class="chip"><i class="sw" style="background:${catColor(file.cat)}"></i>${esc(catLabel(file.cat))}</span>${flags.join('')}</div>
      ${whatBox(rule, file.rule ? null : d.anchor_path, dir.path)}
      <div class="actions"><button class="btn small" id="openBtn">${t('Mostrar no Explorer')}</button>
        <button class="btn small" id="backDir">${t('Voltar para a pasta')}</button>
        <button class="btn small" id="trashBtn">${t('Mandar para a Lixeira')}</button></div></div>`;
  } else {
    info = `<div class="card info">
      <div class="kicker">${t('Pasta')}</div><h2>${esc(dir.name)}</h2>
      <div class="path">${esc(dir.path)}</div>
      <div class="bignum">${fmt(dir.alloc)}</div>
      <div class="muted">${t('{files} arquivos · {dirs} subpastas · última modificação {ago}', { files: fmtN(dir.nfiles), dirs: fmtN(dir.ndirs), ago: ago(dir.mtime) })}</div>
      <div class="chips"><span class="chip"><i class="sw" style="background:${catColor(dir.cat)}"></i>${esc(catLabel(dir.cat))}</span>
        ${dir.flags & FLAG.DENIED ? `<span class="chip warn">${t('Sem acesso: conteúdo não lido')}</span>` : ''}</div>
      ${whatBox(d.rule, d.anchor_path, dir.path)}
      <div class="actions"><button class="btn small" id="openBtn">${t('Abrir no Explorer')}</button>
        <button class="btn small" id="bigHere">${t('Maiores arquivos aqui dentro')}</button>
        <button class="btn small" id="askBtn">${t('Perguntar à IA o que é')}</button>
        <button class="btn small" id="trashBtn">${t('Mandar para a Lixeira')}</button></div></div>`;
  }
  const subs = d.subdirs.filter(x => x.value > 0);
  const subHtml = subs.map(x => `<div class="row" data-id="${x.id}" title="${esc(x.name)}">
      <span class="nm">${ICON.dir}${esc(x.name)}${x.denied ? ` <span class="chip warn">${t('sem acesso')}</span>` : ''}</span>
      <span class="bar"><i style="width:${(x.value / total * 100).toFixed(1)}%;background:${catColor(x.cat)}"></i></span>
      <span class="v">${fmt(x.value)}</span></div>`).join('');
  const fileHtml = d.files.map(f => `<div class="row ${f.fid === S.selFile ? 'sel' : ''}" data-fid="${f.fid}" title="${esc(f.name)}">
      <span class="nm">${ICON.file}${esc(f.name)}${f.flags & FLAG.CLOUD ? ' ☁' : ''}</span>
      <span class="muted" style="font-size:11.5px;text-align:right">${ago(f.mtime)}</span>
      <span class="v">${fmt(f.alloc)}</span></div>`).join('');
  $('#side').innerHTML = `${info}
    <div class="card list"><h3><span>${t('Subpastas')}</span><span class="muted">${fmtN(subs.length)}</span></h3>${subHtml || `<div class="muted">${t('Nenhuma subpasta.')}</div>`}</div>
    <div class="card list" id="fileList"><h3><span>${t('Arquivos nesta pasta')}</span><span class="muted">${fmtN(dir.own_files)}${dir.own_files > d.files.length ? ' ' + t('(mostrando {n} maiores)', { n: d.files.length }) : ''}</span></h3>
      ${d.files_listed ? (fileHtml || `<div class="muted">${t('Nenhum arquivo solto aqui.')}</div>`) : `<div class="muted">${t('A lista de arquivos só é guardada no snapshot mais recente.')}</div>`}</div>`;
  $('#side').querySelectorAll('.row[data-id]').forEach(r => r.onclick = () => loadDir(+r.dataset.id));
  $('#side').querySelectorAll('.row[data-fid]').forEach(r => r.onclick = () => { S.selFile = +r.dataset.fid; renderSide(); });
  $('#openBtn').onclick = () => api('open', { snap: S.snap.id, dir: dir.id, fid: file ? file.fid : null }).catch(e => toast(e.message));
  const b = $('#backDir'); if (b) b.onclick = () => { S.selFile = null; renderSide(); };
  const bh = $('#bigHere'); if (bh) bh.onclick = () => { S.files = { ...S.files, under: dir.id, underPath: dir.path }; setTab('files'); };
  const ab = $('#askBtn'); if (ab) ab.onclick = () => askAI(dir.path);
  $('#trashBtn').onclick = () => file ? trashFromExplorer(dir.path.replace(/\\$/, '') + '\\' + file.name, file.alloc) : trashFromExplorer(dir.path, dir.alloc);
  if (file) { const el = $('#side .row.sel'); if (el) el.scrollIntoView({ block: 'center' }); $('#side').scrollTop = 0; }
}

function whatBox(rule, anchorPath, path) {
  if (!rule) return `<div class="what"><h3>${t('O que é isso?')}</h3><p>${t('Não há uma regra conhecida para este local. Ele entra em <b>Outros</b>. Olhe as subpastas e os arquivos maiores para identificar o programa responsável.')}</p></div>`;
  const inherited = anchorPath && anchorPath.toLowerCase() !== path.toLowerCase();
  return `<div class="what"><h3>${t('O que é isso?')}</h3>
    <p><b>${esc(rule.label)}</b>${inherited ? ` <span class="muted">${t('(dentro de {path})', { path: esc(anchorPath) })}</span>` : ''}</p>
    <p>${esc(rule.desc)}</p>${safetyHtml(rule.safety)}</div>`;
}

/* ------------------------------------------------------------------ biggest files */
async function renderFiles() {
  const f = S.files;
  const catOpts = Object.entries(S.cats).map(([k, v]) => `<option value="${k}" ${f.cat === k ? 'selected' : ''}>${esc(v.label)}</option>`).join('');
  $('#main').innerHTML = `
    <div class="filters">
      <input id="fq" placeholder="${t('Buscar pelo nome…')}" value="${esc(f.q)}" style="width:220px">
      <input id="fext" placeholder="${t('Extensão (ex.: mp4)')}" value="${esc(f.ext)}" style="width:150px">
      <select id="fcat"><option value="">${t('Todas as categorias')}</option>${catOpts}</select>
      <select id="fmin">
        ${[[0, t('Qualquer tamanho')], [10 << 20, '≥ 10 MB'], [100 << 20, '≥ 100 MB'], [1 << 30, '≥ 1 GB']].map(([v, l]) => `<option value="${v}" ${f.min == v ? 'selected' : ''}>${l}</option>`).join('')}
      </select>
      ${f.under ? `<span class="chip">${t('Dentro de: {path}', { path: esc(f.underPath) })} <button class="btn small" id="clrUnder">✕</button></span>` : ''}
      <button class="btn primary" id="fgo">${t('Buscar')}</button>
    </div>
    <div id="ftable"><div class="spinner"></div></div>`;
  if (!Object.keys(S.cats).length) { try { S.cats = (await api(`overview?snap=${S.snap.id}`)).categories; } catch { } }
  const go = () => {
    S.files = { ...S.files, q: $('#fq').value, ext: $('#fext').value, cat: $('#fcat').value, min: +$('#fmin').value };
    loadFiles();
  };
  $('#fgo').onclick = go;
  ['#fq', '#fext'].forEach(s => $(s).onkeydown = e => { if (e.key === 'Enter') go(); });
  ['#fcat', '#fmin'].forEach(s => $(s).onchange = go);
  const cu = $('#clrUnder'); if (cu) cu.onclick = () => { S.files.under = null; S.files.underPath = ''; renderFiles(); };
  loadFiles();
}

async function loadFiles() {
  const f = S.files;
  const qs = new URLSearchParams({ snap: S.snap.id, q: f.q, ext: f.ext, cat: f.cat, min: f.min, limit: 500 });
  if (f.under) qs.set('under', f.under);
  let r;
  try { r = await api('files?' + qs); } catch (e) { $('#ftable').innerHTML = `<div class="muted">${esc(e.message)}</div>`; return; }
  S.cats = r.categories;
  if (!r.files_listed) { $('#ftable').innerHTML = `<div class="empty"><p>${t('A lista de arquivos só é guardada no snapshot mais recente. Selecione-o no topo.')}</p></div>`; return; }
  const total = r.files.reduce((a, x) => a + x.alloc, 0);
  $('#ftable').innerHTML = `<div class="muted" style="margin-bottom:8px">${t('{n} arquivos', { n: fmtN(r.files.length) })}${r.files.length >= 500 ? ' ' + t('(os 500 maiores)') : ''} · ${t('somam {size}', { size: fmt(total) })}</div>
    <table class="grid fixed"><colgroup><col style="width:28%"><col><col style="width:96px"><col style="width:110px"><col style="width:200px"><col style="width:52px"></colgroup>
    <thead><tr><th>${t('Arquivo')}</th><th>${t('Pasta')}</th><th class="num">${t('Tamanho')}</th><th class="num">${t('Modificado')}</th><th>${t('Categoria')}</th><th></th></tr></thead>
    <tbody>${r.files.map(x => `<tr>
      <td><span class="link" data-dir="${x.dir}" data-fid="${x.fid}">${esc(x.name)}</span>${x.flags & FLAG.CLOUD ? ' ☁' : ''}</td>
      <td class="p" title="${esc(x.path)}">&lrm;${esc(x.path)}&lrm;</td>
      <td class="num"><b>${fmt(x.alloc)}</b></td>
      <td class="num" title="${fmtDate(x.mtime)}">${ago(x.mtime)}</td>
      <td><i class="sw" style="background:${catColor(x.cat)}"></i> ${esc(catLabel(x.cat))}</td>
      <td><button class="btn small" data-open="${x.dir}:${x.fid}" title="${t('Mostrar no Explorer')}">📂</button></td></tr>`).join('')}</tbody></table>`;
  $('#ftable').querySelectorAll('.link').forEach(a => a.onclick = () => goExplore(+a.dataset.dir, +a.dataset.fid));
  $('#ftable').querySelectorAll('[data-open]').forEach(b => b.onclick = () => {
    const [dir, fid] = b.dataset.open.split(':').map(Number);
    api('open', { snap: S.snap.id, dir, fid }).catch(e => toast(e.message));
  });
}

/* ------------------------------------------------------------------ types */
async function renderTypes() {
  loading();
  let r;
  try { r = await api(`types?snap=${S.snap.id}`); } catch (e) { return showError(e); }
  const max = r.exts.length ? r.exts[0].alloc : 1;
  $('#main').innerHTML = `<div class="types">
    <div class="card"><h3 style="font-size:15px;margin-bottom:6px">${t('Espaço por tipo de arquivo')}</h3><div class="chart" id="typeChart"></div></div>
    <div class="card" style="overflow:auto;max-height:600px"><h3 style="font-size:15px;margin-bottom:6px">${t('Extensões que mais ocupam')}</h3>
      <table class="grid"><thead><tr><th>${t('Extensão')}</th><th>${t('Tipo')}</th><th class="num">${t('Arquivos')}</th><th class="num">${t('Tamanho')}</th></tr></thead>
      <tbody>${r.exts.slice(0, 120).map(x => `<tr>
        <td><span class="link" data-ext="${esc(x.ext)}">${x.ext.startsWith('#') ? esc(x.ext.slice(1)) : x.ext ? '.' + esc(x.ext) : t('(sem extensão)')}</span></td>
        <td class="muted">${esc(x.family)}</td><td class="num">${fmtN(x.n)}</td>
        <td class="num"><span class="tbar" style="width:${Math.max(2, x.alloc / max * 80)}px"></span><b>${fmt(x.alloc)}</b></td></tr>`).join('')}</tbody></table></div></div>`;
  $('#main').querySelectorAll('[data-ext]').forEach(a => a.onclick = () => {
    const e = a.dataset.ext;
    if (!e) return;
    S.files = e.startsWith('#') ? { q: e.slice(1), ext: '', cat: '', min: 0, under: null, underPath: '' }
      : { q: '', ext: e, cat: '', min: 0, under: null, underPath: '' };
    setTab('files');
  });
  disposeChart();
  S.chart = echarts.init($('#typeChart'));
  const fams = r.families.slice(0, 14).reverse();
  const palette = ['#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f', '#edc948', '#b07aa1', '#ff9da7', '#9c755f', '#bab0ac', '#2f9e8f', '#8cd17d', '#d37295', '#a0cbe8'];
  S.chart.setOption({
    grid: { left: 190, right: 70, top: 10, bottom: 20 },
    tooltip: { formatter: p => `<b>${esc(p.name)}</b><br>${fmt(p.value)} · ${t('{n} arquivos', { n: fmtN(fams[p.dataIndex].n) })}` },
    xAxis: { type: 'value', axisLabel: { formatter: v => fmt(v), color: cssVar('--muted') }, splitLine: { lineStyle: { color: cssVar('--line') } } },
    yAxis: { type: 'category', data: fams.map(f => f.family), axisLabel: { color: cssVar('--text') } },
    series: [{
      type: 'bar', data: fams.map((f, i) => ({ value: f.alloc, itemStyle: { color: palette[(fams.length - 1 - i) % palette.length], borderRadius: [0, 4, 4, 0] } })),
      label: { show: true, position: 'right', formatter: p => fmt(p.value), color: cssVar('--muted') },
    }],
  });
}

/* ------------------------------------------------------------------ timeline (phase 3) */
const EV = {
  update: { label: 'Windows Update', symbol: 'diamond', color: '#4e79a7' },
  install: { label: 'Programa instalado', symbol: 'triangle', color: '#59a14f' },
  uninstall: { label: 'Programa removido', symbol: 'path://M0,0 L10,0 L5,10 Z', color: '#9c755f' },
  restore: { label: 'Ponto de restauração', symbol: 'pin', color: '#b07aa1' },
  crash: { label: 'Travamento / desligamento', symbol: 'diamond', color: '#e15759' },
  deferred: { label: 'Snapshot adiado', symbol: 'circle', color: '#a0a7b4' },
  defender: { label: 'Definições do Defender', symbol: 'circle', color: '#76b7b2', minor: true },
  store: { label: 'Atualização de app da Store', symbol: 'circle', color: '#76b7b2', minor: true },
  usn_gap: { label: 'Lacuna no journal do NTFS', symbol: 'circle', color: '#a0a7b4' },
};
const evLabel = k => t((EV[k] || {}).label || k);
const HIDSOLID = { ntfs: '#7d8894', reserve: '#7088b8', shadow: '#a98670', unknown: '#b8bec9' };
const HIDLABEL = { ntfs: 'Metadados do NTFS', reserve: 'Reserva do Windows', shadow: 'Pontos de restauração', unknown: 'Não identificado' };
S.tl = { days: null, minor: false, a: null, b: null, data: null };   // days=null -> pick automatically
const RANGES = [[1, '24 horas'], [7, '7 dias'], [30, '30 dias'], [90, '90 dias'], [365, '1 ano']];

async function renderTimeline() {
  $('#main').innerHTML = `<div class="tl">
    <div class="card" id="agentCard"><div class="spinner"></div></div>
    <div class="card">
      <div class="tl-head"><h3>${t('Uso do disco {drive} ao longo do tempo', { drive: esc(S.volume) })}</h3>
        <div class="seg" id="rangeSeg">${RANGES.map(([d, l]) => `<button data-d="${d}">${t(l)}</button>`).join('')}</div>
        <label class="muted" style="font-size:13px"><input type="checkbox" id="minorEv"> ${t('mostrar atualizações do Defender e da Store')}</label></div>
      <div class="chart" id="tlChart" style="height:400px"></div>
      <div class="muted" style="font-size:12px">${t('Áreas coloridas: retratos completos (snapshots), por categoria. Linha escura: medição do espaço usado a cada 15 min. Linha tracejada: pagefile.sys. Ícones no topo: eventos do Windows. Ctrl + roda do mouse dá zoom.')}
        ${t('<b>Clique em um ponto de snapshot</b> para compará-lo com o anterior.')}</div>
    </div>
    <div class="card" id="actCard"></div>
    <div class="card" id="cmpCard"></div>
  </div>`;
  $('#minorEv').checked = S.tl.minor;
  $('#minorEv').onchange = e => { S.tl.minor = e.target.checked; drawTimeline(); };
  let d;
  try { d = await api(`timeline?volume=${S.volume}&days=365`); } catch (e) { return showError(e); }
  S.tl.data = d;
  if (!S.tl.days) {        // smallest range that still shows every snapshot
    const oldest = d.snapshots.length ? d.snapshots[0].finished : Date.now() / 1000;
    const ageDays = (Date.now() / 1000 - oldest) / 86400 * 1.15;
    S.tl.days = (RANGES.find(([r]) => r >= ageDays) || RANGES[RANGES.length - 1])[0];
  }
  const syncRange = () => document.querySelectorAll('#rangeSeg button').forEach(b => b.classList.toggle('on', +b.dataset.d === S.tl.days));
  syncRange();
  document.querySelectorAll('#rangeSeg button').forEach(b => b.onclick = () => { S.tl.days = +b.dataset.d; S.tl.userRange = true; syncRange(); drawTimeline(); });
  S.cats = d.categories;
  renderAgent(d.agent);
  disposeChart();
  S.chart = echarts.init($('#tlChart'));
  S.chart.on('click', p => {
    if (p.data && p.data.snap) {
      const ids = d.snapshots.map(s => s.id), i = ids.indexOf(p.data.snap);
      if (i > 0) { S.tl.a = ids[i - 1]; S.tl.b = ids[i]; renderCompare(); }
      else toast(t('Este é o primeiro snapshot do período: não há anterior para comparar.'));
    }
  });
  drawTimeline();
  renderActivity();
  const snaps = d.snapshots;
  if (!S.tl.b || !snaps.find(s => s.id === S.tl.b)) {
    S.tl.b = snaps.length ? snaps[snaps.length - 1].id : null;
    S.tl.a = snaps.length > 1 ? snaps[snaps.length - 2].id : null;
  }
  renderCompare();
}

function renderAgent(a) {
  const st = a.state || {};
  const vs = (st.volumes || {})[S.volume] || {};
  const set = a.settings;
  const lastRun = st.last_run ? t('última medição {when}', { when: ago2(st.last_run) }) : t('ainda não rodou');
  let status;
  if (!a.installed) {
    status = `<div class="agent-row"><span class="agent-dot off"></span><div><b>${t('Coleta automática desativada.')}</b>
      <div class="muted">${t('Ative para o DiskLens medir o disco a cada 15 minutos e fazer um retrato completo por dia, sempre esperando o computador estar livre: sem jogo ou app em tela cheia, processador abaixo de {cpu}% e fora da bateria.', { cpu: set.cpu_limit })} ${a.admin ? '' : t('O Windows vai pedir permissão de administrador uma vez.')}</div></div>
      <button class="btn primary" data-act="install">${t('Ativar coleta automática')}</button></div>`;
  } else {
    let next;
    if (vs.running_since) next = t('snapshot em andamento desde {when}', { when: fmtTime(vs.running_since) });
    else if (vs.deferrals) next = t('próximo snapshot <b>adiado {n}×</b> ({reason}). Nova tentativa às {when}', { n: vs.deferrals, reason: esc(vs.last_reason || ''), when: fmtTime(vs.next_attempt) });
    else {
      const last = (S.tl.data.snapshots.slice(-1)[0] || {}).finished;
      next = last ? t('próximo snapshot a partir de {when}', { when: fmtDate(last + set.snapshot_hours * 3600) }) : t('primeiro snapshot na próxima execução');
    }
    const stale = vs.pending_since && Date.now() / 1000 - vs.pending_since > 3 * 86400;
    status = `<div class="agent-row"><span class="agent-dot ${stale ? 'warn' : 'on'}"></span><div><b>${t('Coleta automática ativa')}</b> · ${lastRun} · ${next}
      ${stale ? `<div class="pausenote" style="margin-top:6px">${t('O snapshot está pendente há {n} dias porque o computador sempre esteve ocupado. Use “Snapshot agora” quando puder.', { n: Math.floor((Date.now() / 1000 - vs.pending_since) / 86400) })}</div>` : ''}</div>
      <div class="actions" style="margin:0"><button class="btn small" data-act="run">${t('Medir agora')}</button><button class="btn small" data-act="snap">${t('Snapshot agora')}</button>
      <button class="btn small" data-act="uninstall">${t('Desativar')}</button></div></div>`;
  }
  const opt = (vals, cur, fmtv) => vals.map(v => `<option value="${v}" ${v == cur ? 'selected' : ''}>${fmtv(v)}</option>`).join('');
  const vols = set.volumes || a.volumes_default;
  const tr = a.tray || {};
  const trayOn = tr.enabled && tr.running;
  const trayState = trayOn ? t('ligado') : tr.enabled ? t('ligado, mas não está rodando') : t('desligado');
  const trayRow = `<div class="agent-row tray-row"><span class="agent-dot ${trayOn ? 'on' : 'off'}"></span><div>
      <b>${t('Ícone na bandeja do Windows: {state}', { state: trayState })}</b>
      <div class="muted">${a.installed ? t('A coleta automática não depende dele: é o Agendador de Tarefas do Windows que a executa, mesmo com o DiskLens fechado.') + ' ' : ''}${t('O ícone mostra o espaço livre (a lupa fica laranja acima de 85% de uso e vermelha acima de 95%), abre o painel com um clique e continua lá quando você fecha esta janela. Usa cerca de 20 MB de memória.')}</div>
      ${tr.enabled ? `<label class="ck" style="margin-top:6px"><input type="checkbox" id="trayAuto" ${tr.autostart ? 'checked' : ''}> ${t('Iniciar com o Windows')}</label>` : ''}</div>
      <div class="actions" style="margin:0">${tr.enabled && !tr.running ? `<button class="btn small" data-tray="start">${t('Iniciar agora')}</button>` : ''}
      <button class="btn small ${tr.enabled ? '' : 'primary'}" data-tray="${tr.enabled ? 'off' : 'on'}">${tr.enabled ? t('Desligar') : t('Ligar ícone na bandeja')}</button></div></div>`;
  $('#agentCard').innerHTML = `${status}${trayRow}
    <details class="agent-set"><summary>${t('Configurações da coleta')}</summary>
      <div class="setgrid">
        <label>${t('Retrato completo a cada')} <select id="setHours">${opt([6, 12, 24, 48, 168], set.snapshot_hours, v => v === 168 ? t('7 dias') : t('{n} horas', { n: v }))}</select></label>
        <label>${t('Adiar se outros programas usarem mais de')} <select id="setCpu">${opt([40, 50, 60, 70, 80, 90], set.cpu_limit, v => t('{n}% da CPU', { n: v }))}</select></label>
        <label>${t('Adiar se a memória passar de')} <select id="setMem">${opt([80, 85, 90, 95], set.mem_limit, v => v + '%')}</select></label>
        <div>${t('Discos com retrato automático:')} ${S.state.volumes.map(v => `<label class="ck"><input type="checkbox" value="${v.volume}" ${vols.includes(v.volume) ? 'checked' : ''}> ${v.volume}</label>`).join('')}</div>
      </div>
      <div class="muted" style="font-size:12px;margin-top:6px">${t('Jogos e apps em tela cheia sempre adiam o snapshot. Durante o scan, ele também pausa sozinho se o computador ficar ocupado. As medições de 15 minutos são instantâneas e não usam o disco.')}</div>
    </details>`;
  $('#agentCard').querySelectorAll('[data-act]').forEach(b => b.onclick = async () => {
    const act = b.dataset.act;
    try {
      if (act === 'install' || act === 'uninstall') {
        const r = await api('agent', { action: act });
        toast(r.ok ? (r.elevated ? t('Confirme a permissão de administrador na janela do Windows…') : t('Pronto!')) : t('Não foi possível alterar a tarefa agendada.'));
        setTimeout(() => S.tab === 'timeline' && renderTimeline(), r.elevated ? 6000 : 1500);
      } else {
        await api('agent', { action: 'run', snapshot: act === 'snap' });
        toast(act === 'snap' ? t('Snapshot iniciado em segundo plano (prioridade mínima).') : t('Medindo…'));
        setTimeout(() => refreshState(false).then(() => S.tab === 'timeline' && renderTimeline()), 4000);
      }
    } catch (e) { toast(e.message); }
  });
  const save = async () => {
    const volumes = [...$('#agentCard').querySelectorAll('.setgrid .ck input:checked')].map(i => i.value);
    try {
      await api('settings', { snapshot_hours: +$('#setHours').value, cpu_limit: +$('#setCpu').value, mem_limit: +$('#setMem').value, volumes });
      toast(t('Configurações salvas.'));
    } catch (e) { toast(e.message); }
  };
  $('#agentCard').querySelectorAll('.setgrid select, .setgrid .ck input').forEach(el => el.onchange = save);
  const trayCall = async body => {
    try {
      a.tray = await api('tray', body);
      renderAgent(a);
      if (body.enabled === true) toast(a.tray.running ? t('Pronto: o ícone está na bandeja (perto do relógio). Pode estar escondido na setinha ^.') : t('Não foi possível iniciar o ícone.'));
      else if (body.enabled === false) toast(t('Ícone removido da bandeja.'));
    } catch (e) { toast(e.message); }
  };
  $('#agentCard').querySelectorAll('[data-tray]').forEach(b => b.onclick = () =>
    trayCall({ enabled: b.dataset.tray !== 'off' }));
  const ta = $('#trayAuto');
  if (ta) ta.onchange = () => trayCall({ autostart: ta.checked });
}

function ago2(ts) {
  const m = (Date.now() / 1000 - ts) / 60;
  if (m < 1) return t('agora há pouco'); if (m < 60) return t('há {n} min', { n: Math.round(m) });
  if (m < 1440) return t('há {n} h', { n: Math.round(m / 60) }); return ago(ts);
}
const fmtTime = ts => new Date(ts * 1000).toLocaleString(LOC(), { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

function drawTimeline() {
  const d = S.tl.data; if (!d || !S.chart) return;
  const snaps = d.snapshots;
  if (!snaps.length && !d.samples.length) {
    S.chart.setOption({ title: { text: t('Ainda não há medições deste disco'), subtext: t('Faça um scan ou ative a coleta automática: o gráfico aparece a partir das primeiras medições.'), left: 'center', top: 'middle', textStyle: { color: cssVar('--muted'), fontSize: 15 }, subtextStyle: { color: cssVar('--muted') } } }, true);
    return;
  }
  const total = Math.max(...snaps.map(s => s.total || 0), ...d.samples.map(s => s[1] + s[2]), 1);
  const series = [];
  // categories, biggest at the bottom
  const catTotals = {};
  snaps.forEach(s => Object.entries(s.cats || {}).forEach(([c, v]) => catTotals[c] = Math.max(catTotals[c] || 0, v)));
  const order = Object.keys(catTotals).sort((a, b) => catTotals[b] - catTotals[a]);
  order.forEach(c => series.push({
    name: catLabel(c), type: 'line', stack: 'snap', areaStyle: { opacity: .85 }, symbol: 'circle', symbolSize: 7,
    lineStyle: { width: 0 }, itemStyle: { color: catColor(c) }, emphasis: { focus: 'series' },
    data: snaps.map(s => ({ value: [s.finished * 1000, (s.cats || {})[c] || 0], snap: s.id })),
  }));
  ['ntfs', 'reserve', 'shadow', 'unknown'].forEach(k => {
    const vals = snaps.map(s => {
      if (s.hidden) return s.hidden[k] || 0;
      return k === 'unknown' ? Math.max(0, s.used - s.scanned) : 0;
    });
    if (!vals.some(v => v > 0)) return;
    series.push({
      name: t(HIDLABEL[k]), type: 'line', stack: 'snap', areaStyle: { opacity: .7 }, symbol: 'circle', symbolSize: 7,
      lineStyle: { width: 0 }, itemStyle: { color: HIDSOLID[k] }, emphasis: { focus: 'series' },
      data: snaps.map((s, i) => ({ value: [s.finished * 1000, vals[i]], snap: s.id })),
    });
  });
  series.push({
    name: t('Usado (medição a cada 15 min)'), type: 'line', showSymbol: false, symbol: 'none', z: 5,
    lineStyle: { width: 2, color: cssVar('--text') }, itemStyle: { color: cssVar('--text') },
    data: d.samples.map(s => [s[0] * 1000, s[1]]),
    markLine: { silent: true, symbol: 'none', lineStyle: { type: 'dashed', color: cssVar('--danger') },
      label: { formatter: t('Capacidade {size}', { size: fmt(total) }), color: cssVar('--muted') }, data: [{ yAxis: total }] },
  });
  if (d.samples.some(s => s[3])) series.push({
    name: 'pagefile.sys', type: 'line', showSymbol: false, symbol: 'none', z: 5,
    lineStyle: { width: 1.5, type: 'dashed', color: '#9c755f' }, itemStyle: { color: '#9c755f' },
    data: d.samples.filter(s => s[3]).map(s => [s[0] * 1000, s[3]]),
  });
  const evs = d.events.filter(e => S.tl.minor || !(EV[e.kind] || {}).minor);
  const byKind = {};
  evs.forEach(e => (byKind[e.kind] = byKind[e.kind] || []).push(e));
  Object.entries(byKind).forEach(([k, list]) => {
    const m = EV[k] || { label: k, symbol: 'circle', color: '#888' };
    series.push({
      name: evLabel(k), type: 'scatter', symbol: m.symbol, symbolSize: k === 'deferred' || m.minor ? 7 : 12, z: 10,
      itemStyle: { color: m.color }, data: list.map(e => ({ value: [e.ts * 1000, total * 1.04], ev: e })),
    });
  });
  const now = Date.now() / 1000;
  let xMinS = now - S.tl.days * 86400;
  const dataTs = [...snaps.map(s => s.finished), ...d.samples.map(s => s[0])];
  if (!S.tl.userRange && dataTs.length) {     // short history: zoom onto the measured period
    const first = Math.min(...dataTs);
    xMinS = Math.max(xMinS, first - Math.max((now - first) * 0.15, 1800));
  }
  const xMin = xMinS * 1000;
  S.chart.setOption({
    animation: false,
    grid: { left: 70, right: 24, top: 40, bottom: 70 },
    legend: { type: 'scroll', top: 0, textStyle: { color: cssVar('--text') } },
    tooltip: {
      trigger: 'item', confine: true,
      formatter: p => {
        if (p.data && p.data.ev) {
          const e = p.data.ev;
          return `<b>${esc(evLabel(e.kind))}</b><br>${fmtDate(e.ts)}<br>${esc(e.title)}${e.detail ? '<br>' + esc(e.detail) : ''}`;
        }
        const ts = p.value[0] / 1000;
        if (p.data && p.data.snap) {
          const s = snaps.find(x => x.id === p.data.snap);
          return `<b>${t('Snapshot de {when}', { when: fmtDate(ts) })}</b><br>${p.marker}${esc(p.seriesName)}: <b>${fmt(p.value[1])}</b><br>${t('Usado: {used} · Livre: {free}', { used: fmt(s.used), free: fmt(s.free) })}<br><span style="opacity:.7">${t('Clique para comparar com o anterior')}</span>`;
        }
        return `<b>${fmtDate(ts)}</b><br>${p.marker}${esc(p.seriesName)}: <b>${fmt(p.value[1])}</b>`;
      },
    },
    xAxis: { type: 'time', min: xMin, max: (now + Math.max((now - xMinS) * 0.03, 300)) * 1000, axisLabel: { color: cssVar('--muted') } },
    yAxis: { type: 'value', min: 0, max: total * 1.08, axisLabel: { formatter: v => fmt(v), color: cssVar('--muted') }, splitLine: { lineStyle: { color: cssVar('--line') } } },
    dataZoom: [{ type: 'inside', zoomOnMouseWheel: 'ctrl', moveOnMouseWheel: false }, { type: 'slider', height: 18, bottom: 12 }],
    series,
  }, true);
}

/* file by file, from the NTFS journal (phase 4) */
const ACT_RANGES = [[3600, '1 hora'], [86400, '24 horas'], [7 * 86400, '7 dias'], [30 * 86400, '30 dias']];
S.act = { span: 86400, sel: null };
const KIND = { new: 'novo', grow: 'cresceu', shrink: 'diminuiu', deleted: 'apagado', small: '' };

function hhmm(ts) {
  const d = new Date(ts * 1000), today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString(LOC(), today ? { hour: '2-digit', minute: '2-digit' } : { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function activityLists(a, compact) {
  const sgn = v => (v > 0 ? '+' : '−') + fmt(Math.abs(v));
  const max = Math.max(1, ...a.grow.map(f => f.delta), ...a.shrink.map(f => -f.delta));
  const row = f => {
    const isAgg = f.is_dir_agg;
    const [folder, , name] = isAgg ? [f.path, '', ''] : (() => { const i = f.path.lastIndexOf('\\'); return [f.path.slice(0, i), '', f.path.slice(i + 1)]; })();
    const when = f.first_ts && f.ts - f.first_ts > 120 ? `${hhmm(f.first_ts)} – ${hhmm(f.ts)}` : hhmm(f.ts);
    return `<div class="drow">
      <div class="dbar"><i style="width:${Math.abs(f.delta) / max * 100}%;background:${f.delta > 0 ? 'var(--grow)' : 'var(--shrink)'}"></i></div>
      <div class="dtxt"><div><b class="${f.delta > 0 ? 'up' : 'down'}">${sgn(f.delta)}</b>
          ${isAgg ? `<b>${t('{n} arquivos pequenos', { n: fmtN(f.nfiles) })}</b> <span class="muted">${t('em')}</span> <span class="pth2">${esc(folder)}</span>`
                  : `<b>${esc(name)}</b> ${KIND[f.kind] ? `<span class="chip">${t(KIND[f.kind])}</span>` : ''}`}</div>
        <div class="muted" style="font-size:12px">${isAgg ? '' : `<span class="pth2">${esc(folder)}</span> · `}${when}${f.size_after ? ' · ' + t('agora {size}', { size: fmt(f.size_after) }) : ''}</div></div></div>`;
  };
  const folders = a.folders.filter(x => Math.abs(x.delta) >= 1024 ** 2).slice(0, compact ? 8 : 12).map(x => `<div class="drow">
      <div class="dbar"><i style="width:${Math.abs(x.delta) / max * 100}%;background:${x.delta > 0 ? 'var(--grow)' : 'var(--shrink)'}"></i></div>
      <div class="dtxt"><b class="${x.delta > 0 ? 'up' : 'down'}">${sgn(x.delta)}</b> <span class="pth2">${esc(x.path)}</span>
        <div class="muted" style="font-size:12px">${t('{n} arquivo(s) · última alteração {when}', { n: fmtN(x.nfiles), when: hhmm(x.ts) })}</div></div></div>`).join('');
  return `<div class="diff-grid">
      <div><h4>${t('Arquivos que mais cresceram')}</h4><div class="dlist">${a.grow.map(row).join('') || `<p class="muted">${t('Nada cresceu 1 MB ou mais.')}</p>`}</div></div>
      <div><h4>${t('Pastas com mais mudança')}</h4><div class="dlist" style="max-height:220px">${folders || '<p class="muted">—</p>'}</div>
        <h4>${t('Espaço liberado (apagados ou diminuídos)')}</h4><div class="dlist" style="max-height:220px">${a.shrink.map(row).join('') || `<p class="muted">${t('Nada foi liberado.')}</p>`}</div>
        ${(a.transient || []).length ? `<h4>${t('Criados e apagados no período')} <span class="muted" style="font-weight:400">${t('(ocuparam espaço temporariamente)')}</span></h4>
          <div class="dlist" style="max-height:220px">${a.transient.map(f => {
            const i = f.path.lastIndexOf('\\');
            return `<div class="drow"><div class="dbar"><i style="width:${Math.min(100, f.gross / max * 100)}%;background:#b8bec9"></i></div>
              <div class="dtxt"><b>${fmt(f.gross)}</b> <b>${esc(f.path.slice(i + 1))}</b>
              <div class="muted" style="font-size:12px"><span class="pth2">${esc(f.path.slice(0, i))}</span> · ${hhmm(f.first_ts)} – ${hhmm(f.ts)}</div></div></div>`;
          }).join('')}</div>` : ''}</div>
    </div>`;
}

async function renderActivity() {
  const box = $('#actCard');
  if (!box) return;
  const now = Date.now() / 1000;
  const from = S.act.sel ? S.act.sel[0] : now - S.act.span, to = S.act.sel ? S.act.sel[1] : now;
  let a;
  try { a = await api(`activity?volume=${S.volume}&from=${from}&to=${to}&limit=60`); } catch (e) { box.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const head = `<div class="tl-head"><h3>${t('Arquivo a arquivo')} <span class="muted" style="font-weight:400;font-size:13px">${t('(journal de alterações do NTFS)')}</span></h3>
      <div class="seg" id="actSeg">${ACT_RANGES.map(([s, l]) => `<button data-s="${s}" class="${!S.act.sel && s === S.act.span ? 'on' : ''}">${t(l)}</button>`).join('')}</div></div>`;
  if (!a.available) {
    const installed = S.tl.data && S.tl.data.agent.installed;
    box.innerHTML = `${head}<p>${t('O Windows registra no <b>journal do NTFS</b> cada arquivo criado, aumentado ou apagado, com data e hora. Com ele, o DiskLens mostra exatamente <b>quais arquivos</b> consumiram espaço e <b>quando</b>, minuto a minuto, mesmo entre dois snapshots.')}</p>
      <p class="muted">${t('Ler o journal exige administrador, por isso ele é lido pela coleta automática (que roda com privilégios elevados). Na primeira leitura, o histórico que o Windows ainda guarda (geralmente alguns dias) aparece de uma vez.')}
      ${a.last_error ? `<br><b>${t('Última tentativa falhou:')}</b> ${esc(a.last_error)}` : ''}</p>
      ${installed ? `<button class="btn primary" id="actRun">${t('Ler o journal agora')}</button>` : `<p class="muted">${t('Ative a coleta automática no topo desta aba.')}</p>`}`;
    const b = $('#actRun');
    if (b) b.onclick = async () => { await api('agent', { action: 'run' }); toast(t('Lendo o journal em segundo plano… atualize a aba em alguns segundos.')); setTimeout(() => S.tab === 'timeline' && renderActivity(), 8000); };
    bindActSeg();
    return;
  }
  const tot = a.totals;
  const selTxt = S.act.sel ? `<span class="chip">${t('Intervalo {from} – {to}', { from: hhmm(S.act.sel[0]), to: hhmm(S.act.sel[1]) })} <button class="btn small" id="actClr">✕</button></span>` : '';
  const stepTxt = a.step >= 86400 ? t('dia') : a.step >= 3600 ? t('hora') : t('15 minutos');
  box.innerHTML = `${head}
    <div class="muted" style="font-size:12.5px;margin-bottom:8px">${t('Histórico desde {since} · lido {when}.', { since: fmtDate(a.since), when: ago2(a.updated) })} ${selTxt}</div>
    ${from < a.tracking_since - 60 ? `<div class="hid-note" style="margin-bottom:8px">ⓘ ${t('Antes de {when} (primeira leitura do journal), só os arquivos <b>criados</b> no período têm tamanho conhecido: o Windows não registra tamanhos, e arquivos antigos que foram apagados ou alterados só podem ser medidos se estavam num snapshot. Daqui em diante, cada alteração é medida.', { when: fmtDate(a.tracking_since) })}</div>` : ''}
    <div class="kpis">
      <div><span>${t('Cresceu')}</span><b class="up">+${fmt(tot.grow)}</b></div>
      <div><span>${t('Liberado')}</span><b class="down">−${fmt(-tot.shrink)}</b></div>
      <div><span>${t('Saldo')}</span><b>${tot.net >= 0 ? '+' : '−'}${fmt(Math.abs(tot.net))}</b></div>
      <div><span>${t('Arquivos criados / apagados')}</span><b>${fmtN(tot.created)} / ${fmtN(tot.deleted)}</b></div>
    </div>
    <div class="chart" id="actChart" style="height:170px"></div>
    <div class="muted" style="font-size:12px;margin-bottom:6px">${t('Quando o espaço foi consumido (vermelho) ou liberado (verde), a cada {step}.', { step: stepTxt })} ${t('<b>Clique em uma barra</b> para ver só aquele intervalo.')}</div>
    ${activityLists(a)}`;
  bindActSeg();
  const c = $('#actClr'); if (c) c.onclick = () => { S.act.sel = null; renderActivity(); };
  if (S.chart3) S.chart3.dispose();
  S.chart3 = echarts.init($('#actChart'));
  S.chart3.setOption({
    animation: false, grid: { left: 64, right: 12, top: 8, bottom: 22 },
    tooltip: { trigger: 'axis', formatter: ps => {
      const b = a.buckets[ps[0].dataIndex];
      return `<b>${hhmm(b[0])} – ${hhmm(b[0] + a.step)}</b><br>${t('Cresceu')}: +${fmt(b[4])}<br>${t('Liberado')}: −${fmt(-b[5])}<br>${t('{created} criados · {deleted} apagados · {changes} alterações', { created: fmtN(b[2]), deleted: fmtN(b[3]), changes: fmtN(b[1]) })}`;
    } },
    xAxis: { type: 'category', data: a.buckets.map(b => hhmm(b[0])), axisLabel: { color: cssVar('--muted') } },
    yAxis: { type: 'value', axisLabel: { formatter: v => (v < 0 ? '−' : '') + fmt(Math.abs(v)), color: cssVar('--muted') }, splitLine: { lineStyle: { color: cssVar('--line') } } },
    series: [
      { type: 'bar', stack: 'a', data: a.buckets.map(b => b[4]), itemStyle: { color: cssVar('--grow') } },
      { type: 'bar', stack: 'a', data: a.buckets.map(b => b[5]), itemStyle: { color: cssVar('--shrink') } },
    ],
  });
  S.chart3.on('click', p => { const b = a.buckets[p.dataIndex]; S.act.sel = [b[0], b[0] + a.step]; renderActivity(); });
}

function bindActSeg() {
  document.querySelectorAll('#actSeg button').forEach(b => b.onclick = () => { S.act.span = +b.dataset.s; S.act.sel = null; renderActivity(); });
}

function renderCompare() {
  const d = S.tl.data, snaps = d.snapshots;
  const box = $('#cmpCard');
  if (snaps.length < 2) {
    box.innerHTML = `<h3>${t('Comparar dois momentos')}</h3><p class="muted">${t('É preciso ter pelo menos dois snapshots deste disco no período.')} ${d.agent.installed ? t('A coleta automática vai criá-los.') : t('Ative a coleta automática acima ou use “Escanear agora”.')}</p>`;
    return;
  }
  const opts = cur => snaps.map(s => `<option value="${s.id}" ${s.id === cur ? 'selected' : ''}>${fmtDate(s.finished)} · ${t('{size} usados', { size: fmt(s.used) })}</option>`).join('');
  box.innerHTML = `<div class="cmp-head"><h3>${t('Comparar')}</h3>
      <select id="cmpA">${opts(S.tl.a)}</select><span>→</span><select id="cmpB">${opts(S.tl.b)}</select>
      <span class="muted" style="font-size:12.5px">${t('ou clique em um ponto do gráfico')}</span></div>
    <div id="diffBox"><div class="spinner"></div></div>`;
  $('#cmpA').onchange = e => { S.tl.a = +e.target.value; loadDiff(); };
  $('#cmpB').onchange = e => { S.tl.b = +e.target.value; loadDiff(); };
  loadDiff();
}

async function loadDiff() {
  const box = $('#diffBox');
  if (S.tl.a === S.tl.b) { box.innerHTML = `<p class="muted">${t('Escolha dois snapshots diferentes.')}</p>`; return; }
  box.innerHTML = '<div class="spinner"></div>';
  let r;
  try { r = await api(`diff?a=${S.tl.a}&b=${S.tl.b}`); } catch (e) { box.innerHTML = `<p class="muted">${esc(e.message)}</p>`; return; }
  const days = (r.b.finished - r.a.finished) / 86400;
  const span = days >= 1 ? t('{n} dias', { n: days.toLocaleString(LOC(), { maximumFractionDigits: 1 }) })
    : days * 24 >= 1 ? t('{n} horas', { n: Math.round(days * 24) }) : t('{n} minutos', { n: Math.round(days * 1440) });
  const sgn = v => (v > 0 ? '+' : v < 0 ? '−' : '') + fmt(Math.abs(v));
  const hiddenTotal = r.used_delta - r.scanned_delta;
  const maxAbs = Math.max(1, ...r.items.map(i => Math.abs(i.delta)));
  const items = r.items.map(i => {
    const name = i.kind === 'files'
      ? t('Arquivos soltos em {path}', { path: esc(i.path) }) + (i.top_files && i.top_files.length ? ` <span class="muted">(${i.top_files.map(f => esc(f.name)).join(', ')})</span>` : '')
      : esc(i.path);
    const tags = [i.new ? `<span class="chip">${t('nova')}</span>` : '', i.gone ? `<span class="chip">${t('removida')}</span>` : '',
      i.partial ? `<span class="chip">${t('demais conteúdos')}</span>` : ''].join('');
    return `<div class="drow ${i.dir_b ? 'click' : ''}" ${i.dir_b ? `data-dir="${i.dir_b}"` : ''}>
      <div class="dbar"><i style="width:${Math.abs(i.delta) / maxAbs * 100}%;background:${i.delta > 0 ? 'var(--grow)' : 'var(--shrink)'}"></i></div>
      <div class="dtxt"><div><b class="${i.delta > 0 ? 'up' : 'down'}">${sgn(i.delta)}</b> ${i.label ? `<b>${esc(i.label)}</b> · ` : ''}<span class="pth2">${name}</span> ${tags}</div>
        <div class="muted" style="font-size:12px">${fmt(i.before)} → ${fmt(i.after)}${i.safety ? ' · ' + esc(S.safety[i.safety] || i.safety) : ''}</div></div></div>`;
  }).join('');
  const files = r.files ? (r.files.grown.length ? `<table class="grid fixed"><colgroup><col><col style="width:110px"><col style="width:110px"><col style="width:110px"></colgroup>
      <thead><tr><th>${t('Arquivo')}</th><th class="num">${t('Antes')}</th><th class="num">${t('Depois')}</th><th class="num">${t('Diferença')}</th></tr></thead><tbody>
      ${r.files.grown.map(f => `<tr><td class="p" title="${esc(f.path)}">&lrm;${esc(f.path)}&lrm;</td><td class="num">${f.new ? `<span class="chip">${t('novo')}</span>` : fmt(f.before)}</td><td class="num">${fmt(f.after)}</td><td class="num"><b class="up">+${fmt(f.delta)}</b></td></tr>`).join('')}
      </tbody></table>` : `<p class="muted">${t('Nenhum arquivo de 10 MB ou mais surgiu ou cresceu 10 MB ou mais.')}</p>`)
    : `<p class="muted">${t('Indisponível para este par: o snapshot mais antigo foi criado antes de o DiskLens guardar os arquivos grandes dos snapshots anteriores.')}</p>`;
  const evs = r.events.filter(e => S.tl.minor || !(EV[e.kind] || {}).minor);
  const evHtml = evs.length ? `<ul class="evlist">${evs.map(e => `<li><span class="evdot" style="background:${(EV[e.kind] || {}).color || '#888'}"></span><span class="muted">${fmtTime(e.ts)}</span> <b>${esc(evLabel(e.kind))}</b>: ${esc(e.title)}${e.detail ? ` <span class="muted">(${esc(e.detail)})</span>` : ''}</li>`).join('')}</ul>`
    : `<p class="muted">${t('Nenhum evento relevante do Windows no período.')}</p>`;
  const jumps = r.jumps.length ? `<ul class="evlist">${r.jumps.map(j => `<li><b class="${j.delta > 0 ? 'up' : 'down'}">${sgn(j.delta)}</b> ${t('entre {from} e {to}', { from: fmtTime(j.from), to: fmtTime(j.to) })}${Math.abs(j.pagefile) > 128 * 1024 ** 2 ? ` · pagefile.sys ${sgn(j.pagefile)}` : ''}</li>`).join('')}</ul>`
    : `<p class="muted">${t('Sem saltos de 256 MB ou mais entre medições de 15 minutos (ou ainda não há medições neste período).')}</p>`;

  box.innerHTML = `
    <div class="diff-head"><div class="muted">${fmtDate(r.a.finished)} → ${fmtDate(r.b.finished)} (${span})</div>
      <p class="summary">${esc(r.summary)}</p>
      <div class="kpis">
        <div><span>${t('Espaço usado')}</span><b class="${r.used_delta > 0 ? 'up' : 'down'}">${sgn(r.used_delta)}</b></div>
        <div><span>${t('Espaço livre')}</span><b>${fmt(r.a.free)} → ${fmt(r.b.free)}</b></div>
        <div><span>${t('Em arquivos visíveis')}</span><b>${sgn(r.scanned_delta)}</b></div>
        <div><span>${t('Em áreas ocultas')}</span><b>${sgn(hiddenTotal)}</b></div>
      </div></div>
    <div class="diff-grid">
      <div><h4>${t('Por categoria')}</h4><div id="catDelta" style="height:320px"></div></div>
      <div><h4>${t('O que mudou')} <span class="muted" style="font-weight:400">${t('(as pastas mais específicas que explicam a mudança; os valores não se sobrepõem)')}</span></h4>
        <div class="dlist">${items || `<p class="muted">${t('Nenhuma pasta mudou 64 MB ou mais.')}</p>`}</div></div>
    </div>
    <h4>${t('Arquivos grandes novos ou que cresceram')}</h4>${files}
    ${r.activity && r.activity.available ? `<h4 style="margin-top:16px">${t('Arquivo a arquivo no período (journal do NTFS)')}</h4>${activityLists(r.activity, true)}` : ''}
    <div class="diff-grid" style="margin-top:12px">
      <div><h4>${t('Eventos do Windows no período')}</h4>${evHtml}</div>
      <div><h4>${t('Quando aconteceu: maiores saltos entre medições')}</h4>${jumps}</div>
    </div>`;
  box.querySelectorAll('.drow[data-dir]').forEach(el => el.onclick = () => {
    S.snap = S.state.snapshots.find(s => s.id === r.b.id) || S.snap;
    renderPickers(false);
    goExplore(+el.dataset.dir);
  });
  // category delta chart
  const rows = Object.entries(r.cat_delta).filter(([, v]) => Math.abs(v) >= 1024 ** 2).map(([c, v]) => ({ name: catLabel(c), v, color: catColor(c) }))
    .concat(r.hidden_delta.filter(h => Math.abs(h.delta) >= 1024 ** 2).map(h => ({ name: HIDLABEL[h.key] ? t(HIDLABEL[h.key]) : h.label, v: h.delta, color: HIDSOLID[h.key] || '#999' })))
    .sort((a, b) => a.v - b.v);
  if (S.chart2) S.chart2.dispose();
  S.chart2 = echarts.init($('#catDelta'));
  S.chart2.setOption({
    grid: { left: 200, right: 70, top: 10, bottom: 24 },
    tooltip: { formatter: p => `<b>${esc(p.name)}</b><br>${sgn(p.value)}` },
    xAxis: { type: 'value', splitNumber: 3, axisLabel: { formatter: v => sgn(v), color: cssVar('--muted'), hideOverlap: true }, splitLine: { lineStyle: { color: cssVar('--line') } } },
    yAxis: { type: 'category', data: rows.map(x => x.name), axisLabel: { color: cssVar('--text') }, axisTick: { show: false } },
    series: [{ type: 'bar', data: rows.map(x => ({ value: x.v, itemStyle: { color: x.color, borderRadius: 3 } })),
      label: { show: true, position: 'right', formatter: p => sgn(p.value), color: cssVar('--muted') } }],
  });
}

/* ------------------------------------------------------------------ modal */
function showModal(html, wide) {
  $('#modalBox').innerHTML = html;
  $('#modalBox').classList.toggle('wide', !!wide);
  $('#modal').hidden = false;
  $('#modal').onclick = e => { if (e.target.id === 'modal') closeModal(); };
}
function closeModal() { $('#modal').hidden = true; $('#modalBox').innerHTML = ''; }

/* ------------------------------------------------------------------ Liberar espaço (cleanup) */
S.cl = { data: null, sel: new Set(), done: new Set(), onlyThisDrive: true, open: { safe: true, tools: false, review: true, programs: false, unknown: false, history: false } };
const VERDICT = { apagar: ['Apagar', 'ok'], desinstalar: ['Desinstalar', 'warn'], manter: ['Manter', 'keep'], revisar: ['Revisar', 'rev'] };
const GROUP = { downloads: 'Instalador antigo em Downloads', old_big: 'Arquivo grande antigo', duplicate: 'Provável duplicado', dev: 'Dependências de projeto parado' };
const ACTION_LABEL = { esvaziar: 'Esvaziar (definitivo)', lixeira: 'Enviar à Lixeira', desinstalar: 'Desinstalar', ferramenta: 'Ferramenta do Windows' };

async function renderCleanup() {
  loading();
  let d;
  try { d = await api(`cleanup?volume=${S.volume}`); } catch (e) { return showError(e); }
  S.cl.data = d;
  if (!d.available) return renderEmpty();
  if (!S.aiSettings) await loadAISettings();
  S.safety = d.safety || S.safety;
  if (!S.cl.initDone) {            // safe caches start selected; everything else is opt-in
    d.safe.forEach(i => S.cl.sel.add(i.id));
    S.cl.initDone = true;
  }
  drawCleanup();
}

function clItems() {
  const d = S.cl.data;
  return [...d.safe, ...d.tools, ...d.review].filter(i => !S.cl.done.has(i.id));
}
function aiOf(id) { const a = S.cl.data.ai; return a ? a.items.find(x => x.id === id) : null; }
function aiBadge(id) {
  const v = aiOf(id); if (!v) return '';
  const [label, c] = VERDICT[v.verdict] || ['?', 'rev'];
  return `<span class="aib ${c}" title="${esc(v.reason)}">${t('IA: {verdict}', { verdict: t(label) })}</span>`;
}

function drawCleanup() {
  const d = S.cl.data;
  const live = clItems();
  const sum = list => list.reduce((a, i) => a + (i.size || 0), 0);
  const safe = d.safe.filter(i => !S.cl.done.has(i.id)), review = d.review.filter(i => !S.cl.done.has(i.id));
  const progs = d.programs.filter(p => !S.cl.onlyThisDrive || !p.drive || p.drive === S.volume);
  const a = d.ai;
  const rb = d.recycle_bin;
  const stale = (Date.now() / 1000 - d.snapshot.finished) / 3600;

  const sec = (key, title, sub, body, count) => `<div class="card clsec">
      <div class="clhead" data-toggle="${key}"><span class="chev">${S.cl.open[key] ? '▾' : '▸'}</span><h3>${title}</h3>
        <span class="muted">${sub}</span><span class="cnt">${count}</span></div>
      ${S.cl.open[key] ? `<div class="clbody">${body}</div>` : ''}</div>`;

  const row = (i, opts = {}) => {
    const checked = S.cl.sel.has(i.id), dis = i.needs_admin;
    const v = aiOf(i.id);
    return `<div class="clrow ${checked ? 'on' : ''} ${dis ? 'dis' : ''}">
      <input type="checkbox" data-id="${i.id}" ${checked ? 'checked' : ''} ${dis ? 'disabled' : ''}>
      <div class="cltxt">
        <div><b>${esc(opts.title || i.label || i.path.split('\\').pop())}</b> ${aiBadge(i.id)} ${opts.tag || ''}</div>
        ${i.path ? `<div class="pth2 muted">${esc(i.path)}</div>` : ''}
        <div class="muted small">${esc(opts.desc || i.reason || i.desc || '')}${dis ? ` · <b>${t('precisa abrir o DiskLens como administrador')}</b>` : ''}</div>
        ${v ? `<div class="aireason">${esc(v.reason)}</div>` : ''}
      </div>
      <div class="clsize">${i.size == null ? `<span class="muted">${t('varia')}</span>` : fmt(i.size)}</div>
      <div class="clact">${i.path ? `<button class="btn small" data-open="${esc(i.path)}" title="${t('Mostrar no Explorer')}">📂</button>` : ''}
        ${opts.ask ? `<button class="btn small" data-ask="${esc(i.path)}" title="${t('Perguntar à IA o que é')}">?</button>` : ''}</div>
    </div>`;
  };

  const props = d.proposals.map(p => `<div class="card prop">
      <div class="clhead"><h3>${p.source === 'mcp' ? t('Proposta de um app de IA (MCP): {title}', { title: esc(p.title || '') }) : t('Proposta da IA: {title}', { title: esc(p.title || '') })}</h3>
        <span class="muted">${fmtDate(p.ts)}</span></div>
      ${p.items.map((it, k) => `<div class="clrow on"><input type="checkbox" data-prop="${p.id}" data-k="${k}" checked>
        <div class="cltxt"><div><b>${esc(t(ACTION_LABEL[it.action] || it.action))}</b>:
          ${esc(it.path || it.name || (d.tools_meta[it.tool] || {}).label || '')}</div>
          <div class="aireason">${esc(it.reason)}</div>${it.warning ? `<div class="muted small">⚠ ${esc(it.warning)}</div>` : ''}</div>
        <div class="clsize">${it.size ? fmt(it.size) : ''}</div><div></div></div>`).join('')}
      ${p.refused.length ? `<details class="muted small"><summary>${t('{n} item(ns) recusado(s) pelo DiskLens por segurança', { n: p.refused.length })}</summary>
        ${p.refused.map(r => `<div>• ${esc(r.path || r.program_id || r.tool || '?')}: ${esc(r.refused)}</div>`).join('')}</details>` : ''}
      <div class="actions"><button class="btn primary" data-runprop="${p.id}">${t('Revisar e executar selecionados')}</button>
        <button class="btn" data-dismiss="${p.id}">${t('Descartar proposta')}</button></div></div>`).join('');

  const progRows = progs.map(p => {
    const v = aiOf(p.id);
    const meta = [esc(p.publisher), p.version ? 'v' + esc(p.version) : '', p.drive ? t('disco {drive}', { drive: p.drive }) : '',
      p.installed ? t('instalado {ago}', { ago: ago(p.installed) }) : '', p.last_change ? t('última alteração na pasta {ago}', { ago: ago(p.last_change) }) : ''].filter(Boolean).join(' · ');
    return `<div class="clrow">
      <div></div>
      <div class="cltxt"><div><b>${esc(p.name)}</b> ${p.dependency ? `<span class="chip warn" title="${t('Outros programas costumam depender disto')}">${t('dependência')}</span>` : ''} ${aiBadge(p.id)}</div>
        <div class="muted small">${meta}</div>
        ${v ? `<div class="aireason">${esc(v.reason)}</div>` : ''}</div>
      <div class="clsize">${p.size ? fmt(p.size) : '<span class="muted">?</span>'}</div>
      <div class="clact"><button class="btn small" data-uninstall="${p.id}" data-name="${esc(p.name)}">${t('Desinstalar')}</button></div></div>`;
  }).join('');

  const unk = d.unknown.map(u => `<div class="clrow"><div></div>
      <div class="cltxt"><div><b>${esc(u.path)}</b> ${aiBadge(u.id)}</div>
        <div class="muted small">${t('Contém: {list}', { list: u.children.map(k => esc(k.name)).join(', ') || '—' })}</div>
        ${aiOf(u.id) ? `<div class="aireason">${esc(aiOf(u.id).reason)}</div>` : ''}<div class="explain" id="ex-${u.id}"></div></div>
      <div class="clsize">${fmt(u.size)}</div>
      <div class="clact"><button class="btn small" data-ask="${esc(u.path)}" data-target="ex-${u.id}">${t('Perguntar à IA')}</button>
        <button class="btn small" data-explore="${u.dir}">${t('Ver')}</button></div></div>`).join('');

  const hist = d.history.length ? `<table class="grid fixed"><colgroup><col style="width:130px"><col><col style="width:100px"><col style="width:90px"></colgroup>
      <tbody>${d.history.map(h => `<tr><td class="muted">${fmtDate(h.ts)}</td><td class="p" title="${esc(h.target)}">&lrm;${esc(h.target)}&lrm; <span class="muted">${esc(h.message)}</span></td>
      <td>${esc(t(ACTION_LABEL[h.action] || h.action))}</td><td class="num">${h.ok ? fmt(h.bytes) : `<span class="up">${t('falhou')}</span>`}</td></tr>`).join('')}</tbody></table>` : `<p class="muted">${t('Nenhuma limpeza feita ainda.')}</p>`;

  const selItems = live.filter(i => S.cl.sel.has(i.id));
  $('#main').innerHTML = `<div class="cl">
    <div class="clhero card">
      <div><div class="muted">${t('Dá para liberar no disco {drive}', { drive: esc(S.volume) })}</div>
        <div class="big">${fmt(sum(safe) + sum(review))}</div>
        <div class="muted small">${t('{safe} com segurança, sem você precisar decidir · {review} para você revisar · mais o que você desinstalar', { safe: fmt(sum(safe)), review: fmt(sum(review)) })}</div></div>
      <div class="clside">
        <div class="rbin">🗑 ${t('Lixeira')}: <b>${fmt(rb.bytes)}</b> (${t('{n} itens', { n: fmtN(rb.items) })}) ${rb.bytes > 0 ? `<button class="btn small" id="emptyRb">${t('Esvaziar')}</button>` : ''}</div>
        <div class="muted small">${t('Baseado no scan de {when}', { when: fmtDate(d.snapshot.finished) })}${stale > 12 ? ` · <b>${t('faça um novo scan para números atualizados')}</b>` : ''}</div>
      </div>
    </div>
    <div id="clResult"></div>
    <div class="card aicard">
      ${aiCardHtml(a)}
    </div>
    ${props}
    ${sec('safe', t('Limpeza segura'), t('caches e temporários: são recriados automaticamente e apagados de vez'), `
      ${safe.map(i => row(i, { desc: i.desc })).join('') || `<p class="muted">${t('Nada a limpar aqui.')}</p>`}`, `${safe.length} · ${fmt(sum(safe))}`)}
    ${sec('tools', t('Ferramentas do Windows'), t('limpezas oficiais do próprio Windows'), d.tools.map(i => row(i, { desc: i.desc })).join(''), d.tools.length)}
    ${sec('review', t('Para revisar'), t('arquivos seus que parecem dispensáveis: vão para a Lixeira'), `
      ${review.map(i => row(i, { title: i.path.split('\\').pop(), tag: `<span class="chip">${t(GROUP[i.group] || i.group)}</span>`, ask: false })).join('') || `<p class="muted">${t('Nenhum candidato encontrado.')}</p>`}`, `${review.length} · ${fmt(sum(review))}`)}
    ${sec('programs', t('Programas instalados'), t('desinstalar abre o desinstalador oficial do programa'), `
      <label class="muted small"><input type="checkbox" id="onlyDrive" ${S.cl.onlyThisDrive ? 'checked' : ''}> ${t('mostrar só programas instalados no disco {drive}', { drive: esc(S.volume) })}</label>
      ${progRows}`, progs.length)}
    ${sec('unknown', t('Pastas grandes desconhecidas'), t('o DiskLens não sabe o que são: pergunte à IA'), unk || `<p class="muted">${t('Nenhuma.')}</p>`, d.unknown.length)}
    ${sec('history', t('Histórico de limpezas'), '', hist, d.history.length)}
    <div class="clbar"><span>${t('{n} selecionado(s)', { n: selItems.length })} · <b>${fmt(sum(selItems))}</b></span>
      <button class="btn primary" id="clGo" ${selItems.length ? '' : 'disabled'}>${t('Revisar e confirmar')}</button></div>
  </div>`;
  bindCleanup();
}

function aiCardHtml(a) {
  const conf = S.aiSettings;
  const provLabel = conf && conf.provider ? `${conf.providers[conf.provider].label}${conf.model ? ' · ' + esc(conf.model) : ''}` : '';
  return `<div class="aihead"><h3>${t('Assistente de IA')}</h3>
      <span class="muted small">${provLabel ? t('Usando {provider}', { provider: provLabel }) : t('Nenhum provedor configurado')}</span>
      <div class="actions" style="margin:0 0 0 auto">
        <button class="btn small" id="aiConf">${t('Configurar')}</button>
        <button class="btn small" id="mcpConf" title="${t('Para usar o DiskLens a partir do app do Claude, Cursor, VS Code, Gemini CLI etc.')}">${t('Usar em outro app de IA (MCP)')}</button>
        <button class="btn primary small" id="aiGo">${a ? t('Analisar de novo') : t('Analisar com IA')}</button></div></div>
    ${a ? `<p class="aisum">${esc(a.summary)}</p>
      <div class="muted small">${t('Análise de {when} por {provider} / {model}. A IA só recomenda: nada é apagado sem a sua confirmação.', { when: fmtDate(a.ts), provider: esc(a.provider), model: esc(a.model) })}
      <button class="btn small" id="aiPick">${t('Selecionar o que a IA recomendou apagar')}</button></div>`
      : `<p class="muted small">${t('A IA olha os candidatos, os programas e as pastas desconhecidas e recomenda o que apagar, desinstalar ou manter, explicando o porquê. Funciona com OpenAI, Google Gemini, DeepSeek, Anthropic ou uma IA local (Ollama).')}</p>`}`;
}

function bindCleanup() {
  const m = $('#main');
  m.querySelectorAll('[data-toggle]').forEach(h => h.onclick = () => { const k = h.dataset.toggle; S.cl.open[k] = !S.cl.open[k]; drawCleanup(); });
  m.querySelectorAll('input[data-id]').forEach(c => c.onchange = () => { c.checked ? S.cl.sel.add(c.dataset.id) : S.cl.sel.delete(c.dataset.id); drawCleanup(); });
  m.querySelectorAll('[data-open]').forEach(b => b.onclick = () => api('open_path', { path: b.dataset.open }).catch(() => toast(t('Use “Ver” na aba Explorar.'))));
  m.querySelectorAll('[data-explore]').forEach(b => b.onclick = () => goExplore(+b.dataset.explore));
  m.querySelectorAll('[data-ask]').forEach(b => b.onclick = () => askAI(b.dataset.ask, b.dataset.target));
  m.querySelectorAll('[data-uninstall]').forEach(b => b.onclick = () => confirmUninstall(b.dataset.uninstall, b.dataset.name));
  m.querySelectorAll('[data-dismiss]').forEach(b => b.onclick = async () => { await api('proposals/dismiss', { id: b.dataset.dismiss }); renderCleanup(); });
  m.querySelectorAll('[data-runprop]').forEach(b => b.onclick = () => {
    const p = S.cl.data.proposals.find(x => x.id === b.dataset.runprop);
    const chosen = p.items.filter((_, k) => { const c = m.querySelector(`input[data-prop="${p.id}"][data-k="${k}"]`); return c && c.checked; });
    confirmRun(chosen.map(it => ({ ...it })), p.id);
  });
  const od = $('#onlyDrive'); if (od) od.onchange = e => { S.cl.onlyThisDrive = e.target.checked; drawCleanup(); };
  const rb = $('#emptyRb'); if (rb) rb.onclick = () => confirmRun([{ action: 'ferramenta', tool: 'empty_recycle', label: t('Esvaziar a Lixeira'), size: S.cl.data.recycle_bin.bytes }]);
  $('#aiConf').onclick = () => aiSettingsModal(false);
  $('#mcpConf').onclick = () => mcpModal();
  $('#aiGo').onclick = runAI;
  const pick = $('#aiPick'); if (pick) pick.onclick = () => {
    let n = 0;
    clItems().forEach(i => { const v = aiOf(i.id); if (v && v.verdict === 'apagar' && !i.needs_admin) { S.cl.sel.add(i.id); n++; } });
    toast(t('{n} item(ns) selecionado(s). Programas são desinstalados um a um, pelo botão de cada um.', { n })); drawCleanup();
  };
  $('#clGo').onclick = () => {
    const sel = clItems().filter(i => S.cl.sel.has(i.id));
    confirmRun(sel.map(i => i.kind === 'tool' ? { action: 'ferramenta', tool: i.tool, label: i.label, size: i.size, _id: i.id }
      : { action: i.kind === 'safe' ? 'esvaziar' : 'lixeira', path: i.path, label: i.label, size: i.size, _id: i.id }));
  };
}

function confirmRun(items, proposalId) {
  if (!items.length) return toast(t('Nada selecionado.'));
  const grp = { esvaziar: [], lixeira: [], ferramenta: [], desinstalar: [] };
  items.forEach(i => grp[i.action] && grp[i.action].push(i));
  const total = list => list.reduce((a, i) => a + (i.size || 0), 0);
  const list = l => `<ul class="cflist">${l.map(i => `<li><span>${esc(i.path || i.name || i.label || (S.cl.data.tools_meta[i.tool] || {}).label)}</span><b>${i.size ? fmt(i.size) : ''}</b></li>`).join('')}</ul>`;
  showModal(`<h2>${t('Confirmar limpeza')}</h2>
    ${grp.esvaziar.length ? `<h4>${t('Apagar definitivamente (caches e temporários) · {size}', { size: fmt(total(grp.esvaziar)) })}</h4>${list(grp.esvaziar)}` : ''}
    ${grp.lixeira.length ? `<h4>${t('Enviar para a Lixeira · {size}', { size: fmt(total(grp.lixeira)) })}</h4>
      <div class="hid-note">${t('Arquivos na Lixeira <b>continuam ocupando espaço</b> até você esvaziá-la. Assim dá para desfazer se algo fizer falta.')}</div>${list(grp.lixeira)}` : ''}
    ${grp.ferramenta.length ? `<h4>${t('Ferramentas do Windows')}</h4>${list(grp.ferramenta)}${grp.ferramenta.some(i => i.tool === 'dism') ? `<div class="muted small">${t('A limpeza de componentes (DISM) pode levar de 5 a 30 minutos.')}</div>` : ''}` : ''}
    ${grp.desinstalar.length ? `<h4>${t('Abrir desinstaladores')}</h4>${list(grp.desinstalar)}` : ''}
    <p class="muted small">${t('Arquivos em uso são pulados. Pastas do Windows e de programas instalados são recusadas automaticamente.')}</p>
    <div class="actions"><button class="btn primary" id="cfOk">${t('Confirmar')}</button><button class="btn" id="cfNo">${t('Cancelar')}</button></div>`, true);
  $('#cfNo').onclick = closeModal;
  $('#cfOk').onclick = async () => {
    closeModal();
    try {
      const r = await api('cleanup/run', { items: items.map(({ _id, label, ...x }) => x), proposal: proposalId || null });
      pollJob(r.job, items);
    } catch (e) { toast(e.message); }
  };
}

function resultHtml(j) {
  const fails = j.results.filter(r => !r.ok);
  return `<div class="card result ${fails.length ? 'warnb' : 'okb'}">
      <h3>${t('Limpeza concluída')}</h3>
      <p>${t('<b>{size}</b> liberados agora', { size: fmt(j.freed) })}${j.disk_freed > 0 ? ' ' + t('(espaço livre no disco aumentou {size})', { size: fmt(j.disk_freed) }) : ''}.</p>
      ${j.recycled ? `<div class="hid-note">${t('<b>{size} foram para a Lixeira</b> e ainda ocupam espaço no disco. É por isso que o espaço livre não aumentou com eles. Quando tiver certeza de que não vai precisar desses arquivos, esvazie a Lixeira.', { size: fmt(j.recycled) })}
        <div class="actions"><button class="btn primary small" id="rbNow">${t('Esvaziar a Lixeira agora ({size})', { size: fmt(j.recycle_bin.bytes) })}</button></div></div>` : ''}
      ${fails.length ? `<details open><summary>${t('{n} item(ns) não puderam ser concluídos', { n: fails.length })}</summary>${fails.map(f => `<div class="small">• ${esc(f.target)}: ${esc(f.message)}</div>`).join('')}</details>` : ''}
      ${j.results.filter(r => r.ok && r.message).map(r => `<div class="small muted">✓ ${esc(r.target)}: ${esc(r.message)}</div>`).join('')}
      <p class="muted small">${t('Os tamanhos mostrados vêm do último scan; faça um novo scan para ver os números atualizados.')}</p></div>`;
}

function pollJob(id, items) {
  let warned = false;
  const tick = async () => {
    let j;
    try { j = await api('cleanup/job?id=' + id); } catch (e) { toast(e.message); return; }
    const box = S.tab === 'cleanup' ? $('#clResult') : null;
    if (j.state === 'running') {
      if (box) box.innerHTML = `<div class="card running"><div class="spinner small"></div> ${t('Executando {i} de {n}:', { i: j.done + 1, n: j.total })} <span class="muted">${esc(j.current)}</span></div>`;
      else if (!warned) { toast(t('Executando…')); warned = true; }
      return setTimeout(tick, 800);
    }
    items.forEach((it, k) => { if (j.results[k] && j.results[k].ok && it._id) { S.cl.done.add(it._id); S.cl.sel.delete(it._id); } });
    const rbBtn = () => { const rbn = $('#rbNow'); if (rbn) rbn.onclick = () => confirmRun([{ action: 'ferramenta', tool: 'empty_recycle', label: t('Esvaziar a Lixeira'), size: j.recycle_bin.bytes }]); };
    if (S.tab === 'cleanup') {
      await renderCleanup();
      const b = $('#clResult');
      b.innerHTML = resultHtml(j);
      rbBtn();
      b.scrollIntoView({ behavior: 'smooth' });
    } else {
      showModal(resultHtml(j) + `<div class="actions"><button class="btn" onclick="closeModal()">${t('Fechar')}</button></div>`, true);
      rbBtn();
    }
  };
  tick();
}

async function trashFromExplorer(path, size) {
  let c;
  try { c = await api('cleanup/check?path=' + encodeURIComponent(path)); } catch (e) { return toast(e.message); }
  if (c.blocked) {
    return showModal(`<h2>${t('Não é possível mandar para a Lixeira')}</h2><p><b>${esc(path)}</b></p><p>${esc(c.blocked)}.</p>
      <p class="muted small">${t('O DiskLens protege pastas do Windows, de programas instalados e as pastas padrão do seu perfil. Para caches e programas, use a aba “Liberar espaço”.')}</p>
      <div class="actions"><button class="btn" onclick="closeModal()">${t('Fechar')}</button></div>`);
  }
  confirmRun([{ action: 'lixeira', path, size }]);
}

function confirmUninstall(id, name) {
  showModal(`<h2>${t('Desinstalar {name}?', { name: esc(name) })}</h2>
    <p>${t('O DiskLens vai abrir o <b>desinstalador oficial</b> do programa. Siga as instruções na janela dele (ele pode pedir permissão de administrador).')}</p>
    ${aiOf(id) ? `<div class="aireason">${t('IA: {verdict}', { verdict: esc(aiOf(id).reason) })}</div>` : ''}
    <div class="actions"><button class="btn primary" id="unOk">${t('Abrir desinstalador')}</button><button class="btn" id="unNo">${t('Cancelar')}</button></div>`);
  $('#unNo').onclick = closeModal;
  $('#unOk').onclick = async () => {
    closeModal();
    try { const r = await api('cleanup/run', { items: [{ action: 'desinstalar', program_id: id }] }); pollJob(r.job, [{ action: 'desinstalar', name }]); }
    catch (e) { toast(e.message); }
  };
}

async function loadAISettings() { try { S.aiSettings = await api('ai/settings'); } catch { S.aiSettings = null; } }

async function runAI() {
  await loadAISettings();
  const s = S.aiSettings;
  if (!s || !s.provider) return aiSettingsModal();
  if (!s.consented) return aiSettingsModal(true);
  const b = $('#aiGo'); b.disabled = true; b.textContent = t('Analisando… (pode levar 1 a 2 minutos)');
  try { await api('ai/analyze', { volume: S.volume }); toast(t('Análise concluída.')); await renderCleanup(); }
  catch (e) { toast(e.message); b.disabled = false; b.textContent = t('Analisar com IA'); }
}

async function askAI(path, targetId) {
  await loadAISettings();
  const s = S.aiSettings;
  if (!s || !s.provider) return aiSettingsModal();
  if (!s.consented) return aiSettingsModal(true);
  const target = targetId ? $('#' + targetId) : null;
  if (target) target.innerHTML = `<span class="muted small">${t('Perguntando à IA…')}</span>`;
  try {
    const r = await api('ai/explain', { path });
    const html = `<div class="aireason">${esc(r.text).replace(/\n/g, '<br>')}<div class="muted small">— ${esc(r.provider)} / ${esc(r.model)}</div></div>`;
    if (target) target.innerHTML = html; else showModal(`<h2>${esc(path)}</h2>${html}<div class="actions"><button class="btn" onclick="closeModal()">${t('Fechar')}</button></div>`);
  } catch (e) { if (target) target.innerHTML = `<span class="up small">${esc(e.message)}</span>`; else toast(e.message); }
}

async function aiSettingsModal(needConsent) {
  await loadAISettings();
  const s = S.aiSettings || { providers: {}, has_key: {} };
  const provs = Object.entries(s.providers);
  const draw = prov => {
    const p = s.providers[prov] || {};
    $('#aiForm').innerHTML = `
      ${p.key ? `<label>${t('Chave de API')} ${s.has_key[prov] ? `<span class="chip ok">${t('salva ✓')}</span>` : ''}
        <input type="password" id="aiKey" placeholder="${s.has_key[prov] ? t('deixe em branco para manter a chave salva') : t('cole aqui a chave de API')}" autocomplete="off"></label>
        <div class="muted small">${t('Fica guardada criptografada pelo Windows (DPAPI): só o seu usuário consegue ler.')}</div>` : ''}
      ${prov === 'ollama' || prov === 'custom' ? `<label>${t('Endereço do serviço')} <input id="aiBase" value="${esc(s.base_url || p.base || '')}" placeholder="http://localhost:11434/v1"></label>` : ''}
      ${prov === 'custom' ? `<label>${t('Chave de API (opcional)')} <input type="password" id="aiKey" autocomplete="off"></label>` : ''}
      <label>${t('Modelo')} <span style="display:flex;gap:6px"><input id="aiModel" list="aiModels" value="${esc(s.provider === prov ? (s.model || p.model || '') : (p.model || ''))}" placeholder="${t('ex.: nome do modelo')}">
        <button class="btn small" id="aiList" type="button">${t('Buscar modelos')}</button></span><datalist id="aiModels"></datalist></label>`;
    $('#aiList').onclick = async () => {
      await saveAI(false);
      try { const r = await api('ai/models'); $('#aiModels').innerHTML = r.models.map(m => `<option value="${esc(m)}">`).join(''); toast(t('{n} modelos encontrados: escolha no campo Modelo.', { n: r.models.length })); }
      catch (e) { toast(e.message); }
    };
  };
  const saveAI = async close => {
    const prov = $('#aiProv').value;
    const body = { provider: prov, model: $('#aiModel') ? $('#aiModel').value : '', consented: $('#aiConsent').checked };
    if ($('#aiKey') && $('#aiKey').value) body.api_key = $('#aiKey').value;
    if ($('#aiBase')) body.base_url = $('#aiBase').value;
    try { S.aiSettings = await api('ai/settings', body); if (close) { closeModal(); toast(t('Configuração salva.')); if (S.tab === 'cleanup') drawCleanup(); } }
    catch (e) { toast(e.message); }
  };
  showModal(`<h2>${t('Assistente de IA')}</h2>
    <label>${t('Provedor')} <select id="aiProv">${provs.map(([k, v]) => `<option value="${k}" ${k === s.provider ? 'selected' : ''}>${esc(v.label)}</option>`).join('')}</select></label>
    <div id="aiForm"></div>
    <div class="hid-note ${needConsent ? 'attn' : ''}"><label><input type="checkbox" id="aiConsent" ${s.consented ? 'checked' : ''}>
      ${t('Entendo que, ao usar um provedor na nuvem, o DiskLens envia <b>caminhos, nomes e tamanhos</b> de pastas e arquivos e os <b>nomes dos programas instalados</b> (nunca o conteúdo dos arquivos). Com o Ollama, nada sai do computador.')}</label></div>
    <div class="actions"><button class="btn primary" id="aiSave">${t('Salvar')}</button><button class="btn" id="aiCancel">${t('Cancelar')}</button></div>`, true);
  draw($('#aiProv').value);
  $('#aiProv').onchange = e => draw(e.target.value);
  $('#aiSave').onclick = () => saveAI(true);
  $('#aiCancel').onclick = closeModal;
}

async function mcpModal() {
  let c;
  try { c = await api('mcp/config'); } catch (e) { return toast(e.message); }
  showModal(`<h2>${t('Usar o DiskLens em outro app de IA (MCP)')}</h2>
    <p>${t('O DiskLens inclui um <b>servidor MCP</b> (Model Context Protocol), um padrão aberto suportado pelo app do Claude, Cursor, VS Code (Copilot), Gemini CLI e outros.')}
    ${t('Depois de configurado, você conversa com a IA no app dela ("o que posso apagar para liberar 10 GB?"): ela consulta os dados do disco e <b>propõe</b> uma limpeza, que aparece aqui nesta aba para você confirmar. A IA não consegue apagar nada sozinha.')}</p>
    <h4>${t('Configuração')}</h4>
    <p class="muted small">${t('Adicione isto à configuração de servidores MCP do seu app (no app do Claude: Configurações → Desenvolvedor → Editar configuração; em geral o arquivo tem uma seção <code>mcpServers</code>):')}</p>
    <pre class="code" id="mcpJson">${esc(c.json)}</pre>
    <div class="actions"><button class="btn primary" id="mcpCopy">${t('Copiar')}</button><button class="btn" id="mcpClose">${t('Fechar')}</button></div>
    <p class="muted small">${t('Ferramentas disponíveis para a IA: resumo do disco, candidatos à limpeza, detalhes de uma pasta, busca de arquivos, mudanças recentes (journal) e propor limpeza.')}</p>`, true);
  $('#mcpCopy').onclick = () => navigator.clipboard.writeText(c.json).then(() => toast(t('Copiado.')), () => toast(t('Selecione o texto e copie com Ctrl+C.')));
  $('#mcpClose').onclick = closeModal;
}

/* ------------------------------------------------------------------ boot */
async function loadI18n() {
  try { Object.assign(I18N, await api('i18n')); } catch { /* keep Portuguese */ }
  document.documentElement.lang = I18N.locale;
  document.querySelectorAll('[data-t]').forEach(el => { el.textContent = t(el.dataset.t); });
  const ls = $('#langSel');
  ls.innerHTML = Object.entries(I18N.langs).map(([k, v]) => `<option value="${k}" ${k === I18N.lang ? 'selected' : ''}>${esc(v)}</option>`).join('');
  ls.onchange = async () => {
    try { await api('settings', { lang: ls.value }); location.reload(); } catch (e) { toast(e.message); }
  };
}

function bindUI() {
  document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => setTab(b.dataset.tab));
  $('#volSel').onchange = async e => { S.volume = e.target.value; S.snap = null; S.dir = 1; await refreshState(true); render(); };
  $('#snapSel').onchange = e => { S.snap = snapsForVolume().find(s => s.id === +e.target.value); S.dir = 1; render(); };
  window.addEventListener('resize', () => { [S.chart, S.chart2, S.chart3].forEach(c => c && c.resize()); });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => render());
}

(async function init() {
  await loadI18n();
  bindUI();
  try { await refreshState(true); } catch (e) { return showError(e); }
  render();
})();
