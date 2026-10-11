/* 「已有文稿」区块的界面量具 —— Node 原生 + CDP，零 npm 依赖（做法沿用 personal-gallery/.verify）。

 *   node .verify\library_ui_check.mjs
 *
 * 为什么要有它：Python 那几套自检打到的是 HTTP 层，而"点按钮有没有反应"这件事
 * 只有真浏览器能证。这个项目**被这类 bug 咬过两次**（`Task.cancellable` 读属性 500 →
 * 前端不看 res.ok → 点取消毫无反应；`/api/tasks/{id}/open` 也是同一个形态），
 * 所以新增的交互一律走一遍真点击。
 *
 * 全程只读：点的是「预览文稿」「查看总结」，不写盘、不起总结作业（不烧 API）。
 * 截图落 .verify\shots\。
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";

const BASE = (process.env.BN_BASE || "http://127.0.0.1:8001").replace(/\/$/, "");
const PORT = Number(process.env.BN_CDP_PORT || 9345);
const SHOTS = resolve(import.meta.dirname, "shots");
const sleep = (ms) => new Promise((done) => setTimeout(done, ms));

setTimeout(() => {
  console.log("\nWATCHDOG 超时退出（120s）");
  process.exit(2);
}, 120000);

function findChrome() {
  for (const candidate of [process.env.BN_CHROME_PATH, process.env.CHROME_PATH].filter(Boolean)) {
    if (existsSync(candidate)) return candidate;
  }
  for (const candidate of [
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  return null;
}

class Cdp {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();
    socket.addEventListener("message", (event) => {
      const text = typeof event.data === "string" ? event.data : Buffer.from(event.data).toString("utf8");
      const message = JSON.parse(text);
      if (message.id && this.pending.has(message.id)) {
        const { done, fail } = this.pending.get(message.id);
        this.pending.delete(message.id);
        if (message.error) fail(new Error(`${message.error.message} (${message.error.code})`));
        else done(message.result);
        return;
      }
      for (const handler of this.listeners.get(message.method) || []) handler(message.params);
    });
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((done, fail) => {
      socket.addEventListener("open", done, { once: true });
      socket.addEventListener("error", () => fail(new Error(`WebSocket 连接失败：${url}`)), { once: true });
    });
    return new Cdp(socket);
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((done, fail) => {
      this.pending.set(id, { done, fail });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  on(method, handler) {
    if (!this.listeners.has(method)) this.listeners.set(method, []);
    this.listeners.get(method).push(handler);
  }

  once(method) {
    return new Promise((done) => this.on(method, done));
  }
}

async function waitForEndpoint(url, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return await response.json();
    } catch { /* 还没起来 */ }
    await sleep(200);
  }
  throw new Error(`等待 ${url} 超时`);
}

/* ------------------------------------------------------------------ 断言 */

let ok = 0;
let bad = 0;
function check(name, cond, extra = "") {
  if (cond) { ok += 1; console.log(`  [OK]   ${name}`); }
  else { bad += 1; console.log(`  [FAIL] ${name} ${extra}`); }
}

