/* JOGA Barbearia — helpers compartilhados (vanilla JS, mobile-first) */

async function api(url, opts = {}) {
  const cfg = { credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, ...opts };
  if (cfg.body && typeof cfg.body !== 'string') cfg.body = JSON.stringify(cfg.body);
  const r = await fetch(url, cfg);
  if (r.status === 401) { location.href = '/login'; throw new Error('Não autenticado'); }
  if (r.status === 403) {
    const j = await r.json().catch(() => ({}));
    if (j.redirect) { location.href = j.redirect; throw new Error(j.error || ''); }
    throw new Error(j.error || 'Acesso negado');
  }
  const j = await r.json().catch(() => ({ ok: false, error: 'Resposta inválida' }));
  if (!r.ok || j.ok === false) throw new Error(j.error || `Erro ${r.status}`);
  return j;
}

const BRL = new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' });
const money = v => BRL.format(Number(v || 0));
function fmtData(iso) { if (!iso) return '—'; const d = new Date(iso + (iso.length === 10 ? 'T00:00:00' : '')); return d.toLocaleDateString('pt-BR'); }
function hhmm(t) { return t ? String(t).slice(0, 5) : ''; }
function hoje() { return new Date().toISOString().slice(0, 10); }
function escapeHtml(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }

// Máscara de telefone BR: só dígitos → (DD) XXXXX-XXXX (celular) ou (DD) XXXX-XXXX (fixo)
function maskTelefone(v) {
  const d = String(v || '').replace(/\D/g, '').slice(0, 11);
  const len = d.length;
  if (len === 0) return '';
  if (len < 3) return '(' + d;
  const ddd = d.slice(0, 2), resto = d.slice(2);
  if (len <= 6) return `(${ddd}) ${resto}`;
  if (len <= 10) return `(${ddd}) ${resto.slice(0, 4)}-${resto.slice(4)}`;
  return `(${ddd}) ${resto.slice(0, 5)}-${resto.slice(5)}`;
}

function toast(msg, tipo = 'ok') {
  let t = document.getElementById('toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.className = 'show ' + tipo;
  clearTimeout(t._tmr); t._tmr = setTimeout(() => t.classList.remove('show'), 3000);
}

const DIAS_SEMANA = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb'];

// ── Shell: bottom nav + sheet "Mais" ──
const NAV_PRIMARIO = [
  { href: '/', ic: '📅', label: 'Agenda' },
  { href: '/comanda', ic: '🧾', label: 'Comandas' },
  { href: '/clientes', ic: '👤', label: 'Clientes' },
  { href: '/caixa', ic: '💵', label: 'Caixa' },
];
const NAV_MAIS = [
  { href: '/assinaturas', ic: '⭐', label: 'Assinaturas' },
  { href: '/despesas', ic: '💸', label: 'Despesas' },
  { href: '/relatorios', ic: '✂️', label: 'Comissões' },
  { href: '/dre', ic: '📈', label: 'Resultado' },
  { href: '/config', ic: '⚙️', label: 'Configurações', role: 'dono' },
];

let ME = null;

async function renderShell(active) {
  try { ME = (await api('/api/me')); } catch (e) { return; }
  const role = ME.role;

  // Barbeiro tem navegação enxuta
  if (role === 'barbeiro') {
    montarBottomNav(active, [
      { href: '/', ic: '📅', label: 'Minha agenda' },
      { href: '/barbeiro', ic: '💈', label: 'Minha comissão' },
    ], []);
    return;
  }
  montarBottomNav(active, NAV_PRIMARIO, NAV_MAIS.filter(i => !i.role || i.role === role));
}

function montarBottomNav(active, principais, mais) {
  const nav = document.createElement('nav');
  nav.className = 'bottomnav';
  let html = principais.map(i =>
    `<a href="${i.href}" class="${i.href === active ? 'active' : ''}"><span class="ic">${i.ic}</span>${i.label}</a>`).join('');
  if (mais.length) html += `<a href="#" onclick="abrirSheet(event)" class="${active === '/mais' ? 'active' : ''}"><span class="ic">☰</span>Mais</a>`;
  nav.innerHTML = html;
  document.body.appendChild(nav);

  if (mais.length) {
    const bg = document.createElement('div'); bg.className = 'sheet-bg'; bg.id = 'sheetbg'; bg.onclick = fecharSheet;
    const sheet = document.createElement('div'); sheet.className = 'sheet'; sheet.id = 'sheet';
    sheet.innerHTML = `<div class="grab"></div>` +
      mais.map(i => `<a href="${i.href}"><span class="ic">${i.ic}</span>${i.label}</a>`).join('') +
      `<a href="/logout" class="danger"><span class="ic">⎋</span>Sair</a>`;
    sheet.onclick = e => e.stopPropagation();
    document.body.appendChild(bg); document.body.appendChild(sheet);
  }
}
function abrirSheet(e) { if (e) e.preventDefault(); document.getElementById('sheetbg').classList.add('open'); document.getElementById('sheet').classList.add('open'); }
function fecharSheet() { document.getElementById('sheetbg').classList.remove('open'); document.getElementById('sheet').classList.remove('open'); }

function openModal(id) { document.getElementById(id).classList.add('open'); }
function closeModal(id) { document.getElementById(id).classList.remove('open'); }
