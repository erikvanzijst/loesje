// Poster archive search: filters live in the URL query string, results load
// page by page as you scroll (like loesje.nl/posters).
(() => {
  const $ = (s, el = document) => el.querySelector(s);
  const els = {
    form: $("#search-form"), q: $("#q"), from: $("#from"), to: $("#to"), words: $("#words"),
    order: $("#order"), list: $("#list"), count: $("#count"), empty: $("#empty"), sentinel: $("#sentinel"),
    wall: $(".wall"), chips: $("#word-chips"), reset: $("#reset"), detail: $("#detail"), footnote: $("#footnote"),
    exportLink: $("#export"),
  };
  const dropdowns = [...document.querySelectorAll(".dropdown")];
  const CHIP_MAX = 15; // chips 1..15, then "16+"
  const nf = new Intl.NumberFormat("nl-NL");
  const df = new Intl.DateTimeFormat("nl-NL", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });

  let state = readUrl();
  let page = 0, total = 0, loading = false, done = false, generation = 0;
  let meta = null;

  // ---- URL state ---------------------------------------------------------
  function readUrl() {
    const p = new URLSearchParams(location.search);
    const ids = (k) => (p.get(k) || "").split(",").filter(Boolean);
    return {
      q: p.get("q") || "", source: ids("source"), subject: ids("subject"),
      from: p.get("from") || "", to: p.get("to") || "", words: p.get("words") || "",
      order: p.get("order") || "newest", poster: p.get("poster") || "",
    };
  }
  function toParams(s, { withPoster = true } = {}) {
    const p = new URLSearchParams();
    if (s.q) p.set("q", s.q);
    if (s.source.length) p.set("source", s.source.join(","));
    if (s.subject.length) p.set("subject", s.subject.join(","));
    if (s.from) p.set("from", s.from);
    if (s.to) p.set("to", s.to);
    if (s.words) p.set("words", s.words);
    if (s.order !== "newest") p.set("order", s.order);
    if (withPoster && s.poster) p.set("poster", s.poster);
    return p;
  }
  function writeUrl(replace = false) {
    const qs = toParams(state).toString();
    const url = location.pathname + (qs ? "?" + qs : "");
    history[replace ? "replaceState" : "pushState"](null, "", url);
  }
  function isFiltered(s) {
    return !!(s.q || s.source.length || s.subject.length || s.from || s.to || s.words);
  }

  // ---- Controls ----------------------------------------------------------
  function syncControls() {
    els.q.value = state.q; els.from.value = state.from; els.to.value = state.to;
    els.words.value = state.words; els.order.value = state.order;
    for (const dd of dropdowns) {
      const key = dd.dataset.key, sel = new Set(state[key]);
      dd.querySelectorAll("input").forEach((i) => (i.checked = sel.has(i.value)));
      const badge = $(".badge", dd);
      badge.hidden = !sel.size; badge.textContent = sel.size;
    }
    const tokens = wordTokens(state.words);
    els.chips.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", tokens.has(c.dataset.token)));
    els.reset.hidden = !isFiltered(state);
    // The export always matches the current search (all posters when nothing is filtered).
    const qs = toParams(state, { withPoster: false }).toString();
    els.exportLink.href = "/api/posters.csv" + (qs ? "?" + qs : "");
  }

  function wordTokens(spec) {
    return new Set(spec.split(/[,\s;]+/).filter(Boolean));
  }

  function buildFilters() {
    for (const dd of dropdowns) {
      const items = dd.dataset.key === "source" ? meta.sources : meta.subjects;
      $(".dropdown-panel", dd).innerHTML = items.filter((t) => t.count > 0).map((t) =>
        `<label><input type="checkbox" value="${t.id}"> ${esc(t.name)} <span class="n">${nf.format(t.count)}</span></label>`
      ).join("");
    }
    // Word-count chips with a small bar showing how common each count is.
    const byCount = new Map(meta.wordHistogram.map((h) => [h.words, h.count]));
    const over = meta.wordHistogram.filter((h) => h.words > CHIP_MAX).reduce((a, h) => a + h.count, 0);
    const chips = [];
    for (let n = 1; n <= CHIP_MAX; n++) chips.push({ token: String(n), label: n, count: byCount.get(n) || 0 });
    chips.push({ token: `${CHIP_MAX + 1}+`, label: `${CHIP_MAX + 1}+`, count: over });
    const max = Math.max(...chips.map((c) => c.count), 1);
    els.chips.innerHTML = chips.map((c) =>
      `<button type="button" class="chip" data-token="${c.token}" aria-pressed="false"
        title="${nf.format(c.count)} posters met ${c.label} ${c.label === 1 ? "woord" : "woorden"}">${c.label}<span class="bar" style="width:${(100 * c.count / max).toFixed(1)}%"></span></button>`
    ).join("");
    if (meta.dateMin) { els.from.min = els.to.min = meta.dateMin; els.from.max = els.to.max = meta.dateMax; }
    els.footnote.textContent = "Het aantal woorden is geteld uit de titel van de poster in het archief, " +
      "gecontroleerd met de tekst die van de afbeelding is gelezen.";
  }

  function update(patch, { replace = false } = {}) {
    state = { ...state, ...patch, poster: "" };
    writeUrl(replace);
    syncControls();
    restart();
  }

  els.form.addEventListener("submit", (e) => { e.preventDefault(); update({ q: els.q.value.trim() }); });
  let qTimer;
  els.q.addEventListener("input", () => {
    clearTimeout(qTimer);
    qTimer = setTimeout(() => update({ q: els.q.value.trim() }, { replace: true }), 300);
  });
  els.from.addEventListener("change", () => update({ from: els.from.value }));
  els.to.addEventListener("change", () => update({ to: els.to.value }));
  els.order.addEventListener("change", () => update({ order: els.order.value }));
  let wTimer;
  els.words.addEventListener("input", () => {
    clearTimeout(wTimer);
    wTimer = setTimeout(() => update({ words: els.words.value.trim() }, { replace: true }), 300);
  });
  els.chips.addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    const tokens = [...wordTokens(state.words)];
    const t = chip.dataset.token;
    const next = tokens.includes(t) ? tokens.filter((x) => x !== t) : [...tokens, t];
    // Keep exact counts sorted for a tidy input value; ranges stay where they were.
    next.sort((a, b) => (/^\d+$/.test(a) && /^\d+$/.test(b) ? a - b : 0));
    update({ words: next.join(", ") });
  });
  els.reset.addEventListener("click", () =>
    update({ q: "", source: [], subject: [], from: "", to: "", words: "" }));

  for (const dd of dropdowns) {
    const toggle = $(".dropdown-toggle", dd), panel = $(".dropdown-panel", dd);
    toggle.addEventListener("click", () => {
      const open = toggle.getAttribute("aria-expanded") !== "true";
      closeDropdowns();
      toggle.setAttribute("aria-expanded", open); panel.hidden = !open;
    });
    panel.addEventListener("change", () => {
      const vals = [...panel.querySelectorAll("input:checked")].map((i) => i.value);
      update({ [dd.dataset.key]: vals });
    });
  }
  function closeDropdowns() {
    for (const dd of dropdowns) { $(".dropdown-toggle", dd).setAttribute("aria-expanded", false); $(".dropdown-panel", dd).hidden = true; }
  }
  document.addEventListener("click", (e) => { if (!e.target.closest(".dropdown")) closeDropdowns(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDropdowns(); });

  window.addEventListener("popstate", () => {
    state = readUrl(); syncControls(); restart();
    state.poster ? openDetail(state.poster, false) : els.detail.open && els.detail.close();
  });

  // ---- Results -----------------------------------------------------------
  function restart() {
    generation++; page = 0; total = 0; done = false; loading = false;
    els.list.innerHTML = ""; els.empty.hidden = true; els.count.textContent = "";
    loadMore();
  }

  async function loadMore() {
    if (loading || done) return;
    loading = true; els.wall.classList.add("loading");
    const gen = generation;
    const p = toParams(state, { withPoster: false });
    p.set("page", page + 1);
    try {
      const res = await fetch("/api/posters?" + p);
      const data = await res.json();
      if (gen !== generation) return; // a newer search started meanwhile
      if (!res.ok) throw new Error(data.error || res.statusText);
      page = data.page; total = data.total;
      els.list.insertAdjacentHTML("beforeend", data.items.map(tileHtml).join(""));
      els.count.textContent = `${nf.format(total)} ${total === 1 ? "poster" : "posters"}`;
      els.empty.hidden = total > 0;
      done = page * data.pageSize >= total;
    } catch (err) {
      if (gen === generation) { els.count.textContent = "Zoeken mislukt"; console.error(err); done = true; }
    } finally {
      if (gen === generation) {
        loading = false; els.wall.classList.remove("loading");
        // Keep filling while the sentinel is still on screen.
        if (!done && els.sentinel.getBoundingClientRect().top < innerHeight + 600) loadMore();
      }
    }
  }
  new IntersectionObserver((entries) => entries[0].isIntersecting && loadMore(), { rootMargin: "600px" })
    .observe(els.sentinel);

  function tileHtml(p) {
    const tilt = (((p.id * 9301 + 49297) % 233280) / 233280 - 0.5) * 1.2; // stable pseudo-random tilt
    const label = esc(p.title || "");
    const img = p.thumb
      ? `<img src="${p.thumb}" alt="${label}" loading="lazy" width="500" height="${Math.round(500 * (p.height || 2560) / (p.width || 1803))}">`
      : `<span class="noimg">${label}</span>`;
    const wc = `${p.words} ${p.words === 1 ? "woord" : "woorden"}`;
    return `<li><button type="button" class="tile" data-id="${p.id}" style="--tilt:${tilt.toFixed(2)}deg"
      aria-label="${label} (${wc})">${img}<span class="wc">${wc}</span></button></li>`;
  }
  els.list.addEventListener("click", (e) => {
    const tile = e.target.closest(".tile");
    if (tile) openDetail(tile.dataset.id);
  });

  // ---- Detail ------------------------------------------------------------
  async function openDetail(id, push = true) {
    const res = await fetch(`/api/posters/${id}`);
    if (!res.ok) return;
    const p = await res.json();
    if (push) { state.poster = String(id); writeUrl(); }
    const terms = (tax) => p.terms.filter((t) => t.taxonomy === tax).map((t) =>
      `<button type="button" class="term" data-tax="${tax}" data-id="${t.id}">${esc(t.name)}</button>`).join("") || "–";
    const date = p.date ? df.format(new Date(p.date + "T00:00:00Z")) : "onbekend";
    $(".detail-body", els.detail).innerHTML = `
      <div>${p.image ? `<img src="${p.image}" alt="${esc(p.title || "")}">` : `<p class="muted">Geen afbeelding beschikbaar.</p>`}</div>
      <div>
        <h2>${esc(p.title || "")}</h2>
        ${p.text ? `<p class="muted">Gelezen van de poster (OCR):</p><p class="poster-text">${esc(p.text)}</p>` : ""}
        <dl>
          <dt>Datum</dt><dd>${date}${p.pubDateKnown ? "" : ` <span class="muted">(datum van plaatsing)</span>`}</dd>
          <dt>Woorden</dt><dd><button type="button" class="term" data-words="${p.words}">${p.words}</button>
            <span class="muted">${{ title: "geteld uit de titel", text: "geteld uit de gelezen postertekst", manual: "handmatig geteld" }[p.wordsSource] || ""}</span></dd>
          <dt>Bronnen</dt><dd>${terms("source")}</dd>
          <dt>Onderwerpen</dt><dd>${terms("subject")}</dd>
        </dl>
        <a class="btn" href="${p.url}" target="_blank" rel="noopener">Bekijk op loesje.nl</a>
      </div>`;
    if (!els.detail.open) els.detail.showModal();
  }
  els.detail.addEventListener("click", (e) => {
    if (e.target === els.detail || e.target.closest(".close")) return els.detail.close();
    const t = e.target.closest(".term");
    if (!t) return;
    els.detail.close();
    if (t.dataset.words) update({ words: t.dataset.words });
    else update({ [t.dataset.tax]: [t.dataset.id] });
  });
  els.detail.addEventListener("close", () => {
    if (state.poster) { state.poster = ""; writeUrl(); }
  });

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  }

  // ---- Boot --------------------------------------------------------------
  fetch("/api/filters").then((r) => r.json()).then((m) => {
    meta = m; buildFilters(); syncControls(); restart();
    if (state.poster) openDetail(state.poster, false);
  });
})();
