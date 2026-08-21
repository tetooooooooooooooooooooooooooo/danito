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

  /* ── documentation: search, scroll-spy, permalinks ───────────────── */
  var docsBody = document.querySelector(".docs-body");
  if (docsBody) {
    var sections = [].slice.call(docsBody.querySelectorAll("section[id]"));
    var tocLink = function (id) {
      return document.querySelector('.toc a[href="#' + id + '"]');
    };

    /* Search. Sixty-odd commands across a dozen sections is more than anybody scrolls
       through, so typing narrows it to the matching rows and hides everything else. */
    var box = document.querySelector("[data-docs-search]");
    var hits = document.querySelector("[data-docs-count]");
    var noHits = document.querySelector("[data-docs-empty]");

    if (box) {
      var filter = function () {
        var q = box.value.trim().toLowerCase();
        var on = q.length > 0;
        docsBody.classList.toggle("filtering", on);
        var found = 0;

        sections.forEach(function (section) {
          var heading = section.querySelector("h2");
          var titleHit = on && heading &&
            heading.textContent.toLowerCase().indexOf(q) !== -1;
          var rows = [].slice.call(section.querySelectorAll("table.commands tbody tr"));
          var showing = 0;

          rows.forEach(function (row) {
            // A section whose own title matches keeps all of its commands, so searching
            // "automod" gives you the whole thing rather than the rows mentioning the word.
            var hit = !on || titleHit || row.textContent.toLowerCase().indexOf(q) !== -1;
            row.hidden = !hit;
            if (hit) showing++;
          });

          // Sections with no command table, like Permissions, match on their heading only.
          // Otherwise every search would drag the prose along with it.
          var keep = !on || (rows.length ? showing > 0 : titleHit);
          section.hidden = !keep;
          var link = tocLink(section.id);
          if (link) link.hidden = !keep;
          found += showing;
        });

        if (hits) {
          hits.hidden = !on;
          hits.textContent = found + (found === 1 ? " command" : " commands");
        }
        if (noHits) noHits.hidden = !(on && found === 0);
      };

      box.addEventListener("input", filter);
      box.addEventListener("keydown", function (e) {
        if (e.key === "Escape") { box.value = ""; filter(); box.blur(); }
      });
      // "/" to search, the way every reference site does, but not while typing somewhere else.
      addEventListener("keydown", function (e) {
        if (e.key !== "/" || e.metaKey || e.ctrlKey) return;
        var tag = (document.activeElement || {}).tagName;
        if (tag === "INPUT" || tag === "TEXTAREA") return;
        e.preventDefault();
        box.focus();
        box.select();
      });
      filter();
    }

    /* Scroll-spy. Position is compared directly rather than watched with an
       IntersectionObserver: jumping down the page moves a heading from below the viewport to
       above it without ever crossing a threshold, so no callback would arrive. */
    var spyLinks = [].slice.call(document.querySelectorAll(".toc a"));
    if (spyLinks.length) {
      var current = null;
      var queued = false;

      var spy = function () {
        queued = false;
        var line = 120;                 // a little under the sticky header
        var active = null;
        sections.forEach(function (section) {
          if (section.hidden) return;
          if (section.getBoundingClientRect().top <= line) active = section.id;
        });
        // Before the first heading, or filtered down to nothing, highlight neither.
        if (active === current) return;
        current = active;
        spyLinks.forEach(function (link) {
          var on = link.getAttribute("href") === "#" + active;
          link.classList.toggle("here", on);
          if (on) { link.setAttribute("aria-current", "true"); }
          else { link.removeAttribute("aria-current"); }
        });
      };

      var askSpy = function () {
        if (queued) return;
        queued = true;
        requestAnimationFrame(spy);
      };
      addEventListener("scroll", askSpy, { passive: true });
      addEventListener("resize", askSpy, { passive: true });
      if (box) box.addEventListener("input", askSpy);
      spy();
    }

    /* Permalinks. Following the link works without any of this; clicking also puts the full
       url on the clipboard, which is the actual reason somebody wants one. */
    docsBody.querySelectorAll("a.anchor").forEach(function (link) {
      link.addEventListener("click", function () {
        var url = location.origin + location.pathname + link.getAttribute("href");
        if (!navigator.clipboard) return;
        navigator.clipboard.writeText(url).then(function () {
          link.classList.add("copied");
          setTimeout(function () { link.classList.remove("copied"); }, 1400);
        }).catch(function () { /* the link still worked */ });
      });
    });
  }

  /* ── the status page keeps itself current ────────────────────────── */
  /* Somebody watching to see whether the bot has come back shouldn't have to keep reloading.
     Everything is rendered server side first, so the page is right before this ever runs. */
  var statusCard = document.querySelector("[data-status]");
  if (statusCard) {
    var put = function (name, text) {
      var el = document.querySelector("[data-status-" + name + "]");
      if (el && text !== null && text !== undefined) el.textContent = text;
    };

    var refresh = function () {
      fetch("/status.json", { headers: { "Accept": "application/json" } })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (data) {
          if (!data) return;
          statusCard.className = "status-card " + data.state;
          var pip = document.querySelector(".part .pip");
          if (pip) pip.className = "pip " + data.state;
          put("heading", data.heading);
          put("detail", data.detail);
          put("quiet", data.seconds_quiet === null || data.seconds_quiet === undefined
              ? "unknown" : data.quiet_for + " ago");
          put("uptime", data.uptime_seconds === null || data.uptime_seconds === undefined
              ? "unknown" : data.uptime);
          put("guilds", (data.guilds || 0).toLocaleString());
          put("latency", data.latency_ms === null || data.latency_ms === undefined
              ? "unknown" : data.latency_ms + " ms");
        })
        .catch(function () { /* a failed poll just means the next one tries again */ });
    };

    var timer = setInterval(refresh, 20000);
    // No point polling a page nobody is looking at.
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) {
        clearInterval(timer);
      } else {
        refresh();
        timer = setInterval(refresh, 20000);
      }
    });
  }

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

  /* ── the message builder ─────────────────────────────────────────── */
  /* Everything here is a convenience over a form that already works: the preview, the
     character counts, the field rows and the draft in local storage. With the script off you
     get a long form with no fields and no preview, which still sends a perfectly good
     message. */
  var builder = document.querySelector("[data-embed-form]");
  if (builder) {
    var DRAFT = "newt:embed:" + location.pathname;
    var pv = function (name) { return builder.querySelector("[data-pv-" + name + "]"); };
    var fieldBox = builder.querySelector("[data-fields]");
    var emptyNote = builder.querySelector("[data-pv-empty]");
    var embedBox = builder.querySelector("[data-preview-embed]");

    var val = function (name) {
      var el = builder.querySelector('[name="' + name + '"]');
      if (!el) return "";
      return el.type === "checkbox" ? el.checked : (el.value || "").trim();
    };

    var escaped = function (text) {
      return String(text).replace(/[&<>"]/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
      });
    };

    /* Just enough markdown to recognise your own message. Discord's is far bigger; this
       covers what people actually put in an embed. Escaped first, always: the preview is
       built from typed text and must never be able to run any of it. */
    var markdown = function (text) {
      return escaped(text)
        .replace(/```([\s\S]+?)```/g, "<code class='block'>$1</code>")
        .replace(/`([^`]+?)`/g, "<code>$1</code>")
        .replace(/\*\*\*([^*]+?)\*\*\*/g, "<b><i>$1</i></b>")
        .replace(/\*\*([^*]+?)\*\*/g, "<b>$1</b>")
        .replace(/\*([^*]+?)\*/g, "<i>$1</i>")
        .replace(/__([^_]+?)__/g, "<u>$1</u>")
        .replace(/~~([^~]+?)~~/g, "<s>$1</s>")
        .replace(/^### (.+)$/gm, "<b class='h3'>$1</b>")
        .replace(/^## (.+)$/gm, "<b class='h2'>$1</b>")
        .replace(/^# (.+)$/gm, "<b class='h1'>$1</b>")
        .replace(/^-# (.+)$/gm, "<small>$1</small>")
        .replace(/\[([^\]]+?)\]\((https?:\/\/[^)\s]+?)\)/g, "<a href='$2'>$1</a>")
        .replace(/\n/g, "<br>");
    };

    var setText = function (node, text, asMarkdown) {
      if (!node) return false;
      var has = !!text;
      node.hidden = !has;
      if (has) node[asMarkdown ? "innerHTML" : "textContent"] = asMarkdown
        ? markdown(text) : text;
      return has;
    };

    var setImage = function (node, url) {
      if (!node) return false;
      var has = /^https?:\/\//i.test(url);
      node.hidden = !has;
      if (has && node.getAttribute("src") !== url) node.src = url;
      return has;
    };

    var rows = function () {
      return [].slice.call(fieldBox ? fieldBox.querySelectorAll("[data-field-row]") : []);
    };

    var drawFields = function () {
      var box = pv("fields");
      if (!box) return false;
      box.innerHTML = "";
      var any = false;
      rows().forEach(function (row) {
        var name = row.querySelector("[data-field-name]").value.trim();
        var text = row.querySelector("[data-field-value]").value.trim();
        if (!name && !text) return;
        any = true;
        var cell = document.createElement("div");
        cell.className = "dc-field" +
          (row.querySelector("[data-field-inline]").checked ? " inline" : "");
        cell.innerHTML = "<b>" + markdown(name) + "</b><span>" + markdown(text) + "</span>";
        box.appendChild(cell);
      });
      return any;
    };

    var draw = function () {
      var channel = builder.querySelector('[name="channel_id"]');
      /* The hash is drawn as an icon beside this, the way Discord draws it, so the one the
         option text carries has to come off or it reads "# #general". */
      var picked = channel && channel.selectedIndex > 0
        ? channel.options[channel.selectedIndex].text.replace(/^#/, "")
        : "wherever you pick";
      builder.querySelector("[data-preview-channel]").textContent = picked;

      var content = builder.querySelector("[data-preview-content]");
      setText(content, val("content"), true);

      /* Discord colours a title blue when it links somewhere, and leaves it white when it
         doesn't. It is the only styling in the embed that depends on another field. */
      pv("title").classList.toggle("linked", !!val("url") && !!val("title"));

      var parts = [
        setText(pv("title"), val("title"), false),
        setText(pv("desc"), val("description"), true),
        drawFields(),
        setImage(pv("image"), val("image")),
        setImage(pv("thumb"), val("thumbnail")),
      ];

      var authorName = val("author_name");
      var author = pv("author");
      author.hidden = !authorName;
      if (authorName) {
        pv("author-name").textContent = authorName;
        setImage(pv("author-icon"), val("author_icon"));
        parts.push(true);
      }

      var footerText = val("footer_text");
      var stamped = val("timestamp");
      var footer = pv("footer");
      footer.hidden = !(footerText || stamped);
      if (footerText || stamped) {
        pv("footer-text").textContent = footerText;
        setImage(pv("footer-icon"), footerText ? val("footer_icon") : "");
        var time = pv("time");
        time.hidden = !stamped;
        if (stamped) {
          time.textContent = (footerText ? " • " : "") +
            new Date().toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
        }
        parts.push(true);
      }

      var colour = val("colour");
      embedBox.style.borderLeftColor = colour || "#1e1f22";
      var swatch = builder.querySelector("[data-colour-text]");
      if (swatch) swatch.textContent = colour || "none";

      var anything = parts.some(Boolean);
      embedBox.hidden = !anything;
      emptyNote.hidden = anything || !!val("content");

      builder.querySelectorAll("[data-limit]").forEach(function (input) {
        var counter = builder.querySelector('[data-count-for="' + input.name + '"]');
        if (!counter) return;
        var limit = parseInt(input.getAttribute("data-limit"), 10);
        var used = (input.value || "").length;
        /* Only once it is worth knowing. A counter on an empty box is clutter. */
        counter.textContent = used > limit * 0.7 ? used + " / " + limit : "";
        counter.classList.toggle("over", used >= limit);
      });
    };

    /* ── field rows ────────────────────────────────────────────────── */
    var renumber = function () {
      rows().forEach(function (row, i) {
        row.querySelector("[data-field-name]").name = "field_name_" + i;
        row.querySelector("[data-field-value]").name = "field_value_" + i;
        row.querySelector("[data-field-inline]").name = "field_inline_" + i;
        row.querySelector("[data-field-number]").textContent = "Field " + (i + 1);
      });
      var add = builder.querySelector("[data-add-field]");
      if (add) add.disabled = rows().length >= 25;
    };

    var addField = function (name, text, inline) {
      if (rows().length >= 25) return;
      var row = document.createElement("div");
      row.className = "field-row";
      row.setAttribute("data-field-row", "");
      row.innerHTML =
        '<div class="field-head"><strong data-field-number></strong>' +
        '<button type="button" class="link" data-remove-field>Remove</button></div>' +
        '<label>Name<input type="text" maxlength="256" data-field-name data-limit="256"></label>' +
        '<label>Text<textarea rows="2" maxlength="1024" data-field-value ' +
        'data-limit="1024"></textarea></label>' +
        '<label class="check"><input type="checkbox" data-field-inline>' +
        '<span>Sit it beside the others</span></label>';
      fieldBox.appendChild(row);
      row.querySelector("[data-field-name]").value = name || "";
      row.querySelector("[data-field-value]").value = text || "";
      row.querySelector("[data-field-inline]").checked = !!inline;
      row.querySelector("[data-remove-field]").addEventListener("click", function () {
        row.remove();
        renumber();
        changed();
      });
      renumber();
    };

    /* ── the draft ─────────────────────────────────────────────────── */
    var collect = function () {
      var out = { fields: [] };
      builder.querySelectorAll("[name]").forEach(function (el) {
        if (el.name === "csrf" || /^field_/.test(el.name)) return;
        out[el.name] = el.type === "checkbox" ? el.checked : el.value;
      });
      rows().forEach(function (row) {
        out.fields.push({
          name: row.querySelector("[data-field-name]").value,
          value: row.querySelector("[data-field-value]").value,
          inline: row.querySelector("[data-field-inline]").checked,
        });
      });
      return out;
    };

    var changed = function () {
      draw();
      try {
        localStorage.setItem(DRAFT, JSON.stringify(collect()));
      } catch (e) {
        /* Private browsing, or a full quota. The form still works, it just won't come back. */
      }
    };

    var restore = function () {
      var saved;
      try {
        saved = JSON.parse(localStorage.getItem(DRAFT) || "null");
      } catch (e) {
        saved = null;
      }
      if (!saved) return;
      Object.keys(saved).forEach(function (key) {
        if (key === "fields") return;
        var el = builder.querySelector('[name="' + key + '"]');
        if (!el) return;
        if (el.type === "checkbox") el.checked = !!saved[key];
        else el.value = saved[key];
      });
      (saved.fields || []).forEach(function (f) { addField(f.name, f.value, f.inline); });
    };

    builder.addEventListener("input", changed);
    builder.addEventListener("change", changed);

    var addButton = builder.querySelector("[data-add-field]");
    if (addButton) {
      addButton.addEventListener("click", function () { addField(); changed(); });
    }

    var clearColour = builder.querySelector("[data-clear-colour]");
    if (clearColour) {
      clearColour.addEventListener("click", function () {
        /* A colour input can't be empty, so the value is blanked by name instead and the
           swatch says "none" until one is picked again. */
        var input = builder.querySelector('[name="colour"]');
        input.value = "";
        changed();
      });
    }

    var reset = builder.querySelector("[data-reset]");
    if (reset) {
      reset.addEventListener("click", function () {
        if (!confirm("Clear everything and start again?")) return;
        builder.reset();
        rows().forEach(function (row) { row.remove(); });
        try { localStorage.removeItem(DRAFT); } catch (e) {}
        renumber();
        draw();
      });
    }

    /* The draft is thrown away only once something really went out. Clearing it on submit
       instead would lose everything somebody typed the moment the server refused a message,
       which is exactly when they least want to start again: every route back here, sent or
       refused, is the same redirect to the same url. */
    if (/[?&]sent=1(&|$)/.test(location.search)) {
      try { localStorage.removeItem(DRAFT); } catch (e) {}
      history.replaceState(null, "", location.pathname);
    }

    restore();
    draw();
  }

  /* ── the insights chart ──────────────────────────────────────────── */
  /* Every period's geometry is already on the page, worked out server side, so switching a
     toggle is a redraw rather than a page load. The toggles stay real links: without this
     script they navigate, and the server renders exactly the same thing. */
  var chartRoot = document.querySelector("[data-chart-root]");
  if (chartRoot) {
    var NS = "http://www.w3.org/2000/svg";
    var charts = JSON.parse(document.querySelector("[data-chart-data]").textContent);
    var svg = chartRoot.querySelector("[data-activity]");
    var wrap = chartRoot.querySelector(".chart-wrap");
    var tip = chartRoot.querySelector("[data-chart-tip]");
    var legend = chartRoot.querySelector("[data-chart-legend]");
    var headingEl = chartRoot.querySelector("[data-chart-heading]");
    var unitEl = chartRoot.querySelector("[data-chart-unit]");
    var state = { period: chartRoot.dataset.period, series: chartRoot.dataset.series };
    var activeBucket = -1;

    var shown = function (series) {
      return series === "both" ? ["joins", "leaves"] : [series];
    };

    var make = function (name, attrs) {
      var node = document.createElementNS(NS, name);
      for (var key in attrs) node.setAttribute(key, attrs[key]);
      return node;
    };

    /* Named around what is already here: `var` is function scoped across this whole file, so
       `chart` would clash with the landing page's retention bars further down and `current`
       with the docs scroll-spy above. Both would silently overwrite this one. */
    var thisChart = function () { return charts[state.period]; };

    /* ── drawing ───────────────────────────────────────────────────── */
    var draw = function () {
      var c = thisChart();
      var names = shown(state.series);
      var frag = document.createDocumentFragment();

      c.gridlines.forEach(function (g) {
        frag.appendChild(make("line", { "class": "gridline", x1: c.left, y1: g.y,
                                        x2: c.w - c.right, y2: g.y }));
        var label = make("text", { "class": "axis", x: c.left - 8, y: g.y + 4,
                                   "text-anchor": "end" });
        label.textContent = g.value;
        frag.appendChild(label);
      });

      if (!c.empty) {
        /* Areas first, all of them, so an overlap never buries a line. */
        names.forEach(function (name) {
          var pts = c.lines[name].points;
          var shape = pts[0].x + "," + c.baseline;
          pts.forEach(function (p) { shape += " " + p.x + "," + p.y; });
          shape += " " + pts[pts.length - 1].x + "," + c.baseline;
          frag.appendChild(make("polygon", { "class": "area " + name, points: shape }));
        });
        names.forEach(function (name) {
          var line = c.lines[name].points.map(function (p) { return p.x + "," + p.y; });
          frag.appendChild(make("polyline", { "class": "line " + name,
                                              points: line.join(" ") }));
        });
        names.forEach(function (name) {
          c.lines[name].points.forEach(function (p) {
            frag.appendChild(make("circle", { "class": "dot " + name,
                                              cx: p.x, cy: p.y, r: 3.5 }));
          });
        });
      }

      c.labels.forEach(function (l) {
        var label = make("text", { "class": "axis", x: l.x, y: c.h - 8,
                                   "text-anchor": "middle" });
        label.textContent = l.text;
        frag.appendChild(label);
      });

      if (c.empty) {
        var note = make("text", { "class": "nothing", "text-anchor": "middle",
                                  x: c.left + (c.w - c.left - c.right) / 2,
                                  y: c.top + (c.h - c.top - c.bottom) / 2 });
        note.textContent = "Nobody joined or left in this period";
        frag.appendChild(note);
      }

      /* The guide sits under the hit columns, which are transparent and catch everything. */
      var guide = make("line", { "class": "guide", x1: 0, y1: c.top, x2: 0, y2: c.baseline });
      guide.style.display = "none";
      frag.appendChild(guide);

      c.hits.forEach(function (hit, i) {
        var rect = make("rect", { "class": "hit", x: hit.x, y: c.top,
                                  width: hit.w, height: c.baseline - c.top });
        rect.addEventListener("pointerenter", function () { point(i); });
        /* Touch reports a pointerenter and never a leave, so tapping elsewhere has to be
           what dismisses it. */
        rect.addEventListener("pointerdown", function () { point(i); });
        frag.appendChild(rect);
      });

      svg.textContent = "";
      svg.appendChild(frag);
      svg.setAttribute("class", "trend " + state.series + (c.empty ? " bare" : ""));
      svg.setAttribute("aria-label", "Members joined and left, " + c.heading.toLowerCase());
      headingEl.textContent = c.heading;
      unitEl.textContent = c.unit;
      hide();
      drawLegend();
    };

    var drawLegend = function () {
      var c = thisChart();
      var html = shown(state.series).map(function (name) {
        return '<span class="key ' + name + '"><i></i>' + c.lines[name].label +
               " <b>" + c.lines[name].total + "</b></span>";
      });
      if (state.series === "both") {
        var net = c.totals.net;
        html.push('<span class="key net' + (net < 0 ? " down" : "") + '">Net <b>' +
                  (net >= 0 ? "+" : "") + net + "</b></span>");
      }
      legend.innerHTML = html.join("");
    };

    /* ── the tooltip ───────────────────────────────────────────────── */
    var hide = function () {
      activeBucket = -1;
      tip.hidden = true;
      var guide = svg.querySelector(".guide");
      if (guide) guide.style.display = "none";
    };

    var point = function (i) {
      var c = thisChart();
      if (c.empty || i === activeBucket) return;
      activeBucket = i;
      var hit = c.hits[i];
      var names = shown(state.series);

      var rows = names.map(function (name) {
        return '<span class="tip-row"><i class="' + name + '"></i>' +
               "<b>" + hit[name] + "</b> " + c.lines[name].label.toLowerCase() + "</span>";
      });
      tip.innerHTML = '<span class="tip-when">' + hit.label + "</span>" + rows.join("");
      tip.hidden = false;

      var guide = svg.querySelector(".guide");
      if (guide) {
        guide.setAttribute("x1", hit.centre);
        guide.setAttribute("x2", hit.centre);
        guide.style.display = "";
      }

      /* The svg scales to its container, so a viewBox coordinate has to be converted before
         an html element can be put on top of it. scrollLeft matters too: on a narrow screen
         the chart scrolls inside the wrapper the tooltip is positioned against. */
      var box = svg.getBoundingClientRect();
      var frame = wrap.getBoundingClientRect();
      var scale = box.width / c.w;
      var highest = Math.min.apply(null, names.map(function (name) {
        return c.lines[name].points[i].y;
      }));
      var left = box.left - frame.left + wrap.scrollLeft + hit.centre * scale;
      tip.style.left = left + "px";
      tip.style.top = (box.top - frame.top + highest * scale) + "px";

      /* Nudged back inside if it would hang off either edge, so the first and last buckets
         are as readable as the ones in the middle. */
      var width = tip.offsetWidth;
      var limit = wrap.clientWidth + wrap.scrollLeft;
      var shift = 0;
      if (left - width / 2 < wrap.scrollLeft + 4) shift = wrap.scrollLeft + 4 - (left - width / 2);
      else if (left + width / 2 > limit - 4) shift = limit - 4 - (left + width / 2);
      tip.style.left = (left + shift) + "px";
    };

    wrap.addEventListener("pointerleave", hide);
    addEventListener("scroll", function () { if (activeBucket !== -1) hide(); }, { passive: true });

    /* ── the toggles ───────────────────────────────────────────────── */
    var pick = function (attr, key) {
      chartRoot.querySelectorAll("[" + attr + "]").forEach(function (link) {
        link.addEventListener("click", function (e) {
          e.preventDefault();
          state[key] = link.getAttribute(attr);
          chartRoot.querySelectorAll("[" + attr + "]").forEach(function (other) {
            other.classList.toggle("on", other === link);
          });
          /* Every other link's href has to follow, or the two toggles would disagree about
             what the page currently shows the moment scripting stops. */
          var url = chartRoot.dataset.url + "?period=" + state.period + "&series=" + state.series;
          history.replaceState(null, "", url);
          chartRoot.querySelectorAll("[data-period-pick]").forEach(function (a) {
            a.href = chartRoot.dataset.url + "?period=" + a.getAttribute("data-period-pick") +
                     "&series=" + state.series;
          });
          chartRoot.querySelectorAll("[data-series-pick]").forEach(function (a) {
            a.href = chartRoot.dataset.url + "?period=" + state.period +
                     "&series=" + a.getAttribute("data-series-pick");
          });
          draw();
        });
      });
    };
    pick("data-period-pick", "period");
    pick("data-series-pick", "series");

    /* Redrawn once on load so the hit columns and the guide exist. The server already put a
       chart here, so nothing visibly changes. */
    draw();
  }

  /* ── monthly or yearly ───────────────────────────────────────────── */
  /* Both plans are in the page already. This only decides which one is on show, so with the
     script missing the page is two cards side by side rather than nothing. */
  var billing = document.querySelector("[data-billing]");
  if (billing) {
    var segs = [].slice.call(billing.querySelectorAll("[data-period]"));
    var cards = [].slice.call(document.querySelectorAll("[data-plan]"));

    var show = function (period) {
      segs.forEach(function (seg) {
        var on = seg.getAttribute("data-period") === period;
        seg.classList.toggle("on", on);
        seg.setAttribute("aria-pressed", on ? "true" : "false");
      });
      cards.forEach(function (card) {
        card.classList.toggle("on", card.getAttribute("data-plan") === period);
      });
    };

    segs.forEach(function (seg) {
      seg.addEventListener("click", function () {
        show(seg.getAttribute("data-period"));
      });
    });

    /* ?plan=yearly so a link can point straight at the one being talked about. Anything else
       leaves the server's choice alone. */
    var wanted = (location.search.match(/[?&]plan=([a-z]+)/) || [])[1];
    if (wanted && segs.some(function (s) { return s.getAttribute("data-period") === wanted; })) {
      show(wanted);
    }
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

/* ── the soundboard page ───────────────────────────────────────────────
   Its own IIFE rather than more code in the one above, because that one is full of names like
   `chart` and `count` that have already collided across features in this file. Nothing in here
   escapes.

   Everything is progressive. Without script the page still lists every sound, still plays them
   through the browser's own controls, still renames and still deletes: only dragging to
   reorder disappears, which is exactly the feature that should be hardest to trigger by
   accident. */
(function () {
  "use strict";

  var list = document.querySelector("[data-sounds]");
  var bar = document.querySelector("[data-reorder-bar]");
  var modal = document.querySelector("[data-modal]");
  var form = document.querySelector("[data-reorder]");

  /* ── playing one at a time ──────────────────────────────────────── */
  var playing = null;
  document.addEventListener("click", function (e) {
    var button = e.target.closest("[data-play]");
    if (!button) return;
    e.preventDefault();
    if (playing) {
      playing.audio.pause();
      playing.button.textContent = "▶";
      var wasSame = playing.button === button;
      playing = null;
      if (wasSame) return;          /* a second click on the same one stops it */
    }
    var audio = new Audio(button.getAttribute("data-play"));
    audio.volume = 0.8;
    audio.play().catch(function () {
      button.textContent = "✕";     /* autoplay refused, or the file has gone */
    });
    button.textContent = "■";
    playing = { audio: audio, button: button };
    audio.addEventListener("ended", function () {
      button.textContent = "▶";
      playing = null;
    });
  });

  /* ── refusing a file Discord would refuse ───────────────────────── */
  var picker = document.querySelector("[data-sound-file]");
  var note = document.querySelector("[data-file-note]");
  if (picker && note) {
    var original = note.textContent;
    picker.addEventListener("change", function () {
      var file = picker.files && picker.files[0];
      note.classList.remove("bad");
      if (!file) { note.textContent = original; return; }

      var maxBytes = parseInt(picker.getAttribute("data-max-bytes"), 10);
      var maxSeconds = parseFloat(picker.getAttribute("data-max-seconds"));
      if (file.size > maxBytes) {
        note.textContent = "That file is " + Math.round(file.size / 1024) + "KB. Discord's "
          + "limit is " + Math.round(maxBytes / 1024) + "KB.";
        note.classList.add("bad");
        return;
      }
      /* Length is the limit people trip over, and the browser knows it without uploading
         anything. Reported rather than blocked: metadata can be wrong, and being told no by
         a page that guessed is worse than being told no by Discord. */
      var probe = new Audio();
      probe.preload = "metadata";
      probe.addEventListener("loadedmetadata", function () {
        URL.revokeObjectURL(probe.src);
        var seconds = probe.duration;
        if (seconds && seconds > maxSeconds) {
          note.textContent = "That is " + seconds.toFixed(1) + " seconds. Discord's limit is "
            + maxSeconds + ", so this will be refused. Trim it first.";
          note.classList.add("bad");
        } else if (seconds) {
          note.textContent = seconds.toFixed(1) + " seconds, "
            + Math.round(file.size / 1024) + "KB. That will go through.";
        }
      });
      probe.src = URL.createObjectURL(file);
    });
  }

  /* ── deleting says what it is deleting ──────────────────────────── */
  document.addEventListener("submit", function (e) {
    var name = e.target.getAttribute && e.target.getAttribute("data-delete");
    if (name === null || name === undefined) return;
    if (!confirm("Delete " + name + "? This cannot be undone, and it goes from everyone's "
                 + "favourites too.")) {
      e.preventDefault();
    }
  });

  if (!list || !bar || !modal || !form) return;

  /* ── dragging ───────────────────────────────────────────────────── */
  var startOrder = ids();
  var held = null;

  function ids() {
    return Array.prototype.map.call(list.children, function (li) {
      return li.getAttribute("data-id");
    });
  }

  list.addEventListener("dragstart", function (e) {
    held = e.target.closest(".sound");
    if (!held) return;
    held.classList.add("held");
    /* Firefox will not start a drag at all without something in the transfer. */
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", held.getAttribute("data-id")); } catch (err) {}
  });

  list.addEventListener("dragend", function () {
    if (held) held.classList.remove("held");
    held = null;
    review();
  });

  list.addEventListener("dragover", function (e) {
    if (!held) return;
    e.preventDefault();
    var over = e.target.closest(".sound");
    if (!over || over === held) return;
    /* Past the halfway line the held item belongs after this one, which is what stops a drag
       flickering between two positions while the pointer sits on a boundary. */
    var box = over.getBoundingClientRect();
    var after = (e.clientY - box.top) / box.height > 0.5;
    list.insertBefore(held, after ? over.nextSibling : over);
  });

  /* ── how much a given order actually costs ──────────────────────── */
  function firstChanged(now) {
    for (var i = 0; i < now.length; i++) {
      if (now[i] !== startOrder[i]) return i;
    }
    return -1;
  }

  function review() {
    var now = ids();
    var from = firstChanged(now);
    if (from < 0) {
      bar.hidden = true;
      return;
    }
    var affected = now.length - from;
    bar.hidden = false;
    bar.querySelector("[data-reorder-note]").textContent =
      affected + (affected === 1 ? " sound moves" : " sounds move")
      + " and will be re-uploaded.";
    form.querySelector("[data-order]").value = now.join(",");
    modal.querySelector("[data-modal-count]").textContent =
      "Reordering re-uploads " + affected + " of " + now.length
      + (now.length === 1 ? " sound." : " sounds.")
      + " Everything from the first one that moved onwards has to be recreated, because"
      + " Discord orders a soundboard by when each sound was uploaded.";
  }

  bar.querySelector("[data-reorder-cancel]").addEventListener("click", function () {
    /* Put them back in the order the page loaded with, rather than reloading and losing any
       half-typed rename in the row next to it. */
    startOrder.forEach(function (id) {
      var li = list.querySelector('[data-id="' + id + '"]');
      if (li) list.appendChild(li);
    });
    review();
  });

  bar.querySelector("[data-reorder-open]").addEventListener("click", function () {
    modal.hidden = false;
    modal.querySelector("[data-modal-cancel]").focus();
  });

  function shut() { modal.hidden = true; }
  modal.querySelector("[data-modal-cancel]").addEventListener("click", shut);
  modal.addEventListener("click", function (e) { if (e.target === modal) shut(); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !modal.hidden) shut();
  });

  modal.querySelector("[data-modal-go]").addEventListener("click", function () {
    modal.querySelector("[data-modal-go]").disabled = true;
    form.submit();
  });
})();
