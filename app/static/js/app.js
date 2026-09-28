/* PayPal Verzehrabrechnungssystem - Interaktion
   Kein Framework, kein Build-Schritt. */
(function () {
  "use strict";

  /* ---------------------------------------------------------- theme */
  var THEME_KEY = "pps-theme";
  var root = document.documentElement;

  function applyTheme(theme) {
    root.setAttribute("data-theme", theme);
    try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* ignore */ }
    var label = document.querySelector("[data-theme-label]");
    if (label) {
      label.textContent = theme === "auto" ? "Design: Auto"
        : theme === "dark" ? "Design: Dunkel" : "Design: Hell";
    }
  }

  function initTheme() {
    var saved = "auto";
    try { saved = localStorage.getItem(THEME_KEY) || "auto"; } catch (e) { /* ignore */ }
    applyTheme(saved);
    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-theme-toggle]");
      if (!btn) return;
      var order = ["auto", "light", "dark"];
      var current = root.getAttribute("data-theme") || "auto";
      applyTheme(order[(order.indexOf(current) + 1) % order.length]);
    });
  }

  /* ---------------------------------------------------------- sidebar */
  function initSidebar() {
    var openBtn = document.querySelector("[data-sidebar-open]");
    function setOpen(open) {
      document.body.classList.toggle("sidebar-open", open);
      var backdrop = document.querySelector(".sidebar-backdrop");
      if (backdrop) backdrop.hidden = !open;
    }
    if (openBtn) {
      openBtn.addEventListener("click", function () { setOpen(true); });
    }
    document.addEventListener("click", function (ev) {
      if (ev.target.closest("[data-sidebar-close]")) setOpen(false);
    });
    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") setOpen(false);
    });
  }

  /* ---------------------------------------------------------- flashes */
  function initFlashes() {
    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-dismiss]");
      if (!btn) return;
      var flash = btn.closest(".flash");
      if (!flash) return;
      flash.style.transition = "opacity .2s, transform .2s";
      flash.style.opacity = "0";
      flash.style.transform = "translateY(-4px)";
      setTimeout(function () { flash.remove(); }, 220);
    });
  }

  /* ---------------------------------------------------------- confirm */
  function initConfirm() {
    document.addEventListener("submit", function (ev) {
      var form = ev.target;
      var message = form.getAttribute("data-confirm");
      if (message && !window.confirm(message)) {
        ev.preventDefault();
        return;
      }
      // guard against double submits on slow connections
      var submitter = ev.submitter;
      if (submitter && !form.hasAttribute("data-no-lock")) {
        setTimeout(function () {
          submitter.disabled = true;
          if (submitter.tagName === "BUTTON" && submitter.dataset.label) {
            submitter.dataset.originalText = submitter.textContent;
            submitter.textContent = submitter.dataset.label;
          }
        }, 0);
      }
    });
  }

  /* ------------------------------------------------- billing progress */
  /* Spec 13: while a run is in flight the admin must see a clear status
     instead of a frozen button. */
  function initBillingProgress() {
    document.querySelectorAll("form[data-billing]").forEach(function (form) {
      var status =
        form.querySelector("[data-billing-status]") ||
        document.querySelector("[data-billing-status]");
      if (!status) return;
      form.addEventListener("submit", function () {
        form.setAttribute("data-billing-running", "1");
        var steps = (form.getAttribute("data-billing") || "").split("|").filter(Boolean);
        var i = 0;
        function tick() {
          if (i < steps.length) {
            status.textContent = steps[i];
            i += 1;
            setTimeout(tick, 1400);
          }
        }
        status.textContent = steps[0] || "Wird verarbeitet...";
        status.classList.add("is-active");
      });
    });
  }
  /* ---------------------------------------------------------- steppers */
  function initSteppers() {
    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-step]");
      if (!btn) return;
      ev.preventDefault();
      var input = btn.parentElement.querySelector("input");
      if (!input) return;
      var step = parseInt(btn.getAttribute("data-step"), 10) || 1;
      var min = parseInt(input.getAttribute("min") || "0", 10);
      var max = parseInt(input.getAttribute("max") || "999", 10);
      var value = (parseInt(input.value || "0", 10) || 0) + step;
      if (value < min) value = min;
      if (value > max) value = max;
      input.value = value;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    });
  }

  /* ---------------------------------------------------------- cart total */
  function initCartTotal() {
    var form = document.querySelector("[data-cart-form]");
    if (!form) return;
    var totalEl = form.querySelector("[data-cart-total]");
    var countEl = form.querySelector("[data-cart-count]");
    var priceOf = {};
    Array.prototype.forEach.call(form.querySelectorAll("[data-price]"), function (el) {
      priceOf[el.name] = parseInt(el.getAttribute("data-price"), 10) || 0;
    });
    var SYMBOLS = {
      EUR: "\u20ac", USD: "$", GBP: "\u00a3", CHF: "CHF", SEK: "kr", NOK: "kr",
      DKK: "kr", PLN: "z\u0142", CZK: "K\u010d", AUD: "$", CAD: "$", JPY: "\u00a5"
    };
    var currency = form.getAttribute("data-currency") || "EUR";
    var symbol = SYMBOLS[currency] || currency;

    function fmt(cents) {
      var neg = cents < 0;
      var v = Math.abs(cents);
      var whole = Math.floor(v / 100);
      var frac = (v % 100).toString();
      while (frac.length < 2) frac = "0" + frac;
      return (neg ? "-" : "") + whole + "," + frac + " " + symbol;
    }

    function recalc() {
      var total = 0, count = 0;
      Array.prototype.forEach.call(form.querySelectorAll("input[data-price]"), function (input) {
        var qty = parseInt(input.value || "0", 10) || 0;
        if (qty < 0) qty = 0;
        total += qty * (priceOf[input.name] || 0);
        count += qty;
        var tile = input.closest(".product-tile");
        if (tile) tile.classList.toggle("has-qty", qty > 0);
      });
      if (totalEl) totalEl.textContent = fmt(total);
      if (countEl) countEl.textContent = String(count);
      var saveBtn = form.querySelector("[data-cart-save]");
      if (saveBtn) saveBtn.disabled = count === 0;
    }

    form.addEventListener("input", recalc);
    form.addEventListener("change", recalc);
    // keyboard: digits adjust the focused stepper
    form.addEventListener("keydown", function (ev) {
      var input = ev.target;
      if (!input.matches || !input.matches("input[data-price]")) return;
      if (ev.key === "ArrowUp") { ev.preventDefault(); input.stepUp(); recalc(); }
      if (ev.key === "ArrowDown") { ev.preventDefault(); input.stepDown(); recalc(); }
    });
    recalc();
  }

  /* ---------------------------------------------------------- quick entry */
  function toast(message, kind) {
    var stack = document.querySelector(".toast-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "toast-stack";
      document.body.appendChild(stack);
    }
    var el = document.createElement("div");
    el.className = "toast toast-" + (kind || "info");
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .2s";
      el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 220);
    }, 2600);
  }

  function initQuickEntry() {
    var panel = document.querySelector("[data-quick-panel]");
    if (!panel) return;
    var form = panel.querySelector("[data-quick-form]");
    var personSelect = panel.querySelector("[data-quick-person]");
    var qtyInput = panel.querySelector("[data-quick-qty]");
    var balanceEl = panel.querySelector("[data-quick-balance]");
    var pending = false;

    panel.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-quick-product]");
      if (!btn || pending) return;
      if (!personSelect || !personSelect.value) {
        toast("Bitte zuerst eine Person waehlen.", "error");
        if (personSelect) personSelect.focus();
        return;
      }
      pending = true;
      btn.disabled = true;
      var body = new URLSearchParams();
      body.set("csrf_token", form.querySelector("[name=csrf_token]").value);
      body.set("person_id", personSelect.value);
      body.set("product_id", btn.getAttribute("data-quick-product"));
      body.set("quantity", qtyInput ? qtyInput.value : "1");

      fetch(panel.getAttribute("data-quick-url") || "/verzehr/schnell", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
        credentials: "same-origin"
      })
        .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
        .then(function (res) {
          if (!res.ok || !res.data.ok) {
            toast(res.data.error || "Buchung fehlgeschlagen", "error");
          } else {
            toast(res.data.message + " \u00b7 " + res.data.line_total, "success");
            if (balanceEl && res.data.balance) balanceEl.textContent = res.data.balance;
            btn.classList.add("flash");
            setTimeout(function () { btn.classList.remove("flash"); }, 520);
          }
        })
        .catch(function () { toast("Netzwerkfehler beim Speichern", "error"); })
        .then(function () {
          pending = false;
          btn.disabled = false;
        });
    });
  }

  /* ---------------------------------------------------------- person filter */
  function initPersonFilter() {
    var input = document.querySelector("[data-filter-list]");
    if (!input) return;
    var targetSel = input.getAttribute("data-filter-list");
    input.addEventListener("input", function () {
      var needle = input.value.trim().toLowerCase();
      var items = document.querySelectorAll(targetSel);
      var visible = 0;
      Array.prototype.forEach.call(items, function (item) {
        var text = (item.getAttribute("data-search") || item.textContent || "").toLowerCase();
        var match = !needle || text.indexOf(needle) !== -1;
        item.classList.toggle("hidden", !match);
        if (match) visible++;
      });
      var empty = document.querySelector("[data-filter-empty]");
      if (empty) empty.classList.toggle("hidden", visible !== 0);
    });
  }

  /* ---------------------------------------------------------- confirm dialog */
  function initInlineConfirm() {
    document.addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-ask]");
      if (!btn) return;
      ev.preventDefault();
      var msg = btn.getAttribute("data-ask");
      if (window.confirm(msg)) {
        var form = btn.closest("form");
        if (form) form.submit();
      }
    });
  }

  /* ---------------------------------------------------------- boot */
  function boot() {
    initTheme();
    initSidebar();
    initFlashes();
    initConfirm();
    initSteppers();
    initBillingProgress();
    initCartTotal();
    initQuickEntry();
    initPersonFilter();
    initInlineConfirm();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
