// Shows the steps of an answer as they happen, then loads the finished answer.
// Without this script the page refreshes itself every few seconds instead.
(function () {
  var list = document.getElementById("steps");
  if (!list || !window.EventSource) return;
  var shown = parseInt(list.getAttribute("data-shown"), 10) || 0;
  var seen = 0;
  var source = new EventSource(list.getAttribute("data-events"));
  source.onmessage = function (e) {
    seen += 1;
    if (seen <= shown) return;          // already on the page when it was made
    var li = document.createElement("li");
    li.textContent = JSON.parse(e.data);
    list.appendChild(li);
  };
  source.addEventListener("done", function () {
    source.close();
    window.location.reload();
  });
  source.onerror = function () {
    // The connection dropped. Fall back to checking again shortly.
    source.close();
    setTimeout(function () { window.location.reload(); }, 4000);
  };
})();