async function main() {
  const chrome = findChrome();
  if (!chrome) throw new Error("找不到 Chrome/Edge");
  await mkdir(SHOTS, { recursive: true });

  const profile = join(process.env.TEMP || ".", `bn-ui-check-${Date.now()}`);
  const child = spawn(chrome, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    "--window-size=1100,1400", "about:blank",
  ], { stdio: "ignore" });

  try {
    await waitForEndpoint(`http://127.0.0.1:${PORT}/json/version`);
    const targets = await waitForEndpoint(`http://127.0.0.1:${PORT}/json/list`);
    const page = targets.find((t) => t.type === "page");
    const cdp = await Cdp.connect(page.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");

    // 真的去收页面抛出的异常（不是查一个没人赋值的全局变量——那等于没测）
    const pageErrors = [];
    cdp.on("Runtime.exceptionThrown", (params) => {
      pageErrors.push(params?.exceptionDetails?.exception?.description
        || params?.exceptionDetails?.text || "未知异常");
    });

    const evaluate = async (expression) => {
      const { result, exceptionDetails } = await cdp.send("Runtime.evaluate", {
        expression, awaitPromise: true, returnByValue: true,
      });
      if (exceptionDetails) throw new Error(exceptionDetails.exception?.description || "页面脚本抛错");
      return result.value;
    };
    const poll = async (expression, timeoutMs = 10000) => {
      const deadline = Date.now() + timeoutMs;
      let last;
      while (Date.now() < deadline) {
        last = await evaluate(expression);
        if (last) return last;
        await sleep(120);
      }
      return last ?? null;
    };
    const clickAt = async (x, y) => {
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
      await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
      await sleep(250);
    };
    const shot = async (name) => {
      const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
      await writeFile(join(SHOTS, name), Buffer.from(data, "base64"));
    };
    const clickButtonIn = async (selector, text, index = 0) => {
      const box = await evaluate(`(() => {
        const rows = [...document.querySelectorAll(${JSON.stringify(selector)})];
        const row = rows.filter((r) => [...r.querySelectorAll("button")].some((b) => b.textContent.trim() === ${JSON.stringify(text)}))[${index}];
        if (!row) return null;
        const btn = [...row.querySelectorAll("button")].find((b) => b.textContent.trim() === ${JSON.stringify(text)});
        const r = btn.getBoundingClientRect();
        if (!r.width) return null;
        return { x: r.left + r.width / 2, y: r.top + r.height / 2, title: row.querySelector(".lib-title")?.textContent };
      })()`);
      if (!box) throw new Error(`找不到按钮「${text}」（${selector}）`);
      await clickAt(box.x, box.y);
      return box.title;
    };

    /* ---- 1. 打开页面：已有文稿必须自动扫出来（不用点任何东西） ---- */
    const loaded = cdp.once("Page.loadEventFired");
    await cdp.send("Page.navigate", { url: `${BASE}/` });
    await Promise.race([loaded, sleep(8000)]);
    const rendered = await poll(`document.querySelectorAll("#library-list .lib-item").length`, 8000);
    check("打开页面就自动列出已有文稿（不点任何按钮）", rendered > 0, ` 实际 ${rendered} 条`);
    check("计数文字与条数一致",
      (await evaluate(`document.querySelector("#task-count") && document.querySelector("#lib-count").textContent`)) === `共 ${rendered} 份`);
    check("没有文稿时才显示空提示",
      (await evaluate(`getComputedStyle(document.querySelector("#lib-empty")).display`)) === "none");
    await shot("01-library-list.png");

    /* ---- 2. 点「预览文稿」→ 预览弹窗打开且有正文 ---- */
    const firstTitle = await clickButtonIn("#library-list .lib-item", "预览文稿");
    const modalOpen = await poll(`!document.querySelector("#modal").classList.contains("hidden")`, 5000);
    check("点「预览文稿」弹窗会打开", modalOpen === true);
    const body = await evaluate(`document.querySelector("#modal-body").textContent`);
    check("预览里有文稿正文（带时间戳）", typeof body === "string" && body.length > 100 && body.includes("[00:00:"),
      ` 长度 ${body ? body.length : 0}`);
    check("弹窗标题就是点的那份", (await evaluate(`document.querySelector("#modal-title").textContent`)) === firstTitle,
      ` ${firstTitle}`);
    await shot("02-preview-modal.png");
    await clickAt(...(await evaluate(`(() => { const r = document.querySelector("#modal-close").getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`)));
    check("点关闭能关掉预览", (await evaluate(`document.querySelector("#modal").classList.contains("hidden")`)) === true);

    /* ---- 3. 点「查看总结」→ 只读总结面板（花钱的按钮必须收起来） ---- */
    const sumTitle = await clickButtonIn("#library-list .lib-item", "查看总结");
    const sumOpen = await poll(`!document.querySelector("#sum-modal").classList.contains("hidden")`, 5000);
    check("点「查看总结」面板会打开", sumOpen === true);
    const sumBody = await poll(`document.querySelector("#sum-body").innerHTML.length`, 8000);
    check("总结正文渲染出来了", sumBody > 100, ` 长度 ${sumBody}`);
    check("状态行说明是读的已存总结",
      /已保存的总结/.test(await evaluate(`document.querySelector("#sum-status").textContent`)),
      ` ${await evaluate(`document.querySelector("#sum-status").textContent`)}`);
    check("只读态：没有「重新生成」",
      (await evaluate(`document.querySelector("#sum-regen").classList.contains("hidden")`)) === true);
    check("只读态：没有模板下拉",
      (await evaluate(`document.querySelector("#sum-template").classList.contains("hidden")`)) === true);
    check("只读态：没有「停止」",
      (await evaluate(`document.querySelector("#sum-stop").classList.contains("hidden")`)) === true);
    check("标题是点的那份", (await evaluate(`document.querySelector("#sum-title").textContent`)) === sumTitle, ` ${sumTitle}`);
    await shot("03-library-summary.png");

    /* ---- 4. 回到任务路径：按钮必须恢复（只读态不能把任务那边也锁死） ---- */
    await clickAt(...(await evaluate(`(() => { const r = document.querySelector("#sum-close").getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()`)));
    check("点关闭能关掉总结面板", (await evaluate(`document.querySelector("#sum-modal").classList.contains("hidden")`)) === true);

    check("页面没有未捕获的脚本错误", pageErrors.length === 0, ` ${pageErrors.join(" | ")}`);
  } finally {
    child.kill();
  }

  console.log(`\n结果：${ok} 通过 / ${bad} 失败`);
  console.log(`截图：${SHOTS}`);
  process.exit(bad ? 1 : 0);
}

main().catch((error) => {
  console.error(`量具自身出错：${error.message}`);
  process.exit(2);
});
