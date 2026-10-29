const $ = (id) => document.getElementById(id);

let STATE = { accounts: [], current: null };

// ───────────── 基础 ─────────────
async function api(path, method = "GET", body = null) {
  const opt = { method, headers: {} };
  if (body !== null) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  let data;
  try {
    data = await res.json();
  } catch (e) {
    throw new Error(`HTTP ${res.status}：返回不是 JSON`);
  }
  if (!data.ok) {
    const err = data.error || "未知错误";
    if (data.logs && data.logs.length) appendLog(data.logs, false);
    throw new Error(err);
  }
  if (data.logs && data.logs.length) appendLog(data.logs, true);
  return data.data;
}

let busyCount = 0;
function setBusy(on) {
  busyCount += on ? 1 : -1;
  if (busyCount < 0) busyCount = 0;
  document.querySelectorAll("button").forEach((b) => {
    if (b.id === "btnClearLog" || b.id === "btnExportLog" || b.id === "btnCounter" || b.id === "btnCounterReset") return;
    b.disabled = busyCount > 0;
  });
}

async function act(label, fn) {
  setBusy(true);
  try {
    const out = await fn();
    toast(`${label} 完成`, "ok");
    return out;
  } catch (e) {
    appendLog([`✗ ${label} 失败：${e.message}`], false);
    toast(`${label} 失败：${e.message}`, "bad");
    return null;
  } finally {
    setBusy(false);
  }
}

function toast(msg, kind = "") {
  let el = document.querySelector(".toast");
  if (!el) {
    el = document.createElement("div");
    el.className = "toast";
    document.body.appendChild(el);
  }
  el.textContent = msg;
  el.className = `toast show ${kind}`;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = "toast"), 3200);
}

function appendLog(lines, ok = true) {
  const el = $("log");
  const text = lines.join("\n");
  const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
  el.textContent += (el.textContent ? "\n" : "") + text;
  if (atBottom) el.scrollTop = el.scrollHeight;
}

// ───────────── 账号 ─────────────
async function refresh() {
  const st = await api("/api/state");
  STATE = st;
  renderCurrent(st.current);
  renderAccounts(st.accounts);
  renderTemplate(st.template);
  renderStorage(st.storage);
  if (st.current) await loadFiles(st.current.uid);
}

function renderCurrent(cur) {
  const el = $("currentAccount");
  if (!cur) {
    el.innerHTML = "尚未选择账号";
    return;
  }
  el.innerHTML =
    `<b>${esc(cur.nickName || "-")}</b>` +
    ` · uid=${cur.uid} · ${esc(cur.userName || "")}` +
    (cur.playerLevel ? ` · Lv.${cur.playerLevel}` : "") +
    (cur.sid ? ` · sid=${cur.sid}` : "");
  $("nickInput").placeholder =
    `输入新昵称（当前：${cur.nickName || "-"}，服务端会自动加 0000_izhaohe_ 前缀）`;
  if (cur.playerLevel) $("levelInput").value = cur.playerLevel;
}

function renderAccounts(list) {
  const sel = $("accountSelect");
  sel.innerHTML = "";
  if (!list.length) {
    const o = document.createElement("option");
    o.textContent = "（账号文件里还没有账号）";
    o.value = "";
    sel.appendChild(o);
    return;
  }
  for (const a of list) {
    const o = document.createElement("option");
    o.value = a.uid;
    o.textContent = `${a.current ? "★ " : ""}${a.nickName || "-"} · ${a.uid}`;
    if (a.current) o.selected = true;
    sel.appendChild(o);
  }
  const cur = list.find((a) => a.current);
  const af = (STATE.paths && STATE.paths.account_file) || "accounts.json";
  $("accountInfo").textContent = cur
    ? `账号来源：${af}（current=${cur.uid}）　创建：${cur.createdAt || "-"}　更新：${cur.updated || "-"}　存档目录：accounts/${cur.uid}/`
    : `账号文件：${af}（还没有账号）`;
}

