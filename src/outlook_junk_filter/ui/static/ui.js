// Behaviour for the Decisions table. The page works without this file; it adds the select-all box, the selection bar
// and resizable columns (widths are remembered in this browser only).
(function () {
  var KEY = "ojf-column-widths";

  function selection() {
    var all = document.getElementById("select-all");
    var boxes = Array.prototype.slice.call(document.querySelectorAll('input[name="sel"]'));
    var info = document.querySelector(".selinfo");
    var count = document.getElementById("selcount");
    if (!info) return;

    function update() {
      var chosen = boxes.filter(function (b) { return b.checked; });
      info.hidden = chosen.length === 0;
      if (count) count.textContent = chosen.length + " selected";
      var totals = { teach: 0, restore: 0, allow: 0, all: chosen.length };
      chosen.forEach(function (b) {
        if (b.dataset.t) totals.teach++;
        if (b.dataset.r) totals.restore++;
        if (b.dataset.a) totals.allow++;
      });
      Array.prototype.forEach.call(document.querySelectorAll("[data-n]"), function (n) {
        n.textContent = totals[n.dataset.n];
      });
      if (all) {
        all.checked = boxes.length > 0 && chosen.length === boxes.length;
        all.indeterminate = chosen.length > 0 && chosen.length < boxes.length;
      }
    }

    boxes.forEach(function (b) { b.addEventListener("change", update); });
    if (all) {
      all.addEventListener("change", function () {
        boxes.forEach(function (b) { b.checked = all.checked; });
        update();
      });
    }
    var clear = document.getElementById("clear-selection");
    if (clear) {
      clear.addEventListener("click", function () {
        boxes.forEach(function (b) { b.checked = false; });
        update();
      });
    }
    update();
  }

  function columns() {
    var table = document.getElementById("decisions");
    if (!table) return;
    var cols = Array.prototype.slice.call(table.querySelectorAll("col"));
    var heads = Array.prototype.slice.call(table.querySelectorAll("thead th"));
    var defaults = cols.map(function (c) { return parseInt(c.dataset.w, 10) || 100; });
    var widths = defaults.slice();
    try {
      var saved = JSON.parse(localStorage.getItem(KEY) || "null");
      if (saved && saved.length === widths.length) widths = saved.map(function (w, i) { return w > 24 ? w : defaults[i]; });
    } catch (e) { /* storage unavailable: use the defaults */ }

    function apply() {
      var total = 0;
      cols.forEach(function (c, i) { c.style.width = widths[i] + "px"; total += widths[i]; });
      table.style.width = total + "px";
    }
    function save() {
      try { localStorage.setItem(KEY, JSON.stringify(widths)); } catch (e) { /* ignore */ }
    }

    heads.forEach(function (th, i) {
      var grip = th.querySelector(".grip");
      if (!grip || th.classList.contains("chk")) return;
      grip.addEventListener("mousedown", function (event) {
        event.preventDefault();
        var startX = event.clientX, startW = widths[i];
        function move(e) { widths[i] = Math.max(40, startW + e.clientX - startX); apply(); }
        function up() {
          document.removeEventListener("mousemove", move);
          document.removeEventListener("mouseup", up);
          save();
        }
        document.addEventListener("mousemove", move);
        document.addEventListener("mouseup", up);
      });
      grip.addEventListener("dblclick", function () { widths[i] = defaults[i]; apply(); save(); });
      grip.addEventListener("click", function (e) { e.preventDefault(); e.stopPropagation(); });
    });
    apply();
  }

  selection();
  columns();
})();
