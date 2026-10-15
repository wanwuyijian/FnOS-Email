/* 邮件定时助手 前端逻辑 */
"use strict";

/* ---------- 基础工具 ---------- */
const $ = (s, el) => (el || document).querySelector(s);
const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtSize = n => n >= 1073741824 ? (n/1073741824).toFixed(1)+" GB"
  : n >= 1048576 ? (n/1048576).toFixed(1)+" MB"
  : n >= 1024 ? (n/1024).toFixed(1)+" KB" : n+" B";

let TOKEN = localStorage.getItem("fnmail_token") || "";

function toast(msg, ok = true) {
  const d = document.createElement("div");
  d.className = "toast " + (ok ? "ok" : "err");
  d.textContent = msg;
  $("#toasts").appendChild(d);
  setTimeout(() => d.remove(), ok ? 3200 : 5200);
}

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (TOKEN) opt.headers["Authorization"] = "Bearer " + TOKEN;
  if (body !== undefined) {
    if (body instanceof Blob || body instanceof ArrayBuffer) {
      opt.body = body;
    } else {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
  }
  const res = await fetch(url, opt);
  if (res.status === 401) { showLogin(); throw new Error("未登录"); }
  const ct = res.headers.get("Content-Type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok || (data && data.ok === false)) {
    throw new Error((data && data.error) || ("HTTP " + res.status));
  }
  return data;
}
const GET = u => api("GET", u);
const POST = (u, b) => api("POST", u, b);
const DEL = u => api("DELETE", u);

/* ---------- 登录 ---------- */
async function showLogin() {
  TOKEN = "";
  localStorage.removeItem("fnmail_token");
  const st = await GET("/api/auth/state").catch(() => null);
  const mask = $("#login-mask");
  mask.style.display = "flex";
  const initialized = st && st.data && st.data.initialized;
  $("#login-lbl").textContent = initialized ? "管理密码" : "首次使用,请设置管理密码(≥6位)";
  $("#login-btn").textContent = initialized ? "进入" : "保存并进入";
  $("#login-hint").textContent = "";
  $("#login-form").onsubmit = async e => {
    e.preventDefault();
    const pwd = $("#login-pwd").value;
    try {
      const r = initialized ? await POST("/api/auth/login", {password: pwd})
                             : await POST("/api/auth/setup", {password: pwd});
      TOKEN = r.data.token;
      localStorage.setItem("fnmail_token", TOKEN);
      mask.style.display = "none";
      $("#login-pwd").value = "";
      boot();
    } catch (err) { $("#login-hint").textContent = err.message; }
  };

  /* 重置数据（忘记密码自救）：点击 -> 输入重置码 -> 清空数据并关闭应用 */
  const resetBox = $("#reset-box"), resetCode = $("#reset-code");
  resetBox.style.display = "none";
  resetCode.value = "";
  $("#reset-link").onclick = () => {
    const show = resetBox.style.display === "none";
    resetBox.style.display = show ? "block" : "none";
    if (show) resetCode.focus();
  };
  $("#reset-go").onclick = async () => {
    const code = (resetCode.value || "").trim();
    if (!code) return toast("请输入重置码", false);
    if (!confirm("确定重置应用数据吗？\n\n将清空邮箱账号、定时任务与发送历史，并关闭应用。此操作不可恢复！")) return;
    const btn = $("#reset-go");
    btn.disabled = true; btn.textContent = "重置中…";
    try {
      const r = await POST("/api/auth/reset", { code });
      TOKEN = "";
      localStorage.removeItem("fnmail_token");
      $("#login-hint").textContent = (r.data && r.data.message) ||
        "数据已重置, 应用正在关闭。请重新打开应用设置新的管理密码。";
      $("#login-pwd").disabled = true;
      $("#login-btn").disabled = true;
      btn.textContent = "应用已关闭";
      toast("数据已重置, 应用即将关闭");
    } catch (e) {
      toast(e.message, false);
      btn.disabled = false; btn.textContent = "确认重置并关闭应用";
    }
  };
}

/* ---------- 导航 ---------- */
const PAGES = { overview: "概览", compose: "写邮件", inbox: "收件箱",
                tasks: "定时任务", accounts: "邮箱账号", system: "系统" };
let currentPage = "overview";

$("#nav").addEventListener("click", e => {
  const a = e.target.closest("a[data-page]");
  if (!a) return;
  $$("nav a").forEach(x => x.classList.remove("active"));
  a.classList.add("active");
  currentPage = a.dataset.page;
  render();
});

$("#btn-logout").onclick = async () => {
  try { await POST("/api/auth/logout"); } catch (_) {}
  showLogin();
};

/* ---------- 页面渲染 ---------- */
async function render() {
  $("#page-title").textContent = PAGES[currentPage];
  const c = $("#content");
  c.innerHTML = '<div class="loading">加载中…</div>';
  try {
    const fn = { overview: pgOverview, compose: pgCompose, inbox: pgInbox,
                 tasks: pgTasks, accounts: pgAccounts, system: pgSystem }[currentPage];
    await fn(c);
  } catch (err) {
    c.innerHTML = `<div class="card"><div class="empty">⚠️ ${esc(err.message)}</div></div>`;
  }
}

