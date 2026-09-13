/**
 * 15 项修复 · 前端真实渲染验证（jsdom：加载真实报告 + 执行真实 JS + 模拟交互）。
 *
 * 覆盖：
 *  1  复习履历（日期 + 方式 + 文件链接）
 *  2  挂载提案：无匹配行不再显示 0.0 / 无"同意"；可复核行正常
 *  3  复习页标题对齐（CSS 规则存在）
 *  4  反馈闭环编号超链接（点击 → 跳转函数被调用）
 *  5  三块标题字号 17px + 卡片页链接
 *  6  数字详情全覆盖 + 详情内编号链接
 *  7  红线卡片显示判定式
 *  8  CSS 变量补齐（注释灰可计算）
 *  9  足迹横轴月份标签
 * 10  家长视图今日解锁（显示科目趋势）
 * 11  单时间戳（北京时间）
 * 12  logo 星星 → 星图学科跳转
 * 13  时间回溯以周为单位（4/3/2/1 周前 + 现在）
 * 14  驯服历程问题编号链接 + 0% 说明
 * 15  能量详情问题编号链接
 *
 * 用法：node verify_15fix_render.js [report_path]
 */
const fs = require('fs');
const path = require('path');
const { JSDOM, VirtualConsole } = require('jsdom');

const ROOT = require('path').resolve(__dirname, '..');
const reportPath = process.argv[2] || path.join(ROOT, 'site/report.html');
const html = fs.readFileSync(reportPath, 'utf-8');

let pass = 0, fail = 0;
const runtimeErrors = [];
function check(cond, label, extra) {
  if (cond) { pass += 1; console.log('  OK  ' + label); }
  else { fail += 1; console.log('  FAIL  ' + label + (extra ? '  <- ' + String(extra).slice(0, 220) : '')); }
}

const vc = new VirtualConsole();
vc.on('warn', (m) => { if (String(m).includes('[FDL] runtime')) runtimeErrors.push(String(m)); });
vc.on('jsdomError', (e) => {
  // jsdom 未实现的浏览器 API（scrollTo/scrollIntoView 等）不算应用缺陷
  if (e && String(e.message).includes('Not implemented')) { return; }
  runtimeErrors.push('jsdomError: ' + (e && e.message));
});

