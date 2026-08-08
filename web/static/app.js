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

  /* ── settings tabs ───────────────────────────────────────────────── */
  /* One pane at a time instead of eight cards down a single page. Nothing is hidden in the
     markup, only by this script, so with JavaScript off the page is the long list it used to
     be and every sidebar link is an ordinary anchor to a section that is really there. */
  var nav = document.querySelector("[data-tabs]");
  var panes = [].slice.call(document.querySelectorAll(".pane"));

  if (nav && panes.length) {
    var links = [].slice.call(nav.querySelectorAll("[data-tab]"));

    /* On a narrow screen the sidebar is a strip you scroll sideways, so the tab you are on can
       sit outside it. Then nothing on screen says which section you are looking at. */
    var keepVisible = function (link) {
      if (nav.scrollWidth <= nav.clientWidth) return;
      var left = link.offsetLeft;
      var right = left + link.offsetWidth;
      if (left < nav.scrollLeft) {
        nav.scrollLeft = left - 12;
      } else if (right > nav.scrollLeft + nav.clientWidth) {
        nav.scrollLeft = right - nav.clientWidth + 12;
      }
    };

    var show = function (id) {
      var matched = false;
      panes.forEach(function (pane) {
        var wanted = pane.id === id;
        pane.hidden = !wanted;
        if (wanted) matched = true;
      });
      links.forEach(function (link) {
        var on = link.getAttribute("data-tab") === id;
        link.classList.toggle("on", on);
        // Tells a screen reader which one it is currently looking at.
        if (on) {
          link.setAttribute("aria-current", "true");
          keepVisible(link);
        } else {
          link.removeAttribute("aria-current");
        }
      });
      return matched;
    };

    // Falls back to the first pane, so a stale or hand-typed hash still lands somewhere.
    var open = function (id) {
      if (!show(id)) show(panes[0].id);
    };

    links.forEach(function (link) {
      link.addEventListener("click", function (e) {
        e.preventDefault();
        var id = link.getAttribute("data-tab");
        // pushState rather than setting location.hash: same shareable url, no jump to the
        // anchor, and the back button still walks through the tabs you opened.
        history.pushState(null, "", "#" + id);
        open(id);
      });
    });

    addEventListener("popstate", function () { open(location.hash.slice(1)); });

    // Saving redirects back with #section, so the tab you were on is the one that reopens.
    open(location.hash.slice(1));
  }

  /* ── all on / all off for the log events ─────────────────────────── */
  /* Twelve toggles is a lot of clicking to get to "record everything", which is what most
     people want. The buttons only exist when this script runs, so nothing is lost without it. */
  /* Setting .checked in code doesn't fire change, and the rows above listen for it, so the
     event is dispatched by hand or the settings stay revealed for rows now switched off. */
  var setAll = function (boxes, on) {
    boxes.forEach(function (box) {
      box.checked = on;
      box.dispatchEvent(new Event("change", { bubbles: true }));
    });
  };

  document.querySelectorAll("[data-log-all]").forEach(function (button) {
    button.addEventListener("click", function () {
      setAll(button.closest("form").querySelectorAll(".logrow input[type=checkbox]"),
             button.getAttribute("data-log-all") === "1");
    });
  });

  /* ── settings that only appear once their row is on ──────────────── */
  /* A row's own controls are irrelevant until you switch it on, and thirteen log events or
     nine automod rules with every box showing at once is a wall. The markup keeps them, so
     with the script off you get the full form rather than settings you can't reach. */
  document.querySelectorAll("[data-reveals]").forEach(function (row) {
    var box = row.querySelector("input[type=checkbox]");
    if (!box) return;
    var sync = function () { row.classList.toggle("on", box.checked); };
    box.addEventListener("change", sync);
    sync();
  });

  /* Automod only gets an "all off", deliberately. Switching every filter on at once is how a
     server ends up deleting its own moderators' messages. */
  document.querySelectorAll("[data-am-all]").forEach(function (button) {
    button.addEventListener("click", function () {
      setAll(button.closest("form").querySelectorAll(".amrow input[type=checkbox]"), false);
    });
  });

  /* ── unsaved changes ─────────────────────────────────────────────── */
  /* Every card is its own form with its own Save, and tabs hide the ones you aren't looking
     at, so it is easy to change something, move on, and never find out it wasn't saved. Each
     form watches itself against how it arrived, marks its tab, and offers to put it back. */
  var dirtyForms = [].slice.call(document.querySelectorAll(".panes form"));
  var leaving = false;

  var stateOf = function (form) {
    // FormData leaves unticked boxes out entirely, so a toggle shows up as a difference.
    var parts = [];
    new FormData(form).forEach(function (value, key) {
      if (key !== "csrf") parts.push(key + "=" + value);
    });
    return parts.join("&");
  };

  dirtyForms.forEach(function (form) {
    var pane = form.closest(".pane");
    var tab = pane && document.querySelector('[data-tab="' + pane.id + '"]');
    var saved = stateOf(form);

    var notice = document.createElement("div");
    notice.className = "unsaved";
    notice.innerHTML = "<span>You have unsaved changes here.</span>";
    var undo = document.createElement("button");
    undo.type = "button";
    undo.className = "tiny";
    undo.textContent = "Undo";
    notice.appendChild(undo);

    // Above the Save button, which is where somebody is already looking.
    var anchor = form.querySelector(".row.end") || form.querySelector("button[type=submit]");
    if (!anchor) return;
    anchor.parentNode.insertBefore(notice, anchor);

    var mark = function () {
      var dirty = stateOf(form) !== saved;
      form.classList.toggle("dirty", dirty);
      if (!tab) return;
      // The pane may be hidden, so the tab has to carry the news.
      var flag = tab.querySelector(".udot");
      if (dirty && !flag) {
        flag = document.createElement("span");
        flag.className = "udot";
        flag.title = "Unsaved changes";
        tab.appendChild(flag);
      } else if (!dirty && flag) {
        flag.remove();
      }
    };

    form.addEventListener("input", mark);
    form.addEventListener("change", mark);

    undo.addEventListener("click", function () {
      form.reset();
      // reset() puts the values back but fires no events, and the rows that reveal their own
      // settings are listening for exactly those.
      form.querySelectorAll("[data-reveals] input[type=checkbox]").forEach(function (box) {
        box.dispatchEvent(new Event("change", { bubbles: true }));
      });
      mark();
    });

    form.addEventListener("submit", function () { leaving = true; });
  });

  if (dirtyForms.length) {
    addEventListener("beforeunload", function (e) {
      if (leaving || !document.querySelector(".panes form.dirty")) return;
      // Switching tabs is safe, since the panes stay on the page. Actually leaving is not.
      e.preventDefault();
      e.returnValue = "";
    });
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