/* ---------- 概览 ---------- */
async function pgOverview(c) {
  const [st, history] = await Promise.all([GET("/api/status"), GET("/api/history")]);
  const s = st.data;
  const tasks = await GET("/api/tasks");
  const recent = [];
  Object.entries(history.data).forEach(([tid, lst]) => lst.forEach(h => recent.push({...h, tid})));
  recent.sort((a, b) => (a.time < b.time ? 1 : -1));
  const up = s.uptime, upStr = `${Math.floor(up/86400)}天 ${Math.floor(up%86400/3600)}时${Math.floor(up%3600/60)}分`;
  c.innerHTML = `
  <div class="grid">
    <div class="stat"><div class="num">${s.accounts}</div><div class="lbl">邮箱账号</div></div>
    <div class="stat"><div class="num">${s.tasks_enabled}/${s.tasks}</div><div class="lbl">启用任务 / 总任务</div></div>
    <div class="stat"><div class="num">${upStr}</div><div class="lbl">运行时长</div></div>
  </div>
  <div class="card" style="margin-top:14px">
    <h3>即将执行</h3>
    <table><tbody>
      ${(() => {
        const en = tasks.data.filter(t => t.enabled && t.next_run)
          .sort((a, b) => (a.next_run || "") > (b.next_run || "") ? 1 : -1).slice(0, 6);
        if (!en.length) return `<tr><td class="empty">暂无启用的任务</td></tr>`;
        return en.map(t => `<tr>
          <td>${esc(t.name)}</td>
          <td class="mono">${esc(t.next_run)}</td>
          <td>${esc((t.to || []).join(", "))}</td>
          <td class="actions"><button class="small" onclick="runTask('${t.id}')">立即运行</button></td>
        </tr>`).join("");
      })()}
    </tbody></table>
  </div>
  <div class="card">
    <h3>最近发送记录</h3>
    ${recent.length ? `<table>
      <thead><tr><th>时间</th><th>任务</th><th>状态</th><th>收件人</th><th>说明</th></tr></thead>
      <tbody>${recent.slice(0, 10).map(h => {
        const t = tasks.data.find(x => x.id === h.tid);
        return `<tr><td class="mono">${esc(h.time)}</td><td>${esc(t ? t.name : h.tid)}</td>
          <td>${h.ok ? '<span class="tag ok">成功</span>' : '<span class="tag err">失败</span>'}</td>
          <td>${esc((h.to || []).join(", "))}</td>
          <td class="mono">${esc(h.error || (h.files || []).map(f => f.split("/").pop()).join(", "))}</td></tr>`;
      }).join("")}</tbody></table>` : `<div class="empty">暂无发送记录</div>`}
  </div>`;
}

window.runTask = async id => {
  try { const r = await POST(`/api/tasks/${id}/run`); toast(r.data.message || "已触发"); }
  catch (e) { toast(e.message, false); }
  setTimeout(render, 800);
};

/* ---------- 写邮件 ---------- */
async function pgCompose(c) {
  const accounts = (await GET("/api/accounts")).data;
  if (!accounts.length) {
    c.innerHTML = `<div class="card"><div class="empty">
      尚未配置邮箱账号, 请先到 <b>邮箱账号</b> 页添加发件邮箱</div></div>`;
    return;
  }
  c.innerHTML = `
  <div class="card">
    <label class="field"><span>发件账号</span>
      <select id="cm-acc">${accounts.map(a =>
        `<option value="${a.id}">${esc(a.name || a.username)} (${esc(a.username)})</option>`).join("")}
      </select></label>
    <label class="field"><span>收件人 (多个用逗号/分号分隔)</span>
      <input type="text" id="cm-to" placeholder="a@163.com, b@qq.com"></label>
    <label class="field"><span>抄送 (可选)</span>
      <input type="text" id="cm-cc"></label>
    <label class="field"><span>主题</span>
      <input type="text" id="cm-subject"></label>
    <label class="field"><span>正文</span>
      <textarea id="cm-body" rows="7"></textarea></label>
    <label class="field"><span>附件 (本地文件 / 目录 / 通配符, 或上传)</span>
      <div class="row">
        <input type="text" id="cm-att-path" placeholder="/vol1/xxx/文件 或 /vol1/backup/**">
        <button style="flex:none" id="cm-att-browse">浏览</button>
        <button style="flex:none" id="cm-att-add">添加</button>
      </div>
      <div class="chips" id="cm-chips"></div>
      <div class="row" style="margin-top:8px">
        <input type="file" id="cm-att-file" multiple style="padding:6px">
        <button style="flex:none" id="cm-att-upload">上传附件</button>
      </div></label>
    <button class="primary" id="cm-send">立即发送</button>
  </div>`;

  const chips = [];
  const drawChips = () => {
    $("#cm-chips").innerHTML = chips.map((p, i) =>
      `<span class="chip"><span class="nm mono">${esc(p)}</span>
       <button class="x" data-i="${i}">×</button></span>`).join("");
    $$("#cm-chips .x").forEach(b => b.onclick = () => {
      chips.splice(+b.dataset.i, 1); drawChips();
    });
  };
  $("#cm-att-add").onclick = () => {
    const v = $("#cm-att-path").value.trim();
    if (!v) return;
    chips.push(v); $("#cm-att-path").value = ""; drawChips();
  };
  $("#cm-att-browse").onclick = () => fileBrowser(path => {
    chips.push(path); drawChips();
  });
  $("#cm-att-upload").onclick = async () => {
    const files = $("#cm-att-file").files;
    if (!files.length) return toast("请先选择文件", false);
    for (const f of files) {
      try {
        const r = await POST(`/api/upload?name=${encodeURIComponent(f.name)}`, f);
        chips.push(r.data.path); toast("已上传: " + f.name);
      } catch (e) { toast("上传失败: " + e.message, false); }
    }
    $("#cm-att-file").value = "";
    drawChips();
  };
  $("#cm-send").onclick = async () => {
    const btn = $("#cm-send");
    btn.disabled = true; btn.textContent = "发送中…";
    try {
      const r = await POST("/api/send", {
        account_id: $("#cm-acc").value,
        to: $("#cm-to").value, cc: $("#cm-cc").value,
        subject: $("#cm-subject").value, body: $("#cm-body").value,
        attachments: chips,
      });
      toast(`发送成功 → ${r.data.to.join(", ")}`);
      $("#cm-to").value = $("#cm-cc").value = $("#cm-subject").value = $("#cm-body").value = "";
      chips.length = 0; drawChips();
    } catch (e) { toast("发送失败: " + e.message, false); }
    btn.disabled = false; btn.textContent = "立即发送";
  };
}

