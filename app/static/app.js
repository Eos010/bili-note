"use strict";

const STATUS_TEXT = {
  queued: "排队中",
  resolving: "解析中",
  downloading: "下载中",
  transcribing: "转写中",
  done: "完成",
  error: "失败",
  cancelled: "已取消",
};

const $ = (sel) => document.querySelector(sel);
const listEl = $("#task-list");
const emptyEl = $("#empty");

// ---------- 选项卡切换 ----------
function switchTab(mode) {
  const isDownload = mode === "download";
  $("#tab-download").classList.toggle("active", isDownload);
  $("#tab-local").classList.toggle("active", !isDownload);
  $("#panel-download").classList.toggle("hidden", !isDownload);
  $("#panel-local").classList.toggle("hidden", isDownload);
}

$("#tab-download").addEventListener("click", () => switchTab("download"));
$("#tab-local").addEventListener("click", () => switchTab("local"));

// ---------- 模型选择（展开切换） ----------
let currentModel = "large-v3";

$("#model-picker-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  $("#model-menu").classList.toggle("hidden");
});

$("#model-menu").addEventListener("click", (e) => {
  const opt = e.target.closest(".model-option");
  if (!opt) return;
  currentModel = opt.dataset.model;
  $("#model-current-label").textContent = opt.querySelector(".model-name").textContent;
  $("#model-menu").querySelectorAll(".model-option").forEach((o) => o.classList.toggle("active", o === opt));
  $("#model-menu").classList.add("hidden");
});

document.addEventListener("click", () => $("#model-menu").classList.add("hidden"));

// ---------- 提交 ----------
function linesOf(el) {
  return el.value.split("\n").map((s) => s.trim()).filter(Boolean);
}

async function postTasks(body) {
  const res = await fetch("/api/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json();
    throw new Error(err.detail || "提交失败");
  }
}

$("#btn-start-download").addEventListener("click", async () => {
  const urls = linesOf($("#urls"));
  const download = $("#opt-download").checked;
  const transcribe = $("#opt-transcribe").checked;

  if (!urls.length) { flash("请先粘贴至少一个链接"); return; }
  if (!download && !transcribe) { flash("下载和转写至少开启一个"); return; }

  const btn = $("#btn-start-download");
  btn.disabled = true;
  try {
    await postTasks({ urls, download, transcribe, model: currentModel });
    $("#urls").value = "";
    flash(`已提交 ${urls.length} 个任务`);
    refresh();
  } catch (e) {
    flash(e.message);
  } finally {
    btn.disabled = false;
  }
});