function renderStorage(st) {
  const el = $("storageWarn");
  if (!el) return;
  if (!st) {
    el.hidden = true;
    return;
  }
  const db = st.db || {};
  const parts = [];

  // 账号来源标签：一眼看出这批账号到底是从数据库读的，还是本地文件
  const tag = $("storeTag");
  if (tag) {
    if (st.backend === "mysql") {
      tag.textContent = st.store || "MySQL";
      tag.className = "h2tag ok";
      tag.title = db.detail || "";
    } else {
      tag.textContent = "本地文件（未用数据库）";
      tag.className = "h2tag bad";
      tag.title = db.detail || db.reason || "";
    }
  }

  // 账号没走数据库：把具体原因写在页面上，省得去翻服务器日志
  if (st.backend !== "mysql") {
    const why = db.detail || db.reason || "未知原因";
    parts.push(
      `⚠ 账号没有走数据库：<b>${esc(why)}</b>` +
      `<br>当前用的是本地文件 <code>${esc(st.store || st.dir || "")}</code>，` +
      `重新部署就会丢。配好 <code>ZHAOHE_DB_HOST</code> 等环境变量后重启即可。`
    );
  }

  if (!st.persistent && st.backend !== "mysql") {
    parts.push(`数据目录 <b>${esc(st.dir)}</b> 不是持久盘。`);
  }

  el.hidden = parts.length === 0;
  el.innerHTML = parts.join("<br>");
}

function renderTemplate(t) {
  if (!t) return;
  $("tplInfo").textContent = t.embedded
    ? `存档模板：后端内嵌 ${fmtSize(t.size)}（${t.path}），无需上传`
    : `当前模板：${t.path}`;
}

async function loadFiles(uid) {
  try {
    const r = await api(`/api/account/files?uid=${encodeURIComponent(uid)}`);
    renderFiles(r);
  } catch (e) {
    renderFiles({ files: [] });
  }
}

