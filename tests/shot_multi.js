/**
 * 多选模式截图 —— 走 CDP 真实点按钮，产出三张对比图：
 *   1) 默认态：行首没有勾选框，列表头按钮写「多选」
 *   2) 多选态：勾选框出现，按钮变「删除选中」，工具条浮现
 *   3) 详情页：右上工具栏多出红色「删除」按钮
 *
 * 用法：node tests/shot_multi.js http://127.0.0.1:8090/ <输出目录>
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const BASE = process.argv[2] || 'http://127.0.0.1:8090/';
const OUT = process.argv[3] || path.resolve(__dirname, '../docs/shots');
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9333;

fs.mkdirSync(OUT, { recursive: true });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function getJSON(url) {
  return new Promise((res, rej) => {
    http.get(url, (r) => {
      let d = '';
      r.on('data', (c) => (d += c));
      r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } });
    }).on('error', rej);
  });
}

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', `--remote-debugging-port=${PORT}`, '--no-proxy-server',
    '--no-first-run', '--no-default-browser-check', '--disable-gpu',
    '--window-size=1440,900', '--hide-scrollbars', 'about:blank',
  ], { stdio: 'ignore' });

  let tabs = null;
  for (let i = 0; i < 40 && !tabs; i++) {
    try { tabs = await getJSON(`http://127.0.0.1:${PORT}/json/list`); } catch (e) { await sleep(200); }
  }
  if (!tabs) { chrome.kill(); throw new Error('Chrome 没起来'); }
  const wsUrl = tabs.find((t) => t.type === 'page').webSocketDebuggerUrl;

  const ws = new WebSocket(wsUrl);                 // Node 22 自带，不用装 ws
  let id = 0; const pending = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  await new Promise((r) => (ws.onopen = r));
  const send = (method, params = {}) => new Promise((res) => {
    const i = ++id; pending.set(i, res); ws.send(JSON.stringify({ id: i, method, params }));
  });

  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', {
      expression: `(async () => { ${expr} })()`, awaitPromise: true, returnByValue: true,
    });
    return r.result?.result?.value;
  };
  const shot = async (name, clip) => {
    const r = await send('Page.captureScreenshot', {
      format: 'png', captureBeyondViewport: false, ...(clip ? { clip } : {}),
    });
    const f = path.join(OUT, name);
    fs.writeFileSync(f, Buffer.from(r.result.data, 'base64'));
    console.log('  写出', f);
  };

  await send('Page.enable');
  await send('Runtime.enable');

  // 等页面把 bootstrap 拉完
  await send('Page.navigate', { url: BASE });
  for (let i = 0; i < 50; i++) {
    const ok = await ev('return !!(window.S && S.accounts && S.accounts.length);').catch(() => false);
    if (ok) break;
    await sleep(300);
  }
  await sleep(1200);

  console.log('1) 默认态（无勾选框）');
  await ev(`
    if (S.multi) setMulti(false);
    S.folder = 'INBOX'; S.account = 'all'; S.selected = null;
    renderAccounts(); renderFolders();
    await loadList(); refreshSelBar();
    await new Promise(r => setTimeout(r, 500));
    return 1;`);
  await sleep(600);
  await shot('multi-1-默认态.png');

  console.log('2) 点「多选」→ 勾选框出现');
  await ev(`
    document.getElementById('btnDeleteSel').click();
    await new Promise(r => setTimeout(r, 700));
    return 1;`);
  await ev(`
    // 顺手勾两封，让「删除选中 (2)」的状态也入镜
    const rows = [...document.querySelectorAll('.mail')].slice(0, 2);
    if (S.mails[0]) toggleSelect(S.mails[0].id, true);
    if (S.mails[1]) toggleSelect(S.mails[1].id, true);
    await loadList(); refreshSelBar();
    await new Promise(r => setTimeout(r, 400));
    return 1;`);
  await sleep(600);
  await shot('multi-2-多选态.png');

  console.log('3) 详情页删除按钮');
  await ev(`
    setMulti(false);
    await new Promise(r => setTimeout(r, 400));
    const first = document.querySelector('.mail');
    if (first) openMail(parseInt(first.dataset.id, 10));
    await new Promise(r => setTimeout(r, 1200));
    return 1;`);
  await sleep(900);
  await shot('multi-3-详情页删除按钮.png');

  console.log('4) 详情页工具栏特写');
  const rect = await ev(`
    const t = document.querySelector('.d-tools');
    if (!t) return null;
    const r = t.getBoundingClientRect();
    return { x: Math.max(0, r.x - 24), y: Math.max(0, r.y - 24), w: r.width + 48, h: r.height + 48 };`);
  if (rect) {
    await shot('multi-4-删除按钮特写.png', { x: rect.x, y: rect.y, width: rect.w, height: rect.h, scale: 2 });
  } else {
    console.log('  （没找到 .d-tools，跳过特写）');
  }

  ws.close();
  chrome.kill();
  console.log('完成');
})().catch((e) => { console.error('出错：', e.message); process.exit(1); });