$("#btn-start-local").addEventListener("click", async () => {
  const files = linesOf($("#files"));
  if (!files.length) { flash("请先粘贴至少一个文件路径"); return; }

  const btn = $("#btn-start-local");
  btn.disabled = true;
  try {
    await postTasks({ files, download: false, transcribe: true, model: currentModel });
    $("#files").value = "";
    flash(`已提交 ${files.length} 个任务`);
    refresh();
  } catch (e) {
    flash(e.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------- 轮询 ----------
async function refresh() {
  try {
    const res = await fetch("/api/tasks");
    const data = await res.json();
    render(data.tasks);
  } catch (_) { /* 服务未就绪时静默 */ }
}

// 一张卡片"要不要重画"的指纹。**总结状态必须算进来**——
// 不算的话，总结在后台往前跑，卡片却纹丝不动（那是 v2.0 的老毛病）。
function fingerprint(t) {
  const s = t.summary || {};
  return [
    t.status, t.phase_detail, t.progress,
    s.status, s.progress, s.index, s.total, s.label, s.has_summary, s.chars, s.error,
  ].join("|");
}

function render(tasks) {
  emptyEl.style.display = tasks.length ? "none" : "block";
  $("#task-count").textContent = tasks.length ? `共 ${tasks.length} 个` : "";

  const byId = {};
  listEl.querySelectorAll(".task").forEach((el) => (byId[el.dataset.id] = el));

  tasks.forEach((t) => {
    const existing = byId[t.id];
    const fp = fingerprint(t);
    if (existing && existing.dataset.fp === fp) {
      return; // 无变化跳过，避免闪烁
    }
    const el = document.createElement("div");
    el.className = "task";
    el.dataset.id = t.id;
    el.dataset.fp = fp;
    el.innerHTML = taskHTML(t);
    if (existing) {
      existing.replaceWith(el);
    } else {
      listEl.appendChild(el);
    }
  });

  // 移除已删除的任务
  Object.entries(byId).forEach(([id, el]) => {
    if (!tasks.some((t) => t.id === id)) el.remove();
  });
}

// 总结在后台跑到哪一步了（任务卡上的那一行）。没有文字稿的任务不显示。
//
// 这一行的定位：**总结相关的动作都归它管**（查看 / 停止 / 重试 / 继续）。
// 主行动行那边就不要再摆同一个动作了——2026-09-20 他截图指出跑着时两处都是「查看」。
function summaryLineHTML(t) {
  const s = t.summary || {};
  if (t.status !== "done" || !t.txt_path || !s.status || s.status === "idle") return "";

  const running = s.status === "running" || s.status === "queued";
  if (running) {
    const queued = s.status === "queued";
    const pct = Math.max(0, Math.min(100, Math.round(s.progress || 0)));
    const detail = s.label || (queued ? "排队中…" : "生成中…");
    return `
      <div class="sum-line">
        <div class="sum-bar${queued ? " queued" : ""}"><i style="width:${pct}%"></i></div>
        <span class="sum-label" title="${escapeHtml(detail)}">${queued ? "" : "总结 "}${escapeHtml(detail)}</span>
        <div class="task-actions">
          <button class="btn small accent" data-act="sum" data-id="${t.id}">查看</button>
          <button class="btn small danger" data-act="sumstop" data-id="${t.id}">停止</button>
        </div>
      </div>`;
  }

  // 失败 / 已停止：按钮文案要跟「点下去真会发生什么」对上（都走 openSummary）——
  // 有存过的总结就是去读它，没有才是重跑/续跑。
  if (s.status === "error") {
    return `
      <div class="sum-line">
        <span class="sum-label err">总结失败：${escapeHtml(s.error || "未知错误")}</span>
        <div class="task-actions">
          <button class="btn small accent" data-act="sum" data-id="${t.id}">${
            s.has_summary ? "查看总结" : "重试"
          }</button>
        </div>
      </div>`;
  }

  if (s.status === "cancelled") {
    return `
      <div class="sum-line">
        <span class="sum-label">总结已停止${s.has_summary ? "（已完成的节留着，可接着跑）" : ""}</span>
        <div class="task-actions">
          <button class="btn small accent" data-act="sum" data-id="${t.id}">${
            s.has_summary ? "查看总结" : "继续生成"
          }</button>
        </div>
      </div>`;
  }

  return "";
}

// 这一行是否已经把总结的入口接管了（接管了主行动行就别再摆一个）
function summaryLineOwnsActions(t) {
  const s = t.summary || {};
  return ["queued", "running", "error", "cancelled"].includes(s.status);
}

function taskHTML(t) {
  const barClass = t.status === "done" ? "done" : t.status === "error" ? "error" : "";
  const pct = Math.round(t.progress);
  const isRunning = ["queued", "resolving", "downloading", "transcribing"].includes(t.status);
  const s = t.summary || {};
  const sumActive = s.status === "running" || s.status === "queued";

  let actions = "";
  if (t.status === "done") {
    // 总结行接管了总结入口时（排队/跑着/失败/已停止），主行动行就不再摆一个——
    // 否则同一个动作会出现两次（2026-09-20 他截图指出跑着时两处都是「查看」）。
    // 规矩：主行动行只放**任务级**动作；总结相关动作跟着总结进度走。
    const sumBtn = (t.txt_path && !summaryLineOwnsActions(t))
      ? `<button class="btn small accent" data-act="sum" data-id="${t.id}">${
          s.has_summary ? "查看总结" : "总结"
        }</button>`
      : "";
    actions = `
      ${sumBtn}
      <button class="btn small" data-act="open" data-id="${t.id}">打开目录</button>
      <button class="btn small" data-act="text" data-id="${t.id}">预览文稿</button>
      <button class="btn small danger" data-act="del" data-id="${t.id}">移除</button>`;
  } else if (t.status === "error" || t.status === "cancelled") {
    actions = `
      <button class="btn small accent" data-act="retry" data-id="${t.id}">重试</button>
      <button class="btn small danger" data-act="del" data-id="${t.id}">移除</button>`;
  } else if (isRunning) {
    const stopping = (t.phase_detail || "").includes("取消");
    actions = `<button class="btn small danger" data-act="cancel" data-id="${t.id}"${stopping ? " disabled" : ""}>${stopping ? "取消中…" : "取消"}</button>`;
  }

  // 「已总结」小标：不占正文位置，扫一眼就知道哪些出过稿
  const sumBadge = (t.status === "done" && s.has_summary && !sumActive)
    ? `<span class="badge sum-ok">已总结</span>` : "";

  const errHtml = t.error ? `<div class="error-text">${escapeHtml(t.error)}</div>` : "";
  return `
    <div class="task-head">
      <span class="task-title" title="${escapeHtml(t.url)}">${escapeHtml(t.title)}</span>
      ${sumBadge}
      <span class="badge ${t.status}">${STATUS_TEXT[t.status] || t.status}</span>
    </div>
    <div class="progress">
      <div class="bar"><div class="${barClass}" style="width:${pct}%"></div></div>
      <div class="meta">
        <span>${escapeHtml(t.phase_detail || "")}</span>
        <div class="task-actions">${actions}</div>
      </div>
    </div>
    ${summaryLineHTML(t)}
    ${errHtml}`;
}

// ---------- 操作（事件委托） ----------
listEl.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const { act, id } = btn.dataset;
  if (act === "open") {
    const res = await fetch(`/api/tasks/${id}/open`, { method: "POST" });
    if (!res.ok) {
      // 不检查 res.ok 就是项目档里记过的「点按钮没反应」——这里必须给反馈
      const d = await res.json().catch(() => ({}));
      flash(d.detail || "打开目录失败");
    }
  } else if (act === "text") {
    openPreview(id);
  } else if (act === "sum") {
    openSummary(id);
  } else if (act === "sumstop") {
    btn.disabled = true;
    const res = await fetch(`/api/tasks/${id}/summary/cancel`, { method: "POST" });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) {
      flash(d.detail || "停止失败");
      btn.disabled = false;
    } else {
      flash("已停止总结（已完成的节保留，下次可以接着跑）");
    }
    refresh();
  } else if (act === "cancel") {
    btn.disabled = true;
    const res = await fetch(`/api/tasks/${id}/cancel`, { method: "POST" });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      flash(d.detail || "取消失败");
      btn.disabled = false;
    } else {
      flash("已请求取消，正在停止…");
    }
    refresh();
  } else if (act === "retry") {
    btn.disabled = true;
    const res = await fetch(`/api/tasks/${id}/retry`, { method: "POST" });
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      flash(d.detail || "重试失败");
      btn.disabled = false;
    } else {
      flash("已重新排队");
    }
    refresh();
  } else if (act === "del") {
    openConfirm(id);
  }
});

// ---------- 移除 / 删除文件 确认 ----------
let confirmTaskId = null;

function shortPath(p) {
  return String(p).split(/[\\/]/).filter(Boolean).slice(-1)[0] || p;
}

