/* MailClient Web —— 前端逻辑（原生 JS，无构建步骤） */

const S = {
  accounts: [],
  providers: [],
  folders: [],
  domainProvider: {},
  user: null,
  ui: null,              // 界面设置（未读置顶 / 未读标红 / 自动同步间隔）
  build: '',             // 界面构建号，用于一眼确认没有加载到缓存里的旧脚本
  account: 'all',        // 'all' 或账号 id
  folder: 'INBOX',
  keyword: '',
  selected: null,
  sel: new Set(),       // 批量删除：勾选中的邮件 id 集合
  mails: [],
  contacts: [],
  syncAccounts: {},      // 账号 id(str) -> 上次同步结果 {ok, count, error}
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
  S.ui = data.ui || null;
  SET.ui = S.ui || SET.ui;
  console.log('MailClient UI build:', S.build || '(未返回，可能加载了缓存里的旧脚本)');

  if (S.account !== 'all' && !S.accounts.some((a) => String(a.id) === String(S.account))) {
    S.account = 'all';
  }
  renderUser();
  renderAccounts();
  renderFolders();
  fillProviderSelect();
  fillComposeAccounts();
  applyUiSettings();
  applySync(data.sync);
  loadList();
  loadContacts();
  initCollapsible();
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

/* ---------------------------------------------------------------- 设置弹窗（2.0.7）
   原来是左下角三个小按钮（提醒 / 改密码 / 退出），按钮小、功能散。
   现在合并成一个「设置」，里面分四页：通用 · 新邮件提醒 · 账号安全 · 关于。
   深链：#settings 通用、#notify 提醒、#pwd 账号安全、#about 关于。 */
const SET_TABS = ['general', 'notify', 'pwd', 'about'];
const SET = { ui: null, tab: 'general' };

function openSettings(tab) {
  switchSetTab(tab || SET.tab || 'general');
  $('settingsMask').classList.add('show');
  loadSettings();
}

function closeSettings() { $('settingsMask').classList.remove('show'); }

function switchSetTab(tab) {
  if (!SET_TABS.includes(tab)) tab = 'general';
  SET.tab = tab;
  document.querySelectorAll('#setTabs .set-tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  document.querySelectorAll('.set-pane').forEach((p) => { p.hidden = p.dataset.pane !== tab; });
  // 「保存」只管通用 + 提醒：账号安全有它自己的按钮，关于页没什么可存的
  const savaable = tab === 'general' || tab === 'notify';
  $('setSave').style.display = savaable ? '' : 'none';
  $('setFootNote').textContent = tab === 'general' ? '通用设置保存后立即生效，不用重启' : '';
  if (tab === 'pwd') {
    $('pwdMsg').style.display = 'none';
    setTimeout(() => $('pwdOld').focus(), 60);
  }
}

function loadSettings() {
  api('/api/settings').then((d) => {
    SET.ui = d.ui;
    $('stUnreadTop').checked = !!d.ui.unread_top;
    $('stUnreadRed').checked = !!d.ui.unread_red;
    $('stSyncInterval').value = d.ui.sync_interval;
    if (d.version) $('setVer').textContent = 'v' + d.version;
    if (d.build) $('setBuild').textContent = d.build;
  }).catch((e) => toast('读取设置失败：' + e.message));
  loadNotifySettings();
}

/** 把界面设置落到页面上（目前只有「未读标红加粗」需要动 CSS）。 */
function applyUiSettings() {
  const ui = SET.ui || S.ui || {};
  document.body.classList.toggle('plain-unread', ui.unread_red === false);
}

async function saveSettings() {
  const btn = $('setSave');
  btn.disabled = true; btn.textContent = '保存中…';
  try {
    const r = await api('/api/settings', {
      method: 'POST',
      body: JSON.stringify({
        unread_top: $('stUnreadTop').checked,
        unread_red: $('stUnreadRed').checked,
        sync_interval: parseInt($('stSyncInterval').value, 10) || 0,
      }),
    });
    SET.ui = r.ui;
    S.ui = r.ui;
    applyUiSettings();
    if (SET.tab === 'notify') {
      const nr = await api('/api/notify', { method: 'POST', body: JSON.stringify(notifyPayload()) });
      NF.settings = nr.settings || NF.settings;
    }
    toast(SET.tab === 'notify' ? '提醒设置已保存' : '设置已保存');
    loadList();                       // 排序方式可能变了，列表要重排
  } catch (e) {
    if (SET.tab === 'notify') notifyMsg(e.message);
    else toast('保存失败：' + e.message);
  } finally {
    btn.disabled = false; btn.textContent = '保存';
  }
}

$('btnSettings').onclick = () => openSettings();
$('btnPwd2').onclick = () => openSettings('pwd');      // 中栏「安全提醒」里的去改密码
$('setClose').onclick = closeSettings;
$('setX').onclick = closeSettings;
$('setSave').onclick = saveSettings;
$('setTabs').addEventListener('click', (e) => {
  const b = e.target.closest('.set-tab');
  if (b) switchSetTab(b.dataset.tab);
});
$('settingsMask').addEventListener('click', (e) => {
  if (e.target === $('settingsMask')) closeSettings();
});
['pwdNew', 'pwdNew2'].forEach((id) => $(id).addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('pwdSave').click();
}));

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
    $('pwdOld').value = ''; $('pwdNew').value = ''; $('pwdNew2').value = '';
    toast('密码已修改，下次登录请用新密码');
    if (S.user) S.user.is_default_pwd = false;
    renderUser();
  } catch (e) {
    showMsg('pwdMsg', e.message);
  } finally {
    btn.disabled = false; btn.textContent = '保存新密码';
  }
};