// 提案桩：2 条有目标 KP（可复核）+ 1 条无目标（无法匹配）
const PROPOSALS = [
  { id: 901, mistake_id: 77001, proposed_kp_code: 'MATH-G4-CALC-01', proposed_kp_id: 999, confidence: 0.92 },
  { id: 902, mistake_id: 77002, proposed_kp_code: 'MATH-G4-READ-02', proposed_kp_id: 998, confidence: 0.71 },
  { id: 903, mistake_id: 77003, proposed_kp_code: null, proposed_kp_id: null, confidence: 0.0,
    rationale: '现有清单中无对应知识点，需新增' },
];
function makeFetch() {
  return function (url) {
    const u = String(url);
    const ok = (data) => Promise.resolve({ ok: true, json: () => Promise.resolve(data) });
    if (u.includes('/api/kp/proposals?')) { return ok({ items: PROPOSALS }); }
    if (/\/api\/kp\/proposals\/\d+\/accept/.test(u)) {
      const id = parseInt(u.match(/proposals\/(\d+)\/accept/)[1], 10);
      return ok({ ok: true, already: id === 902, mistake_id: 77000 + id, kp_id: 999 });
    }
    if (u.includes('/api/intervention/done')) { return ok({ ok: true }); }
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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async function main() {
  await sleep(200);

  console.log('1. 复习履历（日期 + 方式 + 文件链接）');
  const allCards = Array.from(doc.querySelectorAll('[data-mcard]'));
  let mcard = null, det = null;
  for (const c of allCards) {
    const d = Array.from(c.querySelectorAll('details')).find(
      (x) => x.querySelector('summary') && x.querySelector('summary').textContent.includes('复习履历'));
    if (d) { mcard = c; det = d; break; }
  }
  check(!!det, '存在带「复习履历」的卡片（' + allCards.length + ' 卡中）');
  if (det) {
    const t = det.textContent;
    check(/复习反馈|口述录音/.test(t), '履历含方式（复习反馈/口述录音）');
    check(/\d\d-\d\d/.test(t), '履历含日期（MM-DD）', t.slice(0, 80));
    // 文件链接可能在另一张卡（口述录音条目）——全卡扫描
    const anyFileLink = allCards.reduce(
      (acc, c) => acc || c.querySelector('details a[href^="file://"]'), null);
    check(!!anyFileLink, '口述录音条目含文件链接（file://）',
      anyFileLink && anyFileLink.getAttribute('href'));
  }

  console.log('2. 挂载提案 0.0 修复（stub：2 可复核 + 1 无法匹配）');
  const kpBlock = doc.getElementById('kp-proposals-block');
  check(!!kpBlock, '存在 #kp-proposals-block');
  if (kpBlock) {
    const rows = kpBlock.querySelectorAll('[data-proposal-id]');
    check(rows.length === 3, '渲染 3 行（实际 ' + rows.length + '）');
    const row903 = kpBlock.querySelector('[data-proposal-id="903"]');
    check(row903 && row903.textContent.includes('无法匹配'), '无目标行显示「无法匹配」');
    check(row903 && !/0\.0/.test(row903.textContent), '无目标行不再显示 0.0', row903 && row903.textContent.slice(0, 80));
    const accBtns903 = row903 ? Array.from(row903.querySelectorAll('button')).map((b) => b.textContent) : [];
    check(accBtns903.length === 1 && accBtns903[0].includes('驳回'), '无目标行只有「驳回（忽略）」按钮', accBtns903.join('/'));
    const cd = doc.getElementById('kpp-count-detail');
    check(cd && cd.textContent.includes('可复核 2') && cd.textContent.includes('无法匹配 1'),
      '计数细化：可复核 2 · 无法匹配 1', cd && cd.textContent);
  }

  console.log('3. 复习页标题对齐（CSS 规则）');
  const cssText = Array.from(doc.querySelectorAll('style')).map((s) => s.textContent).join('\n');
  check(cssText.includes('#review-list > h3, #review-list > h4'), '存在标题跨列规则（grid-column: 1 / -1）');

  console.log('4/5. 反馈闭环编号链接 + 三块标题字号');
  const fk = doc.getElementById('feedback-kpi-block');
  check(!!fk, '存在 #feedback-kpi-block');
  if (fk) {
    const h3 = fk.querySelector('h3');
    check(h3 && h3.getAttribute('style').includes('17px'), '反馈层闭环标题 17px');
    const link = fk.querySelector('a.fd-mid-link');
    check(!!link, '待办行内编号为超链接');
    if (link) {
      const calls = [];
      const orig = win.__fdJumpToMistake;
      win.__fdJumpToMistake = function (m) { calls.push(m); };
      link.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
      win.__fdJumpToMistake = orig;
      check(calls.length === 1, '点击编号触发跳转函数（mid=' + calls[0] + '）');
    }
  }
  const pareto = doc.getElementById('pareto-intervention-block');
  if (pareto) {
    check(pareto.querySelector('h3').getAttribute('style').includes('17px'), '干预动作标题 17px');
    check(!!pareto.querySelector('a.fd-mid-link'), '干预动作含看错题卡链接');
  } else { check(false, '存在 #pareto-intervention-block'); }
  const diag = doc.getElementById('diagnosis-block');
  if (diag) {
    check(diag.querySelector('h3').getAttribute('style').includes('17px'), '错因诊断标题 17px');
    check(!!diag.querySelector('a.fd-mid-link'), '错因诊断含看错题卡链接');
  } else { check(false, '存在 #diagnosis-block'); }

  console.log('6. 数字放大 + 详情全覆盖 + 详情内编号链接');
  check(/\.card \.v \{ font-size: 34px/.test(cssText), '.card .v 字号 34px');
  const cockCards = doc.querySelectorAll('#cockpit-cards .card');
  const withDetail = Array.from(cockCards).filter((c) => c.hasAttribute('data-detail'));
  check(withDetail.length === cockCards.length && cockCards.length >= 12,
    '驾驶舱卡片全部带详情（' + withDetail.length + '/' + cockCards.length + '）');
  const qCard = Array.from(cockCards).find((c) => c.textContent.includes('复习队列积压'));
  if (qCard) {
    try {
      qCard.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
      const panelOpen = doc.getElementById('detail-panel').classList.contains('open');
      check(panelOpen, '点击数字卡打开详情面板');
      const dlinks = doc.getElementById('detail-links');
      check(dlinks && dlinks.textContent.includes('#770'),
        '详情面板含问题编号链接', dlinks && dlinks.textContent.slice(0, 80));
      doc.getElementById('detail-close').click();
    } catch (e) { check(false, '详情交互无异常', e.message); }
  }

  console.log('7. 红线判定式');
  const rlBox = doc.getElementById('redlines');
  check(rlBox && rlBox.textContent.includes('判定：'), '红线卡片显示「判定：」式');
  check(rlBox && !rlBox.textContent.includes('SIR 100%'), '红线不再误报 SIR 100% 健康信号',
    rlBox && rlBox.textContent.slice(0, 120));

  console.log('8. CSS 变量补齐（注释灰）');
  check(cssText.includes('--text-secondary: #7A6F5D'), ':root 定义 --text-secondary（灰）');
  check(cssText.includes('--text-tertiary: #9C9184'), ':root 定义 --text-tertiary');

  console.log('9. 足迹横轴月份标签');
  const trail = doc.getElementById('trail-heat');
  check(trail && /月/.test(trail.textContent), '足迹 SVG 含月份标签', trail && trail.textContent.slice(0, 60));

  console.log('10. 家长视图今日解锁');
  const pb = doc.getElementById('parent-body');
  check(pb && pb.textContent.includes('科目级掌握趋势'), '家长视图显示「科目级掌握趋势」');
  check(pb && !pb.textContent.includes('屏蔽中'), '不再显示「屏蔽中」冻结卡');
  check(pb && pb.textContent.includes('2026-09-06'), '数据截止日标注保留');

  console.log('11. 单时间戳');
  const meta = doc.querySelector('header.hero .meta');
  check(meta && !meta.textContent.includes('UTC'), '无 UTC 冗余时间戳', meta && meta.textContent.slice(0, 90));
  check(meta && meta.textContent.includes('北京时间'), '标注「北京时间」');

  console.log('12. logo 星星 → 星图学科');
  const stars = doc.querySelectorAll('.logo-star');
  check(stars.length === 5, '5 颗可点星星（实际 ' + stars.length + '）');
  const subjects = Array.from(stars).map((s) => s.getAttribute('data-subject'));
  check(subjects.join(',') === 'MATH,CHINESE,ENGLISH,SCIENCE,ALL', '星星映射到 4 学科 + 全部', subjects.join(','));
  if (typeof win.__fdOpenStarmap === 'function') {
    stars[0].dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await sleep(120);
    const smTab = doc.querySelector('nav.tabs button[data-panel="starmap"]');
    check(smTab && smTab.getAttribute('aria-selected') === 'true', '点击星星切到学习星图 tab');
    const onBtn = doc.querySelector('#sm-subjects button.on');
    check(onBtn && onBtn.textContent.includes('数学'), '星图选中「数学」学科', onBtn && onBtn.textContent);
  } else { check(false, '存在 __fdOpenStarmap'); }

  console.log('13. 时间回溯以周为单位');
  const slider = doc.getElementById('time-slider');
  check(slider && slider.getAttribute('max') === '4', '滑块 5 档（max=4）');
  check(doc.getElementById('time-label').textContent === '现在', '默认标签「现在」');
  slider.value = '3';
  slider.dispatchEvent(new win.Event('input', { bubbles: true }));
  check(doc.getElementById('time-label').textContent === '1 周前', '回溯到「1 周前」',
    doc.getElementById('time-label').textContent);

  console.log('14. 驯服历程编号链接 + 0% 说明');
  const monCards = doc.querySelectorAll('#monster-cards .card');
  check(monCards.length > 0, '图鉴卡片渲染（' + monCards.length + '）');
  const monWithIds = Array.from(monCards).find((c) => c.textContent.includes('问题编号'));
  check(!!monWithIds, '驯服历程含「问题编号」');
  if (monWithIds) {
    check(!!monWithIds.querySelector('a.fd-mid-link'), '编号为超链接');
  }
  const zeroCard = Array.from(monCards).find((c) => /尚未驯服/.test(c.textContent));
  check(!!zeroCard, '0% 卡片显示「尚未驯服（门槛…）」说明');

  console.log('15. 能量详情编号链接');
  const enCards = doc.querySelectorAll('#energy-list .card');
  const enCard = Array.from(enCards).find((c) => c.hasAttribute('data-detail'));
  if (enCard) {
    const d = JSON.parse(decodeURIComponent(enCard.getAttribute('data-detail')));
    check(Array.isArray(d.links) && d.links.length > 0, '能量详情载荷含编号链接', JSON.stringify(d.links || []).slice(0, 80));
    enCard.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    await sleep(60);
    const dlinks = doc.getElementById('detail-links');
    check(dlinks && dlinks.textContent.includes('#770'), '详情面板渲染编号链接');
    doc.getElementById('detail-close').click();
  } else { check(false, '能量卡片带详情'); }

  console.log('红线 · 无运行时错误 / 无 emoji');
  const EMOJI = /[\u{1F300}-\u{1F9FF}\u{2600}-\u{26FF}\u{2700}-\u{27BF}]/u;
  check(!EMOJI.test(doc.documentElement.outerHTML), '渲染产物无 emoji');
  check(runtimeErrors.length === 0, '无运行时错误', runtimeErrors.join(' | '));

  console.log('\n==== 汇总: ' + pass + ' 通过 / ' + fail + ' 失败 ====');
  process.exit(fail === 0 ? 0 : 1);
})();
