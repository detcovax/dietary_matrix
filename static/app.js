// Progressive enhancements. Every form still works without JavaScript;
// the server re-checks everything.
(function () {
  const UNIT = {
    serving: ["serving", 1], each: ["each", 1],
    g: ["weight", 1], kg: ["weight", 1000], oz: ["weight", 28.349523125], lb: ["weight", 453.59237],
    ml: ["volume", 1], l: ["volume", 1000], tsp: ["volume", 4.92892159375], tbsp: ["volume", 14.78676478125],
    floz: ["volume", 29.5735295625], cup: ["volume", 236.5882365], pint: ["volume", 473.176473],
    quart: ["volume", 946.352946],
  };
  const KCAL = { carbs: 4, fat: 9, protein: 4 };
  const MACROS = ["carbs", "fat", "protein"];
  const dataEl = document.getElementById("food-data");
  const FOODS = dataEl ? JSON.parse(dataEl.textContent) : {};
  const FRACTIONS = { "½": " 1/2", "⅓": " 1/3", "⅔": " 2/3", "¼": " 1/4", "¾": " 3/4", "⅛": " 1/8" };

  const fmt = (n, d = 0) => n.toLocaleString(undefined, { maximumFractionDigits: d });

  function parseQty(text) {
    let s = String(text || "").trim().replace(",", ".");
    for (const [ch, rep] of Object.entries(FRACTIONS)) s = s.split(ch).join(rep);
    const m = s.trim().match(/^(?:(\d+(?:\.\d+)?)\s+)?(\d+(?:\.\d+)?)(?:\s*\/\s*(\d+(?:\.\d+)?))?$/);
    if (!m) return null;
    const [, whole, num, den] = m;
    let v;
    if (den !== undefined) { if (+den === 0) return null; v = +num / +den + (whole ? +whole : 0); }
    else if (whole) return null;
    else v = +num;
    return v > 0 ? v : null;
  }

  function plural(word, q) {
    if (!word || q <= 1 + 1e-9 || word.endsWith("s")) return word;
    return /(ch|sh|x)$/.test(word) ? word + "es" : word + "s";
  }

  function toServings(food, q, unit) {
    if (!food || q == null || !UNIT[unit]) return null;
    const [dim, factor] = UNIT[unit];
    if (dim === "serving") return q;
    if (dim === "each") return food.each ? q / food.each : null;
    if (dim === "weight") return food.g ? (q * factor) / food.g : null;
    if (dim === "volume") return food.ml ? (q * factor) / food.ml : null;
    return null;
  }

  const foodKcal = (food) => Object.values(food.cats).reduce((a, b) => a + b, 0);

  // Only offer units the chosen food can be measured in
  function syncUnits(select, food) {
    select.querySelectorAll("option").forEach((opt) => {
      const ok = !food || toServings(food, 1, opt.value) !== null;
      opt.disabled = !ok;
      opt.hidden = !ok;
      if (opt.value === "each") opt.textContent = food && food.each ? plural(food.each_label, 2) : "each";
    });
    select.querySelectorAll("optgroup").forEach((grp) => {
      grp.hidden = [...grp.children].every((o) => o.disabled);
    });
    if (select.selectedOptions[0] && select.selectedOptions[0].disabled) select.value = "serving";
  }

  // ---- Log form -----------------------------------------------------------
  document.querySelectorAll("[data-log-form]").forEach((form) => {
    const foodSel = form.querySelector("[data-food-select]");
    const unitSel = form.querySelector("[data-unit-select]");
    const qtyIn = form.querySelector("[data-qty]");
    const out = form.querySelector("[data-log-preview]");
    if (!foodSel) return;
    const update = (resync) => {
      const food = FOODS[foodSel.value];
      if (resync) syncUnits(unitSel, food);
      if (!food) { out.textContent = ""; return; }
      const q = parseQty(qtyIn.value);
      if (q === null) { out.textContent = "Enter an amount like 1, 3/4 or 1 1/2."; return; }
      const s = toServings(food, q, unitSel.value);
      if (s === null) { out.textContent = "That unit isn't set up for this food."; return; }
      out.innerHTML = `<strong>${fmt(s * foodKcal(food))} kcal</strong>` +
        (unitSel.value === "serving" || Math.abs(s - q) < 0.005 ? "" : `, about ${fmt(s, 2)} ${plural("serving", s)} of ${food.serving}`);
    };
    foodSel.addEventListener("change", () => update(true));
    unitSel.addEventListener("change", () => update(false));
    qtyIn.addEventListener("input", () => update(false));
    update(true);
  });

  // ---- Recipe builder -----------------------------------------------------
  document.querySelectorAll("[data-recipe-form]").forEach((form) => {
    const list = form.querySelector("[data-comp-list]");
    const tpl = document.getElementById("comp-row-tpl");
    const yieldIn = form.querySelector("[data-yield]");
    const weightIn = form.querySelector('input[name="weight"]');
    const $ = (sel) => form.querySelector(sel);

    function recalc() {
      const cats = {}, macros = { carbs: 0, fat: 0, protein: 0 };
      let batchKcal = 0, grams = 0, allWeighed = true, rows = 0;
      list.querySelectorAll("[data-comp-row]").forEach((row) => {
        const food = FOODS[row.querySelector("[data-food-select]").value];
        const q = parseQty(row.querySelector("[data-qty]").value);
        const unit = row.querySelector("[data-unit-select]").value;
        const cell = row.querySelector("[data-row-kcal]");
        const s = toServings(food, q, unit);
        if (!food) { cell.textContent = ""; return; }
        rows++;
        if (s === null) { cell.textContent = q === null ? "" : "unit not set up"; allWeighed = false; return; }
        const k = s * foodKcal(food);
        cell.textContent = `${fmt(k)} kcal`;
        batchKcal += k;
        for (const [c, v] of Object.entries(food.cats)) cats[c] = (cats[c] || 0) + v * s;
        for (const m of MACROS) macros[m] += food.macros[m] * s;
        if (food.g) grams += food.g * s; else allWeighed = false;
      });
      const y = parseQty(yieldIn.value) || 1;
      const per = batchKcal / y;
      $("[data-sum-kcal]").textContent = fmt(per);
      for (const m of MACROS) $(`[data-sum-${m}]`).textContent = fmt(macros[m] / y, 1);

      const bar = $("[data-sum-split]"), listEl = $("[data-sum-split-list]");
      bar.innerHTML = ""; listEl.innerHTML = "";
      Object.entries(cats).filter(([, v]) => batchKcal && v / batchKcal >= 0.005)
        .sort((a, b) => b[1] - a[1]).forEach(([c, v]) => {
          const pct = (v / batchKcal) * 100;
          const i = document.createElement("i");
          i.dataset.cat = c; i.style.width = pct + "%"; i.title = `${c} ${fmt(pct)}%`;
          bar.appendChild(i);
          const li = document.createElement("li");
          li.innerHTML = `<span class="dot" data-cat="${c}"></span>${c} <b>${fmt(v / y)} kcal</b>`;
          listEl.appendChild(li);
        });

      let batch = rows ? `Whole batch: ${fmt(batchKcal)} kcal.` : "Add ingredients to see the totals.";
      if (rows && allWeighed && grams) {
        batch += ` Ingredients weigh about ${fmt(grams)} g.`;
        weightIn.placeholder = `about ${fmt(grams)} g`;
      } else {
        weightIn.placeholder = "optional";
      }
      $("[data-sum-batch]").textContent = batch;
    }

    form.addEventListener("change", (e) => {
      if (e.target.matches("[data-food-select]")) {
        const row = e.target.closest("[data-comp-row]");
        syncUnits(row.querySelector("[data-unit-select]"), FOODS[e.target.value]);
      }
      recalc();
    });
    form.addEventListener("input", recalc);
    form.addEventListener("click", (e) => {
      if (e.target.matches("[data-remove-row]")) {
        const rows = list.querySelectorAll("[data-comp-row]");
        const row = e.target.closest("[data-comp-row]");
        if (rows.length > 1) row.remove();
        else row.querySelectorAll("input, select").forEach((el) => (el.value = el.tagName === "SELECT" && el.matches("[data-unit-select]") ? "serving" : ""));
        recalc();
      }
      if (e.target.matches("[data-add-row]")) {
        const row = tpl.content.firstElementChild.cloneNode(true);
        list.appendChild(row);
        row.querySelector("select").focus();
      }
    });
    list.querySelectorAll("[data-comp-row]").forEach((row) =>
      syncUnits(row.querySelector("[data-unit-select]"), FOODS[row.querySelector("[data-food-select]").value]));
    recalc();
  });

  // ---- Build page ----------------------------------------------------------
  document.querySelectorAll("[data-unit-select][data-food-id]").forEach((sel) => syncUnits(sel, FOODS[sel.dataset.foodId]));
  document.querySelectorAll("[data-resubmit]").forEach((el) => {
    el.addEventListener("change", () => el.form && el.form.submit());
  });

  // ---- Small helpers used elsewhere ----------------------------------------
  document.querySelectorAll("form[data-autosubmit]").forEach((form) => {
    form.addEventListener("change", () => form.submit());
  });

  document.querySelectorAll("[data-kcal-form]").forEach((box) => {
    const out = box.querySelector("[data-kcal-out]");
    if (!out) return;
    const update = () => {
      let total = 0;
      box.querySelectorAll("[data-macro]").forEach((i) => (total += (parseFloat(i.value) || 0) * KCAL[i.dataset.macro]));
      out.textContent = fmt(total);
    };
    box.addEventListener("input", update);
    update();
  });

  document.querySelectorAll("[data-sum-group]").forEach((group) => {
    const out = document.querySelector(`[data-sum-out="${group.dataset.sumGroup}"]`);
    group.addEventListener("input", () => {
      let sum = 0;
      group.querySelectorAll("input[type=number]").forEach((i) => (sum += parseFloat(i.value) || 0));
      out.textContent = `${Math.round(sum * 10) / 10}%`;
    });
  });

  document.querySelectorAll("[data-filter]").forEach((input) => {
    const tables = document.querySelectorAll(input.dataset.filter);
    input.addEventListener("input", () => {
      const q = input.value.trim().toLowerCase();
      tables.forEach((table) => table.querySelectorAll("tbody[data-group]").forEach((group) => {
        let shown = 0;
        group.querySelectorAll("tr[data-name]").forEach((row) => {
          const match = !q || row.dataset.name.includes(q);
          row.hidden = !match;
          if (match) shown++;
        });
        group.hidden = q && shown === 0;
      }));
    });
  });

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (e) => { if (!confirm(form.dataset.confirm)) e.preventDefault(); });
  });
})();