async function openConfirm(id) {
  confirmTaskId = id;
  const title = listEl.querySelector(`[data-id="${id}"] .task-title`)?.textContent || "任务";
  $("#confirm-title").textContent = `移除「${title}」`;
  $("#confirm-note").textContent = "读取中…";
  $("#confirm-del").innerHTML = "";
  $("#confirm-keep").innerHTML = "";
  $("#confirm-del-count").textContent = "";
  $("#confirm-keep-wrap").classList.add("hidden");
  $("#confirm-modal").classList.remove("hidden");

  try {
    const res = await fetch(`/api/tasks/${id}/files`);
    const plan = await res.json();
    if (!res.ok) throw new Error(plan.detail || `HTTP ${res.status}`);
    $("#confirm-note").textContent = plan.note || "";

    const del = plan.targets || [];
    $("#confirm-del-count").textContent = del.length ? `（${del.length} 项）` : "";
    if (!del.length) {
      $("#confirm-del").innerHTML = `<li class="muted">没有可删除的产物文件</li>`;
    } else {
      del.forEach((p) => {
        const li = document.createElement("li");
        li.innerHTML = `<span title="${escapeHtml(p)}">${escapeHtml(shortPath(p))}</span>`;
        $("#confirm-del").appendChild(li);
      });
    }

    const kept = plan.kept || [];
    if (kept.length) {
      $("#confirm-keep-wrap").classList.remove("hidden");
      kept.forEach((p) => {
        const li = document.createElement("li");
        li.innerHTML = `<span title="${escapeHtml(p)}">${escapeHtml(shortPath(p))}</span>`;
        $("#confirm-keep").appendChild(li);
      });
    }
  } catch (e) {
    $("#confirm-note").textContent = `读取失败：${e.message}`;
  }
}

async function doDelete(withFiles) {
  const id = confirmTaskId;
  if (!id) return;
  const btn = withFiles ? $("#confirm-withfiles") : $("#confirm-only");
  btn.disabled = true;
  try {
    const res = await fetch(`/api/tasks/${id}${withFiles ? "?files=true" : ""}`, { method: "DELETE" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    $("#confirm-modal").classList.add("hidden");
    confirmTaskId = null;
    flash(withFiles ? `已移除任务，并删除 ${(data.removed || []).length} 项文件` : "已移除任务记录");
    refresh();
  } catch (e) {
    flash(`删除失败：${e.message}`);
  } finally {
    btn.disabled = false;
  }
}

$("#confirm-close").addEventListener("click", () => { $("#confirm-modal").classList.add("hidden"); confirmTaskId = null; });
$("#confirm-only").addEventListener("click", () => doDelete(false));
$("#confirm-withfiles").addEventListener("click", () => doDelete(true));
$("#confirm-modal").addEventListener("click", (e) => {
  if (e.target === $("#confirm-modal")) { $("#confirm-modal").classList.add("hidden"); confirmTaskId = null; }
});

// ---------- 文字稿预览 ----------
async function openPreview(id) {
  try {
    const res = await fetch(`/api/tasks/${id}/text`);
    if (!res.ok) return;
    const text = await res.text();
    const title = listEl.querySelector(`[data-id="${id}"] .task-title`)?.textContent || "文字稿";
    $("#modal-title").textContent = title;
    $("#modal-body").textContent = text;
    $("#modal").classList.remove("hidden");
  } catch (_) {}
}

$("#modal-close").addEventListener("click", () => $("#modal").classList.add("hidden"));
$("#modal").addEventListener("click", (e) => { if (e.target === $("#modal")) $("#modal").classList.add("hidden"); });
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  closeSummary();
  $("#modal").classList.add("hidden");
  $("#set-modal").classList.add("hidden");
  $("#confirm-modal").classList.add("hidden");
  confirmTaskId = null;
});

// ================================================================
// 已有文稿（打开页面自动扫描 output/）
//
// 与任务列表刻意分开：任务表是**内存态**（服务一重启就空了），而 output/ 下的
// .txt 是**文件事实**，一直在。以前要看/总结一份旧文稿，得手动把路径粘到「本地转写」框；
// 现在扫出来直接能看。
//
// 后端只回 id，前端从不上送路径 —— 路径穿越那类问题在 `app/library.py` 那侧就没有入口。
// ================================================================

let libItems = [];
let libFp = "";            // 整块清单的指纹：没变就不重画（10 秒一轮，重画会闪）

async function refreshLibrary() {
  try {
    const res = await fetch("/api/library");
    if (!res.ok) return;
    const data = await res.json();
    renderLibrary(data.items || []);
  } catch (_) { /* 服务未就绪时静默 */ }
}

function libFingerprint(items) {
  return items.map((i) => [i.id, i.mtime, i.has_summary, i.summary_bytes].join(":")).join("|");
}

function fmtCount(n) {
  return n >= 10000 ? `${(n / 10000).toFixed(1)} 万字` : `${n} 字`;
}

function fmtDate(mtime) {
  const d = new Date(mtime * 1000);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}

function renderLibrary(items) {
  libItems = items;
  const fp = libFingerprint(items);
  if (fp === libFp) return;
  libFp = fp;

  const list = $("#library-list");
  list.innerHTML = "";
  $("#lib-empty").style.display = items.length ? "none" : "block";
  $("#lib-count").textContent = items.length ? `共 ${items.length} 份` : "";

  items.forEach((it) => {
    const el = document.createElement("div");
    el.className = "lib-item";
    el.dataset.id = it.id;
    el.innerHTML = `
      <div class="lib-main">
        <span class="lib-title" title="${escapeHtml(it.name)}">${escapeHtml(it.title)}</span>
        <span class="lib-meta">${fmtCount(it.chars)} · ${escapeHtml(it.duration)} · ${fmtDate(it.mtime)}${
          it.has_media ? " · 有视频" : ""}${it.has_srt ? " · 有字幕" : ""}</span>
      </div>
      ${it.has_summary ? '<span class="badge sum-ok">已总结</span>' : ""}
      <div class="task-actions">
        <button class="btn small" data-lib-act="text" data-id="${it.id}">预览文稿</button>
        ${it.has_summary
          ? `<button class="btn small accent" data-lib-act="sum" data-id="${it.id}">查看总结</button>`
          : ""}
      </div>`;
    list.appendChild(el);
  });
}

