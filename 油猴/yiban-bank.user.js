// ==UserScript==
// @name         易班考试 · 题库桥接版
// @namespace    local.yiban.bank
// @version      0.1.0
// @description  把页面上渲染出来的题目发给本地题库服务取答案；题库里没有的随机选。考完可把整页采集入库。
// @author       local
// @match        *://*.yiban.cn/*
// @match        *://*.yooc.me/*
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// @connect      localhost
// @run-at       document-idle
// ==/UserScript==

/*
 * 架构说明
 * ---------
 * 浏览器里读不到本地 SQLite，所以由 bank.py 提供一个只监听本机的桥接服务：
 *
 *     python bank.py serve            # 默认 http://127.0.0.1:8765
 *
 * 本脚本把「题干 + 当前渲染出来的选项（按显示顺序）」发给 /ask，
 * 服务端按选项原文反查，返回**对应这个顺序**的字母。所以平台打乱选项也不会答错。
 * 题库里没有的题，本脚本随机选一个。
 *
 * 采集入库：交卷后（或查看试卷页）页面会显示「正确答案：X」，
 * 此时点「采集入库」，脚本把整页题目拼成文本发给 /learn 入库。
 *
 * ⚠️ 下面三个 adopt* 函数是「适配层」。平台的 DOM 结构我没见过，
 *    这里用的是通用启发式规则，很可能需要按实际页面调一次。
 *    点面板上的「诊断」会把脚本识别到的东西全打印出来，照着调就行。
 */

