/* Landing page behaviour. Vanilla and self contained: no CDN, nothing to load, nothing to
   break if a third party goes down.

   Everything here is decoration or demonstration. If it never runs, the page still reads. */

(function () {
  "use strict";

  var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ── the header gains a border once you leave the top ─────────────── */
  var bar = document.querySelector(".bar");
  if (bar) {
    var onScroll = function () {
      bar.classList.toggle("scrolled", window.scrollY > 8);
    };
    addEventListener("scroll", onScroll, { passive: true });
    onScroll();
  }

  /* ── reveal sections as they arrive ──────────────────────────────── */
  var targets = [].slice.call(document.querySelectorAll("[data-reveal]"));

  if (still) {
    targets.forEach(function (el) { el.classList.add("shown"); });
  } else {
    /* A sweep on scroll rather than an IntersectionObserver. Jumping straight down the page,
       which is what a deep link or a restored scroll position does, moves an element from
       below the viewport to above it in one frame. The intersection ratio is 0 both times, no
       threshold is crossed, and no callback ever arrives, so those elements stay invisible
       forever. Comparing positions directly has no such gap. */
    var pending = targets.slice();
    var queued = false;

    var sweep = function () {
      queued = false;
      var limit = window.innerHeight - 60;
      pending = pending.filter(function (el) {
        var box = el.getBoundingClientRect();
        var reached = box.top < limit;
        if (!reached) return true;

        // Stagger a row so it arrives as a wave, but anything already scrolled past appears
        // at once: there is nothing to animate in if it is behind you.
        var behind = box.bottom < 0;
        var delay = behind ? 0 : parseInt(el.getAttribute("data-delay") || "0", 10);
        setTimeout(function () { el.classList.add("shown"); }, delay);
        return false;
      });
      if (!pending.length) removeEventListener("scroll", request);
    };

    var request = function () {
      if (queued) return;
      queued = true;
      requestAnimationFrame(sweep);
    };

    addEventListener("scroll", request, { passive: true });
    addEventListener("resize", request, { passive: true });
    sweep();
  }

  /* ── cards lit from wherever the cursor is ───────────────────────── */
  if (!still && matchMedia("(hover: hover)").matches) {
    document.querySelectorAll(".feature").forEach(function (card) {
      card.addEventListener("pointermove", function (e) {
        var r = card.getBoundingClientRect();
        card.style.setProperty("--mx", (e.clientX - r.left) + "px");
        card.style.setProperty("--my", (e.clientY - r.top) + "px");
      });
    });
  }

  /* ── the survey demo ─────────────────────────────────────────────── */
  /* A real copy of what members see, so the pitch doesn't have to be taken on trust. */
  var demo = document.querySelector("[data-demo]");
  if (demo) {
    var reply = demo.querySelector(".mock-reply");
    var buttons = demo.querySelectorAll(".mock-btn");

    var wording = function (score) {
      if (score >= 9) return "Thanks for rating the server <b>" + score +
        "/10</b>! Enjoy your stay.";
      if (score >= 6) return "Thanks for rating the server <b>" + score +
        "/10</b>! Enjoy your stay.";
      return "Thanks for rating the server <b>" + score +
        "/10</b>. The staff will see this.";
    };

    buttons.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var score = btn.textContent.trim();
        buttons.forEach(function (b) { b.classList.remove("picked"); });
        btn.classList.add("picked");

        reply.innerHTML = wording(parseInt(score, 10));
        reply.classList.remove("show");
        // Reflow so the animation restarts when a second number is picked.
        void reply.offsetWidth;
        reply.classList.add("show");

        var counter = demo.querySelector("[data-votes]");
        if (counter) {
          counter.textContent = (parseInt(counter.textContent, 10) + 1);
        }
      });
    });
  }

  /* ── retention bars fill when they come into view ────────────────── */
  var chart = document.querySelector("[data-chart]");
  if (chart) {
    var fill = function () {
      chart.querySelectorAll(".meter-fill").forEach(function (b) {
        b.style.width = b.getAttribute("data-pct") + "%";
      });
    };
    if (still) {
      fill();
    } else {
      // Same reasoning as the reveal sweep: an empty chart is worse than one that filled
      // while you were scrolling past it.
      var check = function () {
        if (chart.getBoundingClientRect().top >= window.innerHeight * 0.85) return;
        fill();
        removeEventListener("scroll", check);
      };
      addEventListener("scroll", check, { passive: true });
      check();
    }
  }
})();