/* ---------------------------------------------------------------- 新邮件提醒 */
/* 各通道的输入框由后端 /api/notify 返回的字段定义渲染，加通道不用改这里。
   表单现在住在「设置 → 新邮件提醒」页里，保存走设置弹窗底部那个按钮。 */
let NF = { channels: {}, settings: null };

function loadNotifySettings() {
  $('notifyMsg').style.display = 'none';
  api('/api/notify').then((data) => {
    NF.channels = data.channels || {};
    NF.settings = data.settings || {};
    const s = NF.settings;
    const sel = $('nfChannel');
    sel.innerHTML = Object.entries(NF.channels)
      .map(([k, v]) => `<option value="${esc(k)}">${esc(v.label)}</option>`).join('');
    sel.value = NF.channels[s.channel] ? s.channel : 'serverchan';
    $('nfEnabled').checked = !!s.enabled;
    $('nfMerge').checked = !!s.merge;
    $('nfSpam').checked = !!s.notify_spam;
    $('nfBaseUrl').value = s.base_url || '';
    $('nfInterval').value = s.min_interval || 0;
    $('nfQuietStart').value = s.quiet_start || '';
    $('nfQuietEnd').value = s.quiet_end || '';
    renderNotifyFields(sel.value);
  }).catch((e) => notifyMsg(e.message));
}

function closeNotifyModal() { $('settingsMask').classList.remove('show'); }

function notifyMsg(text, ok) {
  const el = $('notifyMsg');
  el.className = 'msg ' + (ok ? 'ok' : 'err');
  el.textContent = text;
  el.style.display = '';
}

function nfFieldsOf(ch) {
  return ((NF.channels[ch] || {}).fields) || [];
}

