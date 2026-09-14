/* MailClient Web —— 前端逻辑（原生 JS，无构建步骤） */

const S = {
  accounts: [],
  providers: [],
  folders: [],
  domainProvider: {},
  user: null,
  build: '',             // 界面构建号，用于一眼确认没有加载到缓存里的旧脚本
  account: 'all',        // 'all' 或账号 id
  folder: 'INBOX',
  keyword: '',
  selected: null,
  mails: [],
  syncTimer: null,
  oauthTimer: null,
  oauthId: null,
};

const $ = (id) => document.getElementById(id);

/* ---------------------------------------------------------------- 工具 */
async function api(url, opts) {
  const r = await fetch(url, Object.assign({ headers: { 'Content-Type': 'application/json' } }, opts));
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ('请求失败 HTTP ' + r.status));
  return data;
}

let toastTimer = null;
function toast(text) {
  const el = $('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

const COLORS = ['#2f6bff', '#00b894', '#e17055', '#8e44ad', '#0984e3', '#d63031', '#00997b'];
function colorOf(key) {
  let h = 0;
  for (const ch of String(key || '?')) h = (h * 31 + ch.charCodeAt(0)) % 997;
  return COLORS[h % COLORS.length];
}
function initials(name, email) {
  const src = (name || email || '?').trim();
  const m = src.match(/[\u4e00-\u9fa5]/);
  if (m) return src.slice(0, 1);
  return src.slice(0, 2).toUpperCase();
}
function parseDate(value) {
  if (!value) return null;
  const d = new Date(value);
  return isNaN(d.getTime()) ? null : d;
}
function fmtListDate(value) {
  const d = parseDate(value);
  if (!d) return (value || '').slice(0, 16);
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  if (d.toDateString() === now.toDateString()) return pad(d.getHours()) + ':' + pad(d.getMinutes());
  if (d.getFullYear() === now.getFullYear()) return (d.getMonth() + 1) + '-' + pad(d.getDate());
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}
function fmtFullDate(value) {
  const d = parseDate(value);
  if (!d) return value || '';
  const pad = (n) => String(n).padStart(2, '0');
  const w = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'][d.getDay()];
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${w} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------------------------------------------------------------- 初始化 */
async function bootstrap() {
  const data = await api('/api/bootstrap');
  S.accounts = data.accounts;
  S.providers = data.providers;
  S.folders = data.folders;
  S.domainProvider = data.domain_provider || {};
  S.user = data.user || null;
  S.build = data.build || '';
  console.log('MailClient UI build:', S.build || '(未返回，可能加载了缓存里的旧脚本)');

  if (S.account !== 'all' && !S.accounts.some((a) => String(a.id) === String(S.account))) {
    S.account = 'all';
  }
  renderUser();
  renderAccounts();
  renderFolders();
  fillProviderSelect();
  fillComposeAccounts();
  applySync(data.sync);
  loadList();
}

/* ---------------------------------------------------------------- 版本自检 */
/* 浏览器可能一直在跑缓存里的旧 app.js（改完代码界面纹丝不动，踩过两次）。
   这里做双保险：
     1. 页面里的构建号（服务端渲染，最权威）vs 接口回的构建号 —— 不一致说明
        html 与 js 不是同一版，直接提示刷新；
     2. 每 60 秒问一次 /api/version，服务端换新版了就提示刷新。 */
let _buildBarShown = false;

function showUpdateBar() {
  if (_buildBarShown) return;
  _buildBarShown = true;
  const bar = $('updateBar');
  if (bar) bar.style.display = 'flex';
}

function watchBuild(current) {
  if (!current) return;
  setInterval(async () => {
    try {
      const r = await fetch('/api/version', { cache: 'no-store' });
      const d = await r.json();
      if (d.build && d.build !== current) showUpdateBar();
    } catch (e) { /* 网络抖动忽略 */ }
  }, 60000);
}

/* ---------------------------------------------------------------- 当前用户 */
function renderUser() {
  const vb = $('verBuild');
  const pageBuild = (document.querySelector('meta[name="mc-build"]') || {}).content || '';
  if (vb && S.build) vb.textContent = S.build;
  if (pageBuild && S.build && pageBuild !== S.build) showUpdateBar();
  watchBuild(pageBuild || S.build);
  const u = S.user;
  if (!u) return;
  $('userName').textContent = u.username;
  $('userAvatar').textContent = (u.username || '?').slice(0, 1).toUpperCase();
  $('userRole').textContent = u.is_default_pwd ? '管理员 · 初始密码' : '管理员';
  // 还挂着初始密码时，中栏顶部给一条提醒（改完自动消失）
  $('pwdBar').classList.toggle('show', !!u.is_default_pwd);
}

/* ---------------------------------------------------------------- 修改密码 */
function openPwdModal() {
  $('pwdOld').value = ''; $('pwdNew').value = ''; $('pwdNew2').value = '';
  $('pwdMsg').style.display = 'none';
  $('pwdMask').classList.add('show');
  setTimeout(() => $('pwdOld').focus(), 60);
}
function closePwdModal() { $('pwdMask').classList.remove('show'); }

$('btnPwd').onclick = openPwdModal;
$('btnPwd2').onclick = openPwdModal;
$('pwdCancel').onclick = closePwdModal;
$('pwdMask').addEventListener('click', (e) => { if (e.target === $('pwdMask')) closePwdModal(); });
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closePwdModal(); });
['pwdNew', 'pwdNew2'].forEach((id) => $(id).addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('pwdSave').click();
}));

