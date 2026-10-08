/* Drifting particle network behind everything. Pauses when hidden; static for reduced motion. */
(function () {
  "use strict";
  var c = document.getElementById("bgfx"); if (!c) return;
  var x = c.getContext("2d"), W = 0, H = 0, P = [], last = 0;
  var still = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
  function size() {
    var d = Math.min(window.devicePixelRatio || 1, 2);
    W = innerWidth; H = innerHeight; c.width = W * d; c.height = H * d; x.setTransform(d, 0, 0, d, 0, 0);
    var n = Math.round(Math.min(70, W * H / 22000));
    while (P.length < n) P.push({ x: Math.random() * W, y: Math.random() * H, vx: (Math.random() - .5) * .22, vy: (Math.random() - .5) * .22, r: .6 + Math.random() * 1.4 });
    P.length = n;
  }
  function draw() {
    x.clearRect(0, 0, W, H);
    for (var i = 0; i < P.length; i++) {
      var a = P[i];
      for (var j = i + 1; j < P.length; j++) {
        var b = P[j], dx = a.x - b.x, dy = a.y - b.y, d = dx * dx + dy * dy;
        if (d < 17000) { x.strokeStyle = "rgba(150,210,255," + (0.16 * (1 - d / 17000)).toFixed(3) + ")"; x.lineWidth = 1; x.beginPath(); x.moveTo(a.x, a.y); x.lineTo(b.x, b.y); x.stroke(); }
      }
      x.fillStyle = "rgba(210,236,255,.7)"; x.beginPath(); x.arc(a.x, a.y, a.r, 0, 6.283); x.fill();
    }
  }
  function tick(t) {
    requestAnimationFrame(tick);
    if (document.hidden || t - last < 33) return; last = t;
    for (var i = 0; i < P.length; i++) {
      var p = P[i]; p.x += p.vx; p.y += p.vy;
      if (p.x < -10) p.x = W + 10; else if (p.x > W + 10) p.x = -10;
      if (p.y < -10) p.y = H + 10; else if (p.y > H + 10) p.y = -10;
    }
    draw();
  }
  size(); draw();
  addEventListener("resize", function () { size(); draw(); });
  if (!still) requestAnimationFrame(tick);
})();