function renderFiles(r) {
  const tb = $("fileTable").querySelector("tbody");
  tb.innerHTML = "";
  if (!r.files || !r.files.length) {
    tb.innerHTML = `<tr><td colspan="4" class="muted">该账号目录暂无文件</td></tr>`;
    return;
  }
  for (const f of r.files) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>${esc(f.name)}</td>` +
      `<td>${fmtSize(f.size)}</td>` +
      `<td>${esc(f.mtime)}</td>` +
      `<td><a href="/api/account/file?uid=${encodeURIComponent(r.uid)}&name=${encodeURIComponent(f.name)}">下载</a></td>`;
    tb.appendChild(tr);
  }
}

function fmtSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

// ───────────── 排行榜 ─────────────
function renderRank(data) {
  const labels = { 0: "大选关榜", 1: "爬塔榜" };
  const endText = data.info && data.info.end_time_text;
  $("rankDeadline").textContent = endText
    ? `榜单截止时间：${endText}`
    : "";
  for (const t of [0, 1]) {
    const box = $(`rankTop${t}`);
    const top = data.tops && data.tops[String(t)];
    if (!top) {
      box.innerHTML = `<div class="title">${labels[t]} · type=${t}</div>
        <div class="line">榜上暂无数据</div>`;
      continue;
    }
    const isMe = data.account && String(top.uid) === String(data.account.uid);
    const isTop = Number(top.rank) === 1;
    box.innerHTML = `
      <div class="title">${labels[t]} · type=${t}</div>
      <div class="name">${esc(top.name)}</div>
      <div class="line">uid：${top.uid}</div>
      <div class="line">分数：<b>${top.value}</b>　名次：${isTop ? "rank=1" : `<span class="warn">rank=${top.rank}（未取到榜一）</span>`}</div>
      <div class="line">${isMe ? "★ 这是我" : "不是我"}</div>`;
  }

  const tb = $("rankTable").querySelector("tbody");
  tb.innerHTML = "";
  const entries = data.entries || [];
  if (!entries.length) {
    tb.innerHTML = `<tr><td colspan="6" class="muted">无数据</td></tr>`;
    return;
  }
  const myUid = data.account ? String(data.account.uid) : null;
  for (const e of entries) {
    const tr = document.createElement("tr");
    const me = String(e.uid) === myUid;
    const topTag = Number(e.rank) === 1 ? `<span class="tag top">榜一</span>` : "";
    tr.innerHTML =
      `<td>${labels[e.type] || e.type}</td>` +
      `<td>${e.rank}</td>` +
      `<td>${esc(e.name)} ${topTag}</td>` +
      `<td>${e.uid}</td>` +
      `<td>${e.value}</td>` +
      `<td class="${me ? "me" : ""}">${me ? "★ 本人" : ""}</td>`;
    tb.appendChild(tr);
  }
}

// ───────────── 事件绑定 ─────────────
function bind() {
  $("btnRefresh").onclick = () => act("刷新", refresh);
  bindCounter();
  bindAutoRank();

  const dbBtn = $("btnDbCheck");
  if (dbBtn) {
    dbBtn.onclick = async () => {
      const out = await act("数据库检测", () => api("/api/db/check", "POST", {}));
      if (!out) return;
      let t;
      if (out.ok) {
        t = `数据库正常：${out.target}，${out.accounts} 个账号，当前 ${out.current ?? "-"}`;
        toast(t, "ok");
      } else {
        t = `数据库异常：${out.detail || out.reason || "未知"}`
          + `（配置=${out.configured ? "有" : "无"}，驱动=${out.driver ? "有" : "无"}）`;
        toast("数据库检测失败，详见日志", "bad");
      }
      $("accountInfo").textContent = t;
    };
  }

  $("btnSelect").onclick = async () => {
    const uid = $("accountSelect").value;
    if (!uid) return toast("请选择账号", "bad");
    const out = await act("切换账号", () => api("/api/account/select", "POST", { uid }));
    if (out) {
      await refresh();
      await loadFiles(out.current.uid);
    } else {
      // 多半是页面上的列表已经过期（账号被删/换库/服务重启过），拉一次新的
      await refresh();
    }
  };

  $("btnLogin").onclick = async () => {
    const out = await act("重新登录", () => api("/api/account/login", "POST", {}));
    if (out) await refresh();
  };

  $("btnRegister").onclick = async () => {
    if (!confirm("新建账号会注册新号、写入账号池并切换为当前账号，继续？")) return;
    const out = await act("新建账号", () => api("/api/account/register", "POST", {}));
    if (out) await refresh();
  };

  $("btnImport").onclick = async () => {
    const body = {
      userName: $("impUser").value.trim(),
      passwd: $("impPass").value.trim(),
      nickName: $("impNick").value.trim(),
      uid: $("impUid").value.trim() || null,
    };
    if (!body.userName || !body.passwd) return toast("请填 userName 和 passwd", "bad");
    const out = await act("导入账号", () => api("/api/account/import", "POST", body));
    if (out) await refresh();
  };

  $("btnNickManual").onclick = async () => {
    const name = $("nickInput").value.trim();
    if (!name) return toast("请输入新昵称，或点「自动生成」", "bad");
    const out = await act("修改昵称", () =>
      api("/api/account/nickname", "POST", { mode: "manual", nickName: name }));
    if (out) {
      $("nickInfo").textContent = `已改为：${out.nickName}`;
      $("nickInput").value = "";
      await refresh();
    }
  };

  $("btnNickAuto").onclick = async () => {
    if (!confirm("将从榜单里挑一个底名并自动加随机数字改名，继续？")) return;
    const out = await act("自动生成昵称", () =>
      api("/api/account/nickname", "POST", { mode: "auto" }));
    if (out) {
      $("nickInfo").textContent = `已改为：${out.nickName}`;
      await refresh();
    }
  };

  $("btnSetLevel").onclick = async () => {
    const level = parseInt($("levelInput").value, 10);
    if (!level || level <= 0) return toast("请填一个正整数等级", "bad");
    if (!confirm(`把玩家等级写为 ${level} 级？`)) return;
    const out = await act("写入玩家等级", () =>
      api("/api/account/level", "POST", { level }));
    if (out) {
      $("levelInfo").textContent =
        `已写入等级 ${out.level}（result=${out.result}）`;
      await refresh();
    }
  };

  $("btnArenaMatch").onclick = async () => {
    const mode = $("arenaMatchMode").value;
    const raw = $("arenaMatchScore").value.trim();
    const wait = parseInt($("arenaMatchWait").value, 10) || 0;
    const keep = $("arenaMatchKeep").checked;
    const body = { wait, keep };

    let desc;
    if (mode === "add") {
      if (raw === "") return toast("加分模式请填增量，如 25", "bad");
      body.mode = "add";
      body.score = parseInt(raw, 10);
      desc = `在存档积分上加 ${body.score} 分`;
    } else if (mode === "absolute") {
      if (raw === "") return toast("绝对值模式请填分数", "bad");
      body.mode = "absolute";
      body.score = parseInt(raw, 10);
      desc = `把服务端积分直接改成 ${body.score}（低于存档会掉榜）`;
    } else {
      desc = "用存档里的积分同步到服务端";
    }

    if (!confirm(`将${desc}；匹配 ${wait} 秒后${keep ? "保留队列" : "取消"}。继续？`)) return;
    const out = await act("匹配推分", () => api("/api/arena/match", "POST", body));
    if (out) {
      const rk = out.rank || {};
      const server = out.server_score != null ? out.server_score
        : (rk.mine ? rk.mine.score : "不在前 100");
      const season = out.season || {};
      const seasonTxt = season.ok
        ? `　<b>赛季剩余 ${season.season_left_text}</b>（到 ${season.season_end_text}）`
        : "";
      $("arenaMatchInfo").innerHTML =
        `推送 score=<b>${out.score}</b>（${out.mode === "add" ? "加分" : "绝对值"}）` +
        `　匹配倒计时=${out.match && out.match.result != null ? out.match.result : "?"}s` +
        `　取消=${out.cancel ? (out.cancel.ok ? "成功" : "失败") : "未取消"}` +
        `　服务端积分=<b>${server}</b>` +
        `　我的名次=${out.my_rank != null ? out.my_rank : (rk.my_rank ?? "?")}` +
        (out.save_score != null ? `　存档积分=${out.save_score}` : "") +
        seasonTxt;
      await refresh();
    }
  };

  $("btnArenaRank").onclick = async () => {
    const out = await act("查竞技场榜", () => api("/api/arena/rank", "POST", {}));
    if (out) {
      const mine = out.mine ? out.mine.score : "不在前 100";
      const top = (out.top || [])
        .map((r) => `#${r.rank} ${r.name} ${r.score}`).join("　");
      const season = out.season || {};
      const seasonTxt = season.ok
        ? `　<b>赛季结束=${season.season_end_text}</b>（剩 ${season.season_left_text}）` +
          `　今日开放 ${season.today_open_text} ~ ${season.today_close_text}`
        : "";
      $("arenaMatchInfo").innerHTML =
        `我的名次=${out.my_rank}　服务端积分=${mine}` +
        (out.save_score != null ? `　存档积分=${out.save_score}` : "") +
        seasonTxt + `　${top}`;
    }
  };

  $("btnArenaScore").onclick = async () => {
    const score = parseInt($("arenaScore").value, 10);
    if (!score || score <= 0) return toast("请填一个正整数积分", "bad");
    const is_win = parseInt($("arenaWin").value, 10);
    if (!confirm(`竞技场结算：${is_win ? "赢" : "输"} +${score} 分，继续？`)) return;
    const out = await act("竞技场加分", () =>
      api("/api/arena/score", "POST", { score, is_win }));
    if (out) {
      $("arenaInfo").textContent =
        `结算成功：iswin=${out.is_win}　addScore=${out.add_score}` +
        `　result=${out.result}　score=${out.score}`;
    }
  };

  $("btnExportJson").onclick = () => downloadFile("/api/accounts/export?format=json");
  $("btnExportCsv").onclick = () => downloadFile("/api/accounts/export?format=csv");

  $("btnImportList").onclick = async () => {
    const file = $("impListFile").files[0];
    let text = $("impListText").value.trim();
    if (file) text = await file.text();
    if (!text) return toast("请选择文件或粘贴 JSON", "bad");
    // ★ 这里只做语法校验，绝不把解析结果发给后端：
    //   uid 是 19 位数，超过 JS 的 2^53 安全整数，JSON.parse 会把它舍入
    //   （1565050802521812992 → 1565050802521813000），后端就会存下假的 uid。
    //   原文交给 Python 的 json 解析才精确。
    try {
      JSON.parse(text);
    } catch (e) {
      return toast(`JSON 解析失败：${e.message}`, "bad");
    }
    const out = await act("导入账号列表", () =>
      api("/api/accounts/import", "POST", {
        data: text,
        select: $("impSelectChk").checked,
      }));
    if (out) {
      $("impListInfo").textContent =
        `新增 ${out.added}，更新 ${out.updated}，过滤重复玩家 ${out.dupe || 0}，` +
        `跳过 ${out.skipped}，账号总数 ${out.total}` +
        (out.errors && out.errors.length ? `；${out.errors.join("；")}` : "");
      $("impListFile").value = "";
      $("impListText").value = "";
      await refresh();
    }
  };

  $("btnRankQuery").onclick = async () => {
    const out = await act("查询榜单", () => api("/api/rank/query", "POST", {}));
    if (out) renderRank(out);
  };

  $("btnJump").onclick = async () => {
    if (!confirm("将查询两个榜的榜一，并按（分数÷100+1）×100 分别上报，继续？")) return;
    const out = await act("jump 冲榜", () => api("/api/rank/jump", "POST", {}));
    if (out) {
      renderRank({ tops: out.tops, entries: [], account: out.account, info: out.info });
      await loadFiles(out.account.uid);
    }
  };

  $("btnWbUpload").onclick = async () => {
    const damage = parseInt($("wbDamage").value, 10);
    if (!damage || damage <= 0) return toast("请填一个正整数伤害", "bad");
    if (!confirm(`上报世界魔王伤害 ${damage}，继续？`)) return;
    const body = {
      damage,
      kind: parseInt($("wbKind").value, 10) || 2000,
      monster: parseInt($("wbMonster").value, 10) || 297,
      send_counter: $("wbCounterChk").checked,
      counter_value: $("wbCounterValue").value.trim() || "1",
    };
    const dataHex = $("wbData").value.trim();
    if (dataHex) body.data_hex = dataHex;
    const out = await act("世界魔王伤害上报", () =>
      api("/api/worldboss/damage", "POST", body));
    if (out) {
      $("wbInfo").textContent =
        `本次 ${out.damage}　累计伤害 ${out.total_damage}　` +
        `战斗次数 ${out.fights}（上限 ${out.max_fights}，剩余 ${out.remaining}）` +
        `　data=${out.data_len}B（${out.data_source}）` +
        (out.counter ? `　146=${out.counter.value}` : "");
    }
  };

  $("btnWbStatus").onclick = async () => {
    const out = await act("世界魔王状态查询", () =>
      api("/api/worldboss/status", "POST", {}));
    if (out) {
      $("wbStatus").textContent =
        `状态 ${out.status_text}（status=${out.status}）` +
        `　累计伤害 ${out.total_damage}　战斗次数 ${out.fights}` +
        `（上限 ${out.max_fights}，剩余 ${out.remaining}）　排名 ${out.rank}` +
        `　本期结束 ${out.end_time_text || "未知"}` +
        `　下期开始 ${out.next_start_text || "未知"}`;
    }
  };

  $("btnWbRank").onclick = async () => {
    const type = parseInt($("wbRankType").value, 10);
    const out = await act("世界魔王排行榜查询", () =>
      api("/api/worldboss/rank", "POST", { type }));
    if (!out) return;
    $("wbRankInfo").textContent =
      `${out.type_name}　我的排名 ${out.my_rank || "未上榜"}` +
      `　我的分数 ${out.my_score}　共 ${out.size} 条`;
    const tb = $("wbRankTable").querySelector("tbody");
    tb.innerHTML = "";
    const entries = out.entries || [];
    if (!entries.length) {
      tb.innerHTML = `<tr><td colspan="7" class="muted">无数据</td></tr>`;
      return;
    }
    const myUid = out.account ? String(out.account.uid) : null;
    for (const e of entries) {
      const tr = document.createElement("tr");
      const me = String(e.uid) === myUid;
      const topTag = e.rank === 1 ? ` <span class="tag top">榜一</span>` : "";
      tr.innerHTML =
        `<td>${e.rank}</td>` +
        `<td class="${me ? "me" : ""}">${esc(e.name)}${topTag}${me ? " ★" : ""}</td>` +
        `<td>${e.uid}</td>` +
        `<td>${e.damage}</td>` +
        `<td>${e.level}</td>` +
        `<td>${e.like}</td>` +
        `<td>${e.status === 0 ? "公开" : "隐藏"}</td>`;
      tb.appendChild(tr);
    }
  };

  $("btnUpload").onclick = async () => {
    const score = parseInt($("score").value, 10);
    if (!score || score <= 0) return toast("请填一个正整数分数", "bad");
    const body = {
      score,
      type: parseInt($("rankType").value, 10),
      rename: $("renameChk").checked,
      verify: $("verifyChk").checked,
    };
    const out = await act("上报分数", () => api("/api/rank/upload", "POST", body));
    if (out) await loadFiles(out.account.uid);
  };

  $("btnExportSave").onclick = () => {
    const chapter = parseInt($("chapter").value, 10) || 10;
    const sw = $("switchesChk").checked ? 1 : 0;
    const uid = (STATE.current && STATE.current.uid) || "";
    const url = `/api/save/export?chapter=${chapter}&open_switches=${sw}`
      + (uid ? `&uid=${uid}` : "") + `&_=${Date.now()}`;
    $("exportInfo").textContent =
      `正在生成第 ${chapter} 章存档${sw ? "（功能全开）" : ""}，稍后自动下载 zip ...`;
    toast("开始生成并下载", "ok");
    downloadFile(url);
  };

  $("btnExportLog").onclick = () => {
    const params = new URLSearchParams();
    if (STATE.current && STATE.current.uid) params.set("uid", STATE.current.uid);
    params.set("_", Date.now());
    downloadFile(`/api/logs/export?${params.toString()}`);
    toast("正在导出日志", "ok");
  };

  $("btnClearLog").onclick = () => ($("log").textContent = "");
}

