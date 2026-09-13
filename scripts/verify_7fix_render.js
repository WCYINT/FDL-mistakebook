/**
 * 7 项修复 · 前端真实渲染验证（jsdom：加载真实报告 + 执行真实 JS + 模拟交互）。
 *
 * 验证项：
 *  A. #1 幂等采纳：提案行渲染 data-proposal-id；点击「批量同意」→ 行被标记
 *     （划线+禁用）、状态栏区分「已处理/幂等通过」
 *  B. #2 今日复习时间：能量面板渲染「今日复习时间」行（session+ASR 拆分）
 *  C. #4 复习日分块：复习列表按「待复习 / 已复习（按复习日）」两区块 + 卡片日期链
 *  D. #5 趋势含今日：trend 数据最后一项为今日（render 出 SVG）
 *  E. #6 反馈闭环：#feedback-kpi-block 渲染 by_trigger 分组 + 待办按钮 → 点击回标
 *  F. 红线：渲染产物无 emoji、无运行时错误
 *
 * fetch 桩策略：模拟 fdl_serve 在线（只对 /api/kp/proposals*、/api/intervention/done
 * 返回成功；其余拒绝），从而走通真实交互链路而非降级分支。
 *
 * 用法：node verify_7fix_render.js [report_path]
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = require('path').resolve(__dirname, '..');
const reportPath = process.argv[2] || path.join(ROOT, '.verify_tmp/report_preview.html');
const html = fs.readFileSync(reportPath, 'utf-8');

let pass = 0, fail = 0;
const runtimeErrors = [];
function check(cond, label, extra) {
  if (cond) { pass += 1; console.log('  OK  ' + label); }
  else { fail += 1; console.log('  FAIL  ' + label + (extra ? '  <- ' + String(extra).slice(0, 200) : '')); }
}

const vc = new VirtualConsole();
vc.on('warn', (m) => { if (String(m).includes('[FDL] runtime')) runtimeErrors.push(String(m)); });
vc.on('jsdomError', (e) => { runtimeErrors.push('jsdomError: ' + (e && e.message)); });
vc.on('error', (m) => { runtimeErrors.push('console.error: ' + m); });

// fetch 桩：模拟在线服务
// 2026-09-13：补 proposed_kp_id —— 前端按"有目标 KP（可复核）"与"无法匹配"分流渲染，
// 无 kp_id 的行不再显示 0.0 分与"同意"按钮（King #2 修复）。
const PROPOSALS = [
  { id: 901, mistake_id: 77001, proposed_kp_code: 'MATH-G4-CALC-01', proposed_kp_id: 999, confidence: 0.92 },
  { id: 902, mistake_id: 77002, proposed_kp_code: 'MATH-G4-READ-02', proposed_kp_id: 998, confidence: 0.71 },
  { id: 903, mistake_id: 77003, proposed_kp_code: 'MATH-G4-NORM-03', proposed_kp_id: 997, confidence: 0.42 },
];
const fetchLog = [];
function makeFetch() {
  return function (url, opts) {
    const u = String(url);
    fetchLog.push(u);
    const ok = (data) => Promise.resolve({ ok: true, json: () => Promise.resolve(data) });
    if (u.includes('/api/kp/proposals?') || u.endsWith('/api/kp/proposals')) {
      return ok({ items: PROPOSALS });
    }
    if (/\/api\/kp\/proposals\/\d+\/accept/.test(u)) {
      const id = parseInt(u.match(/proposals\/(\d+)\/accept/)[1], 10);
      // 902 号模拟后端"此前已处理"幂等路径
      return ok({ ok: true, already: id === 902, mistake_id: 77000 + id, kp_id: 999 });
    }
    if (u.includes('/api/intervention/done')) {
      return ok({ ok: true, id: 1, message: '干预动作已标记完成' });
    }
    return Promise.reject(new Error('offline-test: ' + u));
  };
}

const dom = new JSDOM(html, {
  runScripts: 'dangerously',
  pretendToBeVisual: true,
  virtualConsole: vc,
  beforeParse(window) {
    window.fetch = makeFetch();
    window.confirm = function () { return true; };
  },
});
const doc = dom.window.document;
const win = dom.window;

// 从 html 文本取载荷（括号匹配；review_all 内含嵌套结构，正则不可靠）
function extractJson(text, key) {
  const i = text.indexOf('"' + key + '"');
  if (i < 0) return null;
  let j = text.indexOf(':', i) + 1;
  while (text[j] === ' ' || text[j] === '\n' || text[j] === '\t' || text[j] === '\r') j += 1;
  const open = text[j], close = open === '[' ? ']' : '}';
  let depth = 0, k = j, instr = false, esc = false;
  while (k < text.length) {
    const c = text[k];
    if (instr) {
      if (esc) esc = false;
      else if (c === '\\') esc = true;
      else if (c === '"') instr = false;
    } else {
      if (c === '"') instr = true;
      else if (c === open) depth += 1;
      else if (c === close) { depth -= 1; if (depth === 0) return JSON.parse(text.slice(j, k + 1)); }
    }
    k += 1;
  }
  return null;
}

const reviewAll = extractJson(html, 'review_all');
const trend = extractJson(html, 'trend');
const ivSum = extractJson(html, 'intervention_summary');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async function main() {
  await sleep(150); // 等各 fetch 桩 promise settle + 各渲染 IIFE 完成

  console.log('A. #1 幂等采纳 · 挂载提案块（stub 在线）');
  const kpBlock = doc.getElementById('kp-proposals-block');
  check(!!kpBlock, '存在 #kp-proposals-block');
  let rows = kpBlock ? kpBlock.querySelectorAll('[data-proposal-id]') : [];
  check(rows.length === PROPOSALS.length, '提案行数 = ' + PROPOSALS.length + '（实际 ' + rows.length + '）');
  const acceptBtn = doc.getElementById('kpp-accept-hi');
  check(!!acceptBtn, '「批量同意高置信」按钮存在');
  if (acceptBtn && rows.length) {
    let threw = false;
    try { acceptBtn.dispatchEvent(new win.MouseEvent('click', { bubbles: true })); } catch (e) { threw = true; }
    check(!threw, '点击批量同意不抛异常');
    await sleep(150); // 等逐条 fetch + 行标记
    const row901 = kpBlock.querySelector('[data-proposal-id="901"]');
    const row902 = kpBlock.querySelector('[data-proposal-id="902"]');
    const row903 = kpBlock.querySelector('[data-proposal-id="903"]');
    check(row901 && row901.style.opacity === '0.45' && row901.style.textDecoration === 'line-through',
      '高置信行(0.92)被标记为已处理');
    check(row902 && row902.style.opacity === '0.45', '幂等行(0.71)同样被标记');
    check(row903 && row903.style.opacity !== '0.45', '低置信行(0.42)未被批量处理');
    const disabledAll = row901 && [...row901.querySelectorAll('button')].every((b) => b.disabled);
    check(!!disabledAll, '已处理行按钮被禁用');
    const st = kpBlock.querySelector('#kpp-status');
    check(st && st.textContent.includes('批量完成'), '状态栏显示批量完成');
    check(st && st.textContent.includes('幂等通过'), '状态栏区分「幂等通过」计数', st && st.textContent);
  }

  console.log('B. #2 今日复习时间 · 能量面板');
  const energyList = doc.getElementById('energy-list');
  check(!!energyList, '存在 #energy-list');
  if (energyList) {
    const t = energyList.textContent;
    check(t.includes('今日复习时间'), '渲染「今日复习时间」行');
    check(t.includes('录音口述'), '含「录音口述 X min + 会话 Y min」拆分');
    check(t.includes('上传复习资料'), '含「上传复习资料提交后更新」说明');
  }

  console.log('C. #4 复习日分块 · 复习列表');
  const reviewList = doc.getElementById('review-list');
  check(!!reviewList, '存在 #review-list');
  if (reviewList) {
    const t = reviewList.textContent;
    const nReviewing = (reviewAll || []).filter((x) => x.status === '待复习').length;
    const nReviewed = (reviewAll || []).filter(
      (x) => x.status !== '待复习' && x.status !== '已解决' && x.is_tamed !== 1).length;
    if (nReviewing > 0) check(t.includes('待复习'), '含「待复习」区块标题');
    if (nReviewed > 0) check(t.includes('已复习（按复习日）'), '含「已复习（按复习日）」区块标题');
    check(t.includes('复习记录：'), '卡片渲染「复习记录：日期链」');
    const withDates = (reviewAll || []).filter((x) => (x.review_dates || []).length > 0);
    if (withDates.length) {
      const mmdd = withDates[0].review_dates[0].slice(5);
      check(t.includes(mmdd), '日期链含载荷日期 ' + mmdd);
      check(/共 \d+ 天/.test(t), '日期链显示「共 N 天」');
    }
    const iA = t.indexOf('待复习'), iB = t.indexOf('已复习（按复习日）');
    if (iA >= 0 && iB >= 0) check(iA < iB, '「待复习」区块排在「已复习」之前');
    check(!/【已解决】/.test(t), '已解决项默认隐藏');
    const nCards = reviewList.querySelectorAll('[data-mcard]').length;
    check(nCards === nReviewing + nReviewed, '卡片数 = 待复习 + 已复习（' + nCards + '）');
  }

  console.log('D. #5 趋势含今日');
  check(Array.isArray(trend) && trend.length > 0, 'trend 载荷存在（' + (trend ? trend.length : 0) + ' 天）');
  if (Array.isArray(trend) && trend.length) {
    const last = trend[trend.length - 1];
    const today = new Date();
    const mmdd = String(today.getMonth() + 1).padStart(2, '0') + '-' + String(today.getDate()).padStart(2, '0');
    check(last.date === mmdd, 'trend 末项为今日 ' + mmdd, last.date);
    check('asr_min' in last, 'trend 条目含 asr_min 字段');
    const svg = doc.querySelector('.trend-svg') || doc.querySelector('svg[viewBox="0 0 900 200"]');
    check(!!svg, '趋势 SVG 已渲染');
  }

  console.log('E. #6 反馈闭环 · P16 块（stub 在线）');
  const fkBlock = doc.getElementById('feedback-kpi-block');
  check(!!fkBlock, '存在 #feedback-kpi-block');
  if (fkBlock) {
    const t = fkBlock.textContent;
    check(t.includes('干预动作待办（按类型分组）'), '含「按类型分组」标题（数据驱动）');
    if (ivSum && ivSum.by_trigger) {
      if (ivSum.by_trigger.FEEDBACK_LOOP) check(t.includes('反馈闭环 ' + ivSum.by_trigger.FEEDBACK_LOOP), '显示「反馈闭环 N」分组计数');
      if (ivSum.by_trigger.PARETO_ROOT) check(t.includes('Pareto 根因 ' + ivSum.by_trigger.PARETO_ROOT), '显示「Pareto 根因 N」分组计数');
    }
    const loopItems = ((ivSum && ivSum.recent) || []).filter((x) => x.trigger === 'FEEDBACK_LOOP');
    if (loopItems.length) {
      check(t.includes('反馈闭环待办（执行后回标）'), '含「反馈闭环待办」标题');
      const btns = fkBlock.querySelectorAll('.fbl-done');
      check(btns.length === Math.min(loopItems.length, 3), '「标记完成」按钮数 = min(条目,3)：' + btns.length);
      const nr = loopItems.filter((x) => x.needs_review).length;
      if (nr > 0) check(t.includes('待复核'), '低置信条目显示「待复核」标签');
      if (btns.length) {
        let threw = false;
        try { btns[0].dispatchEvent(new win.MouseEvent('click', { bubbles: true })); } catch (e) { threw = true; }
        check(!threw, '「标记完成」点击不抛异常');
        await sleep(100);
        check(btns[0].textContent === '[已标记完成]', '点击后回标「[已标记完成]」', btns[0].textContent);
      }
    }
  }

  console.log('F. 红线');
  const EMOJI = /[\u{1F300}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}]/u;
  check(!EMOJI.test(doc.documentElement.outerHTML), '渲染产物无 emoji');
  check(runtimeErrors.length === 0, '无运行时错误', runtimeErrors.join(' | '));
  const acceptCalls = fetchLog.filter((u) => /\/accept/.test(u)).length;
  check(acceptCalls === 2, 'accept 端点被调用 2 次（仅高置信）', acceptCalls);

  console.log('\n==== 汇总: ' + pass + ' 通过 / ' + fail + ' 失败 ====');
  process.exit(fail === 0 ? 0 : 1);
})();