// 调试/分享用：#pwd 深链直接打开修改密码弹窗
if ((location.hash || '') === '#pwd') openPwdModal();

$('pwdSave').onclick = async () => {
  const oldPwd = $('pwdOld').value;
  const newPwd = $('pwdNew').value;
  const again = $('pwdNew2').value;
  if (!oldPwd) return showMsg('pwdMsg', '请输入当前密码');
  if (newPwd.length < 6) return showMsg('pwdMsg', '新密码至少 6 位');
  if (newPwd !== again) return showMsg('pwdMsg', '两次输入的新密码不一致');
  if (newPwd === oldPwd) return showMsg('pwdMsg', '新密码不能和当前密码相同');

  const btn = $('pwdSave');
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    await api('/api/password', { method: 'POST', body: JSON.stringify({ old: oldPwd, new: newPwd }) });
    closePwdModal();
    toast('密码已修改，下次登录请用新密码');
    if (S.user) S.user.is_default_pwd = false;
    renderUser();
  } catch (e) {
    showMsg('pwdMsg', e.message);
  } finally {
    btn.disabled = false; btn.textContent = '保存新密码';
  }
};

/* ---------------------------------------------------------------- 左栏 */
function countFor(accountId) {
  const zero = {};
  S.folders.forEach((f) => { zero[f.key] = { total: 0, unread: 0 }; });
  if (accountId === 'all') {
    S.accounts.forEach((a) => {
      Object.entries(a.counts || {}).forEach(([k, v]) => {
        if (!zero[k]) zero[k] = { total: 0, unread: 0 };
        zero[k].total += v.total; zero[k].unread += v.unread;
      });
    });
  } else {
    const acc = S.accounts.find((a) => String(a.id) === String(accountId));
    Object.entries((acc && acc.counts) || {}).forEach(([k, v]) => { zero[k] = v; });
  }
  return zero;
}