function renderNotifyFields(ch) {
  const def = NF.channels[ch] || {};
  const link = def.register
    ? ` <a href="${esc(def.register)}" target="_blank" rel="noopener">怎么获取 →</a>` : '';
  $('nfNote').innerHTML = (def.note ? esc(def.note) : '') + link;

  const configured = (NF.settings && NF.settings.configured) || {};
  const pub = ((NF.settings && NF.settings.public) || {})[ch] || {};
  $('nfFields').innerHTML = nfFieldsOf(ch).map((f) => {
    const key = esc(f.key);
    // 非密钥字段（微信模板原文之类）回显，改起来不用重新粘贴
    if (f.public) {
      const val = esc(pub[f.key] || '');
      if (f.type === 'textarea') {
        return `<div class="field"><label>${esc(f.label)}</label>
          <textarea id="nf-${key}" rows="5" placeholder="${esc(f.placeholder || '')}">${val}</textarea>${f.hint ? `<div class="field-hint">${esc(f.hint)}</div>` : ''}</div>`;
      }
      return `<div class="field"><label>${esc(f.label)}</label>
        <input id="nf-${key}" value="${val}" placeholder="${esc(f.placeholder || '')}" autocomplete="off"></div>`;
    }
    if (f.options && f.options.length) {
      return `<div class="field"><label>${esc(f.label)}</label>
        <select id="nf-${key}">${f.options.map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join('')}</select></div>`;
    }
    if (f.type === 'textarea') {
      return `<div class="field"><label>${esc(f.label)}</label>
        <textarea id="nf-${key}" rows="5" placeholder="${esc(f.placeholder || '')}"></textarea>${f.hint ? `<div class="field-hint">${esc(f.hint)}</div>` : ''}</div>`;
    }
    // 已存过的密钥不回显明文，用占位提示「留空则不修改」
    const ph = configured[ch] ? '已保存，留空则不修改' : (f.placeholder || '');
    return `<div class="field"><label>${esc(f.label)}</label>
      <input id="nf-${key}" placeholder="${esc(ph)}" autocomplete="off"></div>`;
  }).join('');

  nfFieldsOf(ch).forEach((f) => {
    const el = $('nf-' + f.key);
    if (el && f.default && el.tagName === 'SELECT') el.value = f.default;
  });
}

function notifyPayload() {
  const ch = $('nfChannel').value;
  const config = {};
  config[ch] = {};
  nfFieldsOf(ch).forEach((f) => {
    const el = $('nf-' + f.key);
    if (el) config[ch][f.key] = el.value;
  });
  return {
    enabled: $('nfEnabled').checked,
    channel: ch,
    config,
    base_url: $('nfBaseUrl').value.trim(),
    merge: $('nfMerge').checked,
    notify_spam: $('nfSpam').checked,
    min_interval: parseInt($('nfInterval').value, 10) || 0,
    quiet_start: $('nfQuietStart').value.trim(),
    quiet_end: $('nfQuietEnd').value.trim(),
  };
}

$('nfChannel').onchange = () => renderNotifyFields($('nfChannel').value);

$('notifyTest').onclick = async () => {
  const btn = $('notifyTest');
  btn.disabled = true; btn.textContent = '发送中…';
  try {
    const r = await api('/api/notify/test', { method: 'POST', body: JSON.stringify(notifyPayload()) });
    notifyMsg('测试已发出 · ' + (r.message || '成功'), true);
  } catch (e) {
    notifyMsg(e.message);
  } finally {
    btn.disabled = false; btn.textContent = '发送测试';
  }
};

/* ══════════════════════════════════ 常用联系人 ══════════════════════════════ */
/* 三个入口共用一个 S.contacts：
     左栏列表（点一下直接写信）、写信弹窗里的挑选面板、通讯录管理弹窗。
   数据只在服务端，前端每次操作后重新拉一次，避免多标签页互相覆盖。 */

function ctInitials(c) {
  const src = (c.name || c.email || '?').trim();
  return /[\u4e00-\u9fa5]/.test(src) ? src.slice(0, 1) : src.slice(0, 2).toUpperCase();
}

async function loadContacts() {
  try {
    const d = await api('/api/contacts');
    S.contacts = d.items || [];
  } catch (e) {
    S.contacts = [];
  }
  renderSideContacts();
  if ($('contactMask').classList.contains('show')) renderContactList();
  if ($('pickPanel').style.display !== 'none') renderPickList();
}

