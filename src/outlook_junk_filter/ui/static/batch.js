// Select-all box for the Decisions table (the page works without it; this only ticks the row boxes).
(function () {
  var all = document.getElementById("select-all");
  if (!all) return;
  all.addEventListener("change", function () {
    var boxes = document.querySelectorAll('input[name="sel"]');
    for (var i = 0; i < boxes.length; i++) boxes[i].checked = all.checked;
  });
})();