function renderAccounts() {
  const box = $('accountList');
  let html = '';
  if (S.accounts.length > 1) {
    html += `<div class="acc-item ${S.account === 'all' ? 'active' : ''}" data-acc="all">
      <div class="avatar" style="background:linear-gradient(135deg,#334155,#64748b)">全</div>
      <div class="meta"><div class="name">全部账号</div><div class="addr">合并显示所有邮件</div></div></div>`;
  }
  S.accounts.forEach((a) => {
    const unread = Object.values(a.counts || {}).reduce((s, v) => s + v.unread, 0);
    html += `<div class="acc-item ${String(S.account) === String(a.id) ? 'active' : ''}" data-acc="${a.id}">
      <div class="avatar" style="background:${colorOf(a.email)}">${esc(initials(a.name, a.email))}</div>
      <div class="meta"><div class="name">${esc(a.name || a.email)}</div>
        <div class="addr">${esc(a.email)}</div></div>
      ${unread ? `<span class="badge">${unread}</span>` : ''}
      <button class="acc-more" title="更多操作（也可以右键这一行）" aria-label="更多操作">⋯</button>
    </div>`;
  });
  if (!S.accounts.length) {
    html = `<div style="font-size:12px;color:#94a3b8;padding:6px 10px;line-height:1.7;">还没有账号，点上面的按钮添加。</div>`;
  }
  box.innerHTML = html;
  box.querySelectorAll('[data-acc]').forEach((el) => {
    el.onclick = () => { S.account = el.dataset.acc; S.selected = null; renderAccounts(); renderFolders(); loadList(); };
    const accId = parseInt(el.dataset.acc, 10);      // 「全部账号」是 NaN，不挂菜单
    if (!Number.isFinite(accId)) return;
    el.title = '右键，或点右边的 ⋯ 可以同步 / 删除这个账号';
    el.oncontextmenu = (e) => { e.preventDefault(); openCtxMenu(accId, e.clientX, e.clientY); };
    const more = el.querySelector('.acc-more');
    if (more) {
      more.onclick = (e) => {
        e.stopPropagation();                         // 别顺手把账号也切了
        const r = more.getBoundingClientRect();
        openCtxMenu(accId, Math.round(r.left), Math.round(r.bottom + 4));
      };
    }
  });
}

/* ---------------------------------------------------------------- 右键菜单：账号 */
let _ctxAccId = null;
function openCtxMenu(accId, x, y) {
  _ctxAccId = accId;
  const m = $('ctxMenu');
  const acc = S.accounts.find((a) => String(a.id) === String(accId));
  $('ctxHead').textContent = acc ? acc.email : '';
  m.style.display = 'block';
  // 避免超出视口右/下边界
  const w = m.offsetWidth, h = m.offsetHeight;
  const px = Math.max(8, Math.min(x, window.innerWidth - w - 8));
  const py = Math.max(8, Math.min(y, window.innerHeight - h - 8));
  m.style.left = px + 'px';
  m.style.top = py + 'px';
}
function hideCtxMenu() { $('ctxMenu').style.display = 'none'; _ctxAccId = null; }

$('ctxSync').onclick = () => {
  const id = _ctxAccId; hideCtxMenu();
  if (!id) return;
  api('/api/sync', { method: 'POST', body: JSON.stringify({ account: id }) })
    .then(() => { toast('已触发同步'); startSyncPolling(); })
    .catch((e) => toast('同步失败：' + e.message));
};

function openConfirm(accId) {
  const acc = S.accounts.find((a) => a.id === accId);
  if (!acc) return;
  $('confirmText').innerHTML = `确定删除账号 <b>${esc(acc.email)}</b> 吗？<br>该账号的全部本地邮件、文件夹与凭据会被一并清除，且不可恢复。`;
  $('confirmOk').dataset.acc = accId;
  $('confirmMask').classList.add('show');
}

$('ctxDelete').onclick = () => { const id = _ctxAccId; hideCtxMenu(); if (id) openConfirm(id); };

$('confirmCancel').onclick = () => $('confirmMask').classList.remove('show');
$('confirmMask').addEventListener('click', (e) => { if (e.target === $('confirmMask')) $('confirmMask').classList.remove('show'); });

$('confirmOk').onclick = async () => {
  const id = parseInt($('confirmOk').dataset.acc, 10);
  $('confirmMask').classList.remove('show');
  try {
    await api('/api/accounts/' + id, { method: 'DELETE' });
    // 若删的是当前选中账号，复位到全部
    if (String(S.account) === String(id)) S.account = 'all';
    S.selected = null;
    await bootstrap();
    renderAccounts(); renderFolders(); loadList();
    toast('账号已删除');
  } catch (e) {
    toast('删除失败：' + e.message);
  }
};

// 点击别处 / 滚动 / 按 Esc 时收起右键菜单
document.addEventListener('click', (e) => { if (!e.target.closest('.ctx-menu')) hideCtxMenu(); });
window.addEventListener('scroll', hideCtxMenu, true);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { hideCtxMenu(); closePwdModal(); }
});

const FOLDER_ICON = { INBOX: '✉', Sent: '➤', Drafts: '✎', Trash: '🗑', Spam: '⚠' };

