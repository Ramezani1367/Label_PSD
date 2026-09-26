/* =============================================================
   Desktop bridge — وقتی اپ از طریق app.py سرو می‌شود تزریق می‌شود.
   رابط کاربری همان است؛ فقط کارهای واقعی به پایتون سپرده می‌شود:
     • انتخاب فولدر با دیالوگ ویندوز
     • اسکن واقعی فولدرها روی دیسک
     • اجرای مستقیم فتوشاپ + نوار پیشرفت زنده
   ============================================================= */
(function () {
  window.__DESKTOP__ = true;

  function api(path, body) {
    return fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body)
    }).then(function (r) { return r.json(); });
  }

  /* ---------------- progress overlay ---------------- */
  var ov = document.createElement("div");
  ov.id = "dtOverlay";
  ov.style.cssText = "position:fixed;inset:0;background:rgba(19,29,56,.45);backdrop-filter:blur(3px);z-index:80;" +
    "display:none;align-items:center;justify-content:center;padding:22px";
  ov.innerHTML =
    '<div style="background:#fff;border-radius:20px;box-shadow:0 24px 60px rgba(17,26,54,.3);width:100%;max-width:620px;overflow:hidden">' +
      '<div style="display:flex;align-items:center;gap:11px;padding:15px 20px;border-bottom:1px solid #e8ecf5">' +
        '<span class="ico-b g">▶</span>' +
        '<h3 style="margin:0;font-size:15px;flex:1">در حال گرفتن خروجی…<span id="dtSub" style="display:block;font-size:11.5px;color:#8792ab;font-weight:400"></span></h3>' +
        '<span class="pill ok" id="dtCount">0 / 0</span>' +
      '</div>' +
      '<div style="padding:16px 20px;display:flex;flex-direction:column;gap:11px">' +
        '<div style="height:10px;border-radius:999px;background:#eef1f8;overflow:hidden">' +
          '<div id="dtBar" style="height:100%;width:0%;border-radius:999px;background:linear-gradient(90deg,#1ec39a,#3d6cf5);transition:.25s"></div>' +
        '</div>' +
        '<div id="dtNow" class="ltr" style="font-size:12px;color:#3d4a66;min-height:20px;font-family:Vazirmatn,Tahoma,sans-serif"></div>' +
        '<pre id="dtLog" class="ltr" style="margin:0;background:#0f1524;color:#cfe0ff;border-radius:12px;padding:10px 12px;' +
          'font-size:11px;line-height:1.7;max-height:190px;overflow:auto;direction:ltr;white-space:pre-wrap"></pre>' +
        '<div class="row" style="justify-content:space-between">' +
          '<span id="dtStat" class="pill">در حال اجرا</span>' +
          '<span style="display:flex;gap:8px">' +
            '<button class="ghost mini" id="dtCancel">توقف</button>' +
            '<button class="primary mini" id="dtClose" style="display:none">بستن</button>' +
            '<button class="primary mini" id="dtOpen" style="display:none">📂 باز کردن پوشه و بستن</button>' +
            '<button class="green mini" id="dtRetry" style="display:none">🔁 اجرای مجدد ناموفق‌ها</button>' +
          '</span>' +
        '</div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(ov);
  var poll = null;

  function showOverlay() { ov.style.display = "flex"; }
  function hideOverlay() { ov.style.display = "none"; }
  document.getElementById("dtClose").onclick = function () { hideOverlay(); };
  document.getElementById("dtCancel").onclick = function () { api("/api/cancel", {}); };
  document.getElementById("dtOpen").onclick = function () {
    api("/api/open-folder", {}).then(function (r) {
      if (r && !r.ok && typeof toast === "function") toast("پوشهٔ خروجی پیدا نشد.", true);
    }).catch(function () {});
    hideOverlay();                       /* همین دکمه، پنجرهٔ گزارش را هم می‌بندد */
  };

  function tick() {
    api("/api/progress").then(function (p) {
      var pct = p.total ? Math.round(p.done / p.total * 100) : 0;
      document.getElementById("dtBar").style.width = pct + "%";
      document.getElementById("dtCount").textContent = p.done + " / " + p.total;
      document.getElementById("dtNow").textContent = p.current || "";
      document.getElementById("dtSub").textContent = p.outDir || "";
      document.getElementById("dtLog").textContent = (p.log || []).slice(-40).join("\n");
      document.getElementById("dtLog").scrollTop = 1e6;
      var st = document.getElementById("dtStat");
      if (p.finished || !p.running) {
        clearInterval(poll); poll = null;
        st.className = "pill " + (p.fail ? "warn" : "ok");
        st.textContent = p.error ? ("خطا: " + p.error) : ("تمام شد — موفق: " + p.ok + "   ناموفق: " + p.fail);
        document.getElementById("dtCancel").style.display = "none";
        document.getElementById("dtClose").style.display = "";
        document.getElementById("dtOpen").style.display = "";
        lastFailed = p.failed || [];
        if (p.zipPath && typeof toast === "function") toast("فایل زیپ ساخته شد: " + p.zipPath);
        document.getElementById("dtRetry").style.display = lastFailed.length ? "" : "none";
        if (!p.error && typeof addHistory === "function" && !historySaved) {
          historySaved = true;
          addHistory({ dir: p.outDir || "", items: lastItems.slice(), fmt: lastFmt, fail: p.fail || 0 });
        }
        if (typeof toast === "function") toast(p.error ? ("خطا: " + p.error) : ("خروجی تمام شد — " + p.ok + " فایل"), !!p.error);
      }
    }).catch(function () {});
  }

  var lastFailed = [], lastItems = [], lastFmt = "", historySaved = false;

  document.getElementById("dtRetry").onclick = function () {
    if (!lastFailed.length) return;
    startExport(lastFailed.slice());
  };

  /* راست‌کلیک → باز کردن فایل در فتوشاپ برای ویرایش */
  window.openInPhotoshop = function (full) {
    api("/api/open-ps", { path: full }).then(function (r) {
      if (typeof toast !== "function") return;
      if (r && r.ok) toast(r.demo ? "حالت دمو: فتوشاپ اجرا نشد." : "در فتوشاپ باز شد: " + full.split("\\").pop());
      else toast("باز کردن در فتوشاپ ناموفق بود" + (r && r.error ? ": " + r.error : ""), true);
    }).catch(function () {
      if (typeof toast === "function") toast("ارتباط با موتور برنامه برقرار نشد.", true);
    });
  };

  /* راست‌کلیک → نمایش فایل در اکسپلورر (فولدر باز می‌شود و خود فایل انتخاب می‌شود) */
  window.revealFile = function (full) {
    api("/api/reveal", { path: full }).then(function (r) {
      if (!r.ok && typeof toast === "function") toast("فایل پیدا نشد: " + full, true);
    }).catch(function () {});
  };

  /* با فوکوس روی کادر جستجو، چیدمان کیبورد ویندوز به انگلیسی سوییچ می‌کند */
  var kbdBusy = false;
  window.switchKeyboardEn = function () {
    if (kbdBusy) return;
    kbdBusy = true;
    setTimeout(function () { kbdBusy = false; }, 400);
    api("/api/kbd-en", {}).catch(function () {});
  };
  (function () {
    var q = el("q");
    if (!q) return;
    q.addEventListener("focus", function () { window.switchKeyboardEn(); });
    q.addEventListener("mousedown", function () { setTimeout(window.switchKeyboardEn, 0); });
  })();

  /* باز کردن پوشه با اکسپلورر ویندوز (جایگزین نسخهٔ کلیپ‌بوردی) */
  window.openOutFolder = function (pth) {
    api("/api/open-folder", { path: pth }).then(function () {}).catch(function () {});
  };

  function fmtList() {
    var f = [];
    if (el("doJpeg").checked) f.push("JPEG");
    if (el("doPdf") && el("doPdf").checked) f.push("PDF");
    if (el("doTiff") && el("doTiff").checked) f.push("TIFF");
    if (el("doCopy").checked) f.push("کپی PSD");
    return f;
  }

  /* فولدرهایی که قبل از افزوده‌شدن «حجم/تاریخ» اسکن شده‌اند را یک‌بار بی‌صدا به‌روز می‌کند */
  function refreshMeta() {
    try {
      var need = (state.folders || []).filter(function (f) {
        return (f.files || []).length && f.files.some(function (x) { return x.s === undefined; });
      });
      if (!need.length) return;
      var roots = need.map(function (f) { return f.path; });
      api("/api/scan", { folders: roots, includeSub: true }).then(function (s) {
        var by = {};
        (s.files || []).forEach(function (f) {
          (by[f.root.toLowerCase()] = by[f.root.toLowerCase()] || []).push(relOf(f));
        });
        var n = 0;
        need.forEach(function (f) {
          var list = by[(f.path || "").toLowerCase()];
          if (list) { f.files = list; n += list.length; }
        });
        if (n) {
          save(); renderFolders(); doSearch();
          if (typeof toast === "function") toast("اطلاعات " + n + " فایل (حجم و تاریخ) به‌روز شد.");
        }
      }).catch(function () {});
    } catch (e) {}
  }

  /* حجم کل سبد را می‌گیرد تا روی دکمهٔ اجرا نوشته شود */
  function refreshBasketSize() {
    if (!state.basket.length) { window.basketBytes = 0; if (typeof refreshJsx === "function") refreshJsx(); return; }
    api("/api/check", { items: state.basket.map(function (f) { return { src: f }; }) }).then(function (r) {
      window.basketBytes = r.totalBytes || 0;
      if (typeof refreshJsx === "function") refreshJsx();
    }).catch(function () {});
  }
  window.refreshBasketSize = refreshBasketSize;

  function startExport(only) {
    var items = only && only.length ? only.slice() : state.basket.slice();
    if (!items.length) { toast("سبد خروجی خالی است.", true); showPage("main"); return; }
    if (!fmtList().length) { toast("حداقل یک گزینهٔ «محتوای خروجی» را انتخاب کن.", true); return; }
    var pre = el("preflight");
    /* ---- بررسی وجود فایل‌ها قبل از باز کردن فتوشاپ ---- */
    api("/api/check", { items: items.map(function (f) { return { src: f }; }) }).then(function (chk) {
      var missing = chk.missing || [];
      if (missing.length) {
        pre.className = "note warn";
        pre.innerHTML = "⚠️ <b>" + missing.length + " فایل پیدا نشد</b> و از اجرا حذف شد (جابه‌جا یا حذف شده‌اند):<br>"
          + '<span class="ltr mono">' + missing.slice(0, 12).map(function (m) { return m.replace(/&/g, "&amp;").replace(/</g, "&lt;"); }).join("<br>")
          + (missing.length > 12 ? "<br>…" : "") + "</span>";
        items = items.filter(function (f) { return missing.indexOf(f) < 0; });
        if (typeof toast === "function") toast(missing.length + " فایل پیدا نشد؛ بقیه اجرا می‌شوند.", true);
        if (!items.length) { toast("هیچ فایل موجودی در سبد نماند.", true); return; }
      } else {
        pre.className = "note warn hidden";
        pre.innerHTML = "";
      }
      window.basketBytes = chk.totalBytes || 0;
      doExport(items);
    }).catch(function () { doExport(items); });
  }

  function doExport(items) {
    lastItems = items.slice();
    lastFmt = fmtList().join(" + ");
    historySaved = false;
    var payload = {
      items: items.map(function (f) { return { src: f }; }),
      outRoot: el("outRoot").value.trim(),
      folderName: outFolderName(),
      dateSuffix: el("nameDate").checked ? dateTag() : "",
      quality: parseInt(el("quality").value, 10) || 12,
      doReport: el("doReport") ? el("doReport").checked : false,
      cvt8bit: el("cvt8bit").checked,
      cvtRGB: el("cvtRGB").checked,
      doJpeg: el("doJpeg").checked,
      doPdf: el("doPdf") ? el("doPdf").checked : false,
      pdfLayers: el("pdfLayers") ? el("pdfLayers").checked : true,
      doZip: el("doZip") ? el("doZip").checked : false,
      doTiff: el("doTiff") ? el("doTiff").checked : false,
      doCopy: el("doCopy").checked,
      copyWhere: el("copyWhere").value
    };
    document.getElementById("dtRetry").style.display = "none";
    document.getElementById("dtCancel").style.display = "";
    document.getElementById("dtClose").style.display = "none";
    document.getElementById("dtOpen").style.display = "none";
    document.getElementById("dtStat").className = "pill";
    document.getElementById("dtStat").textContent = "در حال اجرا";
    showOverlay();
    api("/api/export", payload).then(function (r) {
      if (r.error) { toast(r.error, true); return; }
      if (poll) clearInterval(poll);
      poll = setInterval(tick, 700); tick();
    });
  }

  /* ---------------- override handlers ---------------- */
  function relOf(f) { return { n: f.name, r: f.rel || "", s: f.size || 0, m: f.mtime || 0 }; }

  el("btnPick").onclick = function () {
    toast("پنجرهٔ انتخاب فولدر باز شد…");
    api("/api/pick-folder", { initial: "" }).then(function (r) {
      if (!r.path) return;
      return api("/api/scan", { folders: [r.path], includeSub: true }).then(function (s) {
        addFolder(r.path, (s.files || []).map(relOf));
        toast(s.count + " فایل PSD پیدا شد.");
      });
    });
  };

  el("btnAddPath").onclick = function () {
    var p = el("newPath").value.trim();
    if (!p) { toast("مسیر را وارد کن.", true); return; }
    api("/api/scan", { folders: [p], includeSub: true }).then(function (s) {
      if (!s.files || !s.files.length) { addFolder(p, []); toast("فولدر اضافه شد ولی PSDای پیدا نشد (مسیر را چک کن).", true); }
      else { addFolder(p, s.files.map(relOf)); toast(s.count + " فایل PSD پیدا شد."); }
      el("newPath").value = "";
    });
  };

  el("btnRescanAll").onclick = function () {
    var roots = state.folders.map(function (f) { return f.path; });
    if (!roots.length) { toast("فولدری ثبت نشده.", true); return; }
    api("/api/scan", { folders: roots, includeSub: true }).then(function (s) {
      var by = {};
      (s.files || []).forEach(function (f) { (by[f.root.toLowerCase()] = by[f.root.toLowerCase()] || []).push(relOf(f)); });
      state.folders.forEach(function (f) { f.files = by[f.path.toLowerCase()] || []; });
      save(); renderFolders(); doSearch();
      toast("اسکن مجدد انجام شد — " + s.count + " فایل.");
    });
  };

  el("btnPickOut").onclick = function () {
    api("/api/pick-folder", { initial: el("outRoot").value }).then(function (r) {
      if (!r.path) return;
      el("outRoot").value = r.path; save(); refreshJsx(); toast("مسیر خروجی: " + r.path);
    });
  };

  el("btnRun").onclick = startExport;
  window.runExport = startExport;

  /* ---------------- desktop-mode cosmetics ---------------- */
  var polished = false;
  document.addEventListener("DOMContentLoaded", polish);
  setTimeout(polish, 0);
  function polish() {
    if (polished) return;               /* فقط یک‌بار (وگرنه المان‌ها تکراری می‌شوند) */
    polished = true;
    var env = el("envNote");
    if (env) { env.className = "note ok"; env.innerHTML = "🖥 <b>حالت دسکتاپ فعال است</b> — فولدرها مستقیماً از روی دیسک اسکن می‌شوند و خروجی بدون هیچ فایل واسطی اجرا می‌شود."; }
    appIcon();
    refreshMeta();
    var oldRender = window.renderBasket;
    if (typeof oldRender === "function") {
      window.renderBasket = function () { oldRender.apply(null, arguments); clearTimeout(window._szT); window._szT = setTimeout(refreshBasketSize, 250); };
    }
    refreshBasketSize();
  }

  /* آیکن واقعی برنامه برای نوار عنوان و تسک‌بار ویندوز (به‌جای کرهٔ زمین) */
  function appIcon() {
    /* آیکن‌های دقیق (۱۶/۲۴/۳۲/۴۸/۶۴/۱۲۸) داخل خود HTML جاسازی شده‌اند و دست‌نخورده می‌مانند؛
       اینجا فقط manifest اضافه می‌شود تا ویندوز پنجره را به‌عنوان یک اپ مستقل بشناسد. */
    try {
      if (document.querySelector("link[rel='manifest']")) return;
      var head = document.head || document.getElementsByTagName("head")[0];
      var l = document.createElement("link");
      l.rel = "manifest"; l.href = "/manifest.json";
      head.appendChild(l);
    } catch (e) {}
  }

})();