function downloadFile(url) {
  const a = document.createElement("a");
  a.href = url;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ───────────── 定时计数器（服务端进程内计时） ─────────────
// 计数状态存在 Python 服务里，刷新页面不重置；前端只按服务端给的
// seconds_to_next 本地走秒，每 10 秒跟服务端校准一次。
let counterState = { running: false, count: 0, interval: 60, nextAt: 0 };
let counterTick = null;
let counterSync = null;

function fmtCountdown(ms) {
  const t = Math.max(0, Math.ceil(ms / 1000));
  return `${String(Math.floor(t / 60)).padStart(2, "0")}:${String(t % 60).padStart(2, "0")}`;
}

function applyCounter(d) {
  counterState.running = !!d.running;
  counterState.count = Number(d.count) || 0;
  counterState.interval = Number(d.interval) || 60;
  counterState.nextAt = d.running && d.seconds_to_next != null
    ? Date.now() + Number(d.seconds_to_next) * 1000
    : 0;
  renderCounter();
}

function renderCounter() {
  // 本地按时间戳推进计数，避免倒计时显示和计数不一致
  if (counterState.running && counterState.nextAt) {
    const step = counterState.interval * 1000;
    while (Date.now() >= counterState.nextAt) {
      counterState.count += 1;
      counterState.nextAt += step;
    }
  }
  const val = counterState.count;
  $("counterValue").textContent = val;
  $("btnCounter").textContent = counterState.running ? "停止计数" : "开始计数";
  if (counterState.running) {
    $("counterState").textContent =
      `运行中 · 每 ${counterState.interval} 秒 +1 · 距下次 +1 还有 ` +
      `${fmtCountdown(counterState.nextAt - Date.now())}（服务端计时，刷新不重置）`;
  } else {
    $("counterState").textContent =
      `已停止 · 当前计数 ${val}（服务端计时，刷新不重置）`;
  }
}

async function syncCounter() {
  try {
    const res = await fetch("/api/counter/status", { cache: "no-store" });
    const j = await res.json();
    if (j && j.ok && j.data) applyCounter(j.data);
  } catch (e) {
    /* 网络抖动不影响页面，下一轮再校准 */
  }
}

async function toggleCounter() {
  const path = counterState.running ? "/api/counter/stop" : "/api/counter/start";
  const out = await act(counterState.running ? "停止计数" : "开始计数",
    () => api(path, "POST", {}));
  if (!out) return;
  applyCounter(out);
  toast(out.running
    ? `计数器已开始（服务端运行），当前 ${out.count} 次`
    : `计数器已停止，共 ${out.count} 次`, "ok");
}

async function resetCounter() {
  const out = await act("计数器清零", () => api("/api/counter/reset", "POST", {}));
  if (!out) return;
  applyCounter(out);
  toast("计数器已清零", "ok");
}

function bindCounter() {
  const btn = $("btnCounter");
  if (!btn) return;
  btn.onclick = toggleCounter;
  $("btnCounterReset").onclick = resetCounter;
  syncCounter().then(() => {
    clearInterval(counterTick);
    counterTick = setInterval(renderCounter, 250);
    clearInterval(counterSync);
    counterSync = setInterval(syncCounter, 10000);
  });
}

// ───────────── 自动刷榜（服务端调度） ─────────────
// 开关 / 建号 / 查榜 / jump 全在 Python 后台线程里跑，页面只负责展示和触发。
let autoRankState = { enabled: false, tasks: [], logs: [] };
let autoRankTimer = null;

function fmtLeft(sec) {
  const t = Math.max(0, Math.floor(sec));
  const m = Math.floor(t / 60);
  const s = t % 60;
  return `${m}分${String(s).padStart(2, "0")}秒`;
}

function renderAutoRank(d) {
  if (!d) return;
  autoRankState = d;

  const tag = $("autoRankTag");
  if (tag) {
    let text = "未启动";
    let cls = "h2tag";
    if (d.enabled && d.finished) { text = "今日已结束"; }
    else if (d.enabled && d.running) { text = "运行中"; cls = "h2tag ok"; }
    else if (d.enabled) { text = "已启动"; cls = "h2tag ok"; }
    tag.textContent = text;
    tag.className = cls;
  }

  const btn = $("btnAutoRank");
  btn.textContent = d.enabled ? "停止自动刷榜" : "启动自动刷榜";
  btn.classList.toggle("primary", !d.enabled);

  const store = d.store ? ` · 账号库：${d.store}` : "";
  if (!d.enabled) {
    $("autoRankNext").textContent =
      "开关关闭。点「启动自动刷榜」立即开始今日任务；开关打开后每天北京时间 0 点自动触发。"
      + store;
  } else if (d.finished) {
    $("autoRankNext").textContent =
      `今日（${d.today}）任务已结束，等待明日北京时间 0 点自动开始。` + store;
  } else {
    const left = d.seconds_to_tick != null ? `（${fmtLeft(d.seconds_to_tick)}后）` : "";
    $("autoRankNext").textContent =
      `今日 ${d.today} · ${d.running ? "正在执行本轮" : "等待下一轮"}` +
      ` · 常规榜下次校准：${d.next_tick_text || "即将开始"}${left}` +
      ` · ${d.sprint_active ? "★ 冲刺窗口内，每 2 分钟复查" : "未到冲刺窗口"}` + store;
  }

  const tb = $("autoRankTable").querySelector("tbody");
  tb.innerHTML = "";
  if (!d.tasks || !d.tasks.length) {
    tb.innerHTML = `<tr><td colspan="8" class="muted">尚未开始（开关打开或点「立即执行一次」后开始建号）</td></tr>`;
  } else {
    const sub = (end, last) => {
      if (!end && !last) return "-";
      return esc(end || "-") + (last ? `<br><span class="muted">${esc(last)}</span>` : "");
    };
    for (const t of d.tasks) {
      const tr = document.createElement("tr");
      const status = t.done
        ? `已结束：${esc(t.reason || "-")}`
        : `进行中（${t.index + 1}/${t.accounts}）`;
      tr.innerHTML =
        `<td>${esc(t.day_text || t.day)}</td>` +
        `<td>${t.accounts}</td>` +
        `<td>${esc(t.current_nick || "-")}</td>` +
        `<td>${esc(t.end_time_text || "-")}</td>` +
        `<td>${sub(t.wb_end_text, t.wb_last)}</td>` +
        `<td>${sub(t.arena_end_text, t.arena_last)}</td>` +
        `<td>${status}</td>` +
        `<td>${esc(t.last || "-")}</td>`;
      tb.appendChild(tr);
    }
  }

  const pre = $("autoRankLog");
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
  pre.textContent = (d.logs || []).join("\n") || "（暂无日志）";
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

async function syncAutoRank() {
  try {
    const res = await fetch("/api/autorank/status", { cache: "no-store" });
    const j = await res.json();
    if (j && j.ok && j.data) renderAutoRank(j.data);
  } catch (e) {
    /* 网络抖动不影响页面 */
  }
}

async function toggleAutoRank() {
  const on = !autoRankState.enabled;
  const out = await act(on ? "启动自动刷榜" : "停止自动刷榜",
    () => api("/api/autorank/toggle", "POST", { on }));
  if (!out) return;
  renderAutoRank(out);
  toast(on ? "自动刷榜已启动" : "自动刷榜已停止", "ok");
}

async function triggerAutoRank() {
  const out = await act("立即执行自动刷榜",
    () => api("/api/autorank/trigger", "POST", {}));
  if (!out) return;
  renderAutoRank(out);
  toast(out.finished ? "今日任务已结束" : "已触发，后台开始执行", "ok");
}

function bindAutoRank() {
  const btn = $("btnAutoRank");
  if (!btn) return;
  btn.onclick = toggleAutoRank;
  $("btnAutoRankNow").onclick = triggerAutoRank;
  syncAutoRank();
  clearInterval(autoRankTimer);
  autoRankTimer = setInterval(syncAutoRank, 3000);
}

// ───────────── 服务运行时长（右上角） ─────────────
// 取一次服务端秒数，之后本地每秒自增（30 秒再校准一次），避免每秒打接口
let upSec = null;
let upTs = 0;

function fmtUptime(seconds) {
  const t = Math.max(0, Math.floor(seconds));
  const days = Math.floor(t / 86400);
  const h = Math.floor((t % 86400) / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const y = Math.floor(days / 365);
  const mo = Math.floor((days % 365) / 30);
  const d = (days % 365) % 30;
  return `${y}年${mo}月${d}日${h}时${m}分${s}秒`;
}

async function syncUptime() {
  try {
    const res = await fetch("/api/uptime", { cache: "no-store" });
    const j = await res.json();
    if (j && j.ok && j.data) {
      const prev = upSec;
      upSec = j.data.seconds;
      upTs = Date.now();
      // 运行时长突然往回跳 = 服务重启过，页面上的账号列表/状态都是旧的，直接刷新
      if (prev != null && upSec + 5 < prev && busyCount === 0) {
        toast("服务已重启，正在刷新页面…", "");
        setTimeout(() => location.reload(), 800);
      }
    }
  } catch (e) {
    /* 网络抖动不影响页面 */
  }
}

function startUptime() {
  const el = $("uptime");
  if (!el) return;
  syncUptime().then(() => {
    if (upSec != null) el.textContent = `运行 ${fmtUptime(upSec)}`;
  });
  setInterval(() => {
    if (upSec == null) return;
    el.textContent = `运行 ${fmtUptime(upSec + (Date.now() - upTs) / 1000)}`;
  }, 1000);
  setInterval(syncUptime, 30000);
}

window.addEventListener("DOMContentLoaded", async () => {
  bind();
  startUptime();
  try {
    await refresh();
  } catch (e) {
    appendLog([`✗ 初始化失败：${e.message}`], false);
    toast(`初始化失败：${e.message}`, "bad");
  }
});