$("#library-list").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-lib-act]");
  if (!btn) return;
  const { libAct, id } = btn.dataset;
  if (libAct === "text") openLibPreview(id, btn);
  else if (libAct === "sum") openLibSummary(id);
});

$("#btn-lib-refresh").addEventListener("click", () => {
  libFp = "";              // 手动刷新时强制重画
  refreshLibrary();
  flash("已重新扫描 output/");
});

// 只读预览：复用任务那边的同一个弹窗（它本来就是"把一段文本摊开看"）
async function openLibPreview(id, btn) {
  const item = libItems.find((i) => i.id === id);
  if (btn) btn.disabled = true;
  try {
    const res = await fetch(`/api/library/${id}/text`);
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${res.status}`);
    }
    const text = await res.text();
    $("#modal-title").textContent = item ? item.title : "文字稿";
    $("#modal-body").textContent = text;
    $("#modal").classList.remove("hidden");
  } catch (e) {
    flash(`预览失败：${e.message}`);
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 看已有文稿的总结：**只读**——没有作业可挂，所以模板/重新生成/停止一律收起来。
// 有本地视频时，点总结里的时间戳照样能跳回视频（媒体地址由后端的同目录挑选给出）。
async function openLibSummary(id) {
  const item = libItems.find((i) => i.id === id);
  sumTaskId = null;
  sumMode = "library";
  detachSummary();
  resetSumView();
  setSumToolsVisible(false);
  sumHasMedia = !!(item && item.has_media);
  sumMediaUrl = sumHasMedia ? `/api/library/${id}/media` : null;

  $("#sum-title").textContent = item ? item.title : "总结";
  $("#sum-modal").classList.remove("hidden");
  setSumStatus("读取已保存的总结…");

  try {
    const res = await fetch(`/api/library/${id}/summary`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    if (!data.exists) {
      setSumStatus("");
      showSumWarn("这份文稿还没有总结。要生成请把它走一遍任务（「本地转写」粘 .txt 路径）。");
      return;
    }
    sumRaw = stripFrontMatter(data.content);
    renderSummary();
    if (data.outline) {
      renderOutlineMarkdown(data.outline);
      $("#sum-outline-label").textContent = "速览（骨架）";
    }
    setSumStatus(`已保存的总结（${sumRaw.length} 字）`
      + (sumHasMedia ? " · 点时间戳可跳转播放" : ""));
  } catch (e) {
    setSumStatus("");
    showSumWarn(`读取失败：${e.message}`);
  }
}

// ================================================================
// 总结（v1.3；v2.1 改成"后台作业 + 可断开的观看窗口"）
//
// 关键观念：**这条 SSE 连接不是总结本身**。总结跑在服务端（`app/sumjobs.py`），
// 这里只负责"看"。所以：
//   - 关掉弹窗 = 掐掉观看窗口，作业继续跑，任务卡上照样有进度；
//   - 切到另一个任务的总结 = 断开旧的、接上新的，两个作业互不影响；
//   - 重新打开 = 服务端把已经发生过的事件重放一遍，接上实时流。
// ================================================================

let sumTaskId = null;       // 当前正在"看"的任务 id
let sumMode = "task";       // task = 挂着任务看；library = 看 output/ 里的已有文稿（只读，没有作业）
let sumMediaUrl = null;     // 点时间戳要跳的媒体地址（任务与文稿两条路各给各的）
let sumSession = 0;         // 观看会话号：断开/切换后，迟到的旧事件一律丢弃
let sumAbort = null;        // 当前 SSE 连接的 AbortController
let sumRaw = "";            // 流式累积的原始 markdown
let sumThink = "";          // 推理模型的思考过程（reasoning_content）
let sumThinkOpen = false;   // 思考区是否展开（默认收起）
let sumStreaming = false;   // 这条观看窗口是否还开着（≠ 作业在不在跑）
let sumHasMedia = false;    // 后端是否报告有可播放媒体
let sumRenderTimer = null;  // 节流渲染
let sumThinkTimer = null;
let sumOutlineOpen = true;  // 速览区是否展开
let sumOutlineRaw = "";     // 骨架流式原文（解析完成前先原样显示）

function setSumStatus(text) {
  $("#sum-status").textContent = text || "";
}

// 停止按钮只在"这条窗口还开着、且作业还在跑"时露出
function setSumStopVisible(visible) {
  $("#sum-stop").classList.toggle("hidden", !visible);
}

// 看已有文稿时，面板上一整套"要花钱的动作"都得收起来（模板 / 时间戳 / 重新生成 / 停止）——
// 那一屏是只读的，摆着按钮等于骗他说点了会重跑。
function setSumToolsVisible(visible) {
  $("#sum-regen").classList.toggle("hidden", !visible);
  $("#sum-template").classList.toggle("hidden", !visible);
  const tsLabel = $("#sum-ts").closest("label");
  if (tsLabel) tsLabel.classList.toggle("hidden", !visible);
  if (!visible) setSumStopVisible(false);
}

// ---- 分节进度条（长稿流程才有；单趟总结不显示） ----
function setSumProgress(index, total, label) {
  const box = $("#sum-progress");
  if (!total) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const pct = Math.max(0, Math.min(100, Math.round((index / total) * 100)));
  $("#sum-progress-fill").style.width = pct + "%";
  $("#sum-progress-label").textContent = label || `第 ${index}/${total} 节`;
}

function hideSumProgress() {
  $("#sum-progress").classList.add("hidden");
  $("#sum-progress-fill").style.width = "0%";
}

// ---- 速览（骨架）：长稿流程的副产品，先给用户一个全局地图 ----
function setOutline(html) {
  $("#sum-outline").classList.remove("hidden");
  $("#sum-outline-body").innerHTML = html;
  $("#sum-outline-body").classList.toggle("hidden", !sumOutlineOpen);
}

function resetOutline() {
  sumOutlineOpen = true;
  sumOutlineRaw = "";
  $("#sum-outline").classList.add("hidden");
  $("#sum-outline-body").innerHTML = "";
  $("#sum-outline-caret").textContent = "▾";
  $("#sum-outline-label").textContent = "速览（骨架）";
}

$("#sum-outline-head").addEventListener("click", () => {
  sumOutlineOpen = !sumOutlineOpen;
  $("#sum-outline-body").classList.toggle("hidden", !sumOutlineOpen);
  $("#sum-outline-caret").textContent = sumOutlineOpen ? "▾" : "▸";
});

// 质量自检：覆盖了多少具体锚点（数字/年份/书名）。后端算好的，不花钱。
function qualityNote(q) {
  if (!q || !q.anchors_total) return "";
  const miss = q.anchors_missed && q.anchors_missed.length
    ? `，漏 ${q.anchors_missed.slice(0, 5).join("、")}` : "";
  return ` · 锚点覆盖 ${q.anchor_coverage}%（${q.anchors_hit}/${q.anchors_total}${miss}）`
    + (q.quote_fidelity < 100 ? ` · ⚠ 引号保真 ${q.quote_fidelity}%` : "");
}

function resetThink() {
  sumThink = "";
  sumThinkOpen = false;
  $("#sum-think").classList.add("hidden");
  $("#sum-think-body").classList.add("hidden");
  $("#sum-think-body").textContent = "";
  $("#sum-think-caret").textContent = "▸";
  $("#sum-think-label").textContent = "模型正在思考…";
}

function renderThink() {
  const el = $("#sum-think-body");
  el.textContent = sumThink;
  if (sumThinkOpen) el.scrollTop = el.scrollHeight;
}

$("#sum-think-head").addEventListener("click", () => {
  sumThinkOpen = !sumThinkOpen;
  $("#sum-think-body").classList.toggle("hidden", !sumThinkOpen);
  $("#sum-think-caret").textContent = sumThinkOpen ? "▾" : "▸";
  if (sumThinkOpen) renderThink();
});

// 关掉弹窗 = 断开观看窗口。**作业不受影响**——这正是 v2.1 要做的事。
function closeSummary() {
  const wasStreaming = sumStreaming;
  detachSummary();
  $("#sum-modal").classList.add("hidden");
  sumTaskId = null;
  sumMode = "task";
  sumMediaUrl = null;
  setSumStopVisible(false);
  $("#sum-warn").classList.add("hidden");
  if (wasStreaming) flash("总结在后台继续跑，任务卡片上能看到进度");
}

// 断开当前 SSE 连接（换任务 / 关弹窗 / 重新生成前都要先断）
function detachSummary() {
  sumSession += 1;                 // 旧会话的事件从此一律丢弃
  sumStreaming = false;
  if (sumAbort) {
    try { sumAbort.abort(); } catch (_) {}
    sumAbort = null;
  }
  if (sumRenderTimer) { clearTimeout(sumRenderTimer); sumRenderTimer = null; }
  if (sumThinkTimer) { clearTimeout(sumThinkTimer); sumThinkTimer = null; }
}

$("#sum-close").addEventListener("click", closeSummary);
$("#sum-modal").addEventListener("click", (e) => { if (e.target === $("#sum-modal")) closeSummary(); });

$("#sum-stop").addEventListener("click", async () => {
  const id = sumTaskId;
  if (!id) return;
  const btn = $("#sum-stop");
  btn.disabled = true;
  try {
    const res = await fetch(`/api/tasks/${id}/summary/cancel`, { method: "POST" });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
    // 不主动断开：服务端会补一个 cancelled 事件过来，让它自己收尾
    setSumStatus("正在停止…");
  } catch (e) {
    flash(`停止失败：${e.message}`);
  } finally {
    btn.disabled = false;
  }
  refresh();
});

$("#sum-template").addEventListener("change", () => {
  const isCustom = $("#sum-template").value === "D";
  $("#sum-custom-wrap").classList.toggle("hidden", !isCustom);
  if (sumRaw && !sumStreaming) hintRegen();
});

// 切换时间戳要求后不自动重跑（避免误触就烧一次调用），提示他手动重生成
$("#sum-ts").addEventListener("change", () => {
  if (sumRaw && !sumStreaming) hintRegen();
});

function hintRegen() {
  setSumStatus("设置已改，点「重新生成」生效（当前显示的仍是上一次的结果）");
}

$("#sum-regen").addEventListener("click", () => {
  if (sumTaskId && !sumStreaming) attachSummary(sumTaskId, true);
});

$("#sum-copy").addEventListener("click", async () => {
  if (!sumRaw) return;
  try {
    await navigator.clipboard.writeText(sumRaw);
    flash("总结已复制到剪贴板");
  } catch (_) {
    flash("复制失败，请手动选中复制");
  }
});

// 加载模板列表
(async function initTemplates() {
  try {
    const res = await fetch("/api/templates");
    const data = await res.json();
    const sel = $("#sum-template");
    sel.innerHTML = "";
    (data.templates || []).forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t.id;
      opt.textContent = `${t.name} · ${t.desc}`;
      sel.appendChild(opt);
    });
    sel.value = "A";
  } catch (_) {}
})();

// 打开总结面板。三种情况，顺序不能乱：
//   ① 后台已经有作业在跑 → 直接挂上去看（**不要**重开一个，那是白烧一次钱）
//   ② 没有作业、但磁盘上有总结 → 读文件显示
//   ③ 都没有 → 起一个新作业
async function openSummary(id) {
  sumTaskId = id;
  sumMode = "task";
  detachSummary();
  resetSumView();
  setSumToolsVisible(true);          // 任务这条路是可以重跑的，按钮要露出来
  sumMediaUrl = `/api/tasks/${id}/media`;
  $("#sum-custom-wrap").classList.toggle("hidden", $("#sum-template").value !== "D");

  const title = listEl.querySelector(`[data-id="${id}"] .task-title`)?.textContent || "总结";
  $("#sum-title").textContent = title;
  $("#sum-modal").classList.remove("hidden");
  setSumStatus("读取状态…");

  let state = null;
  try {
    const res = await fetch(`/api/tasks/${id}/summary/status`);
    if (res.ok) state = await res.json();
  } catch (_) {}
  if (sumTaskId !== id) return;     // 读状态期间用户又切走了

  if (state && (state.status === "running" || state.status === "queued")) {
    setSumStatus("接上后台正在跑的总结…");
    attachSummary(id);
    return;
  }

  setSumStatus("读取已保存的总结…");
  try {
    const res = await fetch(`/api/tasks/${id}/summary`);
    if (res.ok) {
      const data = await res.json();
      if (sumTaskId !== id) return;
      if (data.exists && data.content) {
        sumRaw = stripFrontMatter(data.content);
        renderSummary();
        if (data.outline) renderOutlineMarkdown(data.outline);
        const t = state || {};
        setSumStatus(`已加载保存的总结（${sumRaw.length} 字）· 点「重新生成」可重跑`
          + (t.status === "cancelled" ? " · 上次是中途停的" : ""));
        return;
      }
    }
  } catch (_) {}

  if (sumTaskId !== id) return;
  if (state && state.status === "error") {
    showSumWarn(`上次总结失败：${state.error || "未知错误"}`);
  }
  attachSummary(id);
}

// 通用的"把面板清空到待命状态"
function resetSumView() {
  sumRaw = "";
  sumHasMedia = false;
  sumMediaUrl = null;
  $("#sum-body").innerHTML = "";
  $("#sum-warn").classList.add("hidden");
  $("#sum-player").classList.add("hidden");
  $("#sum-video").removeAttribute("src");
  $("#sum-custom-wrap").classList.toggle("hidden", $("#sum-template").value !== "D");
  resetThink();
  resetOutline();
  hideSumProgress();
}

// 骨架速览是 markdown 列表（- [HH:MM:SS] **标题** —— 主旨），复用同一套 inline 渲染，
// 这样时间戳照样可点跳转。
function renderOutlineMarkdown(md) {
  const html = md.split("\n").filter((x) => x.trim()).map((line) => {
    const m = line.match(/^[-*]\s+(.*)$/);
    return `<div class="outline-row">${inline(m ? m[1] : line)}</div>`;
  }).join("");
  setOutline(html);
}

function stripFrontMatter(md) {
  return md.replace(/^---\r?\n[\s\S]*?\r?\n---[ \t]*\r?\n?/, "").replace(/^\n+/, "");
}

/**
 * 接上（或新建）一个总结作业，并订阅它。
 *
 * 这里是"关闭 ≠ 停止"的落点：`detachSummary()` 断的是**这一条连接**，
 * 服务端作业完全不知道也不关心。断线重连靠服务端的回放，不靠前端缓存。
 */
async function attachSummary(id, restart) {
  detachSummary();
  const session = sumSession;
  sumRaw = "";
  sumHasMedia = false;
  sumMediaUrl = `/api/tasks/${id}/media`;
  $("#sum-body").innerHTML = "";
  $("#sum-warn").classList.add("hidden");
  $("#sum-player").classList.add("hidden");
  resetThink();
  resetOutline();
  hideSumProgress();
  $("#sum-regen").disabled = true;
  setSumStopVisible(true);
  setSumStatus(restart ? "重新生成…" : "连接中…");

  const ac = new AbortController();
  sumAbort = ac;
  sumStreaming = true;

  const body = {
    template: $("#sum-template").value || "A",
    custom_prompt: $("#sum-custom").value || "",
    with_timestamp: $("#sum-ts").checked,
    restart: !!restart,
  };

  try {
    const res = await fetch(`/api/tasks/${id}/summarize`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ac.signal,
    });
    if (sumSession !== session) return;
    if (!res.ok) {
      let msg = `请求失败（${res.status}）`;
      try { msg = (await res.json()).detail || msg; } catch (_) {}
      setSumStatus("");
      showSumWarn(msg);
      return;
    }
    await readSSE(res, session);
  } catch (e) {
    if (e && e.name === "AbortError") return;      // 主动断开，不是错误
    if (sumSession !== session) return;
    setSumStatus("");
    showSumWarn(`请求出错：${e.message}`);
  } finally {
    // 只有"还是同一个会话"时才收尾——否则会把新会话的状态踩掉
    if (sumSession === session) {
      sumStreaming = false;
      sumAbort = null;
      $("#sum-regen").disabled = false;
      setSumStopVisible(false);
      flushRender(true);
      refresh();
    }
  }
}

async function readSSE(res, session) {
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    if (sumSession !== session) { try { reader.cancel(); } catch (_) {} return; }
    buf += decoder.decode(value, { stream: true });

    let idx;
    while ((idx = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      handleSSE(block, session);
    }
  }
}

function handleSSE(block, session) {
  if (session !== undefined && session !== sumSession) return;
  let event = "message";
  const dataLines = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  });
  if (!dataLines.length) return;
  let payload = {};
  try { payload = JSON.parse(dataLines.join("\n")); } catch (_) { return; }

  if (event === "start") {
    setSumStatus(`生成中 · ${payload.model} · 模板 ${payload.template}`);
  } else if (event === "meta") {
    setSumStatus(`生成中 · 文稿 ${payload.chars} 字 / ${payload.segments} 段 / 时长 ${payload.duration}`);
  } else if (event === "plan") {
    // 长度评估：开工前就把"这份稿子会怎么处理、要等多久、大概出多少字"讲清楚
    setSumStatus(payload.note || "");
  } else if (event === "stage") {
    if (payload.stage === "outline") {
      setSumStatus("生成骨架（先把全文切成几节）…");
    } else if (payload.stage === "resume") {
      setSumStatus(payload.title || "接着上次继续…");
    } else if (payload.stage === "section") {
      setSumProgress(payload.index, payload.total,
        `第 ${payload.index}/${payload.total} 节 · ${payload.title}`
        + (payload.reused ? "（用上次的，跳过）" : ""));
    }
  } else if (event === "outline_delta") {
    // 骨架很短，流式原样铺出来，用户能看见"它在分段"而不是干等
    sumOutlineRaw += payload.text || "";
    setOutline(`<pre class="outline-live">${escapeHtml(sumOutlineRaw)}</pre>`);
    $("#sum-outline-label").textContent = "正在生成骨架…";
  } else if (event === "outline") {
    $("#sum-outline-label").textContent = `速览（骨架）· ${(payload.sections || []).length} 节`;
    if (payload.markdown) renderOutlineMarkdown(payload.markdown);
  } else if (event === "warn") {
    showSumWarn(payload.message || "");
  } else if (event === "reasoning") {
    // 推理模型会先吐几十秒的思考过程——必须显示，否则用户只看到空白
    sumThink += payload.text || "";
    $("#sum-think").classList.remove("hidden");
    $("#sum-think-label").textContent = `模型正在思考…（${sumThink.length} 字）`;
    if (sumThinkTimer) return;
    sumThinkTimer = setTimeout(() => { sumThinkTimer = null; renderThink(); }, 150);
  } else if (event === "delta") {
    // 正文开始了 → 思考区收起，把位置让给正文
    if (sumThink && !sumRaw) {
      $("#sum-think-label").textContent = `模型思考过程（${sumThink.length} 字，点开看）`;
      $("#sum-think-body").classList.add("hidden");
      sumThinkOpen = false;
      $("#sum-think-caret").textContent = "▸";
    }
    sumRaw += payload.text || "";
    scheduleRender();
  } else if (event === "done") {
    sumHasMedia = !!payload.has_media;
    flushRender(true);
    hideSumProgress();
    setSumStopVisible(false);
    const thinkNote = payload.reasoning_chars
      ? ` · 思考 ${payload.reasoning_chars} 字` : "";
    if (payload.outline) renderOutlineMarkdown(payload.outline);
    if (payload.outline) $("#sum-outline-label").textContent = "速览（骨架）";
    if (payload.truncated) {
      setSumStatus(`已保存：${payload.path}${thinkNote}`);
      showSumWarn("⚠ 已达输出上限（finish_reason = length），总结可能没写完。"
        + "注意：推理模型的 max_tokens 是「思考 + 正文」共用的，可在设置里调大「最大输出 tokens」。");
    } else {
      setSumStatus(`已保存：${payload.path}${thinkNote}${qualityNote(payload.quality)}`
        + (sumHasMedia ? " · 点时间戳可跳转播放" : ""));
    }
  } else if (event === "error") {
    setSumStatus("");
    hideSumProgress();
    setSumStopVisible(false);
    showSumWarn(payload.message || "未知错误");
  } else if (event === "cancelled") {
    // 后台作业被停掉（可能是这里点的，也可能是另一个窗口点的）
    hideSumProgress();
    setSumStopVisible(false);
    setSumStatus(sumRaw ? `已停止，当前已有 ${sumRaw.length} 字` : "已停止");
    showSumWarn(payload.message || "总结已停止。");
  }
}

function showSumWarn(msg) {
  const el = $("#sum-warn");
  el.textContent = msg;
  el.classList.remove("hidden");
}

function scheduleRender() {
  if (sumRenderTimer) return;
  sumRenderTimer = setTimeout(() => { sumRenderTimer = null; renderSummary(); }, 120);
}

function flushRender(force) {
  if (sumRenderTimer) { clearTimeout(sumRenderTimer); sumRenderTimer = null; }
  if (force || sumRaw) renderSummary();
}

function renderSummary() {
  const el = $("#sum-body");
  el.innerHTML = renderMarkdown(sumRaw);
  if (!sumStreaming) return;
  // 打字光标
  const cursor = document.createElement("span");
  cursor.className = "cursor";
  el.appendChild(cursor);
  el.scrollTop = el.scrollHeight;
}

// ---------- 极简 Markdown 渲染（够用即可，零依赖） ----------
function renderMarkdown(md) {
  const lines = md.split("\n");
  let html = "";
  let inList = false;

  const closeList = () => { if (inList) { html += "</ul>"; inList = false; } };

  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    const t = line.trim();

    if (!t) { closeList(); continue; }

    const h = t.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      closeList();
      const lvl = Math.min(6, h[1].length + 1); // # → h2，避免和面板标题抢层级
      html += `<h${lvl}>${inline(h[2])}</h${lvl}>`;
      continue;
    }

    const li = t.match(/^[-*+]\s+(.*)$/);
    if (li) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += `<li>${inline(li[1])}</li>`;
      continue;
    }

    const ol = t.match(/^\d+[.)]\s+(.*)$/);
    if (ol) {
      if (!inList) { html += "<ul class=\"ordered\">"; inList = true; }
      html += `<li>${inline(ol[1])}</li>`;
      continue;
    }

    if (/^(---|\*\*\*|___)$/.test(t)) { closeList(); html += "<hr>"; continue; }

    closeList();
    html += `<p>${inline(t)}</p>`;
  }
  closeList();
  return html;
}

function inline(s) {
  let out = escapeHtml(s);
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  // 时间戳 → 可点击（跳回视频）
  out = out.replace(/\[(\d{1,2}):(\d{2}):(\d{2})\]/g, (_m, h, m, sec) => {
    const total = parseInt(h, 10) * 3600 + parseInt(m, 10) * 60 + parseInt(sec, 10);
    return `<span class="ts" data-sec="${total}" title="跳到视频这一处">[${h}:${m}:${sec}]</span>`;
  });
  // 原话引用
  out = out.replace(/「([^」]*)」/g, '<span class="orig">「$1」</span>');
  return out;
}

// 点时间戳 → 跳转
$("#sum-body").addEventListener("click", (e) => {
  const ts = e.target.closest(".ts");
  if (!ts) return;
  seekTo(parseInt(ts.dataset.sec, 10) || 0);
});

async function seekTo(sec) {
  // 有可播的媒体就跳媒体。地址来自哪条路（任务 / 已有文稿）由 sumMediaUrl 决定——
  // 这里不再自己拼 URL，否则两条路会各写一份。
  if (sumHasMedia && sumMediaUrl) {
    const v = $("#sum-video");
    $("#sum-player").classList.remove("hidden");
    const apply = () => {
      try { v.currentTime = sec; v.play(); } catch (_) {}
    };
    if (!v.getAttribute("src")) {
      v.setAttribute("src", sumMediaUrl);
      v.addEventListener("loadedmetadata", apply, { once: true });
    } else if (v.readyState >= 1) {
      apply();
    } else {
      v.addEventListener("loadedmetadata", apply, { once: true });
    }
    return;
  }

  // 没有本地媒体（如「仅转写」模式）→ 跳 B 站原页面对应时间
  if (sumTaskId) {
    try {
      const res = await fetch(`/api/tasks/${sumTaskId}`);
      const t = await res.json();
      if (t.kind === "url" && t.url) {
        const sep = t.url.includes("?") ? "&" : "?";
        window.open(`${t.url}${sep}t=${sec}`, "_blank");
        return;
      }
    } catch (_) {}
  }
  flash("这个任务没有可跳转的视频文件");
}

// ================================================================
// 设置
// ================================================================

let settingsCache = null;

$("#btn-settings").addEventListener("click", openSettings);
$("#set-close").addEventListener("click", () => $("#set-modal").classList.add("hidden"));
$("#set-modal").addEventListener("click", (e) => { if (e.target === $("#set-modal")) $("#set-modal").classList.add("hidden"); });

async function openSettings() {
  $("#set-modal").classList.remove("hidden");
  $("#set-msg").textContent = "";
  try {
    const res = await fetch("/api/settings");
    const cfg = await res.json();
    settingsCache = cfg;
    $("#set-base").value = cfg.llm.base_url || "";
    $("#set-model").value = cfg.llm.model || "";
    $("#set-maxtok").value = cfg.llm.max_tokens || 8000;
    $("#set-concurrent").value = (cfg.summarize && cfg.summarize.max_concurrent) || 3;
    $("#set-thinking").checked = cfg.llm.thinking_enabled !== false;
    $("#set-key").value = "";
    if (cfg.llm.api_key_from_env) {
      $("#set-key-note").textContent = " · 已由环境变量 BILI_NOTE_API_KEY 提供";
    } else if (cfg.llm.api_key_set) {
      $("#set-key-note").textContent = ` · 当前：${cfg.llm.api_key}`;
    } else {
      $("#set-key-note").textContent = " · 尚未配置";
    }
  } catch (e) {
    $("#set-msg").textContent = `读取设置失败：${e.message}`;
  }
}

// 收集要提交的字段：Key 留空 = 不修改（绝不能用打码值覆盖真 Key）
function collectSettings() {
  const llm = {
    base_url: $("#set-base").value.trim(),
    model: $("#set-model").value.trim(),
    max_tokens: parseInt($("#set-maxtok").value, 10) || 8000,
    thinking_enabled: $("#set-thinking").checked,
  };
  const key = $("#set-key").value.trim();
  if (key) llm.api_key = key;
  return {
    llm,
    summarize: { max_concurrent: Math.max(1, parseInt($("#set-concurrent").value, 10) || 3) },
  };
}

$("#set-save").addEventListener("click", async () => {
  $("#set-msg").textContent = "保存中…";
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings()),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const cfg = await res.json();
    $("#set-key").value = "";
    $("#set-key-note").textContent = cfg.llm.api_key_set ? ` · 当前：${cfg.llm.api_key}` : " · 尚未配置";
    $("#set-msg").textContent = "已保存 ✓";
    flash("设置已保存");
  } catch (e) {
    $("#set-msg").textContent = `保存失败：${e.message}`;
  }
});

$("#set-test").addEventListener("click", async () => {
  $("#set-msg").textContent = "测试中…";
  // 先存再测，让测试用上刚填的值
  try {
    await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings()),
    });
  } catch (_) {}
  try {
    const res = await fetch("/api/models");
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const models = data.models || [];
    const dl = $("#model-list");
    dl.innerHTML = "";
    models.forEach((m) => {
      const o = document.createElement("option");
      o.value = m;
      dl.appendChild(o);
    });
    $("#set-msg").textContent = models.length
      ? `连接成功 ✓ 可用模型 ${models.length} 个（点「模型」输入框可见列表）`
      : "连接成功 ✓ 但没返回模型列表，请手动填写模型名";
  } catch (e) {
    $("#set-msg").textContent = `连接失败：${e.message}`;
  }
});

// ================================================================
// 顶栏：打开本地输出目录
// ================================================================

$("#btn-open-out").addEventListener("click", async () => {
  const btn = $("#btn-open-out");
  btn.disabled = true;                       // 连点会弹一堆资源管理器窗口
  setTimeout(() => { btn.disabled = false; }, 800);
  try {
    const res = await fetch("/api/open-output", { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    flash(`已打开输出目录：${data.path}`);
  } catch (e) {
    flash(`打开输出目录失败：${e.message}`);
  }
});

// ================================================================
// 版本号（唯一来源是 app/__init__.py，这里只管显示）
// ================================================================

(async function initVersion() {
  try {
    const res = await fetch("/api/version");
    if (!res.ok) return;
    const data = await res.json();
    if (data.version) {
      const v = String(data.version).replace(/\.0$/, "");  // 2.0.0 → v2.0
      $("#brand-ver").textContent = `v${v}`;
      $("#brand-ver").title = `bili-note ${data.version}`;
    }
  } catch (_) { /* 拿不到就不显示，别在界面上留个空壳 */ }
})();

// ---------- 工具 ----------
function flash(msg) {
  $("#hint").textContent = msg;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

setInterval(refresh, 1500);
refresh();

// 已有文稿清单：打开页面就扫一遍，之后低频轮询（10 秒）——
// 转写完成、总结落盘都会让这份清单变样，而它跟任务进度不是一回事，不必 1.5 秒一次。
setInterval(refreshLibrary, 10000);
refreshLibrary();