/* ---------- 收件箱 ---------- */
async function pgInbox(c) {
  const accounts = (await GET("/api/accounts")).data;
  if (!accounts.length) {
    c.innerHTML = `<div class="card"><div class="empty">请先添加邮箱账号</div></div>`;
    return;
  }
  c.innerHTML = `
  <div class="toolbar">
    <select id="ib-acc" style="width:220px">${accounts.map(a =>
      `<option value="${a.id}" data-proto="${esc(a.protocol)}">${esc(a.name || a.username)}</option>`).join("")}</select>
    <select id="ib-folder" style="width:150px"><option>INBOX</option></select>
    <select id="ib-limit" style="width:110px">
      <option value="10">最近10封</option><option value="20" selected>最近20封</option>
      <option value="50">最近50封</option></select>
    <div class="grow"></div>
    <button id="ib-refresh">刷新</button>
  </div>
  <div id="ib-list"><div class="loading">加载中…</div></div>`;

  const loadFolders = async () => {
    try {
      const r = await GET(`/api/folders?account=${$("#ib-acc").value}`);
      const sel = $("#ib-folder");
      sel.innerHTML = (r.data.folders || ["INBOX"]).map(f =>
        `<option>${esc(f)}</option>`).join("");
    } catch (_) {}
  };
  const loadMails = async () => {
    $("#ib-list").innerHTML = '<div class="loading">收取中…(依网络而定)</div>';
    try {
      const acc = accounts.find(a => a.id === $("#ib-acc").value);
      const proto = (acc.protocol || "imap").toLowerCase();
      const r = await GET(`/api/inbox?account=${$("#ib-acc").value}` +
        `&proto=${proto}&folder=${encodeURIComponent($("#ib-folder").value)}&limit=${$("#ib-limit").value}`);
      const mails = r.data.mails || [];
      if (!mails.length) { $("#ib-list").innerHTML = '<div class="empty">收件箱为空</div>'; return; }
      $("#ib-list").innerHTML = mails.map((m, i) => `
        <div class="mail-item">
          <div class="mail-head" data-i="${i}">
            <div class="subj">${esc(m.subject || "(无主题)")}</div>
            <div class="from"><span>📧 ${esc(m.from)}</span><span>${esc(m.date)}</span></div>
          </div>
          <div class="mail-body" style="display:none" id="mb-${i}">
            ${m.html ? `<iframe id="mf-${i}"></iframe>` : `<div>${esc(m.text)}</div>`}
            ${m.attachments && m.attachments.length
              ? `<div class="attach-list">附件: ${m.attachments.map(a =>
                  `${esc(a.filename)} (${fmtSize(a.size)})`).join(" | ")}</div>` : ""}
          </div>
        </div>`).join("");
      $$(".mail-head", $("#ib-list")).forEach(h => h.onclick = () => {
        const i = +h.dataset.i, body = $("#mb-" + i);
        const show = body.style.display === "none";
        body.style.display = show ? "block" : "none";
        if (show && mails[i].html) {
          const ifr = $("#mf-" + i);
          ifr.srcdoc = `<!DOCTYPE html><html><head><meta charset="utf-8">
            <style>body{font:13px/1.7 -apple-system,"Microsoft YaHei",sans-serif;margin:8px;color:#222}</style></head>
            <body>${mails[i].html}</body></html>`;
        }
      });
    } catch (e) {
      $("#ib-list").innerHTML = `<div class="empty">收取失败: ${esc(e.message)}</div>`;
    }
  };
  $("#ib-refresh").onclick = loadMails;
  $("#ib-acc").onchange = () => { loadFolders().then(loadMails); };
  $("#ib-folder").onchange = loadMails;
  $("#ib-limit").onchange = loadMails;
  await loadFolders();
  await loadMails();
}