function renderFolders() {
  const counts = countFor(S.account);
  $('folderList').innerHTML = S.folders.map((f) => {
    const c = counts[f.key] || { total: 0, unread: 0 };
    // 未读用蓝色角标、总数用灰色小字，分开显示。
    // 原来拼成一个 "5 / 5"，看不出哪个是未读、哪个是总数。
    const num = (c.unread ? `<em class="unread">${c.unread}</em>` : '')
              + (!c.unread && c.total ? `<span class="total">${c.total}</span>` : '');
    return `<div class="folder-item ${S.folder === f.key ? 'active' : ''}" data-folder="${f.key}"
      title="${f.label}：未读 ${c.unread} 封 / 共 ${c.total} 封">
      <span class="ico">${FOLDER_ICON[f.key] || '•'}</span><span>${f.label}</span>
      <span class="num">${num}</span></div>`;
  }).join('');
  $('folderList').querySelectorAll('[data-folder]').forEach((el) => {
    el.onclick = () => { S.folder = el.dataset.folder; S.selected = null; renderFolders(); loadList(); };
  });
}

/* ---------------------------------------------------------------- 中栏 */
let searchTimer = null;
$('searchInput').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { S.keyword = e.target.value.trim(); loadList(); }, 260);
});

async function loadList() {
  const params = new URLSearchParams({ folder: S.folder });
  if (S.account !== 'all') params.set('account', S.account);
  if (S.keyword) params.set('q', S.keyword);
  const data = await api('/api/emails?' + params.toString());
  S.mails = data.items;

  const label = data.folder_label || S.folder;
  const acc = S.accounts.find((a) => String(a.id) === String(S.account));
  const accName = S.account === 'all' ? '' : (acc ? acc.name : '');
  // 账号名单独成一段：栏位不够时先压缩它，文件夹名始终完整
  $('listTitle').innerHTML =
    (accName ? `<span class="lt-acc">${esc(accName)}</span><span class="lt-sep">·</span>` : '')
    + `<span class="lt-folder">${esc(label)}${S.keyword ? ' · 搜索' : ''}</span>`;
  $('listCount').textContent = S.mails.length ? `${S.mails.length} 封` : '';

  const box = $('mailList');
  if (!S.mails.length) {
    box.innerHTML = `<div class="empty" style="height:60%;"><div>
      <div class="big">✉</div>
      <p>${S.keyword ? '没有匹配的邮件' : '这个文件夹还是空的'}</p>
      <p style="font-size:12px;">${S.accounts.length ? '点「立即同步」拉取服务器邮件' : '先在左侧添加一个邮箱账号'}</p>
    </div></div>`;
    return;
  }
  const showAcc = S.account === 'all';
  box.innerHTML = S.mails.map((m) => `
    <div class="mail ${m.seen ? '' : 'unread'} ${S.selected === m.id ? 'active' : ''}" data-id="${m.id}">
      <div class="avatar">${esc(initials(m.from_name, m.from_addr || m.msg_from))}</div>
      <div class="body">
        <div class="top">
          <span class="from">${esc(m.from_name || m.from_addr || m.msg_from || '(无发件人)')}</span>
          ${showAcc ? `<span class="tagacc">${esc(m.account_name)}</span>` : ''}
          ${m.has_attachment ? '<span class="clip">📎</span>' : ''}
          <span class="date">${esc(fmtListDate(m.date))}</span>
        </div>
        <div class="subj">${esc(m.subject || '(无主题)')}</div>
        <div class="prev">${esc(m.preview || '')}</div>
      </div>
    </div>`).join('');
  box.querySelectorAll('.mail').forEach((el) => {
    el.onclick = () => openMail(parseInt(el.dataset.id, 10));
  });
}

