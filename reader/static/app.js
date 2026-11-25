/* 阅读器交互：树折叠 / 手风琴 / 下拉菜单 / switch 文案 */
(function () {
  var COLLAPSE_KEY = "diting.tree.collapsed";

  function readCollapsed() {
    try {
      return JSON.parse(localStorage.getItem(COLLAPSE_KEY) || "[]");
    } catch (e) {
      return [];
    }
  }

  function writeCollapsed(list) {
    try {
      localStorage.setItem(COLLAPSE_KEY, JSON.stringify(list));
    } catch (e) {
      /* 忽略：隐私模式下写不进去不影响使用 */
    }
  }

  function applyTreeState() {
    var collapsed = readCollapsed();
    document.querySelectorAll(".arrow[data-toggle]").forEach(function (arrow) {
      var id = arrow.getAttribute("data-toggle");
      var ul = document.getElementById(id);
      if (!ul) return;
      var isCollapsed = collapsed.indexOf(id) >= 0;
      ul.hidden = isCollapsed;
      arrow.textContent = isCollapsed ? "▶" : "▼";
    });
  }

  function toggleCat(id) {
    var collapsed = readCollapsed();
    var idx = collapsed.indexOf(id);
    if (idx >= 0) collapsed.splice(idx, 1);
    else collapsed.push(id);
    writeCollapsed(collapsed);
    applyTreeState();
  }

  function closeMenus(except) {
    document.querySelectorAll(".menu").forEach(function (menu) {
      if (menu !== except) menu.hidden = true;
    });
  }

  /* ★ 工具栏不参与局部刷新，里面的隐藏 sel 会变陈旧 —— 每次切换范围都要同步，
     否则「标记为已读」会作用在错误的 feed 上 */
  function syncSel(sel) {
    if (!sel) return;
    document.querySelectorAll("input[name='sel']").forEach(function (i) {
      i.value = sel;
    });
  }

  function syncSelFromUrl() {
    try {
      syncSel(new URLSearchParams(location.search).get("sel"));
    } catch (e) { /* ignore */ }
  }

  /* ---------------- 标签页标题 ----------------
     ★ 阅读页切范围（树 / 筛选 / 分组）走 hx-push-url 的局部刷新，整页不重载，
       而 htmx 1.9 不会把响应里的 <title> 应用到 document.title（只有历史
       回放时才恢复）—— 不补这一手，开十个 feed 全是同一个标签页标题。
     ★ 标题的唯一来源是工具栏的 #toolbar-title（服务端渲染 + OOB 更新），
       这里只做搬运、不另算一套"当前范围"的判据（两套判据迟早会漂）。
       其他页面没有这个元素，标题由服务端渲染，直接跳过。 */
  function syncTabTitle() {
    var el = document.getElementById("toolbar-title");
    if (!el) return;
    var name = (el.textContent || "").trim();
    if (name) document.title = name + " · 谛听";
  }

  /* ---------------- 级别多选过滤 ----------------
     ★★ 勾选框只是"给人看"的，真正提交的是同一表单里的 hidden `levels`（逗号分隔）。
     同步必须发生在 htmx 读表单**之前**：htmx 的 `change` 监听挂在 <form> 上（冒泡阶段），
     所以这里用**捕获阶段**（document 的 capture 早于 form 的 bubble）。
     否则会拿着旧的 hidden 值发请求，症状是"勾了没反应 / 差一拍"。 */
  function allLevelBoxes() {
    return Array.prototype.slice.call(document.querySelectorAll("[data-lv]"));
  }

  function pickedLevels() {
    var seen = [];
    allLevelBoxes().forEach(function (b) {
      var v = b.getAttribute("data-lv");
      if (b.checked && seen.indexOf(v) < 0) seen.push(v);
    });
    return seen.sort();
  }

  function applyLevels(vals) {
    var boxes = allLevelBoxes();
    if (!boxes.length) return;
    var all = vals.length >= boxes.length;   // 四个都在 = 不筛
    boxes.forEach(function (b) {
      b.checked = vals.indexOf(b.getAttribute("data-lv")) >= 0;
    });
    var form = boxes[0].closest("form");
    if (!form) return;
    var hidden = form.querySelector("input[name='levels']");
    if (hidden) hidden.value = all ? "" : vals.join(",");
    var label = form.querySelector("[data-lv-label]");
    if (label) {
      label.textContent = all ? "级别：全部"
        : "级别：" + vals.map(function (v) { return "L" + v; }).join("、");
    }
  }

  function onLevelToggle(box) {
    /* ★ 全不勾没有意义（列表会空），"空 = 全看"又容易误解 —— 干脆不许取消最后一个 */
    if (!box.checked && !pickedLevels().length) {
      box.checked = true;
      if (window.toast) window.toast("至少要保留一个级别", 2200);
    }
    applyLevels(pickedLevels());
  }

  function syncLevelsFromUrl() {
    if (!allLevelBoxes().length) return;
    var raw = null;
    try { raw = new URLSearchParams(location.search).get("levels"); } catch (e) { return; }
    var vals = (raw || "").split(",").map(function (s) { return s.trim(); });
    vals = vals.filter(function (v, i) { return v !== "" && vals.indexOf(v) === i; });
    if (!vals.length) vals = allLevelBoxes().map(function (b) { return b.getAttribute("data-lv"); });
    applyLevels(vals);
  }

  /* ★ 捕获阶段：必须早于 htmx 挂在 form 上的冒泡监听 */
  document.addEventListener("change", function (evt) {
    var box = evt.target && evt.target.closest ? evt.target.closest("[data-lv]") : null;
    if (box) onLevelToggle(box);
  }, true);

  /* ---------------- 右键菜单 ---------------- */
  /* 同一分类下的所有分组（给 feed 的「移入分组」用）。
     ★ P0 只在**同分类内**移动：跨分类 = 改 Miniflux 归属，会清零该源的解析错误
       计数、让历史条目改属、并改变判据继承（见方案 §4.3）—— 后端也会拒绝。 */
  function siblingGroups(node) {
    var cat = node.closest(".node.cat");
    if (!cat) return [];
    return Array.prototype.slice.call(cat.querySelectorAll(".node.group")).map(function (g) {
      var a = g.querySelector("a");
      return { id: g.getAttribute("data-group"), title: a ? a.textContent.trim() : "" };
    });
  }

  /* 手动顺序属于"这一层"，所以每一行的菜单只撤销**它自己那一层**。
     这一层没被拖过（`data-ord="0"`，由 treeorder.apply 按库里有没有行给）就什么都不加
     —— 点了没反应的菜单项是噪音，不是功能。 */
  function pushOrderReset(items, node, label) {
    if (node.getAttribute("data-ord") !== "1") return;
    var kind = nodeKind(node);
    items.push({ label: "恢复默认顺序（" + label + "）", act: "order-reset",
                 kind: kind, scope: scopeOf(node, kind) });
  }

  var CTX = {
    tree: function (node) {
      /* 「星标文章」是固定入口，不是可操作节点 —— 右键不给菜单 */
      if (node.classList.contains("star")) return [];
      var isCat = node.classList.contains("cat");
      var isGroup = node.classList.contains("group");
      var scope = isCat ? "cat:" + node.getAttribute("data-cat")
                        : "feed:" + (node.getAttribute("data-feed")
                                     || (node.querySelector("a").getAttribute("href").match(/feed:(\d+)/) || [])[1]);

      /* 分组节点：★ 刻意**不给**「配置分级逻辑」—— 分组是纯视图层，没有判据继承。
         给了会让人以为"配了没用"，其实是设计如此（方案 §2.3）。
         ★ 但「立即刷新本组」是有的：分组有明确的成员集合，刷组 = 刷组内全部源。 */
      if (isGroup) {
        var ga = node.querySelector("a");
        var gcat = node.closest(".node.cat");
        var gitems = [
          { label: "立即刷新本组", act: "refresh",
            scope: "group:" + node.getAttribute("data-group") },
          { label: "添加 feed…", act: "feed-add",
            cat: gcat ? gcat.getAttribute("data-cat") : "",
            group: node.getAttribute("data-group") },
          { label: "重命名分组…", act: "grp-rename",
            group: node.getAttribute("data-group"), title: ga ? ga.textContent.trim() : "" },
          { label: "删除分组（源回到分类下）", act: "grp-delete",
            group: node.getAttribute("data-group"), title: ga ? ga.textContent.trim() : "" }
        ];
        /* ★ 目录行撤销的是**目录之间**这一层（拖动目录行排出来的）；组内 feed 的顺序
           要到那一组里的 feed 行上恢复 —— 两层各撤各的，一个菜单项只管一层。 */
        pushOrderReset(gitems, node, "目录");
        return gitems;
      }

      var items = [{ label: "配置分级逻辑", act: "config", scope: scope }];
      if (isCat) {
        var ca = node.querySelector("a");
        var empty = node.getAttribute("data-empty") === "1";
        items.push({ label: "重命名分类…", act: "cat-rename",
                     cat: node.getAttribute("data-cat"),
                     title: ca ? ca.textContent.trim() : "" });
        items.push({ label: "添加 feed…", act: "feed-add",
                     cat: node.getAttribute("data-cat"), group: "" });
        items.push({ label: "新建分组…", act: "grp-create", cat: node.getAttribute("data-cat") });
        /* ★ 只管「分类之间」这一层。这一层下面的「目录之间」「本分类的 feed」
           到目录行 / feed 行上各自撤销 —— 一个菜单项管一层，才不会撤错。 */
        pushOrderReset(items, node, "分类");
        if (empty) {
          /* ★ 空分类没有源可刷，不显示「立即刷新」；改给「移除」。 */
          items.push({ label: "移除分类…", act: "cat-delete",
                       cat: node.getAttribute("data-cat"),
                       title: ca ? ca.textContent.trim() : "" });
        } else {
          items.push({ label: "立即刷新本分类", act: "refresh", scope: scope });
        }
        return items;
      }

      /* ★ 没配分级判据时，「查看 prompt」和「重判」都没有意义：
         - 没有判据 ⇒ 不调 LLM，也就没有 prompt 可看；
         - 评级全是 default_level，没有 LLM 判定可推翻。
         判据标记由 _tree_feed.html 的 data-cfg 给出（分类继承也算已配置）。 */
      var hasCfg = node.getAttribute("data-cfg") === "1";
      if (hasCfg) {
        items.push({ label: "查看 prompt", act: "prompt", scope: scope });
      }

      // ★ 立即刷新与快速刷新开关**解耦**：没开启快速刷新的源也能手动刷一次
      items.push({ label: "立即刷新", act: "refresh", scope: scope });

      // ★ 快速刷新只在 feed 级：关掉时就叫「开启」，开着就叫「关闭」
      var fastOn = !!node.querySelector(".fast");
      items.push({ label: fastOn ? "关闭快速刷新" : "开启快速刷新",
                   act: fastOn ? "fast-off" : "fast-on", scope: scope });

      // ★ 标题翻译（feed 级）。开着时叫「关闭」，默认关。
      var trOn = node.getAttribute("data-translate") === "1";
      items.push({ label: trOn ? "关闭标题翻译" : "开启标题翻译",
                   act: trOn ? "tr-off" : "tr-on", scope: scope });

      /* ---- 分组移动（右键与拖动同一条路）---- */
      var feedId = scope.split(":")[1];
      var raw = node.getAttribute("data-group") || "";
      var cur = raw === "0" ? "" : raw;            // data-group="0" = 未分组
      var groups = siblingGroups(node);
      groups.forEach(function (g) {
        if (String(g.id) !== cur) {
          items.push({ label: "→ 移入「" + g.title + "」", act: "grp-move",
                       feed: feedId, group: g.id });
        }
      });
      if (cur) items.push({ label: "→ 移出分组", act: "grp-move", feed: feedId, group: "" });
      // 分类下没有分组时**什么都不加**：空提示只是噪音，建组入口本来就该在分类行。

      /* 撤销拖动重排 —— 撤的是**这一行所在那一层**（组内 or 分类根），标签跟着走。 */
      pushOrderReset(items, node, cur ? "本组内 feed" : "本分类 feed");

      /* ---- feed 自身的编辑（写 Miniflux）----
         ★ 重命名放最后、删除放倒数第二：改名是常用动作但排在危险项之后不显眼，
           而删除必须靠后且带确认。★ 不提供「切换分类」入口（跨分类的代价见服务端）。 */
      var fa = node.querySelector("a");
      items.push({ label: "重命名", act: "feed-rename",
                   feed: feedId, title: fa ? fa.textContent.trim() : "" });
      items.push({ label: "删除 feed", act: "feed-delete",
                   feed: feedId, title: fa ? fa.textContent.trim() : "" });
      return items;
    },
    entry: function (row) {
      var id = (row.id || "").replace("row-", "");
      var items = [0, 1, 2, 3].map(function (lv) {
        return { label: "设为 L" + lv, act: "level", ids: id, level: lv };
      });
      /* ★ 2026-11-20：无分级判据的源不显示「立即判定/重新判定」——
         后端 rate_single 对这种源走零成本默认路径（不调模型，只写 default），
         给了入口也是假动作。判据口径与树节点 data-cfg 同源（_annotate_cfg
         已把「分类继承」算进 f.has_cfg），所以这里直接读行的 data-cfg。
         ★ 「立即判定」「重新判定」两个字符串必须留着（verify_ui_shell /
         verify_triage_state 钉了它们），只把 push 收进条件里。 */
      if (row.getAttribute("data-cfg") === "1") {
        items.push(row.getAttribute("data-rated") === "1"
          ? { label: "重新判定", act: "rate-re", ids: id }
          : { label: "立即判定", act: "rate-now", ids: id });
      }
      /* ★ 标题翻译：有译文 → 重新翻译；没译文但 feed 开着 → 翻译；
         feed 没开 → 不显示（译了也看不到，徒增困惑）。 */
      if (row.getAttribute("data-translated") === "1") {
        items.push({ label: "重新翻译标题", act: "tr-re", ids: id });
      } else if (row.getAttribute("data-feed-translate") === "1") {
        items.push({ label: "翻译标题", act: "tr-re", ids: id });
      }
      /* ★ 抓取全文（fetch original content）：很多源只给一句话摘要，
         右键抓源站正文用于阅读/总结。不显示"已抓过"状态在菜单上——
         重复抓取由后端 need_confirm 统一弹框（同时覆盖划线锚点警告），
         避免给每行加 data 属性 + 列表多行查库。 */
      /* ★ 「标记为未读」只给已读行：点开即标已读，误点开要有可逆出口；
         未读行给这个入口是空动作（点了状态没变，只会让人怀疑是否生效）。
         行上的 data-unread 由 _row.html 渲染、展开正文后由 afterSwap 同步。 */
      if (row.getAttribute("data-unread") === "0") {
        items.push({ label: "标记为未读", act: "mark-unread", ids: id });
      }
      items.push({ label: "抓取全文", act: "fetch-full", ids: id });
      /* ★ 2026-11-16 撤销「发送到 wiki」右键入口（用户拍板）：文章正文里
         噪声太大，wiki 只接收**划线 / 批注**这类「我的判断」信号。
         后端 `/wiki/ingest` 与 core 层的 `force` 提炼仍在（脚本 / 测试可用），
         但界面不再暴露这条入口 —— 否则人一点就走进「整篇正文提候选」的
         低质量路径，还让 `WIKI_INGEST_ARTICLE_CHARS` 的后半篇截断变成
         需要纠结的问题。★ 别顺手加回来：`verify_wiki.py` 有守卫钉住
         app.js 里不得再出现该动作名。 */
      return items;
    }
  };

  /* ★ 用 DOM 节点 + textContent 拼菜单，不用 innerHTML：
     菜单里会出现**用户自己起的组名**，innerHTML 拼接等于把组名当 HTML 解析。 */
  function showCtxMenu(x, y, items) {
    var menu = document.getElementById("ctx-menu");
    if (!menu) return;
    menu.innerHTML = "";
    items.forEach(function (it, i) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("data-idx", String(i));
      b.textContent = it.label;
      menu.appendChild(b);
    });
    menu.hidden = false;
    menu.style.left = x + "px";
    menu.style.top = y + "px";
    menu._items = items;
  }

  function hideCtxMenu() {
    var menu = document.getElementById("ctx-menu");
    if (menu) { menu.hidden = true; menu._items = null; }
  }

  /* 删除分类后，若当前正看着这个分类（或它下面的分组），回「全部文章」——
     否则会停在一个已经不存在的 `?sel=cat:N` 上。 */
  function selUnderCategory(catId) {
    var sel = currentSel();
    if (sel === "cat:" + catId) return true;
    if (sel.indexOf("group:") === 0) {
      var gid = sel.split(":")[1];
      var g = document.querySelector('.node.group[data-group="' + gid + '"]');
      var c = g && g.closest(".node.cat");
      return !!c && c.getAttribute("data-cat") === String(catId);
    }
    return false;
  }

  function afterCategoryDelete(catId) {
    if (selUnderCategory(catId)) location.href = "/";
    else location.reload();
  }

  /* ---------------- 标题翻译队列（2026-11-20） ----------------
     ★ 后端仍持全局锁 + 逐篇判预算闸（翻译花钱，不能并发冲破闸）——前端做的是
       「排队 + 局部刷新」，不是并发打请求：连点多篇自动逐篇翻，每篇成功只换
       那行的标题 DOM，不整页 reload（reload 会冲掉展开的正文和滚动位置）。
     ★ 局部刷新走 /hx/row/<id>?local=1 拿**服务端渲染**的行，只摘 .col-title ——
       前端不自己拼译文（权威渲染在服务端，与对话 done 帧同一原则）。
     ★ 遇「翻译任务正在跑」（后台 tick / 别的源回填占锁）→ 留队首延迟重试，
       不弹失败；有上限防死循环。同一 id 不重复入队。 */
  var trQueue = [];
  var trPumping = false;
  var trRetries = {};
  var TR_MAX_RETRY = 20;      // 20 × 2.5s ≈ 50s，后台回填通常 1 分钟内结束
  var TR_RETRY_MS = 2500;

  function trEnqueue(id) {
    id = String(id);
    if (trQueue.indexOf(id) >= 0) {
      window.toast("这篇已在翻译队列里", 2000);
      return;
    }
    trQueue.push(id);
    window.toast("已加入翻译队列（" + trQueue.length + " 篇）", 2200);
    trPump();
  }

  function trRefreshRow(id) {
    fetch("/hx/row/" + encodeURIComponent(id) + "?local=1",
          { headers: { "HX-Request": "true" } })
      .then(function (r) { return r.ok ? r.text() : ""; })
      .then(function (html) {
        if (!html) return;
        var box = document.createElement("div");
        box.innerHTML = html;
        var fresh = box.querySelector(".entry-row");
        var old = document.getElementById("row-" + id);
        if (!fresh || !old) return;
        var tNew = fresh.querySelector(".col-title");
        var tOld = old.querySelector(".col-title");
        if (tNew && tOld) tOld.innerHTML = tNew.innerHTML;   // 权威渲染：只摘标题
        old.setAttribute("data-translated", "1");            // 下次右键变「重新翻译」
      })
      .catch(function () { /* 局部刷新失败不打断队列，下次进列表自然更正 */ });
  }

  /* ★ 抓取全文后的收尾（2026-11-20）：成功只提示，不整页 reload ——
     正文此时还没展开（人在右键菜单里点的），reload 会丢掉列表滚动/展开状态。
     下次展开详情时 hx_entry_detail 会重新 get_entry，自然读到 Miniflux 里的全文。
     ★ 如果该行此刻正展开着（极少：右键在标题行、正文却开着），用 htmx 重发
       详情 POST 换正文；服务端权威渲染，不自己拼 HTML。 */
  function ffDone(id, r) {
    if (!r || !r.ok) {
      window.toast("抓取失败：" + ((r && r.error) || "未知错误"), 7000);
      return;
    }
    var grew = (r.after || 0) > (r.before || 0);
    window.toast("已抓取全文" + (grew ? "（" + r.before + " → " + r.after + " 字）" : "")
                 + "，展开即可阅读", 6000);
    var detail = document.getElementById("detail-" + id);
    if (detail && !detail.hidden && window.htmx) {
      var head = detail.closest(".entry-row").querySelector(".entry-head");
      if (head) window.htmx.trigger(head, "click");   // 重发展开 → 换正文
    }
  }

  function trPump() {
    if (trPumping || !trQueue.length) return;
    trPumping = true;
    var id = trQueue[0];
    var delay = 250;                       // 正常篇间小歇
    window.toast("翻译标题中…（队列 " + trQueue.length + " 篇）", 60000);
    postForm("/translate/entry", { entry_id: id })
      .then(function (r) {
        if (r && r.ok) {
          trQueue.shift();
          delete trRetries[id];
          trRefreshRow(id);
          window.toast("已翻译，剩 " + trQueue.length + " 篇", 1800);
        } else {
          var err = (r && r.error) || "未知错误";
          if (err.indexOf("正在跑") >= 0 && (trRetries[id] || 0) < TR_MAX_RETRY) {
            trRetries[id] = (trRetries[id] || 0) + 1;
            delay = TR_RETRY_MS;           // 留队首、延迟重试；不 shift
          } else {
            trQueue.shift();
            delete trRetries[id];
            window.toast("翻译失败：" + err, 6000);
          }
        }
      })
      .catch(function () {
        trQueue.shift();
        delete trRetries[id];
        window.toast("翻译请求失败：" + id, 6000);
      })
      .then(function () {
        trPumping = false;
        if (trQueue.length) setTimeout(trPump, delay);
      });
  }

  function runCtx(item) {
    hideCtxMenu();
    if (item.act === "config") {
      openModal("/triage/config?scope=" + item.scope);
    } else if (item.act === "prompt") {
      openModal("/triage/prompt?feed=" + item.scope.split(":")[1]);
    } else if (item.act === "fast-on" || item.act === "fast-off") {
      // ★ 快速刷新走 API refresh（绕过 worker 池与 host 限额），由后端按
      //   FeedOverride.fast_interval_min 节流；这里只负责开关配置
      postForm("/triage/fastrefresh", {
        feed: item.scope.split(":")[1],
        action: item.act === "fast-on" ? "on" : "off"
      }).then(function () { location.reload(); });
    } else if (item.act === "tr-on" || item.act === "tr-off") {
      /* ★ 开启回填是后台任务：立刻返回，前端轮询 /translate/status，
         期间先 toast 长提示 —— 50 条标题要几秒到几十秒，不能让人以为没反应。 */
      var trFeed = item.scope.split(":")[1];
      postForm("/translate/feed", {
        feed: trFeed, action: item.act === "tr-on" ? "on" : "off"
      }).then(function (r) {
        if (!r || !r.ok) {
          window.toast("标题翻译设置失败：" + ((r && r.error) || "未知错误"), 6000);
          return;
        }
        if (item.act === "tr-off") {
          window.toast("已关闭标题翻译（译文保留，重开不用重翻）", 2500);
          setTimeout(function () { location.reload(); }, 500);
          return;
        }
        window.toast("已开启，正在翻译最近 " + (r.days || 7) + " 天（最多 "
                     + (r.max || "N") + " 条）…", 60000);
        if (r.queued) window.toast("已有翻译任务在跑，稍后自动继续…", 4000);
        pollTranslate();
      }).catch(function () {
        window.toast("标题翻译设置失败，请重试", 6000);
      });
    } else if (item.act === "tr-re") {
      /* ★ 2026-11-20：改为排队 + 局部刷新。后端仍串行判闸/持锁，前端连点
         多篇不再被「已有任务在跑」直接拒绝，也不再每篇整页 reload。 */
      trEnqueue(item.ids);
    } else if (item.act === "fetch-full") {
      /* ★ 抓取全文：Miniflux 同步打源站（慢站几十秒），长 toast。
         覆盖前二次确认由后端 need_confirm 统一判（已抓过 / 有划线锚点），
         confirm=1 重发 —— 与日报重跑同契约，后端权威。 */
      var ffId = item.ids;
      window.toast("正在抓取全文，源站较慢可能要几十秒…", 120000);
      postForm("/hx/fetch-full/" + encodeURIComponent(ffId), {}).then(function (r) {
        if (r && r.need_confirm) {
          if (!confirm(r.msg)) { window.toast("已取消抓取", 3000); return; }
          return postForm("/hx/fetch-full/" + encodeURIComponent(ffId),
                          { confirm: "1" }).then(function (r2) { ffDone(ffId, r2); });
        }
        ffDone(ffId, r);
      }).catch(function () { window.toast("抓取请求失败，请重试", 6000); });
    } else if (item.act === "refresh") {
      // ★ 右键刷新与工具栏刷新走同一条链路（后台任务 + 轮询 + 重绘树）
      startRefresh({ sel: item.scope });
    } else if (item.act === "rate-now" || item.act === "rate-re") {
      if (item.act === "rate-re" &&
          !confirm("重新判定会覆盖这条已有的评级（含人工调整），继续？")) return;
      window.toast(item.act === "rate-re" ? "重新判定中…" : "判定中…", 60000);
      postForm("/triage/rate-now", {
        entry_id: item.ids, mode: item.act === "rate-re" ? "re" : "now"
      }).then(function (r) {
        if (!r.ok) { window.toast("判定失败：" + (r.error || "未知错误"), 6000); return; }
        window.toast((r.detail || "已判定") + "，正在刷新…", 2500);
        setTimeout(function () { location.reload(); }, 600);
      });
    } else if (item.act === "mark-unread") {
      /* ★ 退回未读：只写 Miniflux，成功才刷新（列表筛选/树未读数都跟着变）。
         失败不 reload、直接给上游人话 —— 静默成功会让人以为点过了。 */
      var muId = item.ids;
      postForm("/hx/mark-unread/" + encodeURIComponent(muId), {}).then(function (r) {
        if (!r || !r.ok) {
          window.toast("标记为未读失败：" + ((r && r.error) || "未知错误"), 6000);
          return;
        }
        window.toast("已标记为未读，正在刷新…", 2000);
        setTimeout(function () { location.reload(); }, 400);
      }).catch(function () { window.toast("标记为未读失败，请重试", 6000); });
    } else if (item.act === "level") {
      if (item.level === 0 && !confirm("设为 L0 会标为已读（从视野消失），确定？")) return;
      postForm("/triage/level", {
        entry_ids: item.ids, level: item.level, note: "手工调整"
      }).then(function () { location.reload(); });
      /* ★ 2026-11-16：原「发送到 wiki」动作分支已撤销（入口与理由见上面
         `items` 构建处的注释）—— 文章内噪声太大，wiki 只接收划线 / 批注
         等高质量信号；后端 `/wiki/ingest` 与 core 的 `force` 提炼仍在，
         脚本 / 测试可用，界面不再有入口。
         ★ 不留「反正走不到」的分支：菜单项撤了、handler 还留着，谁把菜单项
           加回来就立刻复活低质量路径。 */

    /* ---------------- 分类 / feed 写操作（写 Miniflux）----------------
       ★ 分类与 feed 的增改是**同步**写 Miniflux；成功才 reload 树。
         feed 新增时 Miniflux 会立刻抓取，慢源可能 10–30 秒，所以先用长 toast 等待。 */
    } else if (item.act === "cat-create") {
      var cname = prompt("新建分类名称（建议带数字前缀，如 60-播客）");
      if (!cname) return;
      postForm("/tree/category/create", { title: cname }).then(function (r) {
        if (!r.ok) { window.toast("新建分类失败：" + (r.error || "未知错误"), 6000); return; }
        location.reload();
      });
    } else if (item.act === "cat-rename") {
      var cnew = prompt("重命名分类", item.title || "");
      if (!cnew) return;
      postForm("/tree/category/rename", { category_id: item.cat, title: cnew })
        .then(function (r) {
          if (!r.ok) { window.toast("重命名失败：" + (r.error || "未知错误"), 6000); return; }
          location.reload();
        });
    } else if (item.act === "cat-delete") {
      if (!confirm("移除分类「" + (item.title || "") + "」？\n\n只允许空分类（0 个源）。"
                   + "该分类下的空分组和分类判据也会一起删除。")) return;
      postForm("/tree/category/delete", { category_id: item.cat }).then(function (r) {
        if (!r.ok) { window.toast("移除失败：" + (r.error || "未知错误"), 6000); return; }
        if (r.warning) {
          window.toast(r.warning, 8000);
          setTimeout(function () { afterCategoryDelete(item.cat); }, 1500);
          return;
        }
        afterCategoryDelete(item.cat);
      });
    } else if (item.act === "feed-add") {
      var furl = prompt("Feed URL（RSS / Atom）");
      if (!furl) return;
      window.toast("正在添加并抓取，可能需要 10–30 秒…", 90000);
      postForm("/tree/feed/create", {
        feed_url: furl, category_id: item.cat, group_id: item.group || ""
      }).then(function (r) {
        if (!r.ok) { window.toast("添加失败：" + (r.error || "未知错误"), 8000); return; }
        if (r.warning) {
          window.toast(r.warning, 8000);
          setTimeout(function () { location.reload(); }, 1500);
          return;
        }
        location.reload();
      }).catch(function () {
        window.toast("添加请求失败，请重试", 6000);
      });

    /* ---------------- 虚拟分组（P0 右键路径）----------------
       ★ 全部只改本地 SQLite，不碰 Miniflux；失败一律 toast 出后端原话
         （例如"只能在本分类内移动"），不要吞掉。 */
    } else if (item.act === "grp-create") {
      var gname = prompt("新建分组名称（只是阅读页的折叠层，不影响分级 / 总结 / 开销）");
      if (!gname) return;
      postForm("/groups/create", { category_id: item.cat, title: gname })
        .then(function (r) {
          if (!r.ok) { window.toast("建组失败：" + (r.error || "未知错误"), 6000); return; }
          location.reload();
        });
    } else if (item.act === "grp-rename") {
      var gnew = prompt("重命名分组", item.title || "");
      if (!gnew) return;
      postForm("/groups/rename", { group_id: item.group, title: gnew })
        .then(function (r) {
          if (!r.ok) { window.toast("改名失败：" + (r.error || "未知错误"), 6000); return; }
          location.reload();
        });
    } else if (item.act === "grp-delete") {
      if (!confirm("删除分组「" + (item.title || "") + "」？组内的源会回到分类下（不会取消订阅）。")) return;
      postForm("/groups/delete", { group_id: item.group }).then(function (r) {
        if (!r.ok) { window.toast("删除失败：" + (r.error || "未知错误"), 6000); return; }
        location.reload();
      });
    } else if (item.act === "grp-move") {
      postForm("/groups/move", { feed_id: item.feed, group_id: item.group })
        .then(function (r) {
          if (!r.ok) { window.toast("移动失败：" + (r.error || "未知错误"), 6000); return; }
          window.toast(r.detail || "已移动", 2000);
          location.reload();
        });

    /* ---------------- 拖动重排：撤销整层的手动顺序 ----------------
       ★ 默认顺序是后端算的（名称 / 创建序 / 未读优先），前端无从推断，
         所以这里必须 reload 才能看到复原后的顺序；成功不 reload 就成了
         "点了没反应"。拖动本身不需要 reload（DOM 已经是新顺序）。 */
    } else if (item.act === "order-reset") {
      postForm("/tree/order/reset", { kind: item.kind, scope: item.scope || "" })
        .then(function (r) {
          if (!r.ok) { window.toast("恢复失败：" + (r.error || "未知错误"), 6000); return; }
          window.toast(r.detail || "已恢复默认顺序", 1800);
          location.reload();
        });

    /* ---------------- feed 重命名 / 删除（写 Miniflux，不可逆）----------------
       ★ 删除前把后果说全：Miniflux 会连这个源的**全部条目**一起删，不是"取消
         订阅但留历史"。只写"确定删除吗"等于没说。 */
    } else if (item.act === "feed-rename") {
      var fnew = prompt("重命名订阅源", item.title || "");
      if (!fnew || fnew === item.title) return;
      postForm("/tree/feed/rename", { feed_id: item.feed, title: fnew })
        .then(function (r) {
          if (!r.ok) { window.toast("重命名失败：" + (r.error || "未知错误"), 6000); return; }
          window.toast("已重命名，正在刷新…", 1500);
          setTimeout(function () { location.reload(); }, 400);
        }).catch(function () {
          window.toast("重命名请求失败，请重试", 6000);
        });
    } else if (item.act === "feed-delete") {
      var fname = item.title || ("#" + item.feed);
      if (!confirm("删除订阅源「" + fname + "」？\n\n"
                 + "· 该源的**全部条目会一起删除**（不是取消订阅保留历史）\n"
                 + "· 本地的分级配置、分组归属、标题翻译也会清掉\n"
                 + "· 此操作**不可撤销**\n\n"
                 + "只是想少看，请用「配置分级逻辑」把它压到 L0。")) return;
      postForm("/tree/feed/delete", { feed_id: item.feed, confirm: "1" })
        .then(function (r) {
          if (!r.ok) { window.toast("删除失败：" + (r.error || "未知错误"), 8000); return; }
          if (r.warning) {
            window.toast(r.warning, 8000);
            setTimeout(function () { location.reload(); }, 1500);
            return;
          }
          window.toast("已删除「" + fname + "」", 2500);
          setTimeout(function () { location.reload(); }, 400);
        }).catch(function () {
          window.toast("删除请求失败，请重试", 6000);
        });
    }
  }

  /* ---------------- 立即刷新（工具栏 + 右键共用） ----------------
     ★ 刷新是**后台任务**：POST /hx/refresh 立刻返回这一批要刷的 feed，
       前端先把树上对应的源点亮成 loading，再轮询 /hx/refresh/status，
       全部跑完后重绘树与列表（/hx/entries 的 OOB 会连标题和树一起换）。
       这样即使某个源卡住 30 秒，页面也不会像"点了没反应"。
     ★ loading 放在**警告图标的位置**：有异常时警告被 loading 顶掉；
       刷完重绘后，仍然失败的源会重新长出警告图标。 */
  var refreshPollTimer = null;
  var refreshStartedAt = 0;

  function currentSel() {
    var el = document.querySelector("input[name='sel']");
    return el ? el.value : "all";
  }

  function setFeedBusy(feedId, on) {
    var node = document.querySelector('#tree .node.feed[data-feed="' + feedId + '"]');
    if (!node) return;
    var row = node.querySelector(".node-row");
    if (!row) return;
    if (on) {
      node.classList.add("is-busy");
      if (!row.querySelector(".spin")) {
        var sp = document.createElement("span");
        sp.className = "spin";
        sp.title = "刷新中…";
        /* 插到警告图标前（CSS 在 busy 时把警告隐藏），没有警告就插到未读数前 */
        var anchor = row.querySelector(".warn") || row.querySelector(".count");
        if (anchor) row.insertBefore(sp, anchor);
        else row.appendChild(sp);
      }
    } else {
      node.classList.remove("is-busy");
      var old = row.querySelector(".spin");
      if (old) old.remove();
    }
  }

  function clearSpins() {
    document.querySelectorAll("#tree .node.feed.is-busy").forEach(function (n) {
      n.classList.remove("is-busy");
      var sp = n.querySelector(".node-row > .spin");
      if (sp) sp.remove();
    });
  }

  function setRefreshBtnBusy(on) {
    var btn = document.getElementById("btn-refresh");
    if (!btn) return;
    btn.classList.toggle("is-busy", !!on);
    btn.disabled = !!on;
  }

  /* 重绘列表：/hx/entries 的响应里带 #tree / #toolbar-title / #mark-form 的 OOB，
     所以一次请求就能把树、标题、列表一起更新。 */
  function reloadEntries() {
    if (!document.getElementById("entry-list") || !window.htmx) return;
    var qs = location.search ? location.search.slice(1) : "";
    window.htmx.ajax("GET", "/hx/entries?" + (qs ? qs + "&" : "") + "nocache=1",
                     { target: "#entry-list", swap: "innerHTML" });
  }

  /* 逐源熄灯：`states` 里已经跑完的源单独收掉 loading，
     不等整批 —— 否则一个 60 秒的 Twitter 源会拖着已经刷完的 5 个源一起转。 */
  function setFeedDone(feedId, ok) {
    setFeedBusy(feedId, false);
    if (!ok) return;
    /* 成功的源先把旧警告灭掉（最终重绘以服务端 parsing_error_count 为准） */
    var node = document.querySelector('#tree .node.feed[data-feed="' + feedId + '"]');
    var warn = node && node.querySelector(".warn");
    if (warn) warn.style.display = "none";
  }

  function tickRefresh(st) {
    var states = (st && st.states) || {};
    ((st && st.feeds) || []).forEach(function (id) {
      var s = states[String(id)];
      if (s && s !== "pending") setFeedDone(id, s === "ok");
    });
  }

  function pollRefresh(onTick, done) {
    if (refreshPollTimer) clearInterval(refreshPollTimer);
    refreshPollTimer = setInterval(function () {
      fetch("/hx/refresh/status")
        .then(function (r) { return r.json(); })
        .then(function (st) {
          if (onTick) onTick(st);
          /* 15 分钟兜底：状态丢了（dev server 重启）也不能永远转圈 */
          if (st.running && Date.now() - refreshStartedAt < 15 * 60 * 1000) return;
          clearInterval(refreshPollTimer);
          refreshPollTimer = null;
          done(st);
        })
        .catch(function () {
          if (Date.now() - refreshStartedAt < 15 * 60 * 1000) return;
          clearInterval(refreshPollTimer);
          refreshPollTimer = null;
          done({});
        });
    }, 700);
  }

  /* 失败原因压成一行给人看：超时 / 500 这两种最常见，别把整段 traceback 倒出来 */
  function shortErr(e) {
    e = String(e || "");
    if (/timed out|timeout/i.test(e)) return "超时";
    if (/\b500\b/.test(e)) return "Miniflux 500";
    if (/\b502\b|\b503\b/.test(e)) return "Miniflux 5xx";
    return e.length > 36 ? e.slice(0, 36) + "…" : e;
  }

  function finishRefresh(st) {
    clearSpins();
    setRefreshBtnBusy(false);
    if (document.getElementById("entry-list")) {
      reloadEntries();
    } else if (st && st.feeds && st.feeds.length) {
      /* /costs、/summary 这些页也有侧栏树，但没有 #entry-list：
         整页重载最省事，也保证警告图标反映刷新后的真实状态。 */
      location.reload();
    }
    if (!st || !st.total) return;
    var msg = "刷新完成 " + (st.ok || 0) + "/" + st.total + " 个源";
    if (st.failed) {
      var e = (st.errors && st.errors[0]) || {};
      var who = e.title || ("feed " + (e.feed_id || "?"));
      msg += "；" + st.failed + " 个失败（" + who + "：" + shortErr(e.error) + "）";
    }
    window.toast(msg, st.failed ? 9000 : 2500);
  }

  function startRefresh(payload) {
    if (refreshPollTimer) return;          // 已经有一批在轮询
    setRefreshBtnBusy(true);
    postForm("/hx/refresh", payload).then(function (r) {
      if (!r || !r.ok) {
        setRefreshBtnBusy(false);
        window.toast((r && r.error) || "刷新失败", 5000);
        return;
      }
      (r.feeds || []).forEach(function (id) { setFeedBusy(id, true); });
      if (!r.started) window.toast("已有刷新在进行，完成后自动更新", 2500);
      else if (r.skipped) window.toast("本次只刷前 " + r.total + " 个源（跳过 " + r.skipped + " 个）", 4000);
      refreshStartedAt = Date.now();
      pollRefresh(tickRefresh, finishRefresh);
    }).catch(function () {
      setRefreshBtnBusy(false);
      window.toast("刷新请求失败，请重试", 5000);
    });
  }

  /* ---------------- 标题翻译：开启回填 + 重译的状态轮询 ----------------
     ★ 回填在后台线程里跑（与刷新同款），这里只负责把「正在翻」变成看得见的反馈。
     10 分钟兜底：即使状态丢了（dev server 重启），也不能让人一直等。 */
  var translatePollTimer = null;

  function pollTranslate() {
    if (translatePollTimer) clearInterval(translatePollTimer);
    var started = Date.now();
    translatePollTimer = setInterval(function () {
      fetch("/translate/status").then(function (r) { return r.json(); })
        .then(function (st) {
          if (st && st.running && Date.now() - started < 10 * 60 * 1000) return;
          clearInterval(translatePollTimer);
          translatePollTimer = null;
          var msg = "翻译完成";
          if (st && st.budget_stop) msg = "翻译池预算已用尽，剩余条目稍后继续";
          else if (st && st.error) msg = "翻译结束：" + shortErr(st.error);
          window.toast(msg, 5000);
          setTimeout(function () { location.reload(); }, 700);
        })
        .catch(function () {
          if (Date.now() - started < 10 * 60 * 1000) return;
          clearInterval(translatePollTimer);
          translatePollTimer = null;
          window.toast("翻译状态获取失败，刷新页面查看结果", 5000);
        });
    }, 1500);
  }

  /* 页面加载时若已有批次在跑（刷新页面 / 同时开了两个标签页），跟随它 */
  function syncRefreshOnLoad() {
    if (!document.getElementById("tree")) return;
    fetch("/hx/refresh/status").then(function (r) { return r.json(); })
      .then(function (st) {
        if (!st || !st.running) return;
        (st.feeds || []).forEach(function (id) { setFeedBusy(id, true); });
        setRefreshBtnBusy(true);
        refreshStartedAt = Date.now();
        pollRefresh(tickRefresh, finishRefresh);
      })
      .catch(function () { /* 状态拿不到就算了，别打扰 */ });
  }

  /* ★ 工具栏刷新 = 刷新「当前范围（sel）的全部源」。
     ★★ 刻意**不用**"当前列表里出现过的源"：一个暂时没有条目的源（被过滤掉、
        或本来就还没抓到东西）永远不会出现在列表里，那样就永远救不回来。
     实际要刷哪些源由后端按 `sel` 推导（分类 / 分组 / 单源 / 全部）。 */
  (function bindRefreshButton() {
    var btn = document.getElementById("btn-refresh");
    if (!btn) return;
    btn.addEventListener("click", function () {
      if (refreshPollTimer) return;
      startRefresh({ sel: currentSel() });
    });
  })();

  /* ---------------- 柱图 hover 明细 ----------------
     ★ 原生 title 不能加粗、也不能保证"柱子上方空白处"都触发；这里用全局浮层。
       内容来自每根柱子里的隐藏 .bar-tip（服务端已转义），复制 innerHTML 不会引入用户输入。
     ★ 浮层用 position:fixed：.bars 有 overflow-x:auto，内部 absolute 会被裁掉。
     ★ 事件委托到 document：柱子是服务端渲染的（没有局部刷新），但委托更省事也更快。 */
  (function barTooltip() {
    var tip = document.getElementById("bar-tip");
    if (!tip) return;
    var current = null;

    function place(x, y) {
      var pad = 14;
      var w = tip.offsetWidth, h = tip.offsetHeight;
      var left = x + pad, top = y + pad;
      if (left + w > window.innerWidth - 8) left = x - w - pad;
      if (top + h > window.innerHeight - 8) top = y - h - pad;
      tip.style.left = Math.max(8, left) + "px";
      tip.style.top = Math.max(8, top) + "px";
    }

    function hide() {
      tip.hidden = true;
      current = null;
      focusKind(null);
    }

    /* 高亮某个色块对应的那一行，其余行变淡（CSS: .has-focus .tip-row） */
    function focusKind(kind) {
      var rows = tip.querySelectorAll(".tip-row");
      var hit = false;
      rows.forEach(function (row) {
        var on = !!kind && row.getAttribute("data-kind") === kind;
        row.classList.toggle("is-hot", on);
        if (on) hit = true;
      });
      tip.classList.toggle("has-focus", hit);
    }

    document.addEventListener("mouseover", function (e) {
      var col = e.target.closest ? e.target.closest(".bar-col") : null;
      if (!col) return;
      var src = col.querySelector(".bar-tip");
      if (!src) { hide(); return; }
      if (current !== src) {
        tip.innerHTML = src.innerHTML;
        current = src;
        tip.hidden = false;
        place(e.clientX, e.clientY);
      }
      /* ★ 悬停到某个色块 → 高亮对应明细；悬停在柱子上方空白 → 全部恢复 */
      var seg = e.target.closest ? e.target.closest(".bar-seg") : null;
      focusKind(seg ? seg.getAttribute("data-kind") : null);
    });

    document.addEventListener("mousemove", function (e) {
      if (tip.hidden || !current) return;
      place(e.clientX, e.clientY);
    });

    document.addEventListener("mouseout", function (e) {
      var col = e.target.closest ? e.target.closest(".bar-col") : null;
      if (!col) return;
      var to = (e.relatedTarget && e.relatedTarget.closest)
        ? e.relatedTarget.closest(".bar-col") : null;
      if (to !== col) hide();
    });

    /* 滚动（含 .bars 的横向滚动）时先收起，避免浮层和柱子脱节 */
    window.addEventListener("scroll", hide, true);
  })();

  function postForm(url, fields) {
    var body = Object.keys(fields).map(function (k) {
      return encodeURIComponent(k) + "=" + encodeURIComponent(fields[k]);
    }).join("&");
    return fetch(url, {
      method: "POST",
      headers: {
        "X-CSRFToken": getCookie("csrftoken"),
        "Content-Type": "application/x-www-form-urlencoded"
      },
      body: body
    }).then(function (r) { return r.json().catch(function () { return { ok: r.ok }; }); });
  }

  function getCookie(name) {
    var m = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
    return m ? m[2] : "";
  }

  /* ---------------- 轻量提示 ----------------
     ★ 单条判定是同步的（一次模型调用几秒到几十秒），没有反馈会让人以为右键没生效。 */
  var toastTimer = null;
  window.toast = function (msg, ms) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = msg;
    el.hidden = false;
    if (toastTimer) clearTimeout(toastTimer);
    if (ms) toastTimer = setTimeout(function () { el.hidden = true; }, ms);
  };

  /* ★ 弹框可叠加：每次 openModal 追加一层，关闭只弹掉触发元素所在的那一层，
     这样「配置分级逻辑」里再点「查看 prompt」，关掉 prompt 还能回到配置弹框。 */
  window.openModal = function (url) {
    var root = document.getElementById("modal-root");
    if (!root) return;
    fetch(url, { headers: { "HX-Request": "true" } })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var layer = document.createElement("div");
        layer.className = "modal-layer";
        layer.style.zIndex = 40 + root.childElementCount * 2;
        layer.innerHTML = html;
        root.appendChild(layer);
        window.htmx.process(layer);
        /* ★ 审批浮层新打开即滚到底（2026-11-19）：对话历史在 .wc-log 里内滚，
           新进来默认停在顶部要手动滚一次才看到最新回答。只认审批浮层，
           别的浮层（设置/prompt）不碰。
           ★ 发送/清空后的滚动由下面 htmx:beforeSwap/afterSwap 那套负责，这里
           只管「刚打开」这一次，两者不冲突。 */
        if (layer.querySelector(".approval-chat")) {
          var alog = layer.querySelector(".approval-chat .wc-log");
          if (alog) wcScrollBottom(alog);
        }
      });
  };

  window.closeModal = function (el) {
    var root = document.getElementById("modal-root");
    if (!root) return;
    var layer = (el && el.closest) ? el.closest(".modal-layer") : null;
    if (!layer) layer = root.lastElementChild;   // 没传元素就关最上层
    if (layer) layer.remove();
  };

  /* 查看 prompt 弹框：全屏 / 还原 */
  window.toggleFullscreen = function (btn) {
    var modal = btn.closest(".modal");
    if (!modal) return;
    var on = modal.classList.toggle("fullscreen");
    btn.textContent = on ? "⤡ 退出全屏" : "⤢ 全屏";
  };

  /* ---------------- 设置面板（左列分组 / 右列设置项） ----------------
     ★ 三个函数必须是**全局的**：面板是 openModal 塞进 innerHTML 的片段，片段里内联的
       <script> 不会被执行，只有 onclick / onchange 这类属性处理器找得到全局函数。
     ★ 保存后**不 reload**：重载会把弹窗一起干掉，所以服务端回带生效值，前端就地回填。 */
  window.prefsTab = function (btn) {
    var shell = btn.closest(".prefs-shell");
    if (!shell) return;
    var key = btn.dataset.g;
    shell.querySelectorAll(".prefs-tab").forEach(function (b) {
      b.classList.toggle("active", b === btn);
    });
    shell.querySelectorAll(".pref-group").forEach(function (sec) {
      sec.hidden = sec.dataset.g !== key;
    });
    /* ★ 外框定高、右列内滚：切组时把右列滚回顶部，否则会停在上一组残留的
       滚动位置上，看起来像"内容错位"。 */
    var main = shell.querySelector(".prefs-main");
    if (main) main.scrollTop = 0;
  };

  function prefsRow(el) {
    return el.closest ? el.closest("tr[data-key]") : null;
  }

  /** 服务端返回的值回填到控件（bool 要转 checked，不能赋值 "False"）。 */
  function prefsFill(input, value) {
    if (input.type === "checkbox") {
      input.checked = String(value).toLowerCase() === "true" ||
        String(value) === "1";
    } else {
      input.value = value;
    }
  }

  function prefsMarkOver(tr, on) {
    var label = tr.querySelector(".pref-label");
    var tag = tr.querySelector(".pref-over");
    if (on) {
      if (!tag) {
        tag = document.createElement("span");
        tag.className = "pref-over";
        tag.title = "已在本页改过，删除后回退 .env/默认";
        tag.textContent = "已改";
        label.appendChild(tag);
      }
    } else if (tag) {
      tag.remove();
    }
    var resetBtn = tr.querySelector(".pref-reset");
    if (resetBtn) resetBtn.hidden = !on;
  }

  window.prefsSave = function (input) {
    var tr = prefsRow(input);
    if (!tr) return;
    var key = tr.dataset.key;
    var value = input.type === "checkbox" ? (input.checked ? "1" : "0") : input.value;
    postForm("/prefs/save", { key: key, value: value }).then(function (res) {
      if (!res.ok) {
        window.toast(res.err || "保存失败", 2500);
        return;
      }
      if (res.value !== undefined) prefsFill(input, res.value);
      prefsMarkOver(tr, true);
      window.toast("已保存", 1800);
    });
  };

  window.prefsReset = function (btn) {
    var tr = prefsRow(btn);
    if (!tr) return;
    var input = tr.querySelector(".pref-field");
    postForm("/prefs/reset", { key: tr.dataset.key }).then(function (res) {
      if (!res.ok) {
        window.toast(res.err || "回退失败", 2500);
        return;
      }
      if (input && res.value !== undefined) prefsFill(input, res.value);
      prefsMarkOver(tr, false);
      window.toast("已回退 .env / 默认值", 2200);
    });
  };

  /* ---------------- .env 配置组（数据源 / 模型 / 运行参数） ----------------
     ★ 密码框留空 = 保持原值；「清除」勾上才发空值，服务端也按同一语义处理。
     ★ 整组一次提交：数据源引擎与地址/账号之间有「必填组合」校验，逐项 onchange
       保存会出现"填了一半被拒"的中间态。 */
  window.prefsSaveEnv = function (btn) {
    var sec = btn.closest(".pref-group");
    if (!sec) return;
    var fields = {};
    sec.querySelectorAll("tr[data-key]").forEach(function (tr) {
      var input = tr.querySelector(".pref-field");
      if (!input) return;
      var key = tr.dataset.key;
      if (input.type === "password") {
        var clear = tr.querySelector(".pref-clear-box");
        if (clear && clear.checked) fields[key + "__clear"] = "1";
        else if (input.value !== "") fields[key] = input.value;
        return;
      }
      if (input.type === "checkbox") fields[key] = input.checked ? "1" : "0";
      else fields[key] = input.value;
    });
    btn.disabled = true;
    postForm("/prefs/env", fields).then(function (res) {
      btn.disabled = false;
      if (!res.ok) {
        window.toast(res.err || "保存失败", 4000);
        return;
      }
      window.toast(res.message || "已保存", 1600);
      /* 刷新一次让「已设置 / 文件」徽标与生效值同步；等 toast 露个脸再刷 */
      setTimeout(function () { window.location.reload(); }, 700);
    }).catch(function () {
      btn.disabled = false;
      window.toast("保存失败（网络）", 3000);
    });
  };

  window.prefsTestDatasource = function (btn) {
    btn.disabled = true;
    window.toast("正在连接数据源…", 0);
    postForm("/prefs/env/test", {}).then(function (res) {
      btn.disabled = false;
      window.toast(res.ok ? res.message : ("连接失败：" + (res.err || "未知错误")),
                   res.ok ? 3500 : 8000);
    }).catch(function () {
      btn.disabled = false;
      window.toast("连接测试失败（网络）", 3000);
    });
  };

  /* ---------------- 高级：整份 .env ---------------- */
  window.prefsLoadEnv = function (btn) {
    btn.disabled = true;
    fetch("/prefs/env/raw", { headers: { "Accept": "application/json" } })
      .then(function (r) { return r.json(); })
      .then(function (res) {
        btn.disabled = false;
        if (!res.ok) {
          window.toast(res.err || "载入失败", 3000);
          return;
        }
        var box = document.getElementById("envraw-box");
        var tag = document.getElementById("envraw-chars");
        if (box) box.value = res.text || "";
        if (tag) tag.textContent = (res.text || "").length + " 字";
      })
      .catch(function () {
        btn.disabled = false;
        window.toast("载入失败（网络）", 3000);
      });
  };

  window.prefsSaveEnvRaw = function (btn) {
    var box = document.getElementById("envraw-box");
    if (!box) return;
    if (!box.value.trim()) {
      window.toast("内容为空，已拒绝保存（先载入原文）", 3000);
      return;
    }
    btn.disabled = true;
    postForm("/prefs/env/raw/save", { text: box.value }).then(function (res) {
      btn.disabled = false;
      window.toast(res.ok ? (res.message || "已保存") : ("保存失败：" + (res.err || "")),
                   res.ok ? 3000 : 6000);
    }).catch(function () {
      btn.disabled = false;
      window.toast("保存失败（网络）", 3000);
    });
  };

  /* ---------------- 全局层文件（画像 / 本体） ----------------
     ★ 与键值项的区别：整篇 markdown、几百到几千字，所以是**显式按钮**保存，
       不用 onchange（键值项那种"失焦即存"在长文上等于误触即写盘）。
     ★ 字符数就地更新：用户要判断"是不是被 2000 字上限截断了"（画像侧的硬约束）。 */
  window.prefsCountChars = function (ta) {
    var box = ta.closest(".pref-file");
    if (!box) return;
    var tag = box.querySelector(".pref-file-chars");
    if (tag) tag.textContent = ta.value.length + " 字";
  };

  window.prefsSaveFile = function (btn) {
    var box = btn.closest(".pref-file");
    if (!box) return;
    var ta = box.querySelector(".pref-file-box");
    if (!ta) return;
    btn.disabled = true;                    /* 防连点重复写盘 */
    postForm("/prefs/file", { key: box.dataset.key, value: ta.value })
      .then(function (res) {
        btn.disabled = false;
        if (!res.ok) {
          window.toast(res.err || "保存失败", 3000);
          return;
        }
        window.prefsCountChars(ta);
        /* ★ 回带的是服务端落盘后的字数（已统一 LF / 结尾换行），不是本地 length */
        var tag = box.querySelector(".pref-file-chars");
        if (tag && res.chars !== undefined) tag.textContent = res.chars + " 字";
        window.toast("已保存 · 立即生效" + (res.backup ? "（旧版已备份）" : ""), 2200);
      })
      .catch(function () {
        btn.disabled = false;
        window.toast("保存失败（网络）", 3000);
      });
  };

  document.addEventListener("contextmenu", function (evt) {
    /* ★ 展开的详情区（正文 / 标题 / 相关页 / 工具浮窗）右键交还浏览器原生菜单：
       选中搜索、复制、翻译在这里才有用；「设为 Lx / 立即判定」只在**列表
       标题行**有意义。.entry-detail 嵌在 .entry-row 里，不先挡掉就会被下面的
       closest(".entry-row") 捕获、弹错菜单还屏蔽原生右键（2026-11-19）。 */
    if (evt.target.closest(".entry-detail")) return;
    var node = evt.target.closest("#tree .node");
    var row = evt.target.closest(".entry-row");
    if (!node && !row) {
      /* ★ 树的空白处（ul 下方 / 侧栏留白）：给「新建分类…」 */
      if (!evt.target.closest("#tree, .sidebar")) return;
      evt.preventDefault();
      showCtxMenu(evt.clientX, evt.clientY,
                  [{ label: "新建分类…", act: "cat-create" }]);
      return;
    }
    evt.preventDefault();
    var items = node ? CTX.tree(node) : CTX.entry(row);
    /* 根节点「全部文章」没有自己的操作，右键它等同于树空白处 */
    if (node && node.classList.contains("root")) {
      items = [{ label: "新建分类…", act: "cat-create" }];
    }
    if (!items.length) return;
    showCtxMenu(evt.clientX, evt.clientY, items);
  });

  /* ---------------- 拖动：同层重排 / feed 换分组 ----------------
     ★ 三种落点，规则互不重叠（后端 /tree/reorder、/tree/feed/move 各自再兜一次）：
       1) **同一层、同一种行**（分类↔分类、目录↔同分类内的目录、feed↔同一层的 feed）
          = 重排：画插入线，松手后把**整层**顺序落库。
       2) feed 落在别一层的目录行 / 分类行 / 那些行里的 feed 上 = 换分组（原有语义）。
          跨分类照旧拦：改 Miniflux 的 category_id 会清零 parsing_error_count、
          把历史条目整体改属、立刻切换判据继承。
       3) 其余一律不落（例如把目录拖到 feed 上）—— 混排不支持，目录段与裸 feed 段
          各自一层，插过去没有对应的存储位置。
     ★ 分类行 / 目录行**只有 `.grip` 手柄**能拖（`draggable` 在手柄上，不在行上）：
       行里还有链接和展开箭头，290px 侧栏整行可拖，一不留神就改了一层的顺序。
       feed 行沿用整行可拖（它本来就是"拖去换组"）。
     ★ 重排先改 DOM 再发请求（所见即所得），失败才 reload：顺序只有一份真相，
       不能让页面停在"看着排好了、库里没写"的假象上。 */
  var drag = null;        // {node, kind, scope, ul}
  var dropNode = null;    // 换分组落点的高亮
  var markNode = null;    // 重排插入线所在的行
  var markSide = "";

  function nodeKind(node) {
    if (!node || !node.classList) return "";
    if (node.classList.contains("cat")) return "cat";
    if (node.classList.contains("group")) return "grp";
    if (node.classList.contains("feed")) return "feed";
    return "";
  }

  function dragIdOf(node, kind) {
    return node.getAttribute(kind === "cat" ? "data-cat"
                               : kind === "grp" ? "data-group" : "data-feed") || "";
  }

  /* 谁可以被拖：手柄所在的分类 / 目录行，或整个 feed 行。feed 行里 favicon 是
     原生可拖的 img（`dragstart` 的 target 就是它），所以一律 closest 回去。 */
  function dragSubjectOf(el) {
    if (!el || !el.closest) return null;
    var grip = el.closest(".grip");
    if (grip) {
      var node = grip.closest(".node");
      var kind = nodeKind(node);
      return (kind === "cat" || kind === "grp") ? node : null;
    }
    return el.closest(".node.feed");
  }

  /* 这一行的兄弟层键 —— 与后端 core/treeorder.py 的 scope 一一对应。 */
  function scopeOf(node, kind) {
    if (kind === "cat") return "";
    if (kind === "grp") {
      var c = node.closest(".node.cat");
      return c ? "cat:" + c.getAttribute("data-cat") : "";
    }
    var g = node.getAttribute("data-group");
    return (g && g !== "0") ? "grp:" + g : "cat:" + node.getAttribute("data-cat");
  }

  /* 只有"同一父列表里的同一种行"才算重排（目录和裸 feed 在同一个 ul 里但不同种）。 */
  function isSibling(dragged, over) {
    return !!over && over !== dragged &&
           over.parentElement === dragged.parentElement &&
           nodeKind(over) === nodeKind(dragged);
  }

  function sideOf(e, node) {
    var row = node.querySelector(".node-row") || node;
    var r = row.getBoundingClientRect();
    return e.clientY < r.top + r.height / 2 ? "before" : "after";
  }

  function setMark(node, side) {
    if (markNode === node && markSide === side) return;
    if (markNode) markNode.classList.remove("order-before", "order-after");
    markNode = node;
    markSide = node ? side : "";
    if (markNode) markNode.classList.add(side === "before" ? "order-before" : "order-after");
  }

  function setDropNode(node) {
    if (dropNode === node) return;
    if (dropNode) dropNode.classList.remove("drop-ok");
    dropNode = node;
    if (dropNode) dropNode.classList.add("drop-ok");
  }

  /* 换分组的落点。原来只认目录行 / 分类行；现在别一层的 feed 行也算（落在谁那一层
     就去谁那儿）—— 22px 高的行里要精确命中目录行，命中率太低。 */
  function moveTargetOf(el) {
    var node = el && el.closest ? el.closest("#tree .node") : null;
    if (!node) return null;
    var kind = nodeKind(node);
    if (kind === "grp" || kind === "cat") return node;
    if (kind === "feed") {
      var g = node.getAttribute("data-group");
      if (g && g !== "0") return node.closest(".node.group");
      return node.closest(".node.cat");
    }
    return null;
  }

  function layerIds(ul, kind) {
    var out = [];
    Array.prototype.forEach.call(ul.children, function (el) {
      if (nodeKind(el) === kind) out.push(dragIdOf(el, kind));
    });
    return out;
  }

  /* 这一层现在有手动顺序了：整层打上 data-ord，右键「恢复默认顺序」立刻出现，
     不用等下一次刷新（排完就想撤销是常态）。 */
  function markLayerOrdered(ul, kind) {
    Array.prototype.forEach.call(ul.children, function (el) {
      if (nodeKind(el) === kind) el.setAttribute("data-ord", "1");
    });
  }

  document.addEventListener("dragstart", function (e) {
    var node = dragSubjectOf(e.target);
    if (!node) return;
    var ul = node.parentElement;
    if (!ul || ul.tagName !== "UL") return;
    var kind = nodeKind(node);
    drag = { node: node, kind: kind, scope: scopeOf(node, kind), ul: ul };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", dragIdOf(node, kind));
    /* 手柄拖的时候浏览器只画那个 12px 的圆点，改用整行当拖影 */
    if (e.target.closest && e.target.closest(".grip") && e.dataTransfer.setDragImage) {
      var row = node.querySelector(".node-row");
      if (row) e.dataTransfer.setDragImage(row, 16, 10);
    }
    node.classList.add("dragging");
  });

  document.addEventListener("dragend", function () {
    if (drag && drag.node) drag.node.classList.remove("dragging");
    drag = null;
    setMark(null, "");
    setDropNode(null);
  });

  /* ★ 没有 dragleave：dragover 每次都会重判落点并清掉旧高亮，
     离开行 / 落到空白处下一帧就清了，多一个监听只会多一处要维护的状态。 */
  document.addEventListener("dragover", function (e) {
    if (!drag) return;
    var over = e.target && e.target.closest ? e.target.closest("#tree .node") : null;
    if (isSibling(drag.node, over)) {
      e.preventDefault();
      e.dataTransfer.dropEffect = "move";
      setDropNode(null);
      setMark(over, sideOf(e, over));
      return;
    }
    setMark(null, "");
    if (drag.kind !== "feed") return;
    var target = moveTargetOf(e.target);
    if (!target || target === drag.node) { setDropNode(null); return; }
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    setDropNode(target);
  });

  document.addEventListener("drop", function (e) {
    if (!drag) return;
    var over = e.target && e.target.closest ? e.target.closest("#tree .node") : null;
    if (isSibling(drag.node, over)) {
      e.preventDefault();
      reorderTo(over, markSide || sideOf(e, over));
      return;
    }
    setMark(null, "");
    if (drag.kind !== "feed") return;
    var target = moveTargetOf(e.target);
    if (target && target !== drag.node) {
      e.preventDefault();
      moveToGroup(target);
    }
  });

  /* 同层重排：先就地移动 DOM，再把这一层的新顺序**整体**交给后端。
     ★ 送整层而不是"从第几挪到第几"：谁排第几取决于这一层现在长什么样，
       逐项挪位的校验就成了猜谜；整层比对不对就整笔拒（后端 treeorder.set_order）。 */
  function reorderTo(target, side) {
    var node = drag.node, ul = drag.ul, kind = drag.kind, scope = drag.scope;
    var next = side === "before" ? target : target.nextElementSibling;
    setMark(null, "");
    if (next === node) return;                       // 就在原地，什么都没变
    ul.insertBefore(node, next);
    postForm("/tree/reorder",
             { kind: kind, scope: scope, ids: layerIds(ul, kind).join(",") })
      .then(function (r) {
        if (!r.ok) {
          window.toast("重排没保存：" + (r.error || "未知错误"), 6000);
          location.reload();          // 库里没写 ⇒ 页面上也不许留着假顺序
          return;
        }
        markLayerOrdered(ul, kind);
      });
  }

  /* feed 换分组 / 出组（同分类内）。 */
  function moveToGroup(target) {
    var node = drag.node;
    var feedCat = node.getAttribute("data-cat");
    var isGroup = target.classList.contains("group");
    var catNode = isGroup ? target.closest(".node.cat") : target;
    var targetCat = catNode ? catNode.getAttribute("data-cat") : null;
    setDropNode(null);

    if (String(feedCat) !== String(targetCat)) {
      window.toast("跨分类移动暂未开放：会清零解析错误计数并改写历史归属", 6000);
      return;
    }
    if (isGroup &&
        String(target.getAttribute("data-group")) ===
          String(node.getAttribute("data-group"))) {
      return;                                   // 拖回原组，什么都不做
    }
    if (!isGroup && String(node.getAttribute("data-group") || "0") === "0") {
      return;                                   // 本来就在分类根
    }

    postForm("/tree/feed/move",
             { feed_id: node.getAttribute("data-feed"),
               target: isGroup ? "group:" + target.getAttribute("data-group")
                               : "cat:" + target.getAttribute("data-cat") })
      .then(function (r) {
        if (!r.ok) { window.toast("移动失败：" + (r.error || "未知错误"), 6000); return; }
        window.toast(r.detail || "已移动", 1500);
        location.reload();
      });
  }

  document.addEventListener("click", function (evt) {
    /* ★ target 不一定是元素（合成的 click 可能落在 document 上）—— 那时
       `closest` 不存在，直接 `.closest` 会抛 TypeError 并**中断整个监听**
       （后面展开分类、行点击等都不执行了）。非元素一律不处理。 */
    if (!evt.target || !evt.target.closest) return;
    var btn = evt.target.closest("#ctx-menu button");
    if (btn) {
      var cmenu = document.getElementById("ctx-menu");
      runCtx((cmenu._items || [])[parseInt(btn.getAttribute("data-idx"), 10)] || {});
      return;
    }
    if (!evt.target.closest("#ctx-menu")) hideCtxMenu();

    /* 箭头：展开 / 收起分类 */
    var arrow = evt.target.closest(".arrow[data-toggle]");
    if (arrow) {
      toggleCat(arrow.getAttribute("data-toggle"));
      return;
    }

    /* 级别下拉的「全部级别」：勾满并**主动发一次 change**，
       否则 htmx 等不到事件，列表不会跟着变 */
    var lvReset = evt.target.closest("[data-lv-reset]");
    if (lvReset) {
      var boxes = allLevelBoxes();
      applyLevels(boxes.map(function (b) { return b.getAttribute("data-lv"); }));
      if (boxes.length) boxes[0].dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }

    /* 下拉箭头：必须在关闭逻辑之前处理，否则刚打开就被关掉 */
    var caret = evt.target.closest("[data-menu-toggle]");
    if (caret) {
      var host = caret.closest(".dropdown") || caret.parentElement;
      var menu = host ? host.querySelector(".menu") : null;
      if (menu) {
        var willOpen = menu.hidden;
        closeMenus(menu);
        menu.hidden = !willOpen;
      }
      return;
    }

    var link = evt.target.closest("#tree a");
    if (link) {
      try {
        var u = new URL(link.getAttribute("hx-get") || link.getAttribute("href"), location.origin);
        syncSel(u.searchParams.get("sel"));
      } catch (e) { /* ignore */ }
      document.querySelectorAll("#tree .node.active").forEach(function (n) {
        n.classList.remove("active");
      });
      var node = link.closest(".node");
      if (node) node.classList.add("active");
    }

    /* ★ 多选菜单（级别）不因"点了菜单里面"而关闭 —— 一次要勾好几个 */
    if (!evt.target.closest(".menu[data-keep-open]")) closeMenus(null);
  });

  /* 菜单里的按钮点完就收起 */
  document.addEventListener("submit", function (evt) {
    var menu = evt.target.closest(".menu");
    if (!menu) menu = evt.target.querySelector(".menu");
    if (menu) menu.hidden = true;
  });

  /* ---------------- 异步表单：不要跳到一个只有 JSON 的页面 ----------------
     ★ 这些接口（/summary/run 等）返回 JSON。用普通 form POST 会让浏览器导航过去，
       只剩一屏 JSON，体验极差。统一改成 fetch + toast，再原地刷新看状态。
     用法：<form data-async="/summary/run" data-msg="正在重跑…"> */
  document.addEventListener("submit", function (evt) {
    var form = evt.target.closest ? evt.target.closest("form[data-async]") : null;
    if (!form) return;
    evt.preventDefault();
    if (form.getAttribute("data-busy") === "1") return;

    var fields = {};
    new FormData(form).forEach(function (v, k) { fields[k] = v; });
    var btn = form.querySelector("button[type=submit]");
    form.setAttribute("data-busy", "1");
    if (btn) btn.disabled = true;

    function done() {
      form.removeAttribute("data-busy");
      if (btn) btn.disabled = false;
    }

    window.toast(form.getAttribute("data-msg") || "正在提交…", 60000);
    var url = form.getAttribute("data-async");

    function doneAfter(r) {
      if (r && r.ok === false) {
        window.toast("没跑起来：" + (r.error || r.reason || "未知原因"), 6000);
        done();
        return;
      }
      /* ★ 第九步：commit 之后 `.drafts/` **不一定为空** —— 自动补链会落新一轮
        草稿。data-async 的 reload 会把浮层关掉，草稿就静默躺在 .drafts/ 里没人
        看见，而 approval_active() 又把编译/提炼锁住。所以把「要不要重新开浮层」
        记进 sessionStorage，reload 之后由顶层读走（见 wikiApprovalOpen 监听下面）。
        ★ toast 用服务端给的 flash，不能只说「已提交」：有断链没处理完时必须说出来。 */
      if (r && r.needs_approval) {
        try { sessionStorage.setItem("wikiOpenApproval", "1"); } catch (e) {}
      }
      window.toast((r && r.flash) || "已提交，正在刷新状态…", 4000);
      setTimeout(function () { location.reload(); }, 800);
    }

    /* ★ 覆盖二次确认：这天/这周的报告上有划线或批注时，后端不直接跑，
       回 need_confirm + msg；人点「仍然重跑」才带 confirm=1 重发。
       服务端同样认 confirm=1 —— 前端拦不住的改包请求不该被当成授权。 */
    function withConfirm() {
      var body = {};
      for (var k in fields) body[k] = fields[k];
      body.confirm = "1";
      return body;
    }

    postForm(url, fields).then(function (r) {
      if (!(r && r.need_confirm)) { doneAfter(r); return undefined; }
      if (!confirm(r.msg || "这份内容上有划线/批注，重跑后它们大概率无法再落位。仍然重跑？")) {
        window.toast("已取消，没有重跑", 4000);
        done();
        return undefined;
      }
      return postForm(url, withConfirm()).then(doneAfter);
    }).catch(function () {
      window.toast("请求失败，请重试", 6000);
      done();
    });
  });

  window.closeDetail = function (btn) {
    var detail = btn.closest(".entry-detail");
    if (detail) {
      detail.hidden = true;
      detail.innerHTML = "";
    }
  };

  /* ---------------- 深链自动展开：`?open=<条目id>` ----------------
     谁在用：**L3 点名的系统通知**。点击通知打开
       `/?sel=feed:<源>&open=<条目>#row-<条目>`
     —— `sel` 让树定位到源，`#row-` 让浏览器滚到那一行，`open` 就是这里：
     把这条的正文**自动拉出来**。点名点进来几乎一定要读，不该再点一次。

     ★ 刻意复用 `.entry-head` 的**点击**，而不是自己 fetch `/hx/entry/<id>`：
       展开这件事已经有现成链路（手风琴互斥、展开即标已读、错误处理都在 htmx 上），
       自己再写一条就会出现"同一个动作两种行为"。
     ★ 时机：htmx 的 `hx-post` 是在它自己的 DOMContentLoaded 处理器里绑定的，
       而那个处理器在 htmx.min.js（head）里就注册了、**早于**本文件（body 末尾），
       所以这里再挂 DOMContentLoaded，执行顺序天然在 htmx 之后 —— 点得动。 */
  var pendingOpenId = null;

  /* 展开一行：所有"从 URL 展开"的入口共用这一段，行为与手点 .entry-head 一致。
     返回 false = 这一行没有可点的头（结构不对，不该静默）。 */
  function expandRow(row, id) {
    var head = row.querySelector(".entry-head");
    if (!head) return false;
    var detail = row.querySelector(".entry-detail");
    if (detail && !detail.hidden && detail.innerHTML.trim() !== "") return true;  // 已展开，别重复拉
    pendingOpenId = id;
    head.click();
    return true;
  }

  function openFromUrl() {
    var id = null;
    try { id = new URLSearchParams(location.search).get("open"); } catch (e) { return; }
    if (!id) return;
    var row = document.getElementById("row-" + id);
    if (row) { expandRow(row, id); return; }

    /* ★ 这一条不在当前列表窗口里（列表只有最近 ENTRY_WINDOW 条）。
       **以前这里是 `return`** —— 从 L3 通知点进一条旧文时，正文不展开、
       不报错、也不给任何提示，看着就像"通知坏了"。静默失败最贵：它连
       "发生了"都没有。
       现在的做法：先用 GET 把这一行单独取回来插到列表顶部（`/hx/row/<id>`，
       无副作用，不受"当前只看 L1"这类筛选影响 —— 人是点名来的），
       再走**同一条**展开链路（标已读、刷树、手风琴互斥都在 htmx 那边）。
       取不到就说人话：文章被清理了 vs 请求失败，是两件事。 */
    fetch("/hx/row/" + encodeURIComponent(id), { headers: { "HX-Request": "true" } })
      .then(function (r) {
        if (r.status === 400 || r.status === 404) throw { gone: true };
        if (!r.ok) throw { status: r.status };
        return r.text();
      })
      .then(function (html) {
        var list = document.getElementById("entry-list");
        if (!list) throw { status: 0 };
        list.insertAdjacentHTML("afterbegin", html);
        /* ★ 必须让 htmx 处理这段新 DOM：`insertAdjacentHTML` 只负责贴标签，
           不会绑定 `hx-post`/`hx-target`。不 process 的话行看起来正常、点下去
           什么都没发生 —— 又一次静默失败（2026-11-03 实测：行补出来了，
           正文没展开，因为 head 上根本没有 htmx 监听）。 */
        if (window.htmx && typeof htmx.process === "function") htmx.process(list);
        var added = document.getElementById("row-" + id);
        if (!added || !expandRow(added, id)) throw { gone: true };
      })
      .catch(function (e) {
        window.toast(e && e.gone
          ? "这篇打不开了：它可能已被清理（不在库里了）"
          : "这篇没取回来（列表只显示最近一批），刷新页面再试一次", 7000);
      });
  }

  /* `?ai=1`：从知识库「继续对话」跳过来，落地就自动弹对话浮窗。
     两处调用：
       · 日报/周报详情 —— 页面加载完浮窗已在 DOM，直接开；
       · feed 文章 —— 浮窗懒加载在 entry 详情里，要等 openFromUrl 展开后浮窗才出现，
         所以在 entry 展开的 afterSwap 收尾里再调一次（见下）。
     找到的 trigger 是页面上第一个 [data-ai-toggle]：详情页只有一个内容块，唯一。 */
  function aiFromUrl() {
    try { if (new URLSearchParams(location.search).get("ai") !== "1") return; }
    catch (e) { return; }
    var toggle = document.querySelector("[data-ai-toggle]");
    var panel = toggle && aiPanelOf(toggle);
    if (panel && panel.hidden) aiOpen(toggle);
  }

  /* `?mk=<id>` / `?msg=<id>`：定位到**那一条**划线/批注、那一条对话消息。
     这是周报里「我的痕迹」链接的落点 —— 没有它就只能跳到文章顶部，
     读者还得自己找那句话在哪（等于不可追溯）。

     两个难点：
       · 正文和对话浮窗都可能是 htmx **异步展开**的 ⇒ 找不到不能算失败，
         要登记下来，在 afterSwap / 短轮询里重试；
       · 必须**幂等**：定位成功后立刻把待办清空，用户随后手动滚走不会被拽回来。 */
  var locateMarkId = null;
  var locateMsgId = null;
  var locateTimer = null;

  /* ★ 进入对话即滚到底（2026-11-19）：最新回答在末尾，用户不该每次手动滚一次。
     文章浮窗的滚动容器是 .ai-panel-body，审批浮层的是 .wc-log —— 传哪个滚哪个。
     ★ 有 ?msg= 深链待定位时调用方必须跳过（滚底会把精确跳到那条的动作覆盖掉）。 */
  function wcScrollBottom(box) {
    if (!box) return;
    try { box.scrollTop = box.scrollHeight; } catch (err) {}
  }

  function flashAndScroll(el) {
    if (!el) return false;
    el.classList.add("mark-flash");
    window.setTimeout(function () { el.classList.remove("mark-flash"); }, 2400);
    /* ★ 用**即时**滚动（不带 behavior:"smooth"）：这里是"跳到那一条"，
       用户要的是立刻看见，不是滑过去的动效；而且 smooth 在无头浏览器里
       直接不生效（实测截图停在页顶），等于没法自动验证这条链路。 */
    try {
      el.scrollIntoView({ block: "center" });
    } catch (e) {
      if (el.scrollIntoView) el.scrollIntoView();
    }
    return true;
  }

  function tryLocate() {
    if (locateMarkId) {
      /* 正文里落位成功的划线是 `<mark data-mark=…>`；没落位的只存在于底部兜底列表 */
      var m = document.querySelector('mark[data-mark="' + locateMarkId + '"]')
           || document.querySelector('.mark-item[data-mark-id="' + locateMarkId + '"]');
      if (m && flashAndScroll(m)) locateMarkId = null;
    }
    if (locateMsgId) {
      var g = document.getElementById("wc-msg-" + locateMsgId);
      if (g && flashAndScroll(g)) locateMsgId = null;
    }
    if (!locateMarkId && !locateMsgId && locateTimer) {
      window.clearInterval(locateTimer);
      locateTimer = null;
    }
  }

  function locateFromUrl() {
    var q;
    try { q = new URLSearchParams(location.search); } catch (e) { return; }
    locateMarkId = q.get("mk") || null;
    locateMsgId = q.get("msg") || null;
    if (!locateMarkId && !locateMsgId) return;
    tryLocate();
    /* 12 × 400ms ≈ 4.8s：足够覆盖「点开文章 → htmx 拉详情 → 渲染划线」这条链路。
       超时就放弃，不无限轮询 —— 目标可能真的不在了（划线被删/消息已删）。 */
    if ((locateMarkId || locateMsgId) && !locateTimer) {
      var tries = 0;
      locateTimer = window.setInterval(function () {
        tryLocate();
        if (++tries >= 12) {
          window.clearInterval(locateTimer);
          locateTimer = null;
        }
      }, 400);
    }
  }

  /* 手风琴：一次只展开一项 */
  document.body.addEventListener("htmx:beforeRequest", function (evt) {
    var target = evt.detail.target;
    if (!target || !target.classList) return;
    if (target.classList.contains("entry-detail") && !target.hidden && target.innerHTML.trim() !== "") {
      target.hidden = true;
      target.innerHTML = "";
      evt.preventDefault();
    }
  });

  document.body.addEventListener("htmx:afterSettle", function (evt) {
    /* ★ 审批草稿列表更新后（包括 oob 更新），同步更新 chat 表单里的 selected_slug 隐藏字段 */
    var list = document.getElementById("approval-list");
    if (list) {
      var activeRow = list.querySelector(".approval-draft-row.is-active");
      if (activeRow) {
        var toggleBtn = activeRow.querySelector(".approval-toggle");
        if (toggleBtn) {
          // 从 hx-get URL 里提取 selected_slug 参数（当前选中的草稿）
          var hxGet = toggleBtn.getAttribute("hx-get") || "";
          var match = hxGet.match(/selected_slug=([^&]+)/);
          if (match) {
            var newSlug = decodeURIComponent(match[1]);
            // 更新 chat 表单里的隐藏字段
            var chatForm = document.querySelector("#approval-chat .wc-form");
            if (chatForm) {
              var hidden = chatForm.querySelector("input[name='selected_slug']");
              if (hidden) {
                if (hidden.value !== newSlug) {
                  hidden.value = newSlug;
                  console.log("[wiki] 更新隐藏字段:", hidden.value);
                }
              } else {
                // 如果没有隐藏字段，创建一个
                hidden = document.createElement("input");
                hidden.type = "hidden";
                hidden.name = "selected_slug";
                hidden.value = newSlug;
                chatForm.appendChild(hidden);
                console.log("[wiki] 创建隐藏字段:", hidden.value);
              }
            }
          }
        }
      }
    }
  });

  /* ★ 审批对话自动滚动（2026-11-09）：panel 整体 outerHTML 换掉后，新 .wc-log
     默认停在顶部，看着像「发完消息跳回开头」。贴底时跟着滚到底；往上翻历史时
     保持原位，不把视线拽走。beforeSwap 时旧 log 还在，先把状态记下来。 */
  var approvalLogAtBottom = false, approvalLogScrollTop = 0;
  document.body.addEventListener("htmx:beforeSwap", function (evt) {
    var target = evt.detail.target;
    if (!target || target.id !== "approval-chat") return;
    var log = target.querySelector(".wc-log");
    if (!log) return;
    approvalLogAtBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    approvalLogScrollTop = log.scrollTop;
  });

  document.body.addEventListener("htmx:afterSwap", function (evt) {
    var target = evt.detail.target;
    if (target && target.classList && target.classList.contains("entry-detail")) {
      document.querySelectorAll(".entry-detail").forEach(function (d) {
        if (d !== target) {
          d.hidden = true;
          d.innerHTML = "";
        }
      });
      target.hidden = false;
      var row = target.closest(".entry-row");
      if (row) {
        row.classList.remove("is-unread");
        /* ★ 服务端展开时已标已读，但只 OOB 刷了树、没重画这一行 —— 菜单的
           「标记为未读」按 data-unread 决定是否出现，这里必须同步翻成 0，
           否则刚读过的那一行右键还是没有反向入口（状态显示与菜单口径分叉）。 */
        row.setAttribute("data-unread", "0");
        /* ★ 深链自动展开的收尾：正文插进来之后行变高了，光靠 `#row-` 的锚点
           可能让正文大半落在视口外。这里把该行顶到容器顶部，一整篇都在眼前。
           （只对"通知点进来的那一条"做，正常手点展开不动视线。） */
        if (pendingOpenId && target.id === "detail-" + pendingOpenId) {
          pendingOpenId = null;
          row.scrollIntoView({ block: "start" });
          aiFromUrl();   // 深链 + ?ai=1：正文展开后浮窗已在 DOM，这里才够得着
        }
      }
    }
    if (target && target.id === "tree") applyTreeState();
    /* ★ `?mk=` / `?msg=` 的正文是刚 swap 进来的 —— 这里必须再试一次定位，
       否则"跳到那条划线"只能落在文章顶部（DOM 里还没有那句话）。 */
    tryLocate();
    /* ★ 审批对话：贴底时跟着新消息滚到底，否则保持原阅读位置。
       ★ htmx 1.x 的 afterSwap detail.target 还是**旧元素**（outerHTML 换掉后已
       脱 DOM，往它写 scrollTop 是无效操作，实测发完消息总停在顶部）——
       必须按 id 重取换进来的新 panel，再拿新 .wc-log。 */
    if (target && target.id === "approval-chat") {
      var newChat = document.getElementById("approval-chat");
      var log = newChat ? newChat.querySelector(".wc-log") : null;
      if (log) {
        setTimeout(function() {
          log.scrollTop = approvalLogAtBottom ? log.scrollHeight : approvalLogScrollTop;
        }, 50);
      }
    }
  });

  /* 分段控件是纯 CSS（:checked ~ .seg-thumb），无需 JS */
  /* ★★ 级别**只在真正的前进/后退时**从 URL 回填。
     千万别挂在 `htmx:afterSettle` / `htmx:pushedIntoHistory` 上：
     连点两个勾选框时，第一个请求可能还在飞（或刚被 hx-sync 顶掉），
     它推上来的 URL 是**上一拍**的 —— 此时按 URL 回填会把用户刚取消的勾又恢复回去。
     （实测踩到：勾掉 L1 再勾掉 L2，结果 URL 变成 levels=0,2,3，L2 又亮了。） */
  document.body.addEventListener("htmx:pushedIntoHistory", syncSelFromUrl);
  window.addEventListener("popstate", function () {
    syncSelFromUrl();
    syncLevelsFromUrl();
  });

  document.body.addEventListener("htmx:afterSettle", function () {
    applyTreeState();
    syncSelFromUrl();
    syncTabTitle();     /* 切范围后标签页标题跟着变（见 syncTabTitle 的说明） */
  });

  /* ---------------- 日志页（/logs）----------------
     ★ 轮询条件是个**全局函数**：htmx 的 `every 5s [...]` 过滤器在全局作用域求值，
       拿不到 IIFE 里的局部变量，所以必须挂到 window 上（模板里那句就是它）。
       顺带把"页面不可见就不轮询"也写在模板条件里 —— 后台标签页没必要每 5s 读一次文件。 */
  window.__logLive = function () {
    var box = document.getElementById("log-live");
    return !box || box.checked;      // 开关不在这一页时返回 true（由 htmx 目标决定发不发）
  };

  /* ★★ 刷新时保住阅读位置：日志是**最新在上**插入的，
     所以在顶部时要跟着新行走（scrollTop 归 0），
     往下翻旧账时要按内容增量补偿 scrollTop，否则每一批新行都会把你正在看的那段顶下去。
     两个模式的分界就用 scrollTop 是否贴着顶部来判断。 */
  function logScrollBox(e) {
    var target = e.detail && e.detail.target;
    if (!target || target.id !== "log-rows") return null;
    return document.getElementById("log-scroll");
  }
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var box = logScrollBox(e);
    if (!box) return;
    box._sh = box.scrollHeight;
    box._st = box.scrollTop;
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var box = logScrollBox(e);
    if (!box || typeof box._st !== "number") return;
    var delta = box.scrollHeight - (box._sh || 0);
    box.scrollTop = box._st <= 2 ? 0 : box._st + delta;
    box._st = box._sh = undefined;
  });

  /* ---------------- 预算保存反馈 ----------------
     /costs 的预算卡是 `hx-post` + `hx-swap="none"`：只写库、不换 DOM，所以
     以前无论成败页面都毫无反应 —— 唯一能判断的办法是刷新页面看数字变没变，
     于是"存了但值没变"（池→键映射漏项）会被当成"没保存上"反复点。
     这里按返回 JSON 给一句话反馈；失败一律报后端原话。 */
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var form = e.target && e.target.closest && e.target.closest(".budget-form");
    if (!form) return;
    var xhr = e.detail && e.detail.xhr;
    var data = null;
    try { data = JSON.parse((xhr && xhr.responseText) || ""); } catch (_) { data = null; }
    if (!data || !data.ok) {
      window.toast((data && data.error) || "预算没保存上，请重试", 5000);
      return;
    }
    window.toast("已保存：" + (data.label || "") + "池 ¥" + data.limit_cny, 2200);
  });

  /* ---------------- wiki 审批 / 退稿的提示 ----------------
     响应带 `HX-Trigger: {"wikiFlash":{"msg":"已入库 wiki/invest/x.md"}}`。
     为什么不在片段里塞一条横幅：横幅会把下面的列表整体顶下去，连批三份页面跳三次。
     ★★ htmx 的 `isRawObject()` 决定要不要包一层 `{value: …}`：
        · `{"event": "字符串"}` → detail 是 `{value: "字符串"}`
        · `{"event": {"msg": …}}` → **detail 就是那个对象本身，没有 value**
     本项目的 wikiFlash 全是后者，所以早先只读 `.value` 拿到的永远是 undefined ——
     toast 一路显示兜底的「已更新」，包括「已入库 / 已退稿 / 编译完成」这些关键提示。
     2026-11-06 第六步 e2e 才把它抓出来（DOM 断言全绿，只有文案不对）。
     下面两种都认，免得改一下格式就静默退化成兜底文案。 */
  document.body.addEventListener("wikiFlash", function (e) {
    var d = e.detail || {};
    var v = ("value" in d) ? d.value : d;
    var msg = (v && typeof v === "object") ? v.msg : v;
    window.toast(msg || "已更新", 2600);
  });

  /* ---------------- 编译完自动弹审批浮层（第五步 5a）----------------
     编译产出草稿时，_fragment 回 `HX-Trigger: {"wikiApprovalOpen": {}}`。
     这里顶层监听 → openModal 拉 /wiki/approval 只读浮层。
     ★ 必须注册在顶层：浮层片段是 openModal 用 innerHTML 塞进 modal-root 的，
       内联 <script> 不会执行（htmx 也不管它），只能靠顶层监听兜住这条链路。 */
  document.body.addEventListener("wikiApprovalOpen", function () {
    window.openModal("/wiki/approval");
  });

  /* ---------------- 审批页标签编辑器（2026-11-16）----------------
     用户要求：「审核页面应该有编辑 tags 的地方，参考微信好友 tag —— 输入 tag
     的时候要能选择已有的 tag」。有了它，存量 wiki 才能一页页过一遍。

     三个动作，每次立刻落盘（POST /wiki/approval/tags → 换掉 #tag-chips）：
       · 点 × 删一个
       · 输入后回车加一个（`,` `，` `、` `;` `；` 也当回车）
       · 从下拉里点一个已有的（按子串过滤，带「N 页」，次数说明它有多常用）

     ★ 没有「保存」按钮，也没有前端暂存：这个工作区会被 13 条渲染路径整块换掉
       （审批对话 OOB / 切草稿 / 查相关页），任何「改了还没保存」的中间态都会在
       换节点时静默消失。落盘即真相，chips 显示的就是磁盘上的。
     ★ 输入框和下拉**不在**被换的节点里（只换 #tag-chips）：否则每加一个标签
       焦点就丢、敲了一半的字也没了，连加三个标签要点四次输入框。
     ★ 串行发请求（data-busy + data-want）：同一编辑器最多一个请求在飞；期间
       又改了就把**最新意图**记在 data-want，回包后立刻补发。不并发是因为写的是
       同一个文件，两次回包顺序不可控 —— 后发的先回会把 chips 覆盖成旧列表。
     ★ 回包不是 chip 片段（登录页 / 404 / 500）就算失败：报错但**不动 DOM**。
       把登录页 HTML 塞进 #tag-chips 比不刷新更难查。
     ★ 打字打了一半就点别处（比如直接点「提交入库」）时，那段字**不是**标签：
       不自动提交（会把「现金」这种半截查询词加成标签），而是留一句可见的提醒。
       静默丢掉才是更坏的那种。 */
  var TAG_SEP = /[,，、;；]/;

  function tagEd(el) {
    return (el && el.closest) ? el.closest(".tag-editor") : null;
  }

  function tagList(ed) {
    var out = [], chips = ed.querySelectorAll(".tag-chip");
    for (var i = 0; i < chips.length; i++) {
      var t = chips[i].getAttribute("data-tag") || "";
      if (t && out.indexOf(t) < 0) out.push(t);
    }
    return out;
  }

  function tagMsg(ed, text) {
    var el = ed.querySelector(".tag-msg");
    if (!el) return;
    el.textContent = text || "";
    el.hidden = !text;
  }

  /* 程序性聚焦：不要顺手把下拉弹开（那会在每次保存后糊住半个面板）。
     focusin 处理器看见 data-focus-quiet 就只吃掉这一次事件。 */
  function tagFocus(ed) {
    var inp = ed.querySelector(".tag-input");
    if (!inp || inp.disabled) return;
    /* 已经聚焦时 focus() 是空操作、不会触发 focusin —— 那就别挂 data-focus-quiet，
       否则这个标记会一直留在节点上，把**下一次**真正的聚焦吃掉（下拉该弹不弹）。 */
    if (document.activeElement === inp) return;
    ed.setAttribute("data-focus-quiet", "1");
    inp.focus();
  }

  function tagDropClose(ed) {
    var drop = ed.querySelector(".tag-drop");
    if (!drop) return;
    drop.hidden = true;
    drop.innerHTML = "";
  }

  /* 每次改动都走这里：先记「想要的标签」，再交给 tagFlush 串行发出去 */
  function tagUpdate(ed, tags) {
    ed.setAttribute("data-want", JSON.stringify(tags));
    tagFlush(ed);
  }

  function tagFlush(ed) {
    if (ed.getAttribute("data-busy") === "1") return;   /* 在飞：data-want 已记下 */
    var want = ed.getAttribute("data-want");
    if (want === null) return;
    ed.removeAttribute("data-want");
    var tags = [];
    try { tags = JSON.parse(want) || []; } catch (e) { tags = []; }
    var body = new URLSearchParams();
    body.append("slug", ed.getAttribute("data-slug") || "");
    tags.forEach(function (t) { body.append("tags", t); });
    ed.setAttribute("data-busy", "1");
    tagDropClose(ed);
    tagMsg(ed, "");
    fetch("/wiki/approval/tags", {
      method: "POST",
      headers: {
        "X-CSRFToken": getCookie("csrftoken"),
        "Content-Type": "application/x-www-form-urlencoded"
      },
      body: body.toString()
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.text();
    }).then(function (html) {
      if (html.indexOf('id="tag-chips"') < 0) throw new Error("回包不是标签片段");
      var box = document.createElement("div");
      box.innerHTML = html.trim();
      var fresh = box.firstChild, old = ed.querySelector("#tag-chips");
      if (!fresh || !old) throw new Error("找不到 chip 容器");
      old.parentNode.replaceChild(fresh, old);
    }).catch(function (e) {
      tagMsg(ed, "标签没保存上（" + ((e && e.message) || "网络错误")
             + "），请再试一次");
    }).then(function () {
      ed.removeAttribute("data-busy");
      if (ed.getAttribute("data-want") !== null) { tagFlush(ed); return; }
      tagFocus(ed);   /* chips 换了节点，焦点会掉到 body：还给输入框，连加不用再点 */
    });
  }

  function tagEsc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function tagVocab(ed) {
    try { return JSON.parse(ed.getAttribute("data-vocab") || "[]") || []; }
    catch (e) { return []; }
  }

  function tagMove(drop, delta) {
    var items = Array.prototype.slice.call(drop.querySelectorAll(".tag-sug"));
    if (!items.length) return;
    var cur = drop.querySelector(".tag-sug.is-active");
    var i = items.indexOf(cur);
    i = (i < 0) ? (delta > 0 ? 0 : items.length - 1) : i + delta;
    if (i < 0) i = items.length - 1;
    if (i >= items.length) i = 0;
    items.forEach(function (el) { el.classList.remove("is-active"); });
    items[i].classList.add("is-active");
  }

  function tagSuggest(ed, inp) {
    var typed = (inp.value || "").trim();
    var q = typed.toLowerCase();
    var have = tagList(ed);
    var max = parseInt(ed.getAttribute("data-max") || "8", 10) || 8;
    var drop = ed.querySelector(".tag-drop");
    if (!drop) return;
    if (have.length >= max) {
      /* 打满了就不再提议新建：一个点了没反应的按钮比没有按钮更糟 */
      drop.innerHTML = '<span class="tag-sug-note">最多 ' + max
        + " 个标签，先删一个再加</span>";
      drop.hidden = false;
      return;
    }
    var items = tagVocab(ed).filter(function (v) {
      if (!v || !v.tag) return false;
      if (have.indexOf(v.tag) >= 0) return false;
      return !q || String(v.tag).toLowerCase().indexOf(q) >= 0;
    }).slice(0, 8);
    var html = "";
    items.forEach(function (v) {
      html += '<button type="button" class="tag-sug" data-tag="' + tagEsc(v.tag)
        + '"><span class="tag-sug-name">' + tagEsc(v.tag) + '</span>'
        + '<span class="tag-sug-n">' + (v.n || 0) + " 页</span></button>";
    });
    var exact = items.some(function (v) { return v.tag === typed; });
    if (typed && !exact && have.indexOf(typed) < 0) {
      html += '<button type="button" class="tag-sug is-new" data-tag="'
        + tagEsc(typed) + '">新建「' + tagEsc(typed) + "」</button>";
    }
    drop.innerHTML = html;
    drop.hidden = !html;
  }

  function tagAdd(ed, text) {
    var tags = tagList(ed), changed = false;
    String(text || "").split(TAG_SEP).forEach(function (p) {
      var t = p.trim();
      if (!t || tags.indexOf(t) >= 0) return;
      tags.push(t);
      changed = true;
    });
    if (changed) tagUpdate(ed, tags);
  }

  document.addEventListener("focusin", function (evt) {
    var inp = evt.target;
    if (!inp || !inp.classList || !inp.classList.contains("tag-input")) return;
    var ed = tagEd(inp);
    if (!ed) return;
    /* 程序性聚焦（保存后把焦点还回来）不弹下拉 —— 否则每存一次都糊住半个面板 */
    if (ed.getAttribute("data-focus-quiet") === "1") {
      ed.removeAttribute("data-focus-quiet");
      return;
    }
    if (!inp.disabled) tagSuggest(ed, inp);   /* 空输入时给最常用的几个 */
  });

  document.addEventListener("input", function (evt) {
    var inp = evt.target;
    if (!inp || !inp.classList || !inp.classList.contains("tag-input")) return;
    var ed = tagEd(inp);
    if (!ed) return;
    tagMsg(ed, "");
    tagSuggest(ed, inp);
  });

  document.addEventListener("focusout", function (evt) {
    var inp = evt.target;
    if (!inp || !inp.classList || !inp.classList.contains("tag-input")) return;
    var ed = tagEd(inp);
    if (!ed) return;
    var left = (inp.value || "").trim();
    /* ★ 半截的字不自动成为标签（那会把「现金」这种查询词写成标签），但也不能
       静默丢掉 —— 留一句可见提醒，人回车一下就好。 */
    tagMsg(ed, left ? "「" + left + "」还没加进标签，按回车确认" : "");
    setTimeout(function () { tagDropClose(ed); }, 120);   /* 留时间给点击下拉 */
  });

  document.addEventListener("keydown", function (evt) {
    var inp = evt.target;
    if (!inp || !inp.classList || !inp.classList.contains("tag-input")) return;
    var ed = tagEd(inp);
    if (!ed) return;
    var drop = ed.querySelector(".tag-drop");
    var active = drop ? drop.querySelector(".tag-sug.is-active") : null;
    if (evt.key === "Enter" || TAG_SEP.test(evt.key)) {
      evt.preventDefault();
      tagAdd(ed, active ? active.getAttribute("data-tag") : inp.value);
      inp.value = "";
      tagDropClose(ed);
      return;
    }
    if (evt.key === "ArrowDown" || evt.key === "ArrowUp") {
      if (!drop || drop.hidden) return;
      evt.preventDefault();
      tagMove(drop, evt.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (evt.key === "Escape" && drop && !drop.hidden) {
      evt.preventDefault();
      tagDropClose(ed);
    }
  });

  document.addEventListener("click", function (evt) {
    var t = evt.target;
    if (!t || !t.closest) return;
    var x = t.closest(".tag-x");
    if (x) {
      evt.preventDefault();
      var ed = tagEd(x);
      var chip = x.closest(".tag-chip");
      if (!ed || !chip) return;
      var name = chip.getAttribute("data-tag") || "";
      tagUpdate(ed, tagList(ed).filter(function (v) { return v !== name; }));
      return;
    }
    var sug = t.closest(".tag-sug");
    if (sug) {
      evt.preventDefault();
      var ed2 = tagEd(sug);
      if (!ed2) return;
      tagAdd(ed2, sug.getAttribute("data-tag"));
      var inp = ed2.querySelector(".tag-input");
      if (inp) { inp.value = ""; inp.focus(); }
      tagDropClose(ed2);
      return;
    }
    if (tagEd(t)) return;
    /* 点编辑器以外 → 收起所有下拉（不收会盖住下面的候选卡） */
    var drops = document.querySelectorAll(".tag-drop");
    for (var i = 0; i < drops.length; i++) {
      var de = tagEd(drops[i]);
      if (de) tagDropClose(de);
    }
  });

  /* ---------------- 整批入库后自动弹浮层（第九步）----------------
     commit 走 data-async → reload，htmx 的 HX-Trigger 那条路用不上；而自动补链
     可能已经在 .drafts/ 里落了新一轮草稿。不重新弹浮层 = 草稿静默生成、人看不见，
     编译/提炼却已被 approval_active() 暂停。标记用 sessionStorage 且**读走就删**，
     避免之后每次刷新都弹一次。 */
  try {
    if (sessionStorage.getItem("wikiOpenApproval") === "1") {
      sessionStorage.removeItem("wikiOpenApproval");
      setTimeout(function () { window.openModal("/wiki/approval"); }, 200);
    }
  } catch (e) {}

  /* ---------------- 选中即划线/批注（阶段 1）----------------
     ★ 交互：在正文里**选中一段文字** → 浮出 toolbar（划线 / 批注）→
       点「批注」弹输入框，回车或点确认保存。与主流阅读器一致。
     ★ 为什么不走底部表单：拷贝文字到底部操作台会中断阅读流，选中即操作才自然。

     实现要点：
       · 监听 .article-tools 上声明的正文容器（body_selector）的 mouseup/selection；
       · 选中后取 window.getSelection() 的文本，并向前后各取 N 字做**文本锚点**
         （prefix/suffix 指纹）—— 渲染时后端靠它在正文里回填 inline 高亮；
       · 保存走 htmx 局部刷新：POST /wiki/highlight / annotation / mark/edit / delete，
         服务端返回「底部列表 + 正文 OOB」两个片段，一次请求同时更新两处，
         **不整页刷新、不跳顶部**（划线/批注后视线仍停在原处）。
     ★ 锚点只用文本，不用 DOM 路径：Miniflux 正文每次抓取可能变，XPath 脆。
     */
  /* ★ 为什么是 32 而不是 16：指纹越长，重复正文里分得越开（财报/研报的并列段落
     常有 20 字以上的雷同前缀）。代价只是每条标记多存几十字节。 */
  var MARKS_PREFIX_LEN = 32;   // 前后各取多少字做指纹
  var marksToolbar = null;
  var marksPop = null;
  /* 产生当前 toolbar 的那个区域（文章正文 / 某条对话消息）。
     ★ 提交时用它而不是再查 `.article-tools`：对话浮窗浮在正文之外，
       再查默认块就会把对话里的划线记到文章头上。 */
  var marksSelCtx = null;

  function marksCtx(el) {
    /* 从某个元素向上找「可标记区域」，拿到 source 与真正的文字容器。
       ★ 两类区域：对话消息（data-mark-type="chat"，source_id = 消息 id）优先，
         其次是文章/报告的 .article-tools。消息优先是因为浮窗浮在正文之外，
         按 .article-tools 找会把对话里的选区记到文章头上。 */
    var msg = el && el.closest ? el.closest("[data-mark-type]") : null;
    if (msg) {
      return {
        type: msg.getAttribute("data-mark-type"),
        id: msg.getAttribute("data-mark-id"),
        selector: "",
        el: msg.querySelector("[data-mark-body]") || msg
      };
    }
    var tools = el && el.closest ? el.closest(".article-tools") : null;
    if (!tools) tools = document.querySelector(".article-tools");
    if (!tools) return null;
    var sel = tools.getAttribute("data-body-selector") || "";
    return {
      type: tools.getAttribute("data-source-type"),
      id: tools.getAttribute("data-source-id"),
      selector: sel,
      el: sel ? document.querySelector(sel) : null
    };
  }

  function marksRemoveFloating() {
    if (marksToolbar) { marksToolbar.remove(); marksToolbar = null; }
    if (marksPop) { marksPop.remove(); marksPop = null; }
  }

  /* ★ htmx 局部提交 marks 端点：target = 底部列表容器。
     服务端响应里带 `#article-body-*` 的 OOB 片段，htmx 会自动用它替换正文 ——
     于是「列表 + 正文高亮」两处一起变，且滚动位置不动。 */
  function marksHxPost(url, fields, ctxOverride) {
    var ctx = ctxOverride || marksCtx(document.querySelector(".marks"));
    if (!ctx && !(fields && fields.source_type)) {
      window.toast("找不到当前内容块", 2500); return;
    }
    /* source_type/source_id 是每个端点都需要的，统一在这里补上，
       避免各调用点漏传（漏了服务端会拒写）。fields 里的值优先 ——
       编辑/删除是「对某条已有标记」操作，它自带的来源比页面上的默认块更准。 */
    var all = {
      source_type: ctx ? ctx.type : "",
      source_id: ctx ? ctx.id : ""
    };
    Object.keys(fields || {}).forEach(function (k) { all[k] = fields[k]; });
    /* ★ target 按**最终来源**算：对话消息的列表容器是 #marks-chat-<消息 id>，
       用页面默认的 .article-tools 来源会算成文章那个容器，请求发出去也刷错了地方。 */
    var target = "#marks-" + all.source_type + "-" + all.source_id;
    marksRemoveFloating();
    marksHoverHide();   /* 正文马上要被 OOB 整块换掉，浮层的 DOM 引用会失效 */
    window.htmx.ajax("POST", url, {
      target: target,
      swap: "outerHTML",
      values: all,
      headers: { "X-CSRFToken": getCookie("csrftoken") }
    });
  }

  /* 与后端 markrender._WS 同一条规则：连续空白折成一个空格。
     ★ 两边必须用**完全一样**的归一化，否则 offset 对不上（一个是源码换行、
       一个是版面文字，差一个换行就整体偏位）。 */
  function marksNorm(s) {
    return (s || "").replace(/\s+/g, " ");
  }

  function marksAnchor() {
    /* 从当前选区生成文本锚点 {text, prefix, suffix, offset, nth}
       ★ 不再是「text + 前后 16 字」。正文里同一句话出现两次时，仅靠文字指纹
         无法定位到用户真正选的那一处，所以额外记两个**位置先验**：
           · offset：选区起点在正文归一化纯文本里的字符下标；
           · nth  ：同样的文字在此之前出现过几次（同一句重复时的序号）。
         后端按「上下文重合度 → 序号 → 位置距离」打分选最优，不假定唯一。 */
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
    var text = marksNorm(sel.toString());
    if (!text.trim()) return null;
    var range = sel.getRangeAt(0);
    var prefix = "", suffix = "", offset = null, nth = null;

    var ctx = marksSelCtx || marksCtx(document.querySelector(".article-tools"));
    var body = (ctx && ctx.el) ? ctx.el : null;
    if (body && body.contains(range.startContainer) && body.contains(range.endContainer)) {
      try {
        /* before：正文开头 → 选区起点 的全部文字（Range.toString 会把跨标签的
           文本按文档序拼起来，正是后端 plain 流的同一顺序） */
        var pre = document.createRange();
        pre.selectNodeContents(body);
        pre.setEnd(range.startContainer, range.startOffset);
        var before = marksNorm(pre.toString());
        offset = before.length;
        prefix = before.slice(-MARKS_PREFIX_LEN);

        var post = document.createRange();
        post.selectNodeContents(body);
        post.setStart(range.endContainer, range.endOffset);
        suffix = marksNorm(post.toString()).slice(0, MARKS_PREFIX_LEN);

        /* nth：同一段文字在 before 里出现过几次（允许重叠计数，与后端一致） */
        var n = 0, from = 0;
        while (true) {
          var k = before.indexOf(text, from);
          if (k < 0) break;
          n++; from = k + 1;
        }
        nth = n;
      } catch (e) {
        /* Range 跨了不可选区域等异常：位置先验丢掉，仍能靠 text+上下文定位 */
        offset = null; nth = null;
      }
    } else {
      /* 拿不到正文容器（极端情况）：退回只从选区两端文本节点各取 N 字 */
      try {
        var sn = range.startContainer, en = range.endContainer;
        prefix = marksNorm(sn.nodeType === 3 ? sn.textContent.slice(0, range.startOffset) : "")
          .trim().slice(-MARKS_PREFIX_LEN);
        suffix = marksNorm(en.nodeType === 3 ? en.textContent.slice(range.endOffset) : "")
          .trim().slice(0, MARKS_PREFIX_LEN);
      } catch (e2) { /* 上下文也没有，靠 text 直匹配 */ }
    }
    return { text: text, prefix: prefix, suffix: suffix, offset: offset, nth: nth };
  }

  function marksSubmit(kind, anchor, extra) {
    var fields = { anchor: JSON.stringify(anchor) };
    if (kind === "highlight") fields.quoted_text = (anchor.text || "").trim();
    else fields.body = extra;
    if (extra && kind === "highlight") fields.note = extra;
    marksHxPost(kind === "highlight" ? "/wiki/highlight" : "/wiki/annotation",
                fields, marksSelCtx);
  }

  function marksShowToolbar(x, y) {
    marksRemoveFloating();
    marksHoverHide();   /* hover 卡片让位给 toolbar，避免两片浮层叠在一起 */
    var bar = document.createElement("div");
    bar.className = "mark-toolbar";
    var btnHl = document.createElement("button");
    btnHl.type = "button";
    btnHl.textContent = "划线";
    var btnAn = document.createElement("button");
    btnAn.type = "button";
    btnAn.textContent = "批注";
    /* ★ 用 click + preventDefault 而非 mousedown：mousedown 会先于全局
       document.mousedown（收起浮层）触发，时序一旦交叉，刚弹的批注框
       会被「点在浮层外」的收起逻辑立刻删掉（实测批注框闪一下就没）。
       click 在 mouseup 之后，且这里 stopPropagation 拦住冒泡，不会误触发收起。 */
    btnHl.addEventListener("click", function (e) {
      e.stopPropagation();
      var anchor = marksAnchor();
      if (!anchor) return;
      marksSubmit("highlight", anchor);
    });
    btnAn.addEventListener("click", function (e) {
      e.stopPropagation();
      marksShowAnnotatePop(x, y);
    });
    bar.appendChild(btnHl);
    bar.appendChild(btnAn);
    document.body.appendChild(bar);
    marksToolbar = bar;
    /* 定位：贴着**鼠标落点**（不是选区中点）。
       ★ 选区一长，中点离手可能隔半屏，点按钮要先挪一大段 —— 这才是"不自然"的来源。
       ★ 默认在指针**下方**：松手时指针就在选区末尾，下方已是选区外的内容，
         所以不会挡住刚选中的那行。
       ★ 右/下越界就收进视口、下方放不下翻到上方。 */
    var w = bar.offsetWidth, h = bar.offsetHeight;
    var left = Math.min(Math.max(8, x), window.innerWidth - w - 8);
    var top = y + 14;
    if (top + h > window.innerHeight - 8) top = Math.max(8, y - h - 12);
    bar.style.left = left + "px";
    bar.style.top = top + "px";
  }

  function marksShowAnnotatePop(x, y) {
    /* 先取锚点再清浮层：清浮层会动 document.activeElement，选区还在，不影响 */
    var anchor = marksAnchor();
    marksRemoveFloating();
    if (!anchor) return;
    var pop = document.createElement("div");
    pop.className = "mark-annotate-pop";
    var ta = document.createElement("textarea");
    ta.placeholder = "写下你的批注…（回车提交）";
    var actions = document.createElement("div");
    actions.className = "mark-annotate-actions";
    var okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "primary";
    okBtn.textContent = "保存";
    var cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "取消";
    actions.appendChild(okBtn);
    actions.appendChild(cancelBtn);
    pop.appendChild(ta);
    pop.appendChild(actions);
    document.body.appendChild(pop);
    marksPop = pop;

    function submit() {
      var body = ta.value.trim();
      if (!body) { window.toast("批注内容不能为空", 2500); return; }
      marksSubmit("annotation", anchor, body);
    }
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
      if (e.key === "Escape") { e.stopPropagation(); marksRemoveFloating(); }
    });
    okBtn.addEventListener("click", submit);
    cancelBtn.addEventListener("click", marksRemoveFloating);

    var w = pop.offsetWidth, h = pop.offsetHeight;
    /* 与 toolbar 同一套定位：左对齐指针、落在指针下方（原地弹出，不跳位置） */
    pop.style.left = Math.min(Math.max(8, x), window.innerWidth - w - 8) + "px";
    var ptop = y + 14;
    if (ptop + h > window.innerHeight - 8) ptop = Math.max(8, y - h - 12);
    pop.style.top = ptop + "px";
    setTimeout(function () { ta.focus(); }, 0);
  }

  function marksOnSelect(e) {
    /* 只在**可标记区域**内、且真的选中了文字时浮 toolbar。
       区域有两类：文章/报告正文，以及对话浮窗里的某条消息（消息级划线）。 */
    var ctx = marksCtx(e.target);
    if (!ctx || !ctx.el) return;
    if (!ctx.el.contains(e.target)) return;
    marksSelCtx = ctx;
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.toString().trim()) {
      marksRemoveFloating();
      return;
    }
    var range = sel.getRangeAt(0);
    var rect = range.getBoundingClientRect();
    if (!rect || (rect.width === 0 && rect.height === 0)) { marksRemoveFloating(); return; }
    /* ★ 用**鼠标/手指的位置**当锚点，而不是选区中点：
       松手那一刻指针就在选区末尾，浮层贴着指针出，点按钮不用再挪。
       键盘选择（Shift+方向键）没有指针坐标，退回选区左上角。 */
    var hasPtr = e && typeof e.clientX === "number" && (e.clientX || e.clientY);
    marksShowToolbar(
      hasPtr ? e.clientX : Math.min(rect.left + rect.width / 2, window.innerWidth - 140),
      hasPtr ? e.clientY : rect.top
    );
  }

  document.addEventListener("mouseup", marksOnSelect);
  document.addEventListener("keyup", function (e) {
    if (e.key === "Shift") marksOnSelect(e);
  });
  /* ★ 收起浮层的时机：点正文空白 / 点浮层之外的任何地方。
     toolbar/批注框内部的点击由其自身处理（内部按钮已 stopPropagation，
     textarea 里点击是 focus，不会误关）。 */
  document.addEventListener("mousedown", function (e) {
    if (!e.target || !e.target.closest) return;
    if (marksToolbar && !marksToolbar.contains(e.target)) {
      /* 点在 toolbar 外：若目标正是批注框（刚由批注按钮弹出）则不收 */
      if (!marksPop || !marksPop.contains(e.target)) marksRemoveFloating();
    } else if (marksPop && !marksPop.contains(e.target)) {
      marksRemoveFloating();
    }
  });

  /* 底部列表的「删」「编辑」按钮：委托处理（列表会被 htmx 整块替换，直接绑会失效） */
  document.addEventListener("click", function (e) {
    if (!e.target || !e.target.closest) return;
    /* 点击正文里的高亮也弹按钮组：触屏根本没有 hover，鼠标快点也可能早于 150ms。
       ★ 两个让位条件：正在划线/批注（toolbar 或输入框已开）不抢；正在选字也不抢。 */
    var mk = e.target.closest("mark.wiki-hl");
    if (mk) {
      if (marksToolbar || marksPop) return;
      var sel = window.getSelection();
      if (sel && !sel.isCollapsed) return;
      if (mk !== marksHoverEl) marksHoverBuild(mk);
      return;
    }
    var del = e.target.closest("[data-mark-del]");
    if (del) {
      var kind = del.getAttribute("data-mark-del");
      var id = del.getAttribute("data-id");
      var marks = del.closest(".marks");
      if (!marks) return;
      /* ★ 删除不可逆，先确认；确认后 htmx 局部刷新（不跳顶） */
      var label = kind === "highlight" ? "划线" : "批注";
      if (!confirm("删除这条" + label + "？此操作不可撤销。")) return;
      marksHxPost("/wiki/mark/delete", {
        source_type: marks.getAttribute("data-source-type"),
        source_id: marks.getAttribute("data-source-id"),
        kind: kind, id: id
      });
      return;
    }
    var edit = e.target.closest("[data-mark-edit]");
    if (edit) {
      marksShowEditPop(edit);
    }
  });

  /* 编辑弹框：**列表**与**正文 inline 高亮**共用这一套。
     ★ 为什么要抽出来：两个入口（底部列表的「编辑」、hover 浮层的「编辑」）若各写
       一份，占位文案/空值校验/提交字段迟早漂成两种行为。
     ★ 划线编辑的是备注（note，可清空 = 撤掉备注），批注编辑的是正文（body，不可空）
       —— 语义由后端 core.marks.edit_mark 定，这里只负责形态。
     opts = {kind, id, cur, rect, source:{type,id}} */
  function marksOpenEditor(opts) {
    var kind = opts.kind === "annotation" ? "annotation" : "highlight";
    var id = opts.id;
    var source = opts.source;
    if (!id || !source || !source.type || !source.id) return;
    var rect = opts.rect || { left: 12, bottom: 12 };
    marksRemoveFloating();
    marksHoverHide();          /* hover 按钮让位，别和输入框叠在一起 */

    var pop = document.createElement("div");
    pop.className = "mark-annotate-pop";
    var ta = document.createElement("textarea");
    ta.value = opts.cur || "";
    ta.placeholder = kind === "annotation"
      ? "批注内容…（回车保存，Shift+回车换行）"
      : "给这条划线写点备注…（回车保存，可留空以清除备注）";
    var actions = document.createElement("div");
    actions.className = "mark-annotate-actions";
    var okBtn = document.createElement("button");
    okBtn.type = "button";
    okBtn.className = "primary";
    okBtn.textContent = "保存";
    var cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.textContent = "取消";
    actions.appendChild(okBtn);
    actions.appendChild(cancelBtn);
    pop.appendChild(ta);
    pop.appendChild(actions);
    document.body.appendChild(pop);
    marksPop = pop;

    function submit() {
      if (kind === "annotation" && !ta.value.trim()) {
        window.toast("批注内容不能为空", 2500); return;
      }
      /* 走同一个局部刷新通道：列表 + 正文 OOB 一起换，视线不跳 */
      marksHxPost("/wiki/mark/edit", {
        source_type: source.type, source_id: source.id,
        kind: kind, id: id, body: ta.value
      });
    }
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
      if (e.key === "Escape") { e.stopPropagation(); marksRemoveFloating(); }
    });
    okBtn.addEventListener("click", submit);
    cancelBtn.addEventListener("click", marksRemoveFloating);

    var w = pop.offsetWidth, h = pop.offsetHeight;
    /* 以触发点（列表按钮 / 被编辑的那段高亮）为锚，下方优先、不够翻上方 */
    var left = Math.min(Math.max(8, rect.left), window.innerWidth - w - 8);
    var top = rect.bottom + 4;
    if (top + h > window.innerHeight - 8) top = Math.max(8, rect.top - h - 6);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    setTimeout(function () { ta.focus(); }, 0);
  }

  /* 底部列表的「编辑」按钮 → 同一套弹框 */
  function marksShowEditPop(btn) {
    var marks = btn.closest(".marks");
    if (!marks) return;
    marksOpenEditor({
      kind: btn.getAttribute("data-mark-edit"),
      id: btn.getAttribute("data-id"),
      cur: btn.getAttribute("data-cur") || "",
      rect: btn.getBoundingClientRect(),
      source: { type: marks.getAttribute("data-source-type"),
                id: marks.getAttribute("data-source-id") }
    });
  }

  /* ---- 正文 inline 高亮的 hover 浮层：右上角「编辑 + 删除」+ 上方文字卡片 ----
     ★ 为什么按钮是**独立节点**、不放进文字卡片里：
       · 按钮必须贴在高亮自己的右上角（"改/删这一条"的空间语义）；
       · 文字卡片以高亮为中心浮在上方。
       合成一张卡片的话，按钮会被卡片宽度带到离高亮很远的地方，点之前还得再找一次。
     ★ 「编辑」不只是改错字：**划线常常先划、备注后补**，所以没有文字的划线也要
       出按钮组 —— 只出 × 的话，事后想给这条例追加备注就完全没入口。
     ★ 必须事件委托：正文会被 htmx OOB 整块替换，绑在 mark 上立刻失效。
     ★ 150ms 才出（扫过正文不吵），220ms 才收 —— 这 220ms 是必须的：
       鼠标从 mark 挪到按钮上要能被接住，否则按钮永远点不中
       （这类 hover 组件最经典的坑）。 */
  var MARKS_HOVER_SHOW = 150;
  var MARKS_HOVER_HIDE = 220;
  var marksHoverEl = null, marksHoverTip = null, marksHoverBtns = null;
  var marksHoverShowT = null, marksHoverHideT = null;

  function marksHoverClearTimers() {
    if (marksHoverShowT) { clearTimeout(marksHoverShowT); marksHoverShowT = null; }
    if (marksHoverHideT) { clearTimeout(marksHoverHideT); marksHoverHideT = null; }
  }

  function marksHoverContains(t) {
    return !!t && ((marksHoverTip && marksHoverTip.contains(t))
                   || (marksHoverBtns && marksHoverBtns.contains(t)));
  }

  function marksHoverHide() {
    marksHoverClearTimers();
    if (marksHoverTip) { marksHoverTip.remove(); marksHoverTip = null; }
    if (marksHoverBtns) { marksHoverBtns.remove(); marksHoverBtns = null; }
    /* ★ 松开"正在操作这一条"的强调：不强调的话，正文里几条高亮挨着时
       用户看不出按钮组到底属于哪条。 */
    if (marksHoverEl && marksHoverEl.classList) marksHoverEl.classList.remove("wiki-hl-hot");
    marksHoverEl = null;
  }

  function marksHoverBuild(el) {
    marksHoverClearTimers();
    if (marksHoverTip) { marksHoverTip.remove(); marksHoverTip = null; }
    if (marksHoverBtns) { marksHoverBtns.remove(); marksHoverBtns = null; }
    marksHoverEl = el;
    if (el.classList) el.classList.add("wiki-hl-hot");

    var text = (el.getAttribute("data-note") || "").trim();
    var isAn = el.classList.contains("wiki-hl-an");
    var id = el.getAttribute("data-mark");
    var r = el.getBoundingClientRect();

    /* ① 按钮组：贴在高亮**右上角外侧** —— 不盖住高亮本身、不盖住正文
         （编辑 = 划线的备注 / 批注的正文；删除 = 这一条） */
    var btns = document.createElement("div");
    btns.className = "mark-hover-actions";

    var editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "mark-hover-edit";
    editBtn.textContent = "编辑";
    editBtn.title = isAn ? "修改这条批注" : "给这条划线加/改备注";
    editBtn.addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      /* ★ 先取几何与文字，再 open —— open 里会清掉 hover 节点 */
      marksOpenEditor({
        kind: isAn ? "annotation" : "highlight",
        id: id,
        cur: text,
        rect: el.getBoundingClientRect(),
        source: marksCtx(el)
      });
    });
    btns.appendChild(editBtn);

    var del = document.createElement("button");
    del.type = "button";
    del.className = "mark-hover-del";
    del.textContent = "×";
    del.title = "删除这条" + (isAn ? "批注" : "划线");
    del.addEventListener("click", function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      if (!id) return;
      /* ★ 点一下就删、不弹 confirm：× 只在 hover 才出、且精确指向这一条，
         误触率低；而原生 confirm 会打断阅读，正是要消掉的"不自然"。 */
      marksHoverHide();
      marksHxPost("/wiki/mark/delete", {
        kind: isAn ? "annotation" : "highlight", id: id
      });
      window.toast("已删除" + (isAn ? "批注" : "划线"), 1800);
    });
    btns.appendChild(del);

    document.body.appendChild(btns);
    marksHoverBtns = btns;
    btns.addEventListener("mouseenter", marksHoverClearTimers);
    btns.addEventListener("mouseleave", function () {
      marksHoverHideT = setTimeout(marksHoverHide, MARKS_HOVER_HIDE);
    });
    var bw = btns.offsetWidth, bh = btns.offsetHeight;
    var bleft = r.right + 3;
    /* 行尾放不下就翻到高亮**左侧**，别硬挤进视口边缘盖住正在读的字 */
    if (bleft + bw > window.innerWidth - 6) bleft = Math.max(6, r.left - bw - 3);
    btns.style.left = bleft + "px";
    /* 垂直居中于高亮：视觉上是"这一段的操作条"，不是压到上一行的空隙里 */
    btns.style.top = Math.max(2, r.top + (r.height - bh) / 2) + "px";

    /* ② 文字卡片：只有真的有文字才建 —— 没文字就只出按钮组，不弹空卡片
          （划线常常没有备注，弹一张空白卡片纯属噪音） */
    if (!text) return;
    var tip = document.createElement("div");
    tip.className = "mark-hover";
    var tx = document.createElement("div");
    tx.className = "mark-hover-text";
    tx.textContent = text;        /* ★ textContent：用户文字绝不走 innerHTML */
    tip.appendChild(tx);
    document.body.appendChild(tip);
    marksHoverTip = tip;
    tip.addEventListener("mouseenter", marksHoverClearTimers);
    tip.addEventListener("mouseleave", function () {
      marksHoverHideT = setTimeout(marksHoverHide, MARKS_HOVER_HIDE);
    });

    /* 位置：水平以高亮为中心；上方优先（不遮正在读的那行），不够才翻到下方 */
    var w = tip.offsetWidth, h = tip.offsetHeight;
    var left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    var top = r.top - h - 12;
    if (top < 8) top = Math.min(r.bottom + 14, window.innerHeight - h - 8);
    tip.style.left = left + "px";
    tip.style.top = top + "px";
  }

  document.addEventListener("mouseover", function (e) {
    if (!e.target || !e.target.closest) return;
    var mk = e.target.closest("mark.wiki-hl");
    if (mk) {
      if (mk === marksHoverEl) { marksHoverClearTimers(); return; }
      marksHoverClearTimers();
      marksHoverShowT = setTimeout(function () { marksHoverBuild(mk); }, MARKS_HOVER_SHOW);
      return;
    }
    if (marksHoverContains(e.target)) { marksHoverClearTimers(); return; }
    if ((marksHoverTip || marksHoverBtns) && !marksHoverHideT) {
      marksHoverHideT = setTimeout(marksHoverHide, MARKS_HOVER_HIDE);
    }
  });

  /* 点到别处就收（浮层是 fixed，不靠 DOM 层级自然消失） */
  document.addEventListener("mousedown", function (e) {
    if (!marksHoverTip && !marksHoverBtns) return;
    if (!e.target || !e.target.closest) return;
    if (marksHoverContains(e.target)) return;
    if (e.target.closest("mark.wiki-hl")) return;
    marksHoverHide();
  });

  /* 滚动就收：fixed 浮层不跟着正文走，滚了位置就错。
     ★ capture=true —— scroll 不冒泡，而正文的滚动容器是 .page 不是 window。 */
  window.addEventListener("scroll", marksHoverHide, true);

  /* ---------------- AI 悬浮入口 + 浮窗（阶段 2.1 改版）----------------
     ★ 为什么从「正文底部一块」改成浮窗：底部那块把正文推长，而且必须滚到底才
       看得见；悬浮入口读到哪里都能唤起，收起后不占版面。
     ★ 为什么懒加载 + 点开才拉：对话历史要查库、要渲染，没人点开就别做。
     ★ 为什么 entry 会自动总结：对齐小微/元宝总结公众号文章的手感（点一下就有
       结果）。只在**首次打开且没有历史**时自动发一次；失败也不重试 ——
       用户那条提问已经落库了，再点开就是有历史，不会反复扣钱。 */
  var aiPendingAuto = null;   // 刚点开、等片段加载完再判断要不要自动总结的 dock

  var AI_PREF_KEY = "diting.aiPanel.v1";
  var AI_MIN_W = 320, AI_MIN_H = 220;

  function aiPrefs() {
    try { return JSON.parse(window.localStorage.getItem(AI_PREF_KEY) || "{}") || {}; }
    catch (e) { return {}; }
  }
  function aiPrefSave(patch) {
    var p = aiPrefs();
    for (var k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) p[k] = patch[k];
    try { window.localStorage.setItem(AI_PREF_KEY, JSON.stringify(p)); } catch (e) {}
  }

  /* 入口按钮与浮窗不在同一棵子树里（按钮在页头，浮窗在正文之后），
     所以用 data-ai-target="<panel id>" 关联 —— 不能用 closest 找父容器。 */
  function aiPanelOf(el) {
    if (!el || !el.closest) return null;
    var t = el.closest("[data-ai-toggle]");
    if (!t) return null;
    var id = t.getAttribute("data-ai-target");
    return id ? document.getElementById(id) : null;
  }
  function aiTriggerOf(panel) {
    return panel ? document.querySelector('[data-ai-target="' + panel.id + '"]') : null;
  }

  /* feed 文章的浮窗默认落在页头动作区**下方**：CSS 里写死的 top 会正好盖住
     「问AI / 原文 / ✕」（实测：浮窗 top=76、按钮 112~138，直接压在上面）。
     只在没有用户记忆位置时算一次，不跟随滚动 —— fixed 元素本来就不跟页面滚。 */
  function aiTopBelowActions() {
    var actions = document.querySelector(".detail-actions");
    if (!actions) return null;
    return Math.round(Math.max(12, actions.getBoundingClientRect().bottom + 10));
  }

  /* 恢复用户摆过的尺寸/位置。★ 必须在 panel.hidden=false 之后调用：
     hidden 时 offsetWidth/offsetHeight 都是 0，钳位会把它压成最小尺寸。 */
  function aiPlace(panel) {
    if (!panel) return;
    var p = aiPrefs(), vw = window.innerWidth, vh = window.innerHeight;
    if (p.w) panel.style.width = Math.round(Math.min(Math.max(AI_MIN_W, p.w), vw - 16)) + "px";
    if (p.h) {
      /* ★ 恢复手调高度时必须同时放开 max-height：那 70vh 的上限是给「自动贴合
         内容」用的，留着会把用户拖高的浮窗又压回去。 */
      panel.style.maxHeight = "none";
      panel.style.height = Math.round(Math.min(Math.max(AI_MIN_H, p.h), vh - 16)) + "px";
    }
    if (p.x != null && p.y != null) {
      var w = panel.offsetWidth, h = panel.offsetHeight;
      panel.style.left = Math.round(Math.min(Math.max(8, p.x), Math.max(8, vw - w - 8))) + "px";
      panel.style.top = Math.round(Math.min(Math.max(8, p.y), Math.max(8, vh - h - 8))) + "px";
      panel.style.right = "auto";
      panel.style.bottom = "auto";
    } else if (panel.getAttribute("data-pos") === "top-right") {
      var top = aiTopBelowActions();
      if (top != null) panel.style.top = top + "px";
    }
  }

  /* 拖动（顶栏）与缩放（右下角）。用 pointer 事件而不是 mouse：触屏/手写笔同一套。 */
  var aiDrag = null;

  document.addEventListener("pointerdown", function (e) {
    if (!e.target || !e.target.closest) return;
    var handle = e.target.closest("[data-ai-resize]");
    var bar = e.target.closest("[data-ai-drag]");
    if (!handle && !bar) return;
    if (e.target.closest("[data-ai-close]")) return;   // 关按钮不是拖动起点
    var panel = (handle || bar).closest(".ai-panel");
    if (!panel) return;
    e.preventDefault();
    var r = panel.getBoundingClientRect();
    aiDrag = { panel: panel, mode: handle ? "resize" : "move",
               x: e.clientX, y: e.clientY,
               left: r.left, top: r.top, w: r.width, h: r.height };
    document.body.classList.add(handle ? "ai-resizing" : "ai-dragging");
    if (bar && bar.setPointerCapture) {
      /* 指针可能在拖动中移出顶栏（甚至移出窗口），捕获后事件仍回到这里 */
      try { bar.setPointerCapture(e.pointerId); } catch (err) {}
    }
  });

  document.addEventListener("pointermove", function (e) {
    if (!aiDrag) return;
    e.preventDefault();
    var p = aiDrag.panel, vw = window.innerWidth, vh = window.innerHeight;
    var dx = e.clientX - aiDrag.x, dy = e.clientY - aiDrag.y;
    if (aiDrag.mode === "move") {
      /* 钳在视口内：拖出屏幕就再也点不到了（没有窗口管理器帮你找回来） */
      p.style.left = Math.round(Math.min(Math.max(8, aiDrag.left + dx),
                                        Math.max(8, vw - p.offsetWidth - 8))) + "px";
      p.style.top = Math.round(Math.min(Math.max(8, aiDrag.top + dy),
                                       Math.max(8, vh - p.offsetHeight - 8))) + "px";
      p.style.right = "auto";
      p.style.bottom = "auto";
    } else {
      p.style.maxHeight = "none";   // 手动缩放就别再被 70vh 钳住
      p.style.width = Math.round(Math.max(AI_MIN_W, Math.min(aiDrag.w + dx, vw - 16))) + "px";
      p.style.height = Math.round(Math.max(AI_MIN_H, Math.min(aiDrag.h + dy, vh - 16))) + "px";
      /* 放大时右下角会顶出视口（贴着右下的浮窗尤其明显）——把面板推回来，
         否则「发送」按钮被推到屏幕外，看起来像浮窗坏了 */
      var r = p.getBoundingClientRect();
      if (r.right > vw - 8) { p.style.left = Math.round(Math.max(8, vw - r.width - 8)) + "px"; p.style.right = "auto"; }
      if (r.bottom > vh - 8) { p.style.top = Math.round(Math.max(8, vh - r.height - 8)) + "px"; p.style.bottom = "auto"; }
    }
  });

  document.addEventListener("pointerup", function () {
    if (!aiDrag) return;
    var r = aiDrag.panel.getBoundingClientRect();
    /* ★ 只「拖」不「缩」时不存宽高：存了就把「短对话自动贴合内容」固化成死高度 */
    var patch = { x: Math.round(r.left), y: Math.round(r.top) };
    if (aiDrag.mode === "resize") { patch.w = Math.round(r.width); patch.h = Math.round(r.height); }
    aiPrefSave(patch);
    aiDrag = null;
    document.body.classList.remove("ai-dragging", "ai-resizing");
  });

  /* 窗口变小后浮窗可能整块跑到视口外，重新钳一次 */
  window.addEventListener("resize", function () {
    var panels = document.querySelectorAll(".ai-panel");
    for (var i = 0; i < panels.length; i++) {
      if (!panels[i].hidden) aiPlace(panels[i]);
    }
  });

  function aiTryAutosend(panel) {
    if (!panel) return;
    var body = panel.querySelector(".ai-panel-body");
    var prompt = (panel.getAttribute("data-autosend") || "").trim();
    if (!body || !prompt) return;
    if (panel.getAttribute("data-autosent")) return;
    var form = body.querySelector(".wc-form");
    if (!form) return;                                  // 片段还没到，等 afterSettle
    var empty = body.querySelector(".wc-empty");
    panel.setAttribute("data-autosent", "1");           // 每次打开只判定一次
    if (!empty) return;                                 // 已有历史：不打扰
    var ta = form.querySelector("textarea");
    if (!ta) return;
    ta.value = prompt;
    wcSubmit(form, ta);                /* 与 Enter / 点发送同一条路径 */
  }

  function aiOpen(trigger) {
    var panel = aiPanelOf(trigger);
    if (!panel) return;
    var body = panel.querySelector(".ai-panel-body");
    panel.hidden = false;
    aiPlace(panel);                       // 恢复用户摆过的尺寸/位置（须在显示之后）
    if (trigger) trigger.setAttribute("aria-expanded", "true");

    if (body && !body.getAttribute("data-loaded")) {
      /* ★ 显式 htmx.ajax() 拉片段，不 dispatch 自定义事件、也不给元素写 hx-get。
         为什么：
         ① `?ai=1` 深链会在 entry 详情刚 swap 进来的 afterSwap 里就调到这里，
            而那一刻 htmx 还没 process 这块新 DOM，`hx-trigger="aiopen"` 的监听器
            根本没绑上 —— dispatch 的事件无人接，浮窗停在「点开才加载…」。
         ② ★ 更不能用裸 `hx-get`（不配 hx-trigger）：htmx 的默认触发器是 **click**，
            于是浮窗里**选字**或点一下消息就会重新请求并整块替换 innerHTML ——
            选区当场清空、内容重排滚一行，划线做不了（2026-11-01 实测抓到
            ai-body-entry-* 被 swap）。URL 因此放在 data-chat-url，htmx 不认。
         htmx.ajax() 自己会 process 目标、走完整 swap/settle，手动点和深链都可靠。 */
      body.setAttribute("data-loaded", "1");
      aiPendingAuto = panel;
      htmx.ajax("GET", body.getAttribute("data-chat-url"), { target: body, swap: "innerHTML" });
    } else {
      /* ★ 已加载过 = 重新打开，滚到底看到最新回答（2026-11-19）。
         首次加载走下面 afterSettle 那条；深链 ?msg= 待定位时不抢。 */
      if (body && !locateMsgId) wcScrollBottom(body);
      aiTryAutosend(panel);               // 已加载过：直接按当前内容判断
    }
    var ta = body && body.querySelector(".wc-form textarea");
    if (ta) setTimeout(function () { ta.focus(); }, 30);
  }

  function aiClose(panel) {
    if (!panel) return;
    panel.hidden = true;
    var t = aiTriggerOf(panel);
    if (t) { t.setAttribute("aria-expanded", "false"); t.focus(); }
  }

  document.addEventListener("click", function (e) {
    if (!e.target || !e.target.closest) return;
    var toggle = e.target.closest("[data-ai-toggle]");
    if (toggle) {
      var panel = aiPanelOf(toggle);
      if (!panel) return;
      if (panel.hidden) aiOpen(toggle); else aiClose(panel);
      return;
    }
    var close = e.target.closest("[data-ai-close]");
    if (close) { aiClose(close.closest(".ai-panel")); return; }
    /* 快捷问题：填进输入框再按正常流程提交（不另开一条「静默发送」的路径） */
    var chip = e.target.closest(".wc-chip");
    if (chip) {
      var form = chip.closest(".wiki-chat") && chip.closest(".wiki-chat").querySelector(".wc-form");
      if (!form) return;
      var ta = form.querySelector("textarea");
      if (!ta) return;
      ta.value = chip.getAttribute("data-wc-ask") || chip.textContent;
      wcSubmit(form, ta);
    }
  });

  /* 片段加载完成 → 若这次是「首次打开」，判断要不要自动总结 */
  document.body.addEventListener("htmx:afterSettle", function (e) {
    var tgt = e.target;
    if (!tgt || !tgt.closest) return;
    var body = tgt.closest(".ai-panel-body");
    if (!body) return;
    /* ★ 片段刚加载完（首次打开）就滚到底看到最新回答（2026-11-19）。
       深链 ?msg= 待定位时不滚，交给 tryLocate 精确跳到那条。 */
    if (!locateMsgId) wcScrollBottom(body);
    var panel = body.closest(".ai-panel");
    if (panel && panel === aiPendingAuto) { aiPendingAuto = null; aiTryAutosend(panel); }
  });

  /* ---------------- 对话输入框：提交 / 草稿 ----------------
     ★ 提交后立刻清空输入框（与聊天软件一致），但清空必须在 requestSubmit()
       **之后** —— htmx 是在 submit 事件的同步路径里读 form 值的，先清空就等于
       把一个空 body 发出去。
     ★ 为什么还要管草稿：服务端返回的是**整块对话片段**（outerHTML），用户
       「等回复时又打了一半的下一个问题」会被一起换掉 —— 那几行字凭空消失，
       比丢焦点严重。所以换之前把草稿抢下来，换完再填回去。
     ★ 发送失败（片段里带 .wc-err）要把刚才那段话还回输入框，否则一句 500
       就让人重打。 */
  var wcSubmitted = "", wcDraft = "", wcFocusBack = false;

  function wcSubmit(form, ta) {
    if (!form || !ta) return;
    wcSubmitted = ta.value;
    wcDraft = "";
    wcFocusBack = true;
    /* requestSubmit 会走原生校验并触发 submit 事件，htmx 正是监听这个事件；
       直接派发 Event("submit") 在部分浏览器上不跑校验。 */
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    ta.value = "";                       /* 已经发出去了，这里只是清屏 */
  }

  /* ---------------- 对话：SSE 流式提交（2026-11-19） ----------------
     ★ 流式是「增强」不是「替换」：form 的 hx-post 老端点一行没动。这里在 submit 的
       **capture 阶段**先拦一道，浏览器支持 fetch 流就走 /stream/send，逐字显示；
       任何环节失败就撤销拦截、放行给 htmx 老端点 —— 用户永远能看到回复或错误，
       不会「点了没反应」（静默失败是本项目最贵的缺陷）。
     ★ 权威渲染在服务端：done 帧带整块渲染好的片段（markdown/marks/OOB 全对），
       chat 面板由前端 outerHTML 覆盖；尾部 OOB 兄弟块由前端按 id 自己替换
       （htmx 不处理 JS 插入的 HTML），绝不自己拼 markdown。
     ★ 草稿：流式期间输入框不被换掉，用户能继续打；done 覆盖前抢下草稿，覆盖后回填。 */
  var wcStreaming = false;               // 同一时刻只允许一条流在飞

  function wcStreamUrl(hxPost) {
    /* /wiki/chat/send → /wiki/chat/stream/send；审批同理。推导而非写死第二份 URL。 */
    return (hxPost || "").replace(/\/send$/, "/stream/send");
  }

  function wcStreamSubmit(form, evt) {
    var ta = form.querySelector("textarea");
    var body = ta ? ta.value : "";
    /* ★ 生成期间绝不放行给 htmx：按钮已 disabled 挡住鼠标，但 textarea 里敲 Enter
       仍会触发 submit（disabled 只挡点击、不挡键盘）。若此时 return false 放行，
       htmx 老端点就被再次触发 → 同一句话发两遍、花两笔钱。所以"在飞"必须吞掉事件。 */
    if (wcStreaming) { evt.preventDefault(); evt.stopPropagation(); return true; }
    if (!body.trim()) return false;             // 空 → 交回默认（required 提示）
    var url = wcStreamUrl(form.getAttribute("hx-post"));
    if (!url || url === form.getAttribute("hx-post")) return false;  // 推不出 → 回退
    if (typeof fetch !== "function" || !window.ReadableStream) return false;

    var panel = form.closest(".wiki-chat, #approval-chat");
    var log = panel && panel.querySelector(".wc-log");
    if (!panel || !log) return false;

    evt.preventDefault();
    evt.stopPropagation();               // 挡住 htmx 的 bubble submit 监听
    /* ★ 流式不走 htmx:afterSettle，清掉 wcSubmit 可能设下的回填标记，
       否则残留的 wcFocusBack=true 会被下次任意 oob settle 误触发。
       草稿由本函数自己抢/回填（见 cleanup/done），不依赖那套。 */
    wcSubmitted = ""; wcDraft = ""; wcFocusBack = false;
    wcStreaming = true;
    /* ★ 生成期间禁用「发送」按钮：自动总结（wcSubmit→submit）也走这条路径，
       所以自动总结在跑时按钮同样是灰的，答完才恢复。模板的 hx-disabled-elt
       在 htmx 老路径会做这件事，但 SSE 拦了 submit，那条禁用没机会生效 —— 这里补上。 */
    var sendBtn = form.querySelector('button[type="submit"]');
    if (sendBtn) sendBtn.disabled = true;
    var question = body;
    ta.value = "";                        // 乐观清屏（与 wcSubmit 一致）

    /* 乐观插入「我」的气泡 + 「AI 正在说」气泡：让发送立刻有反馈，不等首字。 */
    var empty = log.querySelector(".wc-empty");
    if (empty) empty.remove();
    var mine = document.createElement("div");
    mine.className = "wc-msg wc-user";
    var mt = document.createElement("div"); mt.className = "wc-text"; mt.textContent = question;
    mine.appendChild(mt);
    var ai = document.createElement("div");
    ai.className = "wc-msg wc-assistant wc-streaming";
    var at = document.createElement("div"); at.className = "wc-text"; at.textContent = "";
    ai.appendChild(at);
    log.appendChild(mine); log.appendChild(ai);
    wcScrollBottom(panel.classList.contains("approval-chat") ? log :
                   (panel.closest(".ai-panel-body") || log));

    var finalized = false;
    function cleanup(ok) {
      wcStreaming = false;
      /* 恢复发送按钮：done 时整块 DOM 会被替换（新按钮天然可用），但 fallback
         时原 form 留在页面上，按钮必须显式解禁，否则回退后永远发不出第二句。 */
      if (sendBtn) sendBtn.disabled = false;
      /* 草稿：done 覆盖整块前抢下来，覆盖后回填。失败则不覆盖，草稿本来还在。 */
      var draft = "";
      var live = document.querySelector(".ai-panel-body .wc-form textarea") ||
                 document.querySelector("#approval-chat .wc-form textarea");
      if (live && ok) draft = live.value;
      if (!ok) { if (mine.parentNode) mine.remove(); if (ai.parentNode) ai.remove(); }
      return draft;
    }
    function fallback() {
      if (finalized) return;
      finalized = true;
      var draft = cleanup(false);
      ta.value = question;               // 还原问题，让 htmx 老端点重发
      if (draft) ta.value = draft;
      /* 走老端点：构造一个干净的 submit，放行给 htmx。
         ★ 打 wcFallback 标记，让 capture 监听这次**不拦截** —— 否则会递归回流式，
           失败路径永远出不去（这正是「回退分支自己也走了被回退的路」的坑）。 */
      form.setAttribute("data-wc-fallback", "1");
      form.dispatchEvent(new Event("submit", { cancelable: true, bubbles: true }));
    }

    var fd = new FormData(form);
    fd.set("body", question);
    fetch(url, { method: "POST", body: fd, headers: { "HX-Request": "true" } })
      .then(function (resp) {
        if (!resp.ok || !resp.body || !resp.body.getReader) throw new Error("stream unsupported");
        var reader = resp.body.getReader();
        var decoder = new TextDecoder("utf-8");
        var buf = "";
        function pump() {
          return reader.read().then(function (result) {
            if (result.done) { if (!finalized) fallback(); return; }
            buf += decoder.decode(result.value, { stream: true });
            var parts = buf.split("\n\n");
            buf = parts.pop();            // 末段可能不完整，留到下一次
            for (var i = 0; i < parts.length && !finalized; i++) {
              var line = parts[i].trim();
              if (line.indexOf("data: ") !== 0) continue;
              var msg;
              try { msg = JSON.parse(line.slice(6)); } catch (e) { continue; }
              if (msg.t === "delta" && at) {
                at.textContent += msg.v;
                wcScrollBottom(panel.classList.contains("approval-chat") ? log :
                               (panel.closest(".ai-panel-body") || log));
              } else if (msg.t === "err") {
                at.textContent = msg.v || "生成失败";
              } else if (msg.t === "done" && msg.html) {
                finalized = true;
                var draft = cleanup(true);
                var tmp = document.createElement("div");
                tmp.innerHTML = msg.html.trim();
                var fresh = tmp.firstElementChild;
                if (fresh && panel.parentNode) {
                  panel.parentNode.replaceChild(fresh, panel);
                  /* ★ 服务端权威片段尾部可能还带 hx-swap-oob 块（审批列表 / 工作区 /
                     计数）。流式是 JS 换 DOM，htmx 不参与，不会替我们做 OOB 替换；
                     只换 firstElementChild 会让「一句话并入」写盘成功、列表却留着旧页。 */
                  Array.prototype.slice.call(tmp.children).forEach(function (node) {
                    if (node === fresh || !node.hasAttribute("hx-swap-oob")) return;
                    var old = node.id ? document.getElementById(node.id) : null;
                    if (old && old.parentNode) old.parentNode.replaceChild(node, old);
                  });
                  var nta = fresh.querySelector(".wc-form textarea");
                  if (nta) {
                    nta.value = draft;
                    nta.focus();
                    try { nta.setSelectionRange(nta.value.length, nta.value.length); } catch (e) {}
                  }
                  var nlog = fresh.querySelector(".wc-log") || fresh;
                  wcScrollBottom(fresh.classList.contains("approval-chat") ? nlog :
                                 (fresh.closest(".ai-panel-body") || nlog));
                } else {
                  fallback();             // 替换失败 → 回退，不静默
                }
              }
            }
            if (!finalized) return pump();
          });
        }
        return pump();
      })
      .catch(function () { fallback(); });
    return true;
  }

  /* capture 阶段拦截两个对话 form 的 submit（htmx 在 bubble 阶段监听，这里先到）。 */
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form || !form.classList || !form.classList.contains("wc-form")) return;
    /* 回退重发：这次放行给 htmx，不再拦截（否则递归回流式，失败永远出不去）。 */
    if (form.getAttribute("data-wc-fallback")) {
      form.removeAttribute("data-wc-fallback");
      return;
    }
    /* csrf token 已在 FormData 里；流式失败会自动回退老端点。 */
    wcStreamSubmit(form, e);
  }, true);

  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var tgt = e.target;
    if (!tgt || !tgt.classList) return;
    if (!tgt.classList.contains("wiki-chat")) return;   /* 只认对话片段 */
    var ta = document.querySelector(".ai-panel-body .wc-form textarea") ||
             document.querySelector(".wc-form textarea");
    if (ta && ta.value) wcDraft = ta.value;
  });

  /* ★ Enter 发送、Shift+Enter 换行（业界通行）。这里必须挡住四种会「误发」的情况：
     1) 输入法正在组字（中文选词敲 Enter）—— Safari/Chrome 里 e.isComposing 为真，
        部分环境下 keyCode 恒为 229；不挡就是「选词顺手把半句话发出去」；
     2) 按住 Shift/Alt/Ctrl/Meta 任一 —— Shift+Enter 换行是长问题的刚需；
     3) 内容为空 —— 不插空行、不发送，也不弹「请填写此字段」的气泡；
     4) 已有请求在飞 —— 连敲 Enter 不该叠出两条一模一样的提问（都花钱）。
     真正提交走 wcSubmit()，和点「发送」按钮是同一条路径（htmx 才拦得住）。 */
  document.addEventListener("keydown", function (e) {
    var ta = e.target;
    if (!ta || !ta.matches || !ta.matches(".wc-form textarea")) return;
    if (e.key !== "Enter") return;
    if (e.isComposing || e.keyCode === 229) return;
    if (e.shiftKey || e.altKey || e.ctrlKey || e.metaKey) return;
    var form = ta.form;
    if (!form) return;
    /* 先无条件挡住默认行为：Enter 在这里只可能是「发送」，不插换行。
       空内容也不插（空框里敲 Enter 冒出个空行、placeholder 还消失，很像坏了），
       同时避开 required 校验的「请填写此字段」气泡。 */
    e.preventDefault();
    if (!ta.value.trim()) return;
    if (form.classList.contains("htmx-request")) return;
    wcSubmit(form, ta);
  });

  /* 发送后焦点还给输入框：htmx 用 outerHTML 换掉了整块对话，原 textarea 已经
     不在文档里，不补这一下 Enter 发送就成了「问一句点一次」。
     顺带滚到底：新消息追加在末尾，浮窗只有一屏高，不滚就等于「按了没反应」。
     ★ 只在「我发起的发送」后做（wcFocusBack 标记），初次加载/自动总结不跳。 */
  document.body.addEventListener("htmx:afterSettle", function () {
    if (!wcFocusBack) return;
    wcFocusBack = false;
    var ta = document.querySelector(".ai-panel-body .wc-form textarea") ||
             document.querySelector(".wc-form textarea");
    if (!ta) return;
    if (wcDraft) {
      ta.value = wcDraft;              /* 等回复时打的草稿，原样还回去 */
      wcDraft = "";
    } else if (wcSubmitted) {
      var chat = ta.closest(".wiki-chat");
      /* 失败才回填：成功时框该是空的，回填会让用户「再按一次 Enter」就重复发 */
      if (chat && chat.querySelector(".wc-err")) ta.value = wcSubmitted;
    }
    wcSubmitted = "";
    ta.focus();
    try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (err) {}
    var box = ta.closest(".ai-panel-body");
    if (box) box.scrollTop = box.scrollHeight;
  });

  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") {
      var open = document.querySelector(".ai-panel:not([hidden])");
      if (open) aiClose(open);
    }
  });


  applyTreeState();
  syncSelFromUrl();
  syncLevelsFromUrl();
  syncRefreshOnLoad();

  /* ★ 深链自动展开必须挂在 DOMContentLoaded 上、**不能**在这里直接调：
     htmx 是在它自己注册的 DOMContentLoaded 处理器里给 `hx-get` 绑事件的，
     而那个处理器在 htmx.min.js（head）里就注册好了 —— 本文件在 body 末尾，
     此刻再注册，回调天然排在它后面（同事件按注册顺序执行）。
     直接在这里点，元素的 `hx-get` 还没绑上，会**静默无效**。 */
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      openFromUrl();
      aiFromUrl();          // 日报/周报详情：浮窗已在 DOM，直接弹
      locateFromUrl();      // ?mk= / ?msg=：定位到那条划线/那条消息
    });
  } else {
    openFromUrl();
    aiFromUrl();
    locateFromUrl();
  }
})();