/* ---------- 定时任务 ---------- */
async function pgTasks(c) {
  const [tasks, accounts] = await Promise.all([
    GET("/api/tasks"), GET("/api/accounts")]);
  c.innerHTML = `
  <div class="toolbar">
    <div class="grow hint">支持: 单次 / 每天 / 每周 / 每月 / 间隔; 错过自动补发; 失败自动重试</div>
    <button class="primary" id="tk-add">新建任务</button>
  </div>
  <div class="card">
    ${tasks.data.length ? `<table>
      <thead><tr><th>名称</th><th>计划</th><th>下次运行</th><th>状态</th>
        <th>最近结果</th><th style="width:200px">操作</th></tr></thead>
      <tbody>${tasks.data.map(t => {
        const sch = describeSchedule(t.schedule);
        const exts = (t.extensions || []).join(", ");
        return `<tr>
          <td><b>${esc(t.name)}</b>${exts ? `<div class="mono hint">类型: ${esc(exts)}</div>` : ""}</td>
          <td>${esc(sch)}</td>
          <td class="mono">${esc(t.next_run || "-")}</td>
          <td>${t.enabled ? '<span class="tag ok">启用</span>' : '<span class="tag off">停用</span>'}</td>
          <td>${t.last_result ? esc(t.last_result) : "-"}
            ${t.last_run ? `<div class="mono hint">${esc(t.last_run)}</div>` : ""}</td>
          <td class="actions">
            <button class="small" onclick="runTask('${t.id}')">运行</button>
            <button class="small" onclick="taskHistory('${t.id}','${esc(t.name)}')">历史</button>
            <button class="small" onclick="editTask('${t.id}')">编辑</button>
            <button class="small danger" onclick="delTask('${t.id}')">删除</button>
          </td></tr>`;
      }).join("")}</tbody></table>` : `<div class="empty">暂无定时任务, 点击右上角新建</div>`}
  </div>`;

  $("#tk-add").onclick = () => editTask(null, accounts.data);
}

function describeSchedule(s) {
  if (!s || !s.type) return "(未设置)";
  const t = s.time || "";
  const wd = ["周一","周二","周三","周四","周五","周六","周日"];
  switch (s.type) {
    case "once": return "单次 " + (s.datetime || "").replace("T", " ");
    case "daily": return "每天 " + t;
    case "weekly": return "每" + (wd[+s.weekday] || "") + " " + t;
    case "monthly": return "每月" + (s.day || "?") + "日 " + t;
    case "interval": return "每" + (s.minutes || "?") + "分钟";
  }
  return s.type;
}

