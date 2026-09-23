/**
 * 2.2.2 功能截图 —— CDP 真实操作，产出四张图：
 *   1) 写信弹窗：发送按钮旁的「定时」下拉（选中 5 分钟后）
 *   2) 定时队列弹窗：一条排队中的任务
 *   3) 已发送文件夹：列表「对方」列显示收件人
 *   4) 定时下拉展开态（模拟键盘展开截不了，改为显示自定义时间输入）
 *
 * 用法：node tests/shot_222.js http://127.0.0.1:8090/ <输出目录>
 */
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const BASE = process.argv[2] || 'http://127.0.0.1:8090/';
const OUT = process.argv[3] || path.resolve(__dirname, '../docs/shots');
const CHROME = 'C:/Program Files/Google/Chrome/Application/chrome.exe';
const PORT = 9334;

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
    '--window-size=1440,1100', '--hide-scrollbars', 'about:blank',
  ], { stdio: 'ignore' });

  let tabs = null;
  for (let i = 0; i < 40 && !tabs; i++) {
    try { tabs = await getJSON(`http://127.0.0.1:${PORT}/json/list`); } catch (e) { await sleep(200); }
  }
  if (!tabs) { chrome.kill(); throw new Error('Chrome 没起来'); }
  const wsUrl = tabs.find((t) => t.type === 'page').webSocketDebuggerUrl;

  const ws = new WebSocket(wsUrl);
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
    if (r.result && r.result.exceptionDetails) throw r.result.exceptionDetails;
    return r.result && r.result.result ? r.result.result.value : null;
  };

  const shot = async (name) => {
    const r = await send('Page.captureScreenshot', {
      format: 'png', captureBeyondViewport: false,
    });
    if (!r.result || !r.result.data) {
      console.error('captureScreenshot 响应异常:', JSON.stringify(r).slice(0, 300));
      throw new Error('截图失败');
    }
    fs.writeFileSync(path.join(OUT, name), Buffer.from(r.result.data, 'base64'));
    console.log('saved', name);
  };

  await send('Page.enable');
  await send('Page.navigate', { url: BASE });
  await sleep(2500);

  /* 1) 写信弹窗 + 定时下拉选中「5 分钟后发送」 */
  await ev(`
    document.getElementById('btnCompose').click();
    await new Promise(r => setTimeout(r, 250));
    document.getElementById('cTo').value = 'zhangsan@qq.com';
    document.getElementById('cSubject').value = '给主人的部署笔记';
    document.getElementById('cBody').value = '这是一封演示用的定时发送邮件。';
    document.getElementById('cWhen').value = '300';
  `);
  await sleep(250);
  await shot('v222-1-写信定时下拉.png');

  /* 2) 自定义时间输入 */
  await ev(`
    document.getElementById('cWhen').value = 'custom';
    document.getElementById('cWhen').dispatchEvent(new Event('change'));
  `);
  await sleep(200);
  await shot('v222-2-自定义时间.png');

  /* 3) 定时队列弹窗（先通过 API 排一条任务进去） */
  const due = Math.floor(Date.now() / 1000) + 1800;
  const fd = `account_id=1&to=lisi@163.com&subject=月报会在定时发送&body=到点自动发出&send_at=${due}`;
  await ev(`
    await fetch('/api/send', { method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: '${fd}' });
  `);
  await ev(`
    document.getElementById('composeMask').classList.remove('show');
    document.getElementById('btnSched').click();
    await new Promise(r => setTimeout(r, 500));
  `);
  await shot('v222-3-定时队列.png');

  /* 4) 已发送文件夹：对方列显示收件人 */
  await ev(`
    document.getElementById('btnSchedClose').click();
    S.folder = 'Sent'; S.multi = false; S.sel.clear();
    await loadList();
    await new Promise(r => setTimeout(r, 400));
  `);
  await sleep(200);
  await shot('v222-4-已发送显示收件人.png');

  chrome.kill();
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