/* ---------------------------------------------------------------- 右栏 */
async function openMail(id) {
  S.selected = id;
  document.querySelectorAll('.mail').forEach((el) => {
    el.classList.toggle('active', parseInt(el.dataset.id, 10) === id);
  });
  const m = await api('/api/emails/' + id);
  const box = $('detail');
  const attach = (m.attachments || []).map((a) => `
    <a href="/api/attachments/${a.id}">
      <span>📎</span><span class="fname">${esc(a.filename)}</span><span style="color:#94a3b8;">下载</span>
    </a>`).join('');

  box.innerHTML = `<div class="inner">
    <div class="d-head">
      <h2>${esc(m.subject || '(无主题)')}</h2>
      <div class="d-meta">
        <div class="avatar" style="background:${colorOf(m.from_addr || m.msg_from)}">${esc(initials(m.from_name, m.msg_from))}</div>
        <div class="who">
          <div class="n">${esc(m.from_name || m.msg_from || '')}</div>
          <div class="a">${esc(m.from_addr || '')}</div>
        </div>
        <div class="when">${esc(fmtFullDate(m.date))}<br>发往 ${esc(m.account_email || '')}</div>
      </div>
      <div class="d-tools">
        <button id="btnSeen">标为未读</button>
        <a href="/api/emails/${m.id}/html" target="_blank" rel="noopener">在新标签打开</a>
        <button id="btnReply">回复</button>
      </div>
      ${attach ? `<div class="attach">${attach}</div>` : ''}
    </div>
    <div class="frame-wrap">
      <iframe class="body" id="bodyFrame" src="/api/emails/${m.id}/html" sandbox="allow-popups allow-popups-to-escape-sandbox"></iframe>
    </div>
  </div>`;

  $('bodyFrame').onload = function () {
    // 沙箱下拿不到内容高度，用「内容不超时贴合、超出则滚动」的近似处理
    this.style.height = '70vh';
  };
  $('btnSeen').onclick = async () => {
    await api(`/api/emails/${m.id}/seen`, { method: 'POST', body: JSON.stringify({ seen: 0 }) });
    toast('已标为未读');
    await refreshCounts();
    loadList();
  };
  $('btnReply').onclick = () => openCompose({ to: m.from_addr, subject: 'Re: ' + (m.subject || '') });

  // 打开即已读：本地角标与列表要跟着变
  refreshCounts();
  loadList();
}

async function refreshCounts() {
  const data = await api('/api/bootstrap');
  S.accounts = data.accounts;
  renderAccounts();
  renderFolders();
}

/* ---------------------------------------------------------------- 同步 */
function applySync(sync) {
  const box = $('syncBox');
  box.classList.toggle('busy', !!sync.running);
  const last = (sync.messages || []).slice(-1)[0];
  $('syncText').textContent = sync.running ? '正在同步…' : '就绪';
  $('syncLine').textContent = last ? last.text : (sync.finished_at ? '上次同步已完成' : '');
  renderWarnings(sync.errors || []);
}

function renderWarnings(errors) {
  const bar = $('warnBar');
  if (!errors.length) { bar.classList.remove('show'); bar.innerHTML = ''; return; }
  const e = errors[0];
  const more = errors.length > 1 ? `<div style="margin-top:6px;color:#a98b34;">另有 ${errors.length - 1} 个账号也有问题</div>` : '';
  bar.innerHTML = `<b>${esc(e.email)}</b>：${esc(e.message)}${more}
    <div class="acts">
      ${e.action_url ? `<a href="${esc(e.action_url)}" target="_blank" rel="noopener">打开邮箱设置</a>` : ''}
      ${e.kind === 'need_auth' ? '<button id="warnAuth">重新授权</button>' : ''}
      <button id="warnRetry">立即重试</button>
    </div>`;
  bar.classList.add('show');
  if ($('warnAuth')) $('warnAuth').onclick = () => openAddAccount('Outlook', e.email);
  $('warnRetry').onclick = syncNow;
}

async function syncNow() {
  const started = await api('/api/sync', { method: 'POST', body: JSON.stringify({}) });
  if (!started.started) toast('已有同步在进行中');
  applySync(started.sync);
  startSyncPolling();
}

function startSyncPolling() {
  clearInterval(S.syncTimer);
  S.syncTimer = setInterval(async () => {
    let st;
    try { st = await api('/api/sync/status'); } catch (e) { return; }
    applySync(st);
    if (!st.running) {
      clearInterval(S.syncTimer);
      await bootstrap2Refresh();
    }
  }, 2000);
}

async function bootstrap2Refresh() {
  await refreshCounts();
  loadList();
}