(function () {
  'use strict';

  const SERVER = 'http://127.0.0.1:8765';
  const ASK_MIN = 0.85;   // 查询阈值，和服务端默认一致
  const VERBOSE = true;   // 控制台打日志

  const log = (...a) => VERBOSE && console.log('[易班题库]', ...a);

  // ============================================================
  // 一、和服务端通信
  // ============================================================

  function call(path, payload) {
    const url = SERVER + path;
    const body = JSON.stringify(payload, null, 0);
    // 优先用 GM_xmlhttpRequest：绕开跨域和混合内容限制
    if (typeof GM_xmlhttpRequest === 'function') {
      return new Promise((resolve, reject) => {
        GM_xmlhttpRequest({
          method: 'POST',
          url,
          headers: { 'Content-Type': 'application/json' },
          data: body,
          timeout: 8000,
          onload: (r) => {
            try { resolve(JSON.parse(r.responseText)); }
            catch (e) { reject(new Error('返回不是 JSON: ' + r.responseText.slice(0, 120))); }
          },
          onerror: () => reject(new Error('连不上本地服务，确认 python bank.py serve 在跑')),
          ontimeout: () => reject(new Error('本地服务超时')),
        });
      });
    }
    // 退回 fetch（需要服务端的 CORS 头，服务端已经发了）
    return fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    }).then((r) => r.json());
  }

  function callGet(path) {
    const url = SERVER + path;
    if (typeof GM_xmlhttpRequest === 'function') {
      return new Promise((resolve, reject) => {
        GM_xmlhttpRequest({
          method: 'GET',
          url,
          timeout: 5000,
          onload: (r) => {
            try { resolve(JSON.parse(r.responseText)); }
            catch (e) { reject(new Error('返回不是 JSON')); }
          },
          onerror: () => reject(new Error('连不上本地服务')),
          ontimeout: () => reject(new Error('超时')),
        });
      });
    }
    return fetch(url).then((r) => r.json());
  }

  const askServer = (text) => call('/ask', { text, min: ASK_MIN });

  /**
   * 一次把整份卷子问完。
   *
   * 为什么必须批量：本地服务虽然快（一次往返约 20ms），但**每次请求的 await 都会让
   * 浏览器有机会重绘**——100 道题就是 100 次重绘机会，于是你能看见翻页。
   * 采集入库之所以快，正是因为它全程不发请求。合并成一次请求后，答题这边
   * 也就没有中间等待了（实测 3 次单条 69ms → 1 次批量 10ms）。
   */
  async function askServerBatch(texts) {
    const r = await call('/ask-batch', { items: texts, min: ASK_MIN });
    return r.results || [];
  }
  const learnServer = (text, source) => call('/learn', { text, source: source || 'script' });

  // ============================================================
  // 二、适配层：从页面读出题目 / 点击选项 / 读正确答案
  // ============================================================

  const OPT_RE = /^([A-H])\s*[.、．)）:：]\s*(\S[\s\S]*)$/;   // "A. xxx"

  // 和服务端 clean_stem 对应的客户端清洗。服务端也有兜底，这边先清一遍，
  // 好处是发出去的文本就是干净的、诊断输出也可读。
  const CHROME_RE = /(?:查看试卷|查看答案|查看解析|随机选择|返回列表|上一题|下一题|答题卡|提交试卷|开始考试|考试时长|剩余时间|我的试卷|错题回顾|冲！|冲!)\s*/g;
  const SECTION_RE = /(?:^|\s)[一二三四五六七八九十]+\s*[、.．]\s*(?:单选题|多选题|判断题|是非题|填空题|简答题|问答题|选择题)\s*/g;
  // 进度条斜杠两侧必须有空格——题面里的分数是 1/3 这种紧挨着的，不能吃
  const PROGRESS_RE = /(?:\d+\s+\/\s+\d+|考试倒计时|剩余\s*\d+[:：]\d+)\s*/g;
  const LEADNO_RE = /^\s*(?:第\s*)?\d{1,4}\s*[、.．)）]\s*(?:[\[（(]\s*\d+\s*分\s*[\]）)])?\s*/;
  const SCORE_RE = /^[\[（(]\s*\d+\s*分\s*[\]）)]\s*/;

  function jsCleanStem(text) {
    const t = (text || '')
      .replace(CHROME_RE, '')
      .replace(SECTION_RE, ' ')
      .replace(PROGRESS_RE, ' ');
    return t.split('\n').map((ln) => {
      let s = ln.trim();
      for (let i = 0; i < 3; i++) {
        const n = s.replace(SCORE_RE, '').replace(LEADNO_RE, '').trim();
        if (n === s) break;
        s = n;
      }
      return s;
    }).join('\n').trim();
  }

  /**
   * 取一个容器里「除了选项和按钮之外的实质文字」——也就是题干。
   *
   * 三条排除都是照真实 DOM 定的（2026-09-23 拿到的 body）：
   *
   * 1. **按钮必须删掉**：另一个油猴脚本注入的 `<button>冲！</button>`、
   *    `<button>随机选择</button>` 就挂在题目容器外层，留着就会被吃进题干；
   * 2. **选项文本要减掉**：选项是 `<li><div class="icon"><svg/></div>
   *    <div class="flex-auto">A.75</div></li>`，题干在同一个容器里；
   * 3. **`正确答案：B` 块要减掉**：它是判分信息，不是题干
   *    （`<div class="rs pa-s isCorrect"><div>正确答案：B</div></div>`）。
   *
   * 最后过一遍 jsCleanStem 剥题号、分值、题型分节、进度条。
   */
  function extraText(container, optionEls) {
    const clone = container.cloneNode(true);
    clone
      .querySelectorAll('button,script,style,noscript,svg,input,#yqbank-panel')
      .forEach((n) => n.remove());
    let t = clone.textContent || '';
    optionEls.forEach((o) => {
      const s = (o.textContent || '').trim();
      if (s) t = t.split(s).join('');
    });
    t = t.replace(/(?:正确答案|参考答案|正确选项)\s*[：:]\s*[A-H][A-H\s、，,]*/g, '');
    return jsCleanStem(t);
  }

  /** 取出所有「像选项」的叶子元素 */
  function adoptOptionElements() {
    const found = [];
    document.querySelectorAll('body *').forEach((el) => {
      if (el.children.length) return;                    // 只看叶子，避免把整块当选项
      if (el.closest('#yqbank-panel')) return;            // 别把自己的面板算进去
      const t = (el.textContent || '').trim();
      if (!t || t.length > 300) return;
      if (OPT_RE.test(t)) found.push(el);
    });
    return found;
  }

  /**
   * 选项当前是否已被选中。
   *
   * 我原先断言「这页的选中态读不出来」——**那是错的**。当时只看了 class 和 radio，
   * 没看图标的 svg：**选中时 svg 里有 2 个 <path>（外圈 + 对勾），未选中只有 1 个**。
   * 这是从参考脚本里学到的（它用 `li.children[0].children[0].childElementCount === 2`）。
   *
   * 这个判断很关键：有了它，多选题才能「只在状态不对时才点一下」，
   * 而不是靠猜点几次——多选的选项是开关，瞎点会把刚选上的取消掉。
   */
  function isChosen(optionEl) {
    const box = optionEl.closest('li,label,div,td,tr') || optionEl;
    // 主判据：图标里的 svg 路径数
    const icon = box.children && box.children[0];
    const svg = icon && icon.children && icon.children[0];
    if (svg && String(svg.tagName).toLowerCase() === 'svg') {
      return svg.childElementCount === 2;
    }
    // 兜底：原生 input / class 标记
    const input = box.querySelector('input');
    if (input) return !!input.checked;
    return /(active|selected|checked|choice|choose|picked)/i.test(box.className || '');
  }

  /** 真正点一下。点最内层的选项文字：click 会冒泡到 li，处理函数挂哪一层都能触发。 */
  function clickOption(optionEl) {
    const box = optionEl.closest('li,label,div,td,tr') || optionEl;
    const input = box.querySelector('input[type=radio],input[type=checkbox]');
    if (input) {
      if (!input.checked) input.click();
      return 'input';
    }
    optionEl.click();
    return 'text';
  }

  /**
   * 把选项设成「选中 / 未选中」——**只在当前状态和目标不一致时才点**。
   *
   * 抄自参考脚本的多选处理：它不只点该选的，还会点掉不该选的。
   * 这样重复运行也安全（已答过的不会被翻反），并且能把「随机选择」留下的痕迹清干净。
   */
  async function setOption(optionEl, want) {
    if (isChosen(optionEl) === want) return false;   // 已经是要的状态，别动它
    clickOption(optionEl);
    if (!fastMode) await sleep(90);                  // 让 React 状态更新跟上
    return true;
  }

  /** 从 n 个里随机取 k 个下标（多选猜答案用） */
  function randomSubset(n) {
    const idxs = [...Array(n).keys()];
    // 多选通常选 2~n 个。只选一个在多选里几乎必错，全选也常常不对，取 2..n 随机。
    const k = n <= 2 ? n : 2 + Math.floor(Math.random() * (n - 1));
    for (let i = idxs.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [idxs[i], idxs[j]] = [idxs[j], idxs[i]];
    }
    return idxs.slice(0, k).sort((a, b) => a - b);
  }

  /**
   * 判断当前这道题是不是多选。
   *
   * 页面上有题型分节标题（`<h2>二、多选题</h2>`）。取文档顺序上离题目容器最近的
   * 那个标题来判断，比猜 class 稳。
   */
  function isMultiQuestion(container) {
    const heads = [...document.querySelectorAll('h1,h2,h3')]
      .filter((h) => /单选|多选|是非|判断/.test(h.textContent || ''));
    let kind = '';
    for (const h of heads) {
      if (h.compareDocumentPosition(container) & Node.DOCUMENT_POSITION_FOLLOWING) {
        kind = h.textContent || '';
      }
    }
    return /多选/.test(kind);
  }

  /** 读某道题容器里显示的正确答案字母；读不到返回 null */
  function adoptRevealedAnswer(container) {
    const text = container.innerText || container.textContent || '';
    const m = text.match(/(?:正确答案|参考答案|正确选项)\s*[：:]\s*([A-H][A-H\s、，,]*)/);
    if (m) return m[1].replace(/[^A-H]/g, '');
    // 有些页面给正确项加 class。CSS 属性选择器默认**区分大小写**，
    // 而真实类名是 isCorrect（大写 C），所以 correct/Correct 两种都要列——
    // 只写 [class*=correct] 匹配不到（实测 DOM 确认）。
    const marks = container.querySelectorAll(
      '.correct,.right,.dui,.is-correct,.isCorrect,'
      + '[class*=correct],[class*=Correct],[class*=right],[class*=Right]'
    );
    const letters = [];
    marks.forEach((el) => {
      const t = (el.innerText || el.textContent || '').trim();
      const mm = t.match(OPT_RE);
      if (mm) letters.push(mm[1]);
    });
    return letters.length ? [...new Set(letters)].sort().join('') : null;
  }

  /**
   * 把页面上的题目识别成 [{ stem, options:[{letter,text,el}], container }]
   * 做法：把「像选项」的元素按最近的共同祖先分组，一组就是一道题。
   */
  function adoptQuestions() {
    const opts = adoptOptionElements();
    if (!opts.length) return [];

    // 每个选项元素往上找 9 层，记录祖先链
    const chains = opts.map((el) => {
      const chain = [];
      let p = el.parentElement;
      for (let i = 0; p && i < 9; i++, p = p.parentElement) chain.push(p);
      return { el, chain };
    });

    const groups = [];
    const used = new Set();
    chains.forEach(({ el, chain }) => {
      if (used.has(el)) return;
      // 找「装这一组选项的**最靠下**的祖先」——即从选项往上第一个包含 2~8 个选项的元素。
      //
      // 这里原本写的是「取最靠上的祖先（best 一路覆盖）」，那是错的：实测往上能一路
      // 取到整页外层（div.jsx-372353390），把顶栏的「查看试卷」和两个注入按钮
      // （<button>冲！</button>、<button>随机选择</button>）全吃进题干——
      // 这就是 20/20 条采集结果被污染的根因。
      let picked = null;
      for (const anc of chain) {
        const inside = opts.filter((o) => anc.contains(o));
        if (inside.length > 8) break;                       // 越往上越多，说明已经越过题目边界
        if (inside.length >= 2) { picked = { anc, inside }; break; }
      }
      if (!picked) return;
      if (picked.inside.some((o) => used.has(o))) return;
      picked.inside.forEach((o) => used.add(o));
      groups.push(picked);
    });

    return groups.map(({ anc: optBox, inside }) => {
      const options = inside.map((el, i) => {
        const t = (el.textContent || '').trim();
        const m = t.match(OPT_RE);
        return {
          letter: m ? m[1] : String.fromCharCode(65 + i), // 页面没字母就按顺序补
          text: m ? m[2].trim() : t,
          el,
        };
      });
      // 选项容器本身只有选项，题干在更外层。往上找到第一个「除了选项/按钮之外
      // 还有实质文字」的祖先，那就是题目容器。
      let container = optBox;
      let p = optBox.parentElement;
      for (let i = 0; p && i < 6; i++, p = p.parentElement) {
        if (extraText(p, inside).length >= 6) { container = p; break; }
      }
      return { stem: extraText(container, inside), options, container };
    }).filter((q) => q.stem && q.options.length >= 2);
  }

  /** 拼成发给服务端的文本：题干 + 按显示顺序标号的选项 */
  function toServerText(q) {
    const lines = [q.stem];
    q.options.forEach((o, i) => {
      lines.push(`${String.fromCharCode(65 + i)}. ${o.text}`);
    });
    return lines.join('\n');
  }

  // ============================================================
  // 三、自动答题
  // ============================================================

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  /** 按文字找按钮。页面上是 上一题 / 下一题 / 提交试卷 */
  function findButton(label) {
    return [...document.querySelectorAll('button')]
      .find((b) => (b.textContent || '').trim() === label) || null;
  }

  /** 读进度 "3 / 100"。用整段文本精确匹配，避免误抓题干里的「1/3」这类分数。 */
  function readProgress() {
    for (const el of document.querySelectorAll('span,div,p,li')) {
      const m = (el.textContent || '').trim().match(/^(\d+)\s*\/\s*(\d+)$/);
      if (m) return [parseInt(m[1], 10), parseInt(m[2], 10)];
    }
    return null;
  }

  /** 轮询等一个条件成立 */
  async function waitUntil(fn, timeout = 5000, step = 100) {
    const t0 = Date.now();
    while (Date.now() - t0 < timeout) {
      if (fn()) return true;
      await sleep(step);
    }
    return false;
  }

  let running = false;
  let lastStop = '';   // 上一轮遍历为什么停下，收尾时一起报出来
  // 快速模式：去掉所有等待。参考脚本「一秒答完」的全部秘密就是这个——
  // 它主循环里没有任何 await/setTimeout，浏览器来不及重绘，所以看不见翻页过程。
  //
  // 更正一处错误推理：我原先写「快会被风控弹验证码」是错的。实测（用户确认）
  // 人机验证是**答题前的准入门槛**，跟答题速度无关。所以快慢不改变会不会遇到验证码。
  // 至于速度是否会影响其它风控判断，我没有证据，就不下结论。
  let fastMode = false;

  /**
   * 依次走过试卷的每一页，对每道识别到的题调用 onQuestion(q)。
   *
   * **自动答题和采集入库共用这一份遍历**——两边各写一遍必然有一边漏翻页。
   * 踩过：采集入库原来只看当前页，而试卷页一次只显示一道，所以点一次只入库一道，
   * 而入库整份卷子恰恰是「蒙对了的题也能拿到正确答案」的唯一途径。
   *
   * 停止条件（任一满足）：页面没题、下一题按钮消失或禁用、进度已到末尾、翻页后题干没变。
   * **不自动交卷**。
   */
  async function forEachQuestion(onQuestion) {
    let count = 0;
    lastStop = '';
    await rewindToFirst();     // 先退回第一题，否则从半路开跑只答剩下的几道
    for (let guard = 0; guard < 300; guard++) {
      const qs = adoptQuestions();
      if (!qs.length) {
        lastStop = count ? `后面没题了` : '没识别到题目';
        break;
      }
      const stemBefore = qs[0].stem;
      for (const q of qs) {
        await onQuestion(q, count);
        count++;
      }
      const next = findButton('下一题');
      const progBefore = readProgress();
      if (!next || next.disabled) { lastStop = '已到最后一题'; break; }
      if (progBefore && progBefore[0] >= progBefore[1]) {
        lastStop = `进度到 ${progBefore[1]} 为止`;
        break;
      }
      // 翻页是否完成的判据：**优先看进度计数器**（读一个数字，便宜），
      // 读不到才退回「整页扫 DOM 比题干」（贵得多）。
      const turned = () => {
        const pr = readProgress();
        if (pr && progBefore && pr[0] !== progBefore[0]) return true;
        const q2 = adoptQuestions();
        return q2.length > 0 && q2[0].stem !== stemBefore;
      };
      next.click();
      if (fastMode) {
        // 快速模式：只做一次同步检查；没过就**每 20ms** 探一次。
        // 原来是 100ms 一探——只要 React 的更新晚一帧，就白等整整 100ms，
        // 100 道题累积成十几秒（实测每题 125~148ms，就是这个数）。
        if (!turned() && !await waitUntil(turned, 2000, 20)) {
          lastStop = '翻页没反应';
          break;
        }
      } else {
        if (!await waitUntil(turned, 5000, 100)) { lastStop = '翻页没反应'; break; }
        await sleep(120);
      }
    }
    return count;
  }

  /**
   * 回到第一题。
   *
   * 参考脚本每次开跑都先点「上一题」退回开头，否则从半路点「自动答题」
   * 只会答剩下的几道——而「答完整份卷子」才是这个按钮的意思。
   */
  async function rewindToFirst() {
    const prog = readProgress();
    if (!prog || prog[0] <= 1) return;
    // 每次都重新找按钮：React 重渲染可能换掉 DOM 节点，缓存下来的会变成游离节点、
    // 点它什么也不会发生（每次多一次 DOM 扫描，可忽略）。
    for (let i = 0; i < prog[0] - 1 && i < 300; i++) {
      const back = findButton('上一题');
      if (!back || back.disabled) break;
      back.click();
      if (!fastMode) await sleep(60);
    }
    if (!fastMode) await sleep(200);
    log(`已退回第一题（原来在第 ${prog[0]} 题）`);
  }

  /**
   * 自动答题：答完当前题后**自动点「下一题」**，一路答到底。
   *
   * 这页一次只显示一道题（底下是 上一题 / 3 / 100 / 下一题），
   * 所以早先「把本页识别到的题全答一遍」只能答一道——页面上本来就只有一道。
   *
   * 停止条件有三个（任一满足就停）：下一题按钮消失或禁用、进度已到最后、翻页后题干没变。
   * **不自动交卷**——交卷留给人来点。
   */
  /**
   * 快速模式的答题：**两遍走**。
   *
   *   第一遍 只读不点：逐页收集「题干 + 选项」，期间不发任何请求
   *   一次批量问本地库，拿到全部答案
   *   回到第一题
   *   第二遍 逐页点选：答案已在手，循环里没有任何网络等待
   *
   * 两遍内部都不做等待，等待只在「翻页后题干没变」的兜底检查里出现，
   * 所以浏览器来不及在题目之间重绘——和参考脚本一个原理。
   */
  async function autoAnswerFast() {
    const items = [];
    const stems = [];
    await forEachQuestion(async (q) => { items.push(toServerText(q)); stems.push(q.stem); });
    if (!items.length) { toast('没识别到题目——点「诊断」看看'); return; }

    let answers = [];
    try {
      answers = await askServerBatch(items);
    } catch (e) {
      toast('连不上本地服务：' + e.message);
      return;
    }
    log(`批量问了 ${items.length} 题，拿到 ${answers.length} 个结果`);

    // 用题干做键去对应答案，而**不是按下标**：万一两遍的遍历因某个停止条件不一致，
    // 下标就会整体错位、把答案点到别的题上；按题干对就不会。
    const byStem = new Map();
    stems.forEach((st, k) => byStem.set(st, answers[k] || {}));

    await rewindToFirst();

    let hit = 0, noEntry = 0, mismatch = 0, clicks = 0;
    await forEachQuestion(async (q) => {
      // 取不到就退回空对象——不能直接访问 undefined.found（会抛错把整轮打断）
      const a = byStem.get(q.stem) || {};
      const letter = (a.found && a.best && a.best.letter) ? a.best.letter : null;
      // 两种「没答案」要分开报：库里没这题 = 需要采集；
      // 库里有但选项对不上 = 题库数据有问题（以前这种完全没痕迹，查不到原因）
      if (!letter) {
        if (!a.found) {
          noEntry++;
        } else {
          mismatch++;
          log('库里有但选项对不上：', q.stem.slice(0, 40),
              '｜库里答案原文=', (a.candidates && a.candidates[0] && a.candidates[0].texts) || a.best,
              '｜本页选项=', q.options.map((o) => o.text));
        }
      }
      const multiQ = isMultiQuestion(q.container);
      let idxs;
      if (letter) {
        idxs = [...new Set(letter)].map((c) => c.charCodeAt(0) - 65);
      } else if (multiQ) {
        idxs = randomSubset(q.options.length);
      } else {
        idxs = [Math.floor(Math.random() * q.options.length)];
      }
      // 多选题遍历所有选项（该选的选上、不该选的取消）；单选只动目标那一个。
      // 这里直接读 isChosen 判断、不做等待：选项之间互不影响，不需要等 React 刷新。
      const want = new Set(idxs);
      const targets = multiQ ? q.options.map((_, k) => k) : idxs;
      for (const k of targets) {
        const t = q.options[k];
        if (!t) continue;
        if (isChosen(t.el) !== want.has(k)) { clickOption(t.el); clicks++; }
      }
      if (letter) hit++;
    });

    toast(`快速模式：答 ${items.length} 题，命中 ${hit}`
      + `，库里没这题 ${noEntry}`
      + (mismatch ? `，库里有但选项对不上 ${mismatch}（明细看控制台）` : '')
      + `，点击 ${clicks} 次（${lastStop}）`);
  }

  async function autoAnswer() {
    if (running) { toast('正在跑，别重复点'); return; }
    running = true;
    if (fastMode) {
      // 快速模式走两遍批量路径（快、看不见翻页）；慢速模式走下面逐题问的老路（慢、像人在点）
      try { await autoAnswerFast(); }
      catch (e) { toast('出错了：' + e.message); }
      finally { running = false; }
      return;
    }
    let n = 0, hit = 0, miss = 0, clicks = 0;
    const t0 = Date.now();
    try {
      await forEachQuestion(async (q) => {
        let letter = null, note = '';
        try {
          const res = await askServer(toServerText(q));
          if (res.found && res.best && res.best.letter) {
            letter = res.best.letter;
            note = `${res.best.score}/${res.best.kind}`;
          } else if (res.found) {
            note = '库里选项对不上';
            log('库里有但选项对不上：', q.stem.slice(0, 40),
                '｜库里答案原文=', (res.candidates && res.candidates[0] && res.candidates[0].texts) || res.best,
                '｜本页选项=', q.options.map((o) => o.text));
          } else {
            note = '题库无此题';
          }
        } catch (e) {
          toast('连不上本地服务，已停下：' + e.message);
          throw e;                       // 抛出去让遍历终止
        }

        const multiQ = isMultiQuestion(q.container);
        let idxs;
        if (letter) {
          idxs = [...new Set(letter)].map((c) => c.charCodeAt(0) - 65);  // 服务端给的位置对应「发过去的顺序」
        } else if (multiQ) {
          // 多选且库里没有：随机选 2..n 个。只选一个在多选里几乎必错（用户反馈过「只选了一个」）
          idxs = randomSubset(q.options.length);
        } else {
          idxs = [Math.floor(Math.random() * q.options.length)];
        }
        // 多选题遍历**所有**选项：该选的选上、不该选的取消掉（抄参考脚本的做法）。
        // 这样重复运行也安全（已答过的不会被翻反），也能把「随机选择」留下的痕迹清干净。
        // 单选只动目标那一个——radio 语义下点了新的就自动取消旧的。
        const want = new Set(idxs);
        const targets = multiQ ? q.options.map((_, i) => i) : idxs;
        for (const i of targets) {
          const target = q.options[i];
          if (!target) continue;
          if (await setOption(target.el, want.has(i))) clicks++;   // 状态已对就不点，返回 false
        }

        if (letter) hit++; else miss++;
        n++;
        log(`#${n}${multiQ ? '[多选]' : ''} `
          + `${letter ? '命中 ' + letter : '随机 ' + idxs.map((i) => String.fromCharCode(65 + i)).join('')}`
          + ` (${note}) ${q.stem.slice(0, 34)}`);
      });
    } catch (e) {
      // 查询失败时已经提示过，这里只负责收尾
    } finally {
      running = false;
    }
    const secs = ((Date.now() - t0) / 1000).toFixed(1);
    // 不报「几个选项没点上」：这页的选中态读不出来（没有 radio、class 也不变），
    // 校验必然失败，报出来是假警报——上一版就是这么误报出「103 个没点上」的。
    toast(`本轮：答 ${n} 题，命中 ${hit}，随机 ${miss}，点击 ${clicks} 次`
      + `（${lastStop}，${secs}s）`);
  }

  // ============================================================
  // 四、采集入库（交卷后 / 查看试卷页）
  // ============================================================

  /** 把一道题拼成「题干 + 选项 + 正确答案」的文本块，服务端直接用现成的解析器入库 */
  function blockOf(q, ans) {
    const lines = [q.stem];
    q.options.forEach((o, i) => lines.push(`${String.fromCharCode(65 + i)}. ${o.text}`));
    lines.push(`正确答案：${ans}`);
    return lines.join(String.fromCharCode(10));
  }

  /**
   * 采集入库：**遍历整份试卷**，把每道题的「题目 + 正确答案」收集起来一次性入库。
   *
   * 必须遍历，不能只看当前页，也不能只收错题——原因：
   *   · 试卷页一次只显示一道题，不翻页就只能采到一道；
   *   · 靠错题清单补题是不行的：**蒙对了的题不会出现在错题里**，
   *     但它同样需要把正确答案入库（题库里没有它）。
   * 遍历能保证每道题的正确答案都入库，跟对错无关。
   */
  async function harvest() {
    if (running) { toast('正在跑，别重复点'); return; }
    running = true;
    const blocks = [];
    const seen = new Set();
    let noAnswer = 0;
    const t0 = Date.now();
    try {
      await forEachQuestion(async (q) => {
        const ans = adoptRevealedAnswer(q.container);
        if (!ans) { noAnswer++; return; }       // 答题页没有答案块，只有交卷后的试卷页才有
        if (seen.has(q.stem)) return;           // 同一道题不重复收
        seen.add(q.stem);
        blocks.push(blockOf(q, ans));
      });
    } finally {
      running = false;
    }

    if (!blocks.length) {
      toast(noAnswer
        ? `扫了 ${noAnswer} 题但都读不到正确答案——确认在**交卷后**的试卷页`
        : '没识别到题目，点「诊断」看看');
      return;
    }
    try {
      // 用 String.fromCharCode(10) 而不是 '\n' 字面量：这个文件多次在传递过程中
      // 被吃掉转义、变成真换行，直接写出字符码最稳。
      const sep = String.fromCharCode(10) + String.fromCharCode(10);
      const res = await learnServer(blocks.join(sep), 'script');
      const t = res.tally || {};
      const secs = ((Date.now() - t0) / 1000).toFixed(1);
      toast(`采集 ${blocks.length} 题：新增 ${t.insert || 0}，重复 ${t.update || 0}`
        + (t.conflict ? `，冲突 ${t.conflict}` : '')
        + `（${lastStop}，${secs}s）`);
      log('入库结果:', res);
    } catch (e) {
      toast('入库失败：' + e.message);
    }
  }

  // ============================================================
  // 五、诊断（用来适配页面结构）
  // ============================================================

  function diagnose() {
    const qs = adoptQuestions();
    console.group('[易班题库] 诊断');
    console.log('识别到题目数:', qs.length);
    qs.forEach((q, i) => {
      const ans = adoptRevealedAnswer(q.container);
      console.log(`--- 第 ${i + 1} 道 ---`);
      console.log('  题干:', q.stem.slice(0, 80));
      console.log('  选项:', q.options.map((o) => `${o.letter}=${o.text.slice(0, 20)}`).join(' | '));
      console.log('  页面显示的答案:', ans);
      console.log('  发给服务端的文本:\n' + toServerText(q));
      console.log('  容器:', q.container.tagName, q.container.className);
    });
    const dump = qs.map((q, i) => {
      const ans = adoptRevealedAnswer(q.container);
      const lines = [q.stem, ...q.options.map((o, k) => `${String.fromCharCode(65 + k)}. ${o.text}`)];
      if (ans) lines.push(`正确答案：${ans}`);
      return lines.join('\n');
    }).join('\n\n');
    console.log('=== 可直接入库的文本 ===\n' + dump);
    console.groupEnd();
    if (qs.length) toast(`识别到 ${qs.length} 道题，详情看控制台（F12）`);
    else toast('没识别到题目，详情看控制台（F12）');
  }

  // ============================================================
  // 六、面板
  // ============================================================

  function toast(msg) {
    const el = document.getElementById('yqbank-toast');
    if (el) { el.textContent = msg; el.style.opacity = '1'; clearTimeout(el._t);
      el._t = setTimeout(() => { el.style.opacity = '0'; }, 4000); return; }
    log(msg);
  }

  function buildPanel() {
    const box = document.createElement('div');
    box.id = 'yqbank-panel';
    box.style.cssText = [
      'position:fixed', 'right:12px', 'bottom:12px', 'z-index:2147483647',
      'font:13px/1.6 system-ui,sans-serif', 'background:#fff', 'color:#222',
      'border:1px solid #bbb', 'border-radius:8px', 'padding:8px 10px',
      'box-shadow:0 4px 16px rgba(0,0,0,.25)', 'width:210px',
    ].join(';');
    box.innerHTML = `
      <div style="font-weight:600;margin-bottom:6px">易班题库桥接</div>
      <div id="yqbank-status" style="color:#666;margin-bottom:6px">连接中…</div>
      <button id="yqbank-answer" style="width:100%;margin-bottom:4px;padding:4px">自动答题</button>
      <button id="yqbank-harvest" style="width:100%;margin-bottom:4px;padding:4px">采集入库</button>
      <button id="yqbank-diag" style="width:100%;padding:4px">诊断</button>
      <label style="display:block;margin-top:6px;font-size:12px;color:#444">
        <input type="checkbox" id="yqbank-fast"> 快速模式（不等待）
      </label>
      <div style="font-size:11px;color:#999;margin-top:2px">
        快 = 看不见翻页过程（参考脚本就是这么做到的）
      </div>
      <button id="yqbank-clear" style="width:100%;margin-top:6px;padding:4px;color:#c00">清空题库</button>
      <div id="yqbank-toast" style="margin-top:6px;font-size:12px;color:#0a0;opacity:0;transition:opacity .3s"></div>
    `;
    document.body.appendChild(box);
    box.querySelector('#yqbank-answer').onclick = () => autoAnswer().catch((e) => toast(e.message));
    box.querySelector('#yqbank-harvest').onclick = () => harvest().catch((e) => toast(e.message));
    box.querySelector('#yqbank-diag').onclick = diagnose;
    // 清空是不可恢复的，所以两道拦：要求手打 yes，再 confirm 一次。
    // 服务端在删之前**自动备份**（返回里带备份文件名），所以点错也能还原。
    // 题目只有脚本采集这一条来源，所以这里就是全清；要按来源细清用命令行
    // （python bank.py clear --source xxx）。
    box.querySelector('#yqbank-clear').onclick = async () => {
      // 换行用 String.fromCharCode(10)，不写字面量转义——这个文件被吃掉过多次（有检查器兜底）
      const NL = String.fromCharCode(10);
      const ask = ['清空题库（不可恢复，服务端会先自动备份）', '',
                   '输入 yes 回车 = 确认清空', '取消或留空 = 不清'].join(NL);
      const typed = prompt(ask, '');
      if (typed === null || typed.trim().toLowerCase() !== 'yes') return;
      if (!confirm(`再确认一次：清空全部题目？${NL}服务端会先备份，但这个操作本身不可恢复。`)) return;
      try {
        const r = await call('/clear', { confirm: true, all: true });
        toast(r.deleted ? `已清空 ${r.deleted} 题，剩 ${r.left} 题；备份 ${r.backup}`
                        : '没有匹配的题');
      } catch (e) {
        toast('清空失败：' + e.message);
      }
    };
    box.querySelector('#yqbank-fast').onchange = (ev) => {
      fastMode = !!ev.target.checked;
      toast(fastMode ? '快速模式：开（不等待，看不见翻页）' : '快速模式：关（带等待，看得见过程）');
    };

    // 探活（走 GM_xmlhttpRequest，避免 HTTPS 页面访问 http://127.0.0.1 被拦）
    callGet('/health')
      .then((h) => {
        const s = box.querySelector('#yqbank-status');
        s.textContent = `已连接：题库 ${h.questions} 题`;
        s.style.color = '#0a0';
      })
      .catch(() => {
        const s = box.querySelector('#yqbank-status');
        s.textContent = '未连接 —— 先跑 python bank.py serve';
        s.style.color = '#c00';
      });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', buildPanel);
  } else {
    buildPanel();
  }
})();
