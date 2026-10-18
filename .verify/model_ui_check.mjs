/* V3「首次向导 + 转写模型设置」界面量具 —— Node 原生 + CDP，零 npm 依赖。

 *   node .verify\model_ui_check.mjs
 *
 * 为什么单独写：V3 新增的向导/下载/模型目录都在前端，Python 自检只能证接口；
 * 「点按钮有没有反应」「绿色/红色有没有显示」只有真浏览器能证。
 * 本脚本**不下载模型**、不点「下载」，只验证首屏与设置页的交互；
 * 下载协议由 selftest_models.py 用本地假源覆盖。
 *
 * 会把 `ui.onboarded` 临时设为 false 以复现首次向导，跑完恢复原值。
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";

const BASE = (process.env.BN_BASE || "http://127.0.0.1:8001").replace(/\/$/, "");
const PORT = Number(process.env.BN_CDP_PORT || 9346);
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

  const before = await (await fetch(`${BASE}/api/asr/status`)).json();
  // 固定成"首次使用、默认 small"的状态，跑完恢复。否则上一轮测试留下的
  // current_model 会让向导默认选中另一个已下载模型，断言就不稳定。
  await fetch(`${BASE}/api/asr/settings`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: "small", onboarded: false }),
  });

  const profile = join(process.env.TEMP || ".", `bn-model-ui-${Date.now()}`);
  const child = spawn(chrome, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
    "--window-size=1100,1500", "about:blank",
  ], { stdio: "ignore" });

  try {
    await waitForEndpoint(`http://127.0.0.1:${PORT}/json/version`);
    const targets = await waitForEndpoint(`http://127.0.0.1:${PORT}/json/list`);
    const page = targets.find((t) => t.type === "page");
    const cdp = await Cdp.connect(page.webSocketDebuggerUrl);
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");

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
    const centerOf = async (selector) => {
      const box = await evaluate(`(() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) return null;
        const r = el.getBoundingClientRect();
        if (!r.width) return null;
        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
      })()`);
      if (!box) throw new Error(`找不到可点击元素：${selector}`);
      return box;
    };
    const clickAt = async (x, y) => {
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
      await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
      await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
      await sleep(250);
    };
    const click = async (selector) => {
      const { x, y } = await centerOf(selector);
      await clickAt(x, y);
    };
    const shot = async (name) => {
      const { data } = await cdp.send("Page.captureScreenshot", { format: "png" });
      await writeFile(join(SHOTS, name), Buffer.from(data, "base64"));
    };

    const loaded = cdp.once("Page.loadEventFired");
    await cdp.send("Page.navigate", { url: `${BASE}/` });
    await Promise.race([loaded, sleep(8000)]);

    /* ---- 1. 首次向导 ---- */
    const shown = await poll(`!document.querySelector("#onboard-modal").classList.contains("hidden")`, 10000);
    check("首次打开会弹向导", shown === true);
    check("向导标题写明 V3",
      /V3/.test(await evaluate(`document.querySelector("#onboard-modal h3").textContent`)));
    const checks = await evaluate(`document.querySelectorAll("#onboard-checks .check-item").length`);
    check("环境检查至少 5 项", checks >= 5, ` 实际 ${checks}`);
    const cards = await evaluate(`document.querySelectorAll("#onboard-models .onboard-model").length`);
    check("模型卡片数量与状态接口一致", cards === (before.models || []).length, ` 实际 ${cards}`);
    const green = await evaluate(`document.querySelectorAll("#onboard-models .rec-green").length`);
    check("模型卡片带绿色/红色建议标记", green >= 1, ` 绿色 ${green}`);
    check("默认选中 small",
      (await evaluate(`document.querySelector("#onboard-models input:checked")?.value`)) === "small");
    check("下载源默认国内镜像",
      (await evaluate(`document.querySelector("#onboard-endpoint").value`)) === "mirror");
    await shot("04-onboarding.png");

    /* ---- 2. 先不下载：关掉向导 + 转写开关自动关闭 ---- */
    await click("#onboard-skip");
    const closed = await poll(`document.querySelector("#onboard-modal").classList.contains("hidden")`, 8000);
    check("点「先不下载」能关掉向导", closed === true);
    check("跳过下载后「转写文字稿」自动关闭",
      (await evaluate(`document.querySelector("#opt-transcribe").checked`)) === false);

    // small 可能已经下载过（作者机器上就有）。切到一个确实未下载的模型，
    // 才能验证主面板的「模型未下载」提示——顺便测一次模型下拉切换。
    const missing = (before.models || []).find((m) => !m.downloaded);
    if (missing) {
      await click("#model-picker-btn");
      await click(`#model-menu .model-option[data-model="${missing.name}"]`);
      await sleep(1600);
      const settingsOpened = await evaluate(`!document.querySelector("#set-modal").classList.contains("hidden")`);
      if (settingsOpened) { await click("#set-close"); await sleep(300); }
    }
    const banner = await poll(`!document.querySelector("#model-banner").classList.contains("hidden")`, 8000);
    check(missing ? "主面板出现「模型未下载」提示" : "所有模型都已下载，跳过未下载提示检查",
      missing ? banner === true : true);
    check(missing ? "提示里有「还没下载」" : "未下载提示不适用",
      missing ? /还没下载/.test(await evaluate(`document.querySelector("#model-banner").textContent`)) : true);

    /* ---- 3. 模型下拉是数据驱动的 ---- */
    await click("#model-picker-btn");
    const menuOpen = await poll(`!document.querySelector("#model-menu").classList.contains("hidden")`, 3000);
    check("点模型下拉能展开", menuOpen === true);
    const options = await evaluate(`document.querySelectorAll("#model-menu .model-option").length`);
    check("下拉选项数量与模型清单一致", options === (before.models || []).length, ` 实际 ${options}`);
    check("下拉项有已下载/未下载标记",
      (await evaluate(`document.querySelectorAll("#model-menu .model-state").length`)) === options);
    await shot("05-model-menu.png");
    await clickAt(20, 20); // 点空白收起

    /* ---- 4. 设置 → 转写模型 ---- */
    await click("#btn-settings");
    const setOpen = await poll(`!document.querySelector("#set-modal").classList.contains("hidden")`, 4000);
    check("齿轮能打开设置", setOpen === true);
    await click(`.set-tab[data-pane="asr"]`);
    const paneOpen = await poll(`!document.querySelector("#set-pane-asr").classList.contains("hidden")`, 4000);
    check("能切到「转写模型」选项卡", paneOpen === true);
    const rows = await poll(`document.querySelectorAll("#asr-models .asr-model").length`, 6000);
    check("设置里列出全部模型", rows === (before.models || []).length, ` 实际 ${rows}`);
    check("模型目录显示的是绝对路径",
      /^[A-Za-z]:[\\/]/.test(await evaluate(`document.querySelector("#asr-dir").value`)));
    check("设备选择有「自动/CPU/GPU」三项",
      (await evaluate(`document.querySelectorAll("#asr-device option").length`)) === 3);
    check("设置页有「打开模型目录」按钮",
      (await evaluate(`!!document.querySelector("#asr-dir-open")`)) === true);
    check("设置页有诊断信息入口",
      /诊断信息/.test(await evaluate(`document.querySelector("#set-pane-asr").textContent`)));
    await shot("06-asr-settings.png");

    check("页面没有未捕获的脚本错误", pageErrors.length === 0, ` ${pageErrors.join(" | ")}`);
  } finally {
    child.kill();
    // 恢复向导状态和当前模型，别让量具改掉用户配置
    await fetch(`${BASE}/api/asr/settings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: before.current_model, onboarded: before.onboarded }),
    }).catch(() => {});
  }

  console.log(`\n结果：${ok} 通过 / ${bad} 失败`);
  console.log(`截图：${SHOTS}`);
  process.exit(bad ? 1 : 0);
}

main().catch((error) => {
  console.error(`量具自身出错：${error.message}`);
  process.exit(2);
});
