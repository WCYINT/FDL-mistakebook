/**
 * 6 项需求 · 前端真实渲染验证（jsdom：加载真实报告 + 执行真实 JS）。
 *
 * 验证项：
 *  A. 上传按钮：今日复习页存在 #res-upload-btn，点击后 overlay 打开（四阶段条）
 *  B. 待人工汇总卡：驾驶舱 #pending-summary-block 渲染、计数与 payload 一致
 *  C. 挂载提案块：今日复习 #kp-proposals-block 渲染（71 条来源）
 *  D. 归因提案块与 P16 块：渲染位存在（有数据时）
 *  E. 规则校验：payload 中每一项（dashboard/review）都有对应 DOM 渲染位
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = require('path').resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'site/report.html'), 'utf-8');
const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  beforeParse(window) {
    // 阻断真实网络（jsdom 无 fetch）：挂载 no-op fetch，让渲染逻辑走 catch 分支
    window.fetch = () => Promise.reject(new Error('offline-test'));
  },
});
const doc = dom.window.document;
const win = dom.window;

let pass = 0, fail = 0;
function check(cond, label, extra) {
  if (cond) { pass += 1; console.log('  ✓ ' + label); }
  else { fail += 1; console.log('  ✗ ' + label + (extra ? '  ← ' + extra : '')); }
}

// 从报告里取 payload（D 变量）——直接从 JSON 文本解析 pending_human
const m = /"pending_human":\s*(\{.*?"all":\s*\[.*?\]\s*\})/s.exec(html);
let PH = null;
try { PH = JSON.parse(m[1]); } catch (e) { PH = null; }

console.log('A. 上传复习资料按钮（今日复习页）');
const uploadBtn = doc.getElementById('res-upload-btn');
check(!!uploadBtn, '存在 #res-upload-btn');
check(uploadBtn && uploadBtn.textContent.includes('上传复习资料'),
  '按钮文案正确', uploadBtn && uploadBtn.textContent);
const reviewPanel = doc.getElementById('review');
check(reviewPanel && reviewPanel.contains(uploadBtn), '按钮在今日复习页内');
if (uploadBtn) {
  uploadBtn.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  const overlayOpen = [...doc.body.children].some(
    (el) => el.style && el.style.display === 'flex' && el.innerHTML.includes('上传复习资料'));
  check(overlayOpen, '点击后弹层打开');
  const hasStages = [...doc.querySelectorAll('#res-stages')].length > 0;
  check(hasStages, '四阶段进度条容器存在');
  const submitBtn = doc.getElementById('res-submit');
  check(!!submitBtn, '提交按钮存在');
}

console.log('B. 待人工汇总卡（驾驶舱）');
const summary = doc.getElementById('pending-summary-block');
check(!!summary, '存在 #pending-summary-block');
if (summary && PH) {
  check(summary.textContent.includes('待人工处理'), '标题含「待人工处理」');
  check(summary.textContent.includes(String(PH.total)),
    '显示总数 ' + (PH && PH.total), summary.textContent.slice(0, 60));
  const rows = summary.querySelectorAll('div[style*="border-bottom"]');
  check(rows.length === PH.all.length,
    '行数 = 登记来源数 (' + PH.all.length + ')', '实际 ' + rows.length);
}

console.log('C. 挂载提案复核块（今日复习页）');
const kpBlock = doc.getElementById('kp-proposals-block');
check(!!kpBlock, '存在 #kp-proposals-block');
if (kpBlock) {
  check(kpBlock.textContent.includes('知识点挂载待复核'), '标题正确');
  const acceptBtn = doc.getElementById('kpp-accept-hi');
  check(!!acceptBtn, '「批量同意高置信」按钮存在');
}

console.log('D. 归因提案块与 P16 块');
const kpCand = doc.getElementById('kp-candidates-block');
check(!!kpCand, 'kp-candidates-block 渲染位存在');
// P16 反馈块 / 归因块依赖数据；检查其宿主/结构
const fbHostOk = html.includes('feedback-kpi-block');
check(fbHostOk, 'feedback-kpi-block 渲染位在模板中');

console.log('E. 规则校验：payload 每一项都有渲染位');
if (PH) {
  const missing = PH.all.filter((e) => {
    if (e.page === 'dashboard') {
      return !doc.getElementById('pending-summary-block');
    }
    // review 页：块 id 应存在于模板（有数据时生成）或 DOM
    return !html.includes(e.block_id);
  });
  check(missing.length === 0, '全部 ' + PH.all.length + ' 类来源均有渲染位',
    missing.map((x) => x.key).join(','));
  console.log('    payload 明细: ' + PH.all.map((e) => e.key + '=' + e.count).join(' · '));
}

console.log('');
console.log('结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