window.editTask = async id => {
  const accounts = (await GET("/api/accounts")).data;
  const task = id ? (await GET("/api/tasks")).data.find(t => t.id === id) : null;
  if (id && !task) return;
  const s = (task && task.schedule) || { type: "daily", time: "08:00" };
  const body = `
    <label class="field"><span>任务名称</span>
      <input id="tk-name" value="${esc(task ? task.name : "")}" placeholder="如: 每日报表"></label>
    <label class="field"><span>发件账号</span>
      <select id="tk-acc">${accounts.map(a =>
        `<option value="${a.id}" ${task && task.account_id === a.id ? "selected" : ""}>
         ${esc(a.name || a.username)}</option>`).join("")}</select></label>
    <label class="field"><span>收件人 (多个用逗号/分号分隔)</span>
      <input id="tk-to" value="${esc(task ? (task.to || []).join(", ") : "")}"></label>
    <label class="field"><span>抄送 (可选)</span>
      <input id="tk-cc" value="${esc(task ? (task.cc || []).join(", ") : "")}"></label>
    <label class="field"><span>邮件主题 (支持 {date} {time} {files} 占位)</span>
      <input id="tk-subject" value="${esc(task ? task.subject : "")}" placeholder="日报 {date}"></label>
    <label class="field"><span>正文</span>
      <textarea id="tk-body" rows="5">${esc(task ? task.body : "")}</textarea></label>
    <label class="field"><span>附件 (本地文件 / 目录 / 通配符)</span>
      <div class="row">
        <input id="tk-att-path" placeholder="/vol1/share/report.xlsx">
        <button style="flex:none" id="tk-att-browse">浏览</button>
        <button style="flex:none" id="tk-att-add">添加</button>
      </div>
      <div class="chips" id="tk-chips"></div></label>
    <label class="field"><span>文件类型过滤 (只发送文件夹内这些类型; 留空=全部)</span>
      <input id="tk-ext" value="${esc((task && task.extensions || []).join(", "))}" placeholder=".pdf, .xlsx, .jpg">
      <div class="hint">仅对「文件夹 / 通配符」展开出的文件生效；你单独手动添加的具体文件不受此过滤。</div></label>
    <label class="field"><span>计划类型</span>
      <select id="tk-sch-type">
        ${["once","daily","weekly","monthly","interval"].map(t =>
          `<option value="${t}" ${s.type === t ? "selected" : ""}>${
            {once:"单次",daily:"每天",weekly:"每周",monthly:"每月",interval:"间隔"}[t]}</option>`).join("")}
      </select></label>
    <div id="tk-sch-extra"></div>
    <label class="field" style="margin-top:10px">
      <input type="checkbox" id="tk-zip" ${task && task.zip_attachments ? "checked" : ""}>
      <span style="display:inline"> 多个附件打包为 zip</span></label>
    <label class="field">
      <input type="checkbox" id="tk-catchup" ${!task || task.catch_up !== false ? "checked" : ""}>
      <span style="display:inline"> 关机错过时间后开机补发</span></label>
    <label class="field"><span>失败重试次数 (0-3)</span>
      <input type="number" id="tk-retry" min="0" max="3" value="${task ? (task.retry ?? 1) : 1}"></label>
    <label class="field">
      <input type="checkbox" id="tk-enabled" ${!task || task.enabled ? "checked" : ""}>
      <span style="display:inline"> 立即启用</span></label>`;

  const m = modal(task ? "编辑任务" : "新建任务", body, [
    { txt: "取消" },
    { txt: task ? "保存" : "创建", primary: true, onclick: async () => {
      const attachments = $$("#tk-chips .chip .nm").map(x => x.textContent);
      const sch = { type: $("#tk-sch-type").value };
      if (sch.type === "once") {
        const v = $("#tk-dt").value;
        if (!v) return toast("请选择执行时间", false);
        sch.datetime = v;
      }
      if (["daily","weekly","monthly"].includes(sch.type)) sch.time = $("#tk-time").value || "08:00";
      if (sch.type === "weekly") sch.weekday = +$("#tk-wd").value;
      if (sch.type === "monthly") sch.day = +$("#tk-day").value || 1;
      if (sch.type === "interval") sch.minutes = +$("#tk-int").value || 60;
      try {
        await POST("/api/tasks", {
          id: task ? task.id : undefined,
          name: $("#tk-name").value || "未命名任务",
          account_id: $("#tk-acc").value,
          to: $("#tk-to").value, cc: $("#tk-cc").value,
          subject: $("#tk-subject").value, body: $("#tk-body").value,
          attachments,
          zip_attachments: $("#tk-zip").checked,
          extensions: $("#tk-ext").value,
          schedule: sch,
          catch_up: $("#tk-catchup").checked,
          retry: +$("#tk-retry").value || 0,
          enabled: $("#tk-enabled").checked,
        });
        toast(task ? "已保存" : "已创建");
        m.close(); render();
      } catch (e) { toast(e.message, false); }
    } },
  ]);

  const chips = task ? [...(task.attachments || [])] : [];
  const drawChips = () => {
    $("#tk-chips").innerHTML = chips.map((p, i) =>
      `<span class="chip"><span class="nm mono">${esc(p)}</span>
       <button class="x" data-i="${i}">×</button></span>`).join("");
    $$("#tk-chips .x").forEach(b => b.onclick = () => {
      chips.splice(+b.dataset.i, 1); drawChips();
    });
  };
  drawChips();
  $("#tk-att-add").onclick = () => {
    const v = $("#tk-att-path").value.trim();
    if (v) { chips.push(v); $("#tk-att-path").value = ""; drawChips(); }
  };
  $("#tk-att-browse").onclick = () => fileBrowser(p => { chips.push(p); drawChips(); });

  const schExtra = () => {
    const t = $("#tk-sch-type").value;
    const wd = ["周一","周二","周三","周四","周五","周六","周日"];
    let html = "";
    if (t === "once")
      html = `<label class="field"><span>执行时间</span>
        <input type="datetime-local" id="tk-dt" value="${esc(s.datetime || "")}"></label>`;
    if (["daily","weekly","monthly"].includes(t))
      html = `<label class="field"><span>时间 (HH:MM)</span>
        <input type="time" id="tk-time" value="${esc(s.time || "08:00")}"></label>`;
    if (t === "weekly")
      html += `<label class="field"><span>星期</span>
        <select id="tk-wd">${wd.map((w, i) =>
          `<option value="${i}" ${+s.weekday === i ? "selected" : ""}>${w}</option>`).join("")}</select></label>`;
    if (t === "monthly")
      html += `<label class="field"><span>几号 (1-28)</span>
        <input type="number" id="tk-day" min="1" max="28" value="${s.day || 1}"></label>`;
    if (t === "interval")
      html = `<label class="field"><span>间隔分钟</span>
        <input type="number" id="tk-int" min="1" value="${s.minutes || 60}"></label>`;
    $("#tk-sch-extra").innerHTML = html;
  };
  $("#tk-sch-type").onchange = schExtra;
  schExtra();
};

window.delTask = async id => {
  if (!confirm("确定删除该任务?")) return;
  try { await DEL(`/api/tasks/${id}`); toast("已删除"); render(); }
  catch (e) { toast(e.message, false); }
};

window.taskHistory = async (id, name) => {
  const r = await GET(`/api/tasks/${id}/history`);
  const rows = (r.data || []).map(h => `<tr>
    <td class="mono">${esc(h.time)}</td>
    <td>${h.trigger === "manual" ? "手动" : h.trigger === "auto-catchup" ? "补发" : "自动"}</td>
    <td>${h.ok ? '<span class="tag ok">成功</span>' : '<span class="tag err">失败</span>'}</td>
    <td class="mono">${esc(h.error || (h.files || []).map(f => f.split("/").pop()).join(", ") || "-")}</td>
  </tr>`).join("");
  modal(`发送历史 - ${name}`,
    rows ? `<table><thead><tr><th>时间</th><th>触发</th><th>结果</th><th>详情</th></tr></thead>
      <tbody>${rows}</tbody></table>` : '<div class="empty">暂无记录</div>',
    [{ txt: "关闭", primary: true }]);
};