$('btnSync').onclick = syncNow;
$('btnRefresh').onclick = () => loadList();
$('btnMarkRead').onclick = async () => {
  // 不传 folder：清的是「当前账号下所有文件夹」（含垃圾邮件）。
  // 左侧账号角标统计的就是各文件夹未读的合计 —— 只标当前文件夹的话，
  // 角标不会归零，看着就像按钮没生效。
  const payload = {};
  if (S.account !== 'all') payload.account = parseInt(S.account, 10);
  const r = await api('/api/emails/mark-all-read', { method: 'POST', body: JSON.stringify(payload) });
  toast(r.cleared ? `已标为已读：${r.cleared} 封` : '没有未读邮件');
  refreshCounts();
  loadList();
};

/* ---------------------------------------------------------------- 添加账号 */
function fillProviderSelect() {
  $('fldProvider').innerHTML = S.providers
    .map((p) => `<option value="${p.key}">${esc(p.label)}</option>`).join('');
  onProviderChange();
}

function currentProvider() {
  const key = $('fldProvider').value;
  return S.providers.find((p) => p.key === key) || S.providers[0];
}

function onProviderChange() {
  const p = currentProvider();
  if (!p) return;
  const isMs = p.oauth === 'microsoft';
  $('pwdField').style.display = isMs ? 'none' : '';
  $('msBox').style.display = isMs ? '' : 'none';
  $('providerHint').innerHTML = esc(p.auth_note || '');
  $('fldImap').value = p.imap || '';
  $('fldImapPort').value = p.imap_port || 993;
  $('fldSmtp').value = p.smtp || '';
  $('fldSmtpPort').value = p.smtp_port || 465;
  $('addMsg').style.display = 'none';
  if (!isMs) { $('deviceBox').style.display = 'none'; stopOauthPolling(); }
}

$('fldProvider').addEventListener('change', onProviderChange);

$('fldEmail').addEventListener('blur', () => {
  const email = $('fldEmail').value.trim().toLowerCase();
  if (!email.includes('@')) return;
  const guess = S.domainProvider[email.split('@')[1]];
  if (guess && $('fldProvider').value !== guess) {
    $('fldProvider').value = guess;
    onProviderChange();
  }
});

function openAddAccount(provider, email) {
  if (provider) { $('fldProvider').value = provider; onProviderChange(); }
  if (email) { $('fldEmail').value = email; }
  $('addMsg').style.display = 'none';
  $('addMask').classList.add('show');
}
$('btnAddAccount').onclick = () => {
  $('fldEmail').value = ''; $('fldPassword').value = '';
  openAddAccount();
};
$('btnAddCancel').onclick = () => { $('addMask').classList.remove('show'); stopOauthPolling(); };

$('btnAddSave').onclick = async () => {
  const btn = $('btnAddSave');
  const body = {
    provider: $('fldProvider').value,
    email: $('fldEmail').value.trim(),
    password: $('fldPassword').value,
    imap_server: $('fldImap').value.trim(),
    imap_port: $('fldImapPort').value,
    smtp_server: $('fldSmtp').value.trim(),
    smtp_port: $('fldSmtpPort').value,
  };
  if (!body.email.includes('@')) { showMsg('addMsg', '请填写完整的邮箱地址'); return; }
  btn.disabled = true; btn.textContent = '连接测试中…';
  try {
    const r = await api('/api/accounts', { method: 'POST', body: JSON.stringify(body) });
    if (r.warning) {
      showMsg('addMsg', r.warning);
      await refreshCounts();
    } else {
      $('addMask').classList.remove('show');
      toast('账号已添加');
      await refreshCounts();
      syncNow();
    }
  } catch (e) {
    showMsg('addMsg', e.message);
  } finally {
    btn.disabled = false; btn.textContent = '保存并测试连接';
  }
};

function showMsg(id, text) {
  const el = $(id);
  el.textContent = text;
  el.style.display = '';
}