function renderSideContacts() {
  const box = $('contactSide');
  if (!S.contacts.length) {
    box.innerHTML = '<div class="side-empty">还没有联系人 —— 发信时的收件人会自动记下来。</div>';
    return;
  }
  box.innerHTML = S.contacts.slice(0, 6).map((c) => `
    <div class="contact-item" data-ct="${c.id}" title="${esc(c.email)}">
      <span class="ct-ico" style="background:${colorOf(c.email)}">${esc(ctInitials(c))}</span>
      <span class="ct-name">${esc(c.name || c.email.split('@')[0])}</span>
      <span class="ct-mail">${esc(c.email)}</span>
    </div>`).join('');
  box.querySelectorAll('[data-ct]').forEach((el) => {
    el.onclick = () => {
      const c = S.contacts.find((x) => String(x.id) === el.dataset.ct);
      if (c) openCompose({ to: c.email });
    };
  });
}

/* ---------------------------------------------------------------- 侧栏分组折叠（2.0.3）
   账号 / 文件夹 / 常用联系人 三组可单独折叠，状态记进 localStorage，刷新后保留。 */
let _collapsibleReady = false;
function initCollapsible() {
  if (_collapsibleReady) return;                 // 静态结构只绑定一次
  _collapsibleReady = true;
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('mc_nav_collapsed') || '{}'); } catch (e) {}
  document.querySelectorAll('.nav-sec').forEach((sec) => {
    const key = sec.dataset.sec;
    if (saved[key]) sec.classList.add('collapsed');
    const t = sec.querySelector('[data-toggle]');
    if (t) t.addEventListener('click', () => {
      sec.classList.toggle('collapsed');
      const cur = {};
      try { Object.assign(cur, JSON.parse(localStorage.getItem('mc_nav_collapsed') || '{}')); } catch (e) {}
      cur[key] = sec.classList.contains('collapsed');
      localStorage.setItem('mc_nav_collapsed', JSON.stringify(cur));
    });
  });
}

/** 往「收件人/抄送」框里追加一个地址，已在框里的不重复加。 */
function addToField(fieldId, c) {
  const el = $(fieldId);
  const shown = c.name ? `${c.name} <${c.email}>` : c.email;
  const cur = (el.value || '').trim();
  const plain = cur.toLowerCase();
  if (plain.includes(c.email.toLowerCase())) {
    toast('这个地址已经在框里了');
    return false;
  }
  el.value = cur ? `${cur}, ${shown}` : shown;
  el.focus();
  return true;
}