/* ---------- 邮箱账号 ---------- */
async function pgAccounts(c) {
  const accounts = (await GET("/api/accounts")).data;
  c.innerHTML = `
  <div class="toolbar">
    <div class="grow hint">163/阿里邮箱使用 <b>授权码</b> 而非登录密码, 见各自官网说明</div>
    <button class="primary" id="acc-add">添加账号</button>
  </div>
  <div class="card">
    ${accounts.length ? `<table>
      <thead><tr><th>名称</th><th>邮箱地址</th><th>预设</th><th>接收协议</th><th style="width:280px">操作</th></tr></thead>
      <tbody>${accounts.map(a => `<tr>
        <td><b>${esc(a.name)}</b></td>
        <td>${esc(a.username)}</td>
        <td>${esc(a.preset)}</td>
        <td>${esc((a.protocol || "imap").toUpperCase())}</td>
        <td class="actions">
          <button class="small" onclick="testAcc('${a.id}','smtp')">测SMTP</button>
          <button class="small" onclick="testAcc('${a.id}','imap')">测IMAP</button>
          <button class="small" onclick="testAcc('${a.id}','pop3')">测POP3</button>
          <button class="small" onclick="editAcc('${a.id}')">编辑</button>
          <button class="small danger" onclick="delAcc('${a.id}')">删除</button>
        </td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">暂无邮箱账号</div>`}
  </div>`;
  $("#acc-add").onclick = () => editAcc(null);
}

window.editAcc = async id => {
  const presets = (await GET("/api/presets")).data;
  const acc = id ? (await GET("/api/accounts")).data.find(a => a.id === id) : null;
  const cur = acc || { preset: "163", smtp_port: 465, smtp_ssl: true,
    imap_port: 993, imap_ssl: true, pop3_port: 995, pop3_ssl: true, protocol: "imap" };
  const p = presets[cur.preset] || presets.custom;

  const body = `
    <label class="field"><span>邮箱类型预设</span>
      <select id="ac-preset">${Object.entries(presets).map(([k, v]) =>
        `<option value="${k}" ${cur.preset === k ? "selected" : ""}>${esc(v.label)}</option>`).join("")}
      </select>
      <div class="hint" id="ac-note">${esc(p.note)}</div></label>
    <label class="field"><span>备注名称</span>
      <input id="ac-name" value="${esc(acc ? acc.name : "")}" placeholder="如: 我的163"></label>
    <label class="field"><span>邮箱地址 / 登录账号</span>
      <input id="ac-user" value="${esc(acc ? acc.username : "")}" placeholder="yourname@163.com"></label>
    <label class="field"><span>${acc ? "授权码 (不变请勿修改)" : "授权码"}</span>
      <input type="password" id="ac-pwd" value="${acc ? "******" : ""}" autocomplete="new-password">
      <div class="hint" id="ac-pwdhint"></div></label>
    <label class="field"><span>发件显示名 (可选)</span>
      <input id="ac-display" value="${esc(acc ? acc.display_name : "")}"></label>
    <label class="field"><span>接收协议</span>
      <select id="ac-proto">
        <option value="imap" ${cur.protocol === "imap" ? "selected" : ""}>IMAP (可收文件夹)</option>
        <option value="pop3" ${cur.protocol === "pop3" ? "selected" : ""}>POP3 (简单收取)</option>
      </select></label>
    <div class="card" style="background:var(--bg3)">
      <h3>SMTP (发件)</h3>
      <label class="field"><span>服务器 / 端口</span>
        <div class="row">
          <input id="ac-smtp-host" value="${esc(cur.smtp_host)}" placeholder="smtp.163.com">
          <input type="number" id="ac-smtp-port" value="${cur.smtp_port}" style="flex:none;width:100px">
        </div></label>
      <label class="field"><input type="checkbox" id="ac-smtp-ssl" ${cur.smtp_ssl ? "checked" : ""}>
        <span style="display:inline"> SSL</span></label>
    </div>
    <div class="card" style="background:var(--bg3)">
      <h3>IMAP</h3>
      <label class="field"><span>服务器 / 端口</span>
        <div class="row">
          <input id="ac-imap-host" value="${esc(cur.imap_host)}" placeholder="imap.163.com">
          <input type="number" id="ac-imap-port" value="${cur.imap_port}" style="flex:none;width:100px">
        </div></label>
      <label class="field"><input type="checkbox" id="ac-imap-ssl" ${cur.imap_ssl ? "checked" : ""}>
        <span style="display:inline"> SSL</span></label>
    </div>
    <div class="card" style="background:var(--bg3)">
      <h3>POP3</h3>
      <label class="field"><span>服务器 / 端口</span>
        <div class="row">
          <input id="ac-pop-host" value="${esc(cur.pop3_host)}" placeholder="pop.163.com">
          <input type="number" id="ac-pop-port" value="${cur.pop3_port}" style="flex:none;width:100px">
        </div></label>
      <label class="field"><input type="checkbox" id="ac-pop-ssl" ${cur.pop3_ssl ? "checked" : ""}>
        <span style="display:inline"> SSL</span></label>
    </div>`;

  const m = modal(acc ? "编辑账号" : "添加账号", body, [
    { txt: "取消" },
    { txt: acc ? "保存" : "添加", primary: true, onclick: async () => {
      const payload = {
        id: acc ? acc.id : undefined,
        name: $("#ac-name").value, username: $("#ac-user").value,
        password: $("#ac-pwd").value, display_name: $("#ac-display").value,
        preset: $("#ac-preset").value, protocol: $("#ac-proto").value,
        smtp_host: $("#ac-smtp-host").value, smtp_port: +$("#ac-smtp-port").value,
        smtp_ssl: $("#ac-smtp-ssl").checked,
        imap_host: $("#ac-imap-host").value, imap_port: +$("#ac-imap-port").value,
        imap_ssl: $("#ac-imap-ssl").checked,
        pop3_host: $("#ac-pop-host").value, pop3_port: +$("#ac-pop-port").value,
        pop3_ssl: $("#ac-pop-ssl").checked,
      };
      try {
        await POST("/api/accounts", payload);
        toast(acc ? "已保存" : "已添加"); m.close(); render();
      } catch (e) { toast(e.message, false); }
    } },
  ]);

  // 预设切换自动填充
  const applyPreset = () => {
    const pr = presets[$("#ac-preset").value] || presets.custom;
    if (!acc || !$("#ac-smtp-host").value) {
      $("#ac-smtp-host").value = pr.smtp.host; $("#ac-smtp-port").value = pr.smtp.port;
      $("#ac-imap-host").value = pr.imap.host; $("#ac-imap-port").value = pr.imap.port;
      $("#ac-pop-host").value = pr.pop3.host; $("#ac-pop-port").value = pr.pop3.port;
    }
    $("#ac-note").textContent = pr.note;
    const pwdHints = {
      "163": "163: 网页版设置 → POP3/SMTP/IMAP → 开启服务 → 新增授权码",
      "aliyun": "阿里个人邮箱: 邮箱设置 → 账户安全/POP3设置 → 开启并生成授权码",
      "aliyun-qiye": "阿里企业邮箱: 管理后台或个人设置中开启 IMAP/POP3/SMTP",
      "custom": "填写邮箱服务商的授权码或密码",
    };
    $("#ac-pwdhint").textContent = pwdHints[$("#ac-preset").value] || "";
  };
  $("#ac-preset").onchange = applyPreset;
  applyPreset();
};

window.delAcc = async id => {
  if (!confirm("删除账号会停用其关联的定时任务, 确定删除?")) return;
  try { await DEL(`/api/accounts/${id}`); toast("已删除"); render(); }
  catch (e) { toast(e.message, false); }
};

window.testAcc = async (id, what) => {
  toast("测试中…");
  try {
    const r = await POST("/api/accounts/test", { id, what });
    toast((r.data && r.data.msg) || "测试成功");
  } catch (e) { toast(e.message, false); }
};

/* ---------- 系统 ---------- */
async function pgSystem(c) {
  const v = (await GET("/api/version")).data;
  c.innerHTML = `
  <div class="card">
    <h3>应用信息</h3>
    <table><tbody>
      <tr><td>应用名称</td><td>fn-mail-assistant (邮件定时助手)</td></tr>
      <tr><td>版本</td><td>v${esc(v.version)}</td></tr>
      <tr><td>Python</td><td>${esc(v.python)}</td></tr>
      <tr><td>平台</td><td>${esc(v.platform)}</td></tr>
    </tbody></table>
    <div class="hint" style="margin-top:10px">
      升级方式: 下载新版 .fpk → fnOS 应用中心 → 手动安装 → 选择"升级"。数据(账号/任务/历史)自动保留。
    </div>
  </div>
  <div class="card">
    <h3>修改管理密码</h3>
    <label class="field"><span>原密码</span>
      <input type="password" id="pw-old" autocomplete="current-password"></label>
    <label class="field"><span>新密码 (至少6位)</span>
      <input type="password" id="pw-new" autocomplete="new-password"></label>
    <label class="field"><span>确认新密码</span>
      <input type="password" id="pw-new2" autocomplete="new-password"></label>
    <button class="primary" id="pw-save">修改密码</button>
    <div class="hint" style="margin-top:8px">修改后其他设备上的登录会被强制退出，当前窗口保持登录状态。</div>
  </div>
  <div class="card">
    <h3>诊断</h3>
    <div class="row">
      <button id="sys-diag" class="primary">下载诊断包 (发给AI排查用)</button>
      <button id="sys-log">刷新运行日志</button>
    </div>
    <pre class="logview" id="sys-logview" style="margin-top:10px">点击"刷新运行日志"查看</pre>
    <div id="sys-crashes"></div>
  </div>`;

  $("#pw-save").onclick = async () => {
    const o = $("#pw-old").value, n = $("#pw-new").value, n2 = $("#pw-new2").value;
    if (!o || !n || !n2) return toast("请完整填写密码", false);
    if (n !== n2) return toast("两次输入的新密码不一致", false);
    if (n.length < 6) return toast("新密码至少6位", false);
    const btn = $("#pw-save");
    btn.disabled = true; btn.textContent = "提交中…";
    try {
      const r = await POST("/api/auth/password", {old_password: o, new_password: n});
      toast((r.data && r.data.message) || "密码修改成功");
      $("#pw-old").value = $("#pw-new").value = $("#pw-new2").value = "";
    } catch (e) { toast(e.message, false); }
    btn.disabled = false; btn.textContent = "修改密码";
  };
  $("#sys-diag").onclick = () => {
    const a = document.createElement("a");
    a.href = "/api/diagnostics";
    a.setAttribute("download", "diagnostics.zip");
    const opt = { headers: {} };
    if (TOKEN) opt.headers["Authorization"] = "Bearer " + TOKEN;
    fetch("/api/diagnostics", opt).then(r => {
      if (r.status === 401) { showLogin(); throw new Error("未登录"); }
      return r.blob();
    }).then(b => {
      a.href = URL.createObjectURL(b);
      a.click();
      toast("诊断包已下载");
    }).catch(e => toast(e.message, false));
  };
  const loadLog = async () => {
    try {
      const r = await GET("/api/logs?lines=300");
      $("#sys-logview").textContent = r.data.log || "(空)";
      const cr = r.data.crashes || [];
      $("#sys-crashes").innerHTML = cr.length
        ? `<h3 style="margin:10px 0 6px">崩溃日志 (${cr.length})</h3>` +
          cr.map(x => `<div class="chips" style="margin-bottom:6px">
            <button class="small" onclick="showCrash('${esc(x.name)}')">查看 ${esc(x.name)} (${fmtSize(x.size)})</button>
          </div>`).join("")
        : "<div class='hint' style='margin-top:8px'>无崩溃记录 ✓</div>";
    } catch (e) { toast(e.message, false); }
  };
  $("#sys-log").onclick = loadLog;
  loadLog();
}
window.showCrash = async name => {
  const opt = { headers: {} };
  if (TOKEN) opt.headers["Authorization"] = "Bearer " + TOKEN;
  const r = await fetch(`/api/logs/crash/${encodeURIComponent(name)}`, opt);
  const txt = await r.text();
  modal("崩溃日志 - " + name, `<pre class="logview" style="max-height:400px">${esc(txt)}</pre>`,
    [{ txt: "关闭", primary: true }]);
};

/* ---------- 文件浏览器 ---------- */
function fileBrowser(onPick) {
  let currentPath = "";
  const m = modal("选择文件", `
    <div class="crumb" id="fb-crumb"></div>
    <div class="fbrowser" id="fb-list"><div class="loading">加载中…</div></div>`,
    [{ txt: "取消" }, { txt: "选择当前文件", primary: true, onclick: () => {
      if (currentPath) { onPick(currentPath); m.close(); }
    } }]);

  const load = async path => {
    $("#fb-list").innerHTML = '<div class="loading">加载中…</div>';
    try {
      const r = await GET(`/api/fs/list?path=${encodeURIComponent(path)}`);
      const d = r.data;
      currentPath = d.path;
      $("#fb-crumb").textContent = "当前: " + d.path +
        "   快捷: " + (d.roots || []).join("  ");
      let html = "";
      if (d.parent && d.parent !== d.path)
        html += `<div class="fitem" data-p="${esc(d.parent)}">📁 .. (上级)</div>`;
      html += (d.items || []).map(it => it.dir
        ? `<div class="fitem" data-p="${esc(it.path)}">📁 ${esc(it.name)}</div>`
        : `<div class="fitem" data-p="${esc(it.path)}" data-file="1">📄 ${esc(it.name)}
           <span style="color:var(--text2);font-size:11px">${fmtSize(it.size)}</span></div>`).join("");
      $("#fb-list").innerHTML = html || '<div class="empty">空目录</div>';
      $$("#fb-list .fitem").forEach(el => el.onclick = () => {
        if (el.dataset.file) { onPick(el.dataset.p); m.close(); }
        else load(el.dataset.p);
      });
    } catch (e) { $("#fb-list").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
  };
  load("/");
}

/* ---------- 模态框 ---------- */
function modal(title, bodyHTML, buttons) {
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.innerHTML = `
    <div class="modal">
      <div class="m-head">${title}<button class="x">×</button></div>
      <div class="m-body">${bodyHTML}</div>
      <div class="m-foot">${(buttons || []).map((b, i) =>
        `<button ${b.primary ? 'class="primary"' : ""} data-b="${i}">${b.txt}</button>`).join("")}</div>
    </div>`;
  document.body.appendChild(mask);
  const close = () => mask.remove();
  mask.querySelector(".x").onclick = close;
  mask.onclick = e => { if (e.target === mask) close(); };
  (buttons || []).forEach((b, i) => {
    const btn = mask.querySelector(`[data-b="${i}"]`);
    btn.onclick = () => b.onclick ? b.onclick() : close();
  });
  return { close };
}

/* ---------- 启动 ---------- */
function tick() {
  $("#side-time").textContent = new Date().toLocaleString("zh-CN");
  if (currentPage === "overview") {
    GET("/api/version").then(r => {
      $("#side-ver").textContent = "v" + r.data.version;
    }).catch(() => {});
  }
}
setInterval(tick, 1000);

async function boot() {
  try {
    const v = await GET("/api/version");
    $("#side-ver").textContent = "v" + v.data.version;
  } catch (_) {}
  render();
}

if (TOKEN) boot(); else showLogin();