/* ---------------------------------------------------------------- 微软授权 */
$('btnMsLogin').onclick = async () => {
  const btn = $('btnMsLogin');
  btn.disabled = true; btn.textContent = '正在申请验证码…';
  try {
    const flow = await api('/api/oauth/start', {
      method: 'POST',
      body: JSON.stringify({ email: $('fldEmail').value.trim(), provider: $('fldProvider').value }),
    });
    S.oauthId = flow.id;
    $('deviceBox').style.display = '';
    $('deviceCode').textContent = flow.user_code;
    const link = flow.verification_uri_complete || flow.verification_uri || 'https://microsoft.com/devicelogin';
    $('deviceLink').href = link;
    $('deviceLink').textContent = link;
    $('addMsg').style.display = 'none';
    startOauthPolling();
  } catch (e) {
    showMsg('addMsg', '申请验证码失败：' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '🔐 使用微软账号登录（OAuth2）';
  }
};

function startOauthPolling() {
  stopOauthPolling();
  S.oauthTimer = setInterval(async () => {
    if (!S.oauthId) return;
    let st;
    try { st = await api('/api/oauth/status?id=' + S.oauthId); } catch (e) { return; }
    if (st.state === 'done') {
      stopOauthPolling();
      $('addMask').classList.remove('show');
      toast('授权成功：' + st.email);
      await refreshCounts();
      syncNow();
    } else if (st.state === 'error' || st.state === 'cancelled') {
      stopOauthPolling();
      if (st.state === 'error') showMsg('addMsg', st.message || '授权失败');
    }
  }, 3000);
}

function stopOauthPolling() {
  clearInterval(S.oauthTimer);
  S.oauthTimer = null;
}

/* ---------------------------------------------------------------- 写邮件 */
function fillComposeAccounts() {
  $('cAccount').innerHTML = S.accounts
    .map((a) => `<option value="${a.id}">${esc(a.name || a.email)} &lt;${esc(a.email)}&gt;</option>`).join('');
}

function openCompose(prefill) {
  if (!S.accounts.length) { toast('请先添加一个邮箱账号'); return; }
  fillComposeAccounts();
  if (S.account !== 'all') $('cAccount').value = S.account;
  $('cTo').value = (prefill && prefill.to) || '';
  $('cCc').value = '';
  $('cSubject').value = (prefill && prefill.subject) || '';
  $('cBody').value = '';
  $('cFiles').value = '';
  $('composeMsg').style.display = 'none';
  $('composeMask').classList.add('show');
}

$('btnCompose').onclick = () => openCompose();
$('btnComposeCancel').onclick = () => $('composeMask').classList.remove('show');

$('btnSend').onclick = async () => {
  const btn = $('btnSend');
  const fd = new FormData();
  fd.append('account_id', $('cAccount').value);
  fd.append('to', $('cTo').value);
  fd.append('cc', $('cCc').value);
  fd.append('subject', $('cSubject').value);
  fd.append('body', $('cBody').value);
  fd.append('html', '1');
  for (const f of $('cFiles').files) fd.append('files', f);

  btn.disabled = true; btn.textContent = '发送中…';
  try {
    const r = await fetch('/api/send', { method: 'POST', body: fd });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || '发送失败');
    $('composeMask').classList.remove('show');
    toast('已发送');
    setTimeout(syncNow, 1200);
  } catch (e) {
    showMsg('composeMsg', e.message);
  } finally {
    btn.disabled = false; btn.textContent = '发送';
  }
};

/* ---------------------------------------------------------------- 启动 */
(async function start() {
  try {
    await bootstrap();
    const st = await api('/api/sync/status');
    if (st.running) { applySync(st); startSyncPolling(); }
    // 支持 #mail=<id> 深链，方便直接分享/刷新定位到某封邮件
    const m = /^#mail=(\d+)$/.exec(location.hash || '');
    if (m) openMail(parseInt(m[1], 10));
    // 调试/分享用：#ctx=<id> 自动弹出账号右键菜单，#confirm=<id> 自动弹出删除确认
    const cx = /^#ctx=(\d+)$/.exec(location.hash || '');
    if (cx) openCtxMenu(parseInt(cx[1], 10), 130, 150);
    const cf = /^#confirm=(\d+)$/.exec(location.hash || '');
    if (cf) openConfirm(parseInt(cf[1], 10));
    if ((location.hash || '') === '#add') openAddAccount();
  } catch (e) {
    toast('加载失败：' + e.message);
  }
})();
