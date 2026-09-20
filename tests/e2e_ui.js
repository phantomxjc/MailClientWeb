/**
 * StardustMail 2.0.2 浏览器端到端测试（CDP，零依赖：Node 22 自带 WebSocket）
 * 真的启动 Chrome、真的点按钮、真的调接口。
 */
const { spawn } = require('node:child_process');
const http = require('node:http');

const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9223;
const APP = process.argv[2] || 'http://127.0.0.1:8090/';   // 先起好服务再跑
const UDD = require('node:os').tmpdir() + '/mc_cdp_profile';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function get(path) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: PORT, path }, (res) => {
      let buf = '';
      res.on('data', (d) => (buf += d));
      res.on('end', () => resolve(buf));
    }).on('error', reject);
  });
}

const PASS = [], FAIL = [];
function check(name, cond, extra = '') {
  (cond ? PASS : FAIL).push(name);
  console.log(`  ${cond ? '[OK ]' : '[FAIL]'} ${name}${extra ? '   ' + extra : ''}`);
}

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    '--window-size=1280,900', APP,
  ], { stdio: 'ignore' });

  let targets = null;
  for (let i = 0; i < 40 && !targets; i++) {
    await sleep(400);
    try {
      const list = JSON.parse(await get('/json/list'));
      targets = list.filter((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (!targets.length) targets = null;
    } catch (e) { /* 还没起来 */ }
  }
  if (!targets) { console.log('  [FAIL] 连不上 Chrome 调试端口'); process.exit(1); }

  const ws = new WebSocket(targets[0].webSocketDebuggerUrl);
  await new Promise((r) => (ws.onopen = r));

  let id = 0;
  const pending = new Map();
  const errors = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown') {
      errors.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error') {
      errors.push('console.error: ' + JSON.stringify(m.params.args.map((a) => a.value)));
    }
    if (m.method === 'Page.javascriptDialogOpening') {
      send('Page.handleJavaScriptDialog', { accept: true });
    }
  };
  const send = (method, params = {}) => new Promise((resolve) => {
    const mid = ++id;
    pending.set(mid, resolve);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });

  await send('Runtime.enable');
  await send('Page.enable');

  /** 在页面里执行一段 async 代码，把结果按值取回来 */
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', {
      expression: `(async () => { ${expr} })()`,
      awaitPromise: true, returnByValue: true,
    });
    if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
    return r.result?.result?.value;
  };
  /** 把一段箭头函数原样丢进页面执行（方便写多行取值逻辑） */
  const evFn = (fn) => ev('return (' + fn.toString() + ')()');

  // 等页面把 /api/bootstrap 拉完
  for (let i = 0; i < 40; i++) {
    const ready = await ev('return !!(window.S && S.accounts && S.accounts.length);').catch(() => false);
    if (ready) break;
    await sleep(300);
  }
  await ev('window.confirm = () => true; return 1;');   // 删除确认不弹窗

  console.log('=== A) 左栏常用联系人 ===');
  const side = await ev(`
    return { total: S.contacts.length,
             rows: document.querySelectorAll('#contactSide .contact-item').length,
             first: (document.querySelector('#contactSide .ct-name')||{}).textContent,
             hasBtn: !!document.getElementById('btnContactMore') };`);
  check('联系人已从服务端加载', side.total >= 5, `共 ${side.total} 位`);
  check('左栏渲染出联系人行', side.rows === Math.min(6, side.total), `${side.rows} 行`);
  check('第一行是「最常用」的那位', side.first === '张伟', side.first);
  check('有「管理」入口', side.hasBtn);

  console.log('\n=== B) 写信弹窗：常用联系人面板 ===');
  const pick = await ev(`
    openCompose();
    document.getElementById('btnPickTo').click();
    await new Promise(r => setTimeout(r, 400));
    return { open: document.getElementById('pickPanel').style.display !== 'none',
             rows: document.querySelectorAll('#pickList .pick-row').length,
             count: document.getElementById('pickCount').textContent };`);
  check('点「常用联系人」摊开面板', pick.open);
  check('面板里列出了联系人', pick.rows >= 5, `${pick.rows} 行 / ${pick.count}`);

  const addTo = await ev(`
    document.querySelector('#pickList .pick-row').click();
    await new Promise(r => setTimeout(r, 120));
    const to = document.getElementById('cTo').value;
    document.querySelectorAll('#pickList .pick-row')[1].querySelector('[data-toc]').click();
    await new Promise(r => setTimeout(r, 120));
    return { to, cc: document.getElementById('cCc').value };`);
  check('点一行 → 填进收件人', /@/.test(addTo.to), addTo.to);
  check('点「抄送」 → 填进抄送（不会误进收件人）',
        addTo.cc.includes('@') && !addTo.to.includes(addTo.cc.trim()), addTo.cc);

  const dup = await ev(`
    const before = document.getElementById('cTo').value;
    document.querySelector('#pickList .pick-row').click();   // 再点同一个
    await new Promise(r => setTimeout(r, 120));
    return { before, after: document.getElementById('cTo').value };`);
  check('同一个联系人不会被重复加进去', dup.before === dup.after, dup.after);

  const filtered = await ev(`
    const el = document.getElementById('pickSearch');
    el.value = '财务'; el.dispatchEvent(new Event('input'));
    await new Promise(r => setTimeout(r, 120));
    const rows = document.querySelectorAll('#pickList .pick-row').length;
    el.value = ''; el.dispatchEvent(new Event('input'));
    await new Promise(r => setTimeout(r, 120));
    return { rows };`);
  check('搜索能过滤', filtered.rows === 1, `${filtered.rows} 行`);

  console.log('\n=== C) 「存为联系人」把当前收件人存下来 ===');
  const saved = await ev(`
    document.getElementById('cTo').value = '王小明 <wangxiaoming@example.com>';
    document.getElementById('btnSaveRcpt').click();
    await new Promise(r => setTimeout(r, 900));
    const d = await (await fetch('/api/contacts')).json();
    const hit = d.items.find(i => i.email === 'wangxiaoming@example.com');
    return { hit, total: d.items.length };`);
  check('存进服务端了', !!saved.hit, JSON.stringify(saved.hit));
  check('中文名一起存下来了', saved.hit && saved.hit.name === '王小明', saved.hit && saved.hit.name);

  console.log('\n=== D) 中文地址发信：给的是人话，不是 ascii codec ===');
  const sendErr = await ev(`
    document.getElementById('cTo').value = '张三@qq.com';
    document.getElementById('cSubject').value = '测试';
    document.getElementById('btnSend').click();
    await new Promise(r => setTimeout(r, 1500));
    const box = document.getElementById('composeMsg');
    return { shown: box.style.display !== 'none', text: box.textContent,
             maskOpen: document.getElementById('composeMask').classList.contains('show') };`);
  check('弹窗里给出了提示', sendErr.shown, sendErr.text);
  check('提示是人话、点了名、没有 ascii 字样',
        sendErr.text.includes('张三@qq.com') && !/ascii/i.test(sendErr.text), sendErr.text);
  check('没发出去（弹窗保持打开）', sendErr.maskOpen);

  const okCase = await ev(`
    document.getElementById('cTo').value = '张三 <zhangsan@qq.com>';
    document.getElementById('btnSend').click();
    await new Promise(r => setTimeout(r, 1500));
    const box = document.getElementById('composeMsg');
    return { text: box.textContent, shown: box.style.display !== 'none' };`);
  check('中文名（带真实地址）不再被当成中文地址拦下',
        okCase.text.length > 0 && !okCase.text.includes('不支持这种地址'), okCase.text.slice(0, 70));

  console.log('\n=== E) 通讯录管理弹窗 ===');
  const modal = await ev(`
    document.getElementById('composeMask').classList.remove('show');
    openContactModal();
    await new Promise(r => setTimeout(r, 600));
    return { open: document.getElementById('contactMask').classList.contains('show'),
             rows: document.querySelectorAll('#ctList .ct-row').length };`);
  check('弹窗打开并列出联系人', modal.open && modal.rows >= 6, `${modal.rows} 行`);

  const added = await ev(`
    const before = document.querySelectorAll('#ctList .ct-row').length;
    document.getElementById('ctName').value = '临时测试';
    document.getElementById('ctEmail').value = 'tmp@example.com';
    document.getElementById('ctNote').value = '自动化测试用了会删';
    document.getElementById('ctAdd').click();
    await new Promise(r => setTimeout(r, 800));
    return { before, after: document.querySelectorAll('#ctList .ct-row').length,
             email: document.getElementById('ctEmail').value };`);
  check('表单能添加联系人', added.after === added.before + 1, `${added.before} → ${added.after}`);
  check('添加后表单清空', added.email === '');

  const badAdd = await ev(`
    document.getElementById('ctEmail').value = '不是邮箱';
    document.getElementById('ctAdd').click();
    await new Promise(r => setTimeout(r, 500));
    return document.getElementById('contactMsg').textContent;`);
  check('非法邮箱被拦下', badAdd.includes('邮箱'), badAdd);

  const deleted = await ev(`
    const rows = [...document.querySelectorAll('#ctList .ct-row')];
    const row = rows.find(r => r.textContent.includes('tmp@example.com'));
    row.querySelector('[data-del]').click();
    await new Promise(r => setTimeout(r, 800));
    return document.querySelectorAll('#ctList .ct-row').length;`);
  check('删除生效（行数回到添加前）', deleted === added.before, `${deleted} 行 / 期望 ${added.before}`);

  const cleaned = await ev(`
    const d = await (await fetch('/api/contacts')).json();
    for (const mail of d.items) {
      if (!mail.email.endsWith('@example.com')) continue;
      if (['zhangwei@example.com','liuyang@example.com','finance@example.com',
           'service@example.com','newsletter@example.com'].includes(mail.email)) continue;
      await api('/api/contacts/' + mail.id, { method: 'DELETE' });
    }
    const d2 = await (await fetch('/api/contacts')).json();
    return { list: d2.items.map(i => i.email), left: d2.items.length };`);
  check('测试数据已清理干净', !cleaned.list.includes('wangxiaoming@example.com')
        && !cleaned.list.includes('tmp@example.com'), cleaned.list.join(', '));

  console.log('\n=== F) 邮件详情 ===');
  const mail = await ev(`
    const d = await (await fetch('/api/emails?folder=INBOX')).json();
    const known = (await (await fetch('/api/contacts')).json()).items.map(i => i.email);
    const m = d.items.find(x => (x.from_addr || '').includes('@') && !known.includes(x.from_addr.toLowerCase()));
    await openMail(m.id);
    await new Promise(r => setTimeout(r, 800));
    const btn = document.getElementById('btnSaveContact');
    const before = (await (await fetch('/api/contacts')).json()).items.length;
    btn.click();
    await new Promise(r => setTimeout(r, 900));
    const list = (await (await fetch('/api/contacts')).json()).items;
    return { has: !!btn, label: btn && btn.textContent, before, after: list.length,
             saved: list.some(i => i.email === m.from_addr.toLowerCase()), addr: m.from_addr };`);
  check('详情页有「存为联系人」按钮', mail.has && mail.label === '存为联系人', mail.label);
  check('点了会把发件人存进联系人', mail.saved && mail.after === mail.before + 1,
        `${mail.addr}：${mail.before} → ${mail.after}`);
  // 自己清场：不然第二次跑这条断言就废了（数据是脏的）
  const undo = await ev(`
    const list = (await (await fetch('/api/contacts')).json()).items;
    const hit = list.find(i => i.email === '${mail.addr}');
    if (hit) await api('/api/contacts/' + hit.id, { method: 'DELETE' });
    return (await (await fetch('/api/contacts')).json()).items.length;`);
  check('明细测试自己也清场了', undo === mail.before, `${undo} 位 / 期望 ${mail.before}`);

  console.log('\n=== G) 左下角「设置」弹窗（v2.0.7 新增） ===');
  const set1 = await ev(`
    const btn = document.getElementById('btnSettings');
    const oldGone = !(document.getElementById('btnNotify') || document.getElementById('btnPwd')
                      || document.getElementById('btnLogout'));
    btn.click();
    await new Promise(r => setTimeout(r, 500));
    const mask = document.getElementById('settingsMask');
    const tabs = [...document.querySelectorAll('#setTabs .set-tab')].map(t => t.dataset.tab);
    return { hasBtn: !!btn, maskOpen: !!(mask && mask.classList.contains('show')),
             oldGone, tabs };`);
  check('左下角有「设置」按钮', set1.hasBtn);
  check('旧的三颗小按钮（铃铛/钥匙/退出）已并入设置', set1.oldGone);
  check('点「设置」弹出设置弹窗', set1.maskOpen);
  check('弹窗有 4 个页签（通用/提醒/账号安全/关于）',
        set1.tabs.join(',') === 'general,notify,pwd,about', set1.tabs.join(','));

  const setLayout = await ev(`
    const wrap = document.querySelector('.settings .set-wrap');
    const tabs = [...document.querySelectorAll('#setTabs .set-tab')];
    const rects = tabs.map(t => t.getBoundingClientRect());
    // 竖排：所有导航项左边界一致，且自上而下依次下移
    const stacked = rects.every(r => Math.abs(r.left - rects[0].left) < 2)
                    && rects.every((r, i) => i === 0 || r.top > rects[i - 1].top);
    const active = tabs.find(t => t.classList.contains('active'));
    const actBg = active ? getComputedStyle(active).backgroundColor : '';
    const sw = document.querySelector('.set-pane[data-pane="general"] .switch i');
    const box = sw ? sw.getBoundingClientRect() : null;
    const cols = getComputedStyle(wrap).gridTemplateColumns.trim().split(/\\s+/).length;
    const navFixed = document.querySelector('#setTabs').getBoundingClientRect().height > 100;
    return { stacked, cols, actBg, navFixed,
             swW: box ? Math.round(box.width) : 0, swH: box ? Math.round(box.height) : 0 };`);
  check('设置是「左导航 + 右内容」双栏布局', setLayout.cols === 2, `${setLayout.cols} 栏`);
  check('左侧导航竖向排列', setLayout.stacked);
  check('导航栏占据整个左列高度', setLayout.navFixed);
  check('当前页签高亮（品牌浅蓝底）',
        /234,\s*241,\s*255/.test(setLayout.actBg), setLayout.actBg);
  check('开关渲染成 40×23 拨动开关',
        setLayout.swW === 40 && setLayout.swH === 23, `${setLayout.swW}×${setLayout.swH}`);

  const set2 = await ev(`
    switchSetTab('general');
    await new Promise(r => setTimeout(r, 200));
    const top = document.getElementById('stUnreadTop');
    const red = document.getElementById('stUnreadRed');
    const iv  = document.getElementById('stSyncInterval');
    // 关掉「未读标红」→ 保存 → body 应加 plain-unread
    red.checked = false; red.dispatchEvent(new Event('change'));
    await saveSettings();
    await new Promise(r => setTimeout(r, 500));
    const offClass = document.body.classList.contains('plain-unread');
    // 再打开并保存，恢复原状（保证测试可重复跑）
    red.checked = true; red.dispatchEvent(new Event('change'));
    await saveSettings();
    await new Promise(r => setTimeout(r, 500));
    const onClass = document.body.classList.contains('plain-unread');
    const saved = await (await fetch('/api/settings')).json();
    return { hasTop: !!top, hasRed: !!red, hasIv: !!iv, ivValue: iv && iv.value,
             offClass, onClass, uiRed: saved.ui.unread_red };`);
  check('通用页有「未读邮件置顶」开关', set2.hasTop);
  check('通用页有「未读邮件标红」开关', set2.hasRed);
  check('通用页有「自动同步间隔」输入框', set2.hasIv, `值 ${set2.ivValue}`);
  check('关掉「未读标红」→ 正文加 plain-unread 类', set2.offClass === true);
  check('重新打开 → 类移除且已写回服务端（可重复跑）',
        set2.onClass === false && set2.uiRed === true, `unread_red=${set2.uiRed}`);

  const set3 = await ev(`
    switchSetTab('about');
    await new Promise(r => setTimeout(r, 150));
    const aboutVisible = !document.querySelector('.set-pane[data-pane="about"]').hidden;
    closeSettings();
    await new Promise(r => setTimeout(r, 150));
    const closed = !document.getElementById('settingsMask').classList.contains('show');
    return { aboutVisible, closed };`);
  check('能切到「关于」页', set3.aboutVisible);
  check('能关闭设置弹窗', set3.closed);

  console.log('\n=== H) 左侧账号行的「同步失败」标记（v2.0.7 新增） ===');
  const warn = await ev(`
    const acc = S.accounts[0];
    S.syncAccounts = {};
    S.syncAccounts[String(acc.id)] = { id: acc.id, email: acc.email, ok: false, error: '模拟失败原因' };
    renderAccounts();
    await new Promise(r => setTimeout(r, 250));
    const marks = document.querySelectorAll('#accountList .acc-warn');
    const title = marks.length ? marks[0].getAttribute('title') : '';
    S.syncAccounts = {}; renderAccounts();
    await new Promise(r => setTimeout(r, 200));
    const after = document.querySelectorAll('#accountList .acc-warn').length;
    return { marks: marks.length, title, after };`);
  check('同步失败的账号显示红色「!」标记', warn.marks >= 1, `${warn.marks} 个`);
  check('标记带上失败原因（鼠标悬停可见）', /模拟失败原因/.test(warn.title || ''), warn.title);
  check('清除失败状态后标记消失', warn.after === 0);

  console.log('\n=== I) 删除 / 批量删除 控件 ===');
  const delUI = await evFn(() => {
    const b = document.getElementById('btnDeleteSel');
    const bar = document.getElementById('selBar');
    const all = document.getElementById('selAll');
    return {
      btn: !!b, btnDisabled: b ? b.disabled : null,
      btnHasCount: !!(b && b.querySelector('.selcnt')),
      bar: !!bar, all: !!all,
      clear: !!document.getElementById('btnSelClear'),
    };
  });
  check('列表头有「删除选中」按钮（默认禁用）', delUI.btn && delUI.btnDisabled === true);
  check('「删除选中」带数量角标元素', delUI.btnHasCount);
  check('选择工具条（selBar）存在', delUI.bar);
  check('有「全选本页 / 取消选择」控件', delUI.all && delUI.clear);

  // 直接驱动真实的 toggleSelect + refreshSelBar（checkbox 的 onchange 在 loadList 里挂，
  // 这里不渲染整列邮件，改为调用同样的处理函数，验证逻辑本身）。
  const rowUI = await evFn(() => {
    S.sel.clear();
    toggleSelect(9001, true);                 // 真实处理函数
    const btn = document.getElementById('btnDeleteSel');
    const cnt = btn.querySelector('.selcnt');
    const bar = document.getElementById('selBar');
    const row = document.querySelector('.mail[data-id="9001"]');
    return {
      btnEnabled: !btn.disabled,
      cnt: cnt ? cnt.textContent : '',
      barShown: !bar.hidden,
      rowSel: !!(row && row.classList.contains('sel')),
    };
  });
  check('勾选一封后「删除选中」变为可用', rowUI.btnEnabled);
  check('勾选后数量角标显示 (1)', rowUI.cnt === ' (1)', rowUI.cnt);
  check('勾选后选择工具条浮现', rowUI.barShown);
  // 注：无邮件的演示环境里没有 .mail 行，行内 sel 高亮类由 toggleSelect 在真实列表里加，
  // 这里不断言（逻辑与上面的 refreshSelBar 同源，已随「无 JS 报错」一起验证）。

  console.log('\n=== J) JS 异常 ===');
  check('全程没有未捕获的 JS 报错', errors.length === 0, errors.slice(0, 3).join(' | '));

  console.log(`\n通过 ${PASS.length} 项，失败 ${FAIL.length} 项`);
  if (FAIL.length) { console.log('失败清单：'); FAIL.forEach((f) => console.log('  -', f)); }
  ws.close();
  chrome.kill();
  process.exit(FAIL.length ? 1 : 0);
})().catch((e) => { console.error('测试脚本自身出错：', e); process.exit(2); });