/** 把输入框里的内容解析成 [{name, email}] —— 只用于「存为联系人」，不参与发信校验。 */
function parseAddrField(value) {
  return String(value || '')
    .split(/[,;，；]/).map((s) => s.trim()).filter(Boolean)
    .map((s) => {
      const m = /^(.*?)[<（(]?\s*([^\s<>()（）]+@[^\s<>()（）]+?)\s*[>）)]?$/.exec(s);
      if (m) return { name: (m[1] || '').trim().replace(/^["'“”]|["'“”]$/g, ''), email: m[2] };
      const m2 = /([^\s<>()（）,;，；]+@[^\s<>()（）,;，；]+)/.exec(s);
      return m2 ? { name: '', email: m2[1] } : null;
    })
    .filter(Boolean);
}

async function saveContacts(list) {
  let ok = 0, skip = 0;
  for (const it of list) {
    try {
      await api('/api/contacts', { method: 'POST', body: JSON.stringify(it) });
      ok++;
    } catch (e) {
      skip++;
    }
  }
  await loadContacts();
  return { ok, skip };
}

/* ---------------- 写信弹窗里的挑选面板 ---------------- */
function renderPickList() {
  const kw = ($('pickSearch').value || '').trim().toLowerCase();
  const items = S.contacts.filter((c) => !kw
    || (c.email || '').toLowerCase().includes(kw)
    || (c.name || '').toLowerCase().includes(kw)
    || (c.note || '').toLowerCase().includes(kw));
  const box = $('pickList');
  box.innerHTML = items.length ? items.map((c) => `
      <div class="pick-row" data-pick="${c.id}">
        <span class="ct-ico" style="background:${colorOf(c.email)}">${esc(ctInitials(c))}</span>
        <span class="ct-name">${esc(c.name || c.email.split('@')[0])}</span>
        <span class="pick-mail">${esc(c.email)}</span>
        <button class="mini-link" data-toc="${c.id}" title="加到抄送">抄送</button>
      </div>`).join('')
    : `<div class="ct-empty">${S.contacts.length ? '没有匹配的联系人' : '还没有联系人 —— 发出去一封邮件，收件人就会自动记下来'}</div>`;

  box.querySelectorAll('[data-pick]').forEach((el) => {
    el.onclick = (ev) => {
      if (ev.target.closest('[data-toc]')) return;
      const c = S.contacts.find((x) => String(x.id) === el.dataset.pick);
      if (c) addToField('cTo', c);
    };
  });
  box.querySelectorAll('[data-toc]').forEach((btn) => {
    btn.onclick = (ev) => {
      ev.stopPropagation();
      const c = S.contacts.find((x) => String(x.id) === btn.dataset.toc);
      if (c) addToField('cCc', c);
    };
  });
  $('pickCount').textContent = `共 ${S.contacts.length} 位`;
}

function togglePick(force) {
  const panel = $('pickPanel');
  const show = force === undefined ? panel.style.display === 'none' : force;
  panel.style.display = show ? '' : 'none';
  if (show) { $('pickSearch').value = ''; renderPickList(); $('pickSearch').focus(); }
}

$('btnPickTo').onclick = () => { togglePick(); if ($('pickPanel').style.display !== 'none') loadContacts(); };
$('pickSearch').addEventListener('input', renderPickList);
$('pickSearch').addEventListener('keydown', (e) => {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const first = $('pickList').querySelector('[data-pick]');
  if (first) first.click();
});
$('btnPickManage').onclick = () => openContactModal();

$('btnSaveRcpt').onclick = async () => {
  const list = parseAddrField($('cTo').value).concat(parseAddrField($('cCc').value));
  if (!list.length) { showMsg('composeMsg', '收件人/抄送还是空的，先填个地址再存。'); return; }
  const { ok, skip } = await saveContacts(list);
  $('composeMsg').style.display = 'none';
  toast(`已存 ${ok} 位常用联系人${skip ? `（${skip} 个地址没识别出来）` : ''}`);
};

/* ---------------- 通讯录管理弹窗 ---------------- */
function openContactModal() {
  $('contactMsg').style.display = 'none';
  $('contactMask').classList.add('show');
  loadContacts();
}

function renderContactList() {
  const kw = ($('ctSearch').value || '').trim().toLowerCase();
  const items = S.contacts.filter((c) => !kw
    || (c.email || '').toLowerCase().includes(kw)
    || (c.name || '').toLowerCase().includes(kw)
    || (c.note || '').toLowerCase().includes(kw));
  const SRC = { manual: '手动添加', mail: '邮件保存', send: '发信自动' };
  const box = $('ctList');
  if (!items.length) {
    box.innerHTML = `<div class="ct-empty">${S.contacts.length ? '没有匹配的联系人' : '还没有联系人，用上面的表单加一个试试'}</div>`;
    return;
  }
  box.innerHTML = items.map((c) => `
    <div class="ct-row" data-row="${c.id}">
      <span class="ct-ico" style="background:${colorOf(c.email)}">${esc(ctInitials(c))}</span>
      <span class="ct-who">
        <b>${esc(c.name || c.email.split('@')[0])}</b>
        <span>${esc(c.email)}${c.note ? ' · ' + esc(c.note) : ''}${c.use_count > 1 ? ` · 用过 ${c.use_count} 次` : ''}</span>
      </span>
      <span class="ct-tag ${c.source === 'manual' ? '' : 'auto'}">${SRC[c.source] || '已保存'}</span>
      <button class="mini-link" data-write="${c.id}">写信</button>
      <button class="ct-del" data-del="${c.id}">删除</button>
    </div>`).join('');

  box.querySelectorAll('[data-write]').forEach((btn) => {
    btn.onclick = () => {
      const c = S.contacts.find((x) => String(x.id) === btn.dataset.write);
      $('contactMask').classList.remove('show');
      if (c) openCompose({ to: c.email });
    };
  });
  box.querySelectorAll('[data-del]').forEach((btn) => {
    btn.onclick = async () => {
      const c = S.contacts.find((x) => String(x.id) === btn.dataset.del);
      if (!c || !confirm(`删除联系人 ${c.name || c.email}？`)) return;
      await api('/api/contacts/' + c.id, { method: 'DELETE' });
      await loadContacts();
      toast('已删除');
    };
  });
}

$('ctAdd').onclick = async () => {
  const email = ($('ctEmail').value || '').trim();
  if (!email || email.indexOf('@') < 0) { contactMsg('请填写正确的邮箱地址'); return; }
  try {
    await api('/api/contacts', {
      method: 'POST',
      body: JSON.stringify({ email: email, name: $('ctName').value.trim(), note: $('ctNote').value.trim() }),
    });
    $('ctName').value = ''; $('ctEmail').value = ''; $('ctNote').value = '';
    $('contactMsg').style.display = 'none';
    await loadContacts();
    toast('已保存');
  } catch (e) {
    contactMsg(e.message);
  }
};
['ctName', 'ctEmail', 'ctNote'].forEach((id) => $(id).addEventListener('keydown', (e) => {
  if (e.key === 'Enter') $('ctAdd').click();
}));
function contactMsg(text) {
  const el = $('contactMsg');
  el.textContent = text;
  el.style.display = '';
}
$('ctSearch').addEventListener('input', renderContactList);
$('contactClose').onclick = () => $('contactMask').classList.remove('show');
$('btnContactMore').onclick = () => openContactModal();

// 调试/分享用：#contacts 深链直接打开通讯录
if ((location.hash || '') === '#contacts') openContactModal();

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
    const r = S.syncAccounts[String(a.id)];
    // 上次同步没成功的账号，行上直接挂一个红色「!」（2.0.7）。
    // 以前只在中间栏顶部飘一条错误，账号一多就不知道到底是哪个没更新。
    const warn = (r && r.ok === false)
      ? `<span class="acc-warn" title="${esc(r.partial ? '部分文件夹没同步：' + (r.error || '') : '没同步成功：' + (r.error || ''))}">!</span>`
      : '';
    html += `<div class="acc-item ${String(S.account) === String(a.id) ? 'active' : ''}" data-acc="${a.id}">
      <div class="avatar" style="background:${colorOf(a.email)}">${esc(initials(a.name, a.email))}</div>
      <div class="meta"><div class="name">${esc(a.name || a.email)}</div>
        <div class="addr">${esc(a.email)}</div></div>
      ${warn}
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
    const r = S.syncAccounts[String(accId)];
    const tip = r
      ? (r.ok === false ? ('上次没同步成功：' + (r.error || '').slice(0, 140))
                        : `上次同步拉到 ${r.count} 封`)
      : '还没同步过';
    el.title = `${tip}\n右键，或点右边的 ⋯ 可以同步 / 删除这个账号`;
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
  if (id) syncAccount(id);
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
  if (e.key === 'Escape') { hideCtxMenu(); closeSettings(); }
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
    <div class="mail ${m.seen ? '' : 'unread'} ${S.selected === m.id ? 'active' : ''} ${S.sel.has(m.id) ? 'sel' : ''}" data-id="${m.id}">
      <label class="mcheck" title="选择这封"><input type="checkbox" data-id="${m.id}" ${S.sel.has(m.id) ? 'checked' : ''}></label>
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
      <button class="mdel" type="button" data-id="${m.id}" title="删除这封邮件">🗑</button>
    </div>`).join('');
  box.querySelectorAll('.mail').forEach((el) => {
    el.onclick = (e) => {
      if (e.target.closest('.mcheck') || e.target.closest('.mdel')) return;
      openMail(parseInt(el.dataset.id, 10));
    };
  });
  box.querySelectorAll('.mcheck input').forEach((cb) => {
    cb.onchange = () => toggleSelect(parseInt(cb.dataset.id, 10), cb.checked);
  });
  box.querySelectorAll('.mdel').forEach((btn) => {
    btn.onclick = (e) => { e.stopPropagation(); deleteOne(parseInt(btn.dataset.id, 10)); };
  });
  // 未读会被排到最上面，而「点开即已读」会让刚点的那封立刻换位置。
  // 重排后把选中项滚回视野里，否则用户会觉得列表突然乱跳、找不到刚看的那封。
  if (S.selected) {
    const cur = box.querySelector(`.mail[data-id="${S.selected}"]`);
    if (cur) cur.scrollIntoView({ block: 'nearest' });
  }
}

/* ---------------------------------------------------------------- 选择 / 删除 */
function toggleSelect(id, checked) {
  if (checked) S.sel.add(id); else S.sel.delete(id);
  const row = document.querySelector(`.mail[data-id="${id}"]`);
  if (row) row.classList.toggle('sel', checked);
  refreshSelBar();
}

function refreshSelBar() {
  const n = S.sel.size;
  const bar = $('selBar'), btn = $('btnDeleteSel'), cnt = $('selCnt'), info = $('selInfo');
  if (bar) bar.hidden = n === 0;
  if (btn) { btn.disabled = n === 0; }
  if (cnt) cnt.textContent = n ? ` (${n})` : '';
  if (info) info.textContent = `已选 ${n} 封`;
  const all = $('selAll');
  if (all) all.checked = n > 0 && n === S.mails.length;
}

async function deleteOne(id) {
  if (!confirm('确定删除这封邮件吗？\n（会移入服务器「已删除」，在里面再删才是彻底删除）')) return;
  try {
    const r = await api('/api/emails/' + id, { method: 'DELETE' });
    if (r && r.failed) toast(`已删除，但 ${r.failed} 封没能在服务器上删掉（账号连接失败）`, 'warn');
    S.sel.delete(id);
    await loadList(); renderFolders(); refreshSelBar();
  } catch (e) {
    toast('删除失败：' + (e.message || e), 'err');
  }
}

async function deleteSelected() {
  const ids = [...S.sel];
  if (!ids.length) return;
  if (!confirm(`确定删除选中的 ${ids.length} 封邮件吗？\n（会移入服务器「已删除」，在里面再删才是彻底删除）`)) return;
  try {
    const r = await api('/api/emails/batch-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    });
    if (r && r.failed) toast(`已删除 ${r.deleted} 封，${r.failed} 封服务器删不动（账号连接失败）`, 'warn');
    S.sel.clear();
    await loadList(); renderFolders(); refreshSelBar();
  } catch (e) {
    toast('删除失败：' + (e.message || e), 'err');
  }
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
        <button id="btnSaveContact" title="把发件人存进常用联系人">存为联系人</button>
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
  $('btnSaveContact').onclick = async () => {
    const addr = m.from_addr || m.msg_from || '';
    if (!addr || addr.indexOf('@') < 0) { toast('这封邮件没有可用的发件人地址'); return; }
    await saveContacts([{ name: m.from_name || '', email: addr }]);
    toast('已存为常用联系人');
  };

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
  S.syncAccounts = sync.accounts || {};
  const last = (sync.messages || []).slice(-1)[0];
  $('syncText').textContent = sync.running ? '正在同步…' : '就绪';
  $('syncLine').textContent = last ? last.text : (sync.finished_at ? '上次同步已完成' : '');
  renderWarnings(sync.errors || []);
  // 同步中的状态也可能带着失败结果（部分账号先跑完），顺手把左侧标记刷新一下
  if (!sync.running && Object.keys(S.syncAccounts).length) renderAccounts();
}

function renderWarnings(errors) {
  const bar = $('warnBar');
  const list = errors || [];
  if (!list.length) { bar.classList.remove('show'); bar.innerHTML = ''; return; }
  const e = list[0];
  const others = list.slice(1);
  // 其它失败账号也要点名（以前只说「另有 N 个」，账号一多就查不出是谁）
  const more = others.length ? `
    <div class="warn-more">
      <b>另外 ${others.length} 个账号也没同步成功：</b>
      ${others.map((o) => `<div class="warn-row"><span class="warn-mail">${esc(o.email)}</span>
        <span class="warn-why">${esc((o.message || '').slice(0, 110))}</span></div>`).join('')}
    </div>` : '';
  const what = e.partial ? '部分文件夹没同步' : '没有同步成功';
  bar.innerHTML = `⚠ <b>${esc(e.email)}</b> ${what}：${esc(e.message)}${more}
    <div class="acts">
      ${e.action_url ? `<a href="${esc(e.action_url)}" target="_blank" rel="noopener">打开邮箱设置</a>` : ''}
      ${e.kind === 'need_auth' ? '<button id="warnAuth">重新授权</button>' : ''}
      <button id="warnRetryOne">只重试这个账号</button>
      <button id="warnRetry">重试全部账号</button>
    </div>`;
  bar.classList.add('show');
  if ($('warnAuth')) $('warnAuth').onclick = () => openAddAccount('Outlook', e.email);
  $('warnRetry').onclick = syncNow;
  $('warnRetryOne').onclick = () => {
    const acc = S.accounts.find((a) => (a.email || '').toLowerCase() === (e.email || '').toLowerCase());
    if (!acc) { toast('找不到这个账号，可能已被删除'); return; }
    syncAccount(acc.id);
  };
}

/** 只同步一个账号（不影响其它账号，失败的账号修好后用这个快速补一下）。 */
async function syncAccount(id) {
  try {
    const started = await api('/api/sync', { method: 'POST', body: JSON.stringify({ account: id }) });
    if (!started.started) toast('已有同步在进行中');
    applySync(started.sync);
    startSyncPolling();
  } catch (e) {
    toast('同步失败：' + e.message);
  }
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
$('btnDeleteSel').onclick = deleteSelected;
$('btnSelClear').onclick = () => { S.sel.clear(); loadList(); refreshSelBar(); };
$('selAll').onchange = (e) => {
  S.mails.forEach((m) => { if (e.target.checked) S.sel.add(m.id); else S.sel.delete(m.id); });
  loadList(); refreshSelBar();
};
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
  togglePick(false);
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
    loadContacts();          // 收件人已自动进常用联系人，刷新左栏
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
    // 调试/截图/分享用：设置弹窗的深链（#settings 通用 / #notify 提醒 / #pwd 账号安全 / #about 关于）
    const tab = ({ '#settings': 'general', '#notify': 'notify',
                   '#pwd': 'pwd', '#about': 'about' })[location.hash || ''];
    if (tab) openSettings(tab);
    // 调试/截图用：#compose 打开写信弹窗；#pick 顺带把常用联系人面板摊开
    const hash = location.hash || '';
    if (hash === '#compose' || hash === '#pick') {
      openCompose();
      if (hash === '#pick') { if ($('composeMask').classList.contains('show')) togglePick(true); }
    }
  } catch (e) {
    toast('加载失败：' + e.message);
  }
})();
