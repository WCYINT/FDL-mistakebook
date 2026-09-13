/**
 * Step 3 星图真实渲染验证（jsdom：加载真实报告 + 执行真实 JS + 模拟点击）。
 *
 * 验证项：
 *  A. 初始渲染：51 节点 / 43 边 / 簇框 / 图例
 *  B. 三级筛选：学科→领域→学期逐级收窄，数量与按钮标签一致
 *  C. 跨学科边：sm-e-cross 存在；筛选到单科后消失
 *  D. 节点详情：点击节点 → 弹层打开，含 Obsidian 链接与复制按钮
 *  E. Obsidian 链接目标文件存在性（解码 file 参数 → 磁盘校验）
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = require('path').resolve(__dirname, '..');
const VAULT = require('path').resolve(__dirname, '..', '..');
const html = fs.readFileSync(path.join(ROOT, 'site/report.html'), 'utf-8');
const dom = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true });
const doc = dom.window.document;
const win = dom.window;

let pass = 0;
let fail = 0;
function check(cond, label, extra) {
  if (cond) { pass += 1; console.log('  ✓ ' + label); }
  else { fail += 1; console.log('  ✗ ' + label + (extra ? '  ← ' + extra : '')); }
}
function click(el) {
  el.dispatchEvent(new win.MouseEvent('click', { bubbles: true, cancelable: true }));
}
const $ = (sel) => doc.querySelector(sel);
const $$ = (sel) => Array.from(doc.querySelectorAll(sel));

console.log('A. 初始渲染');
const svg = $('#starmap-svg');
check(!!svg, 'starmap-svg 存在');
const nodes0 = $$('.sm-node');
const edges0 = $$('.sm-e');
check(nodes0.length === 51, '节点数 = 51', '实际 ' + nodes0.length);
check(edges0.length === 43, '边数 = 43', '实际 ' + edges0.length);
check($$('.sm-cluster').length >= 4, '簇框 >= 4', '实际 ' + $$('.sm-cluster').length);
check($$('.sm-e-cross').length === 1, '跨学科边 = 1', '实际 ' + $$('.sm-e-cross').length);
check($$('.sm-e-auto').length === 13, '自动边 = 13', '实际 ' + $$('.sm-e-auto').length);
check($$('.sm-e-suggest').length === 29, '待复核边 = 29', '实际 ' + $$('.sm-e-suggest').length);
const stat0 = $('#sm-stat').textContent;
check(stat0.includes('51') && stat0.includes('43'), '统计行 = 51 知识点 · 43 关联', stat0);

console.log('B. 三级筛选');
function findBtn(boxSel, prefix) {
  return $$(boxSel + ' .sf-btn').find((b) => b.textContent.startsWith(prefix));
}
const mathBtn = findBtn('#sm-subjects', '数学');
const mathCount = parseInt(/（(\d+)）/.exec(mathBtn.textContent)[1], 10);
click(mathBtn);
const nodes1 = $$('.sm-node').length;
check(nodes1 === mathCount, '学科=数学 → ' + mathCount + ' 节点', '实际 ' + nodes1);
check($$('.sm-e-cross').length === 0, '筛到数学后跨科边隐藏');
const domBtn = findBtn('#sm-domains', '数与代数');
check(!!domBtn, '领域筛选出现「数与代数」');
const domCount = parseInt(/（(\d+)）/.exec(domBtn.textContent)[1], 10);
click(domBtn);
const nodes2 = $$('.sm-node').length;
check(nodes2 === domCount, '领域=数与代数 → ' + domCount + ' 节点', '实际 ' + nodes2);
const termBtn = findBtn('#sm-terms', '小A');
check(!!termBtn, '学期筛选出现「小A」');
if (termBtn) {
  const termCount = parseInt(/（(\d+)）/.exec(termBtn.textContent)[1], 10);
  click(termBtn);
  const nodes3 = $$('.sm-node').length;
  check(nodes3 === termCount, '级联 数学∩数与代数∩小A → ' + termCount + ' 节点（含领域约束）', '实际 ' + nodes3);
  check(termCount === 8, '数与代数 ∩ 小A = 8（12 张小A中 3 张在图形与几何、1 张在综合与实践）', '实际 ' + termCount);
}
// 复位领域 → 学期=小A 单独筛选应为 12（全部 12 张奥数卡都在数学）
click($$('#sm-domains .sf-btn')[0]);
const termBtn2 = findBtn('#sm-terms', '小A');
click(termBtn2);
const nodesXa = $$('.sm-node').length;
check(nodesXa === 12, '数学∩小A → 12 节点（与卡片目录 小A 桶一致）', '实际 ' + nodesXa);
// 复位到全部
click($$('#sm-terms .sf-btn')[0]);
click($$('#sm-subjects .sf-btn')[0]);
check($$('.sm-node').length === 51, '复位后回到 51 节点');

console.log('C. 跨学科边视觉');
const crossPath = $('.sm-e-cross');
check(!!crossPath, '跨学科边有独立 class（金色高亮）');
check(crossPath && /D99A2B|sm-e-cross/.test(crossPath.getAttribute('class') || ''),
  '跨学科边 class 正确', crossPath && crossPath.getAttribute('class'));

console.log('D. 节点详情 + Obsidian 链接');
const aNode = $$('.sm-node').find((n) => /MATH-G4/.test(n.getAttribute('data-code')));
click(aNode.querySelector('circle'));
const panel = $('#detail-panel');
check(panel.classList.contains('open'), '详情弹层已打开');
const title = $('#detail-title').textContent;
check(title.includes('知识点详情'), '标题含「知识点详情」', title);
const links = $$('#detail-links a');
check(links.length === 2, '两个操作按钮（Obsidian / 复制路径）', '实际 ' + links.length);
const obsA = links.find((a) => (a.textContent || '').includes('Obsidian'));
check(!!obsA, '存在「在 Obsidian 打开」按钮');
const href = obsA ? obsA.getAttribute('href') : '';
check(href.startsWith('obsidian://open?vault=Frank%20SWE&file='), 'URI 前缀正确', href.slice(0, 60));
const copyA = links.find((a) => (a.textContent || '').includes('复制'));
check(!!copyA, '存在「复制卡片路径」按钮');

console.log('E. Obsidian 链接全量文件存在性');
let checked = 0;
let missing = [];
$$('.sm-node').forEach((g) => {
  const det = JSON.parse(decodeURIComponent(g.getAttribute('data-detail')));
  const link = (det.links || []).find((l) => l.href);
  if (!link) { return; }
  const fileParam = decodeURIComponent(/[?&]file=([^&]+)/.exec(link.href)[1]);
  const abs = path.join(VAULT, fileParam);
  checked += 1;
  if (!fs.existsSync(abs)) { missing.push(fileParam); }
});
check(checked === 51, '51 个节点全部有 Obsidian 链接', '实际 ' + checked);
check(missing.length === 0, '链接目标文件全部存在', missing.slice(0, 3).join(' | '));

console.log('');
console.log('结果：' + pass + ' 通过 / ' + fail + ' 失败');
process.exit(fail ? 1 : 0);
