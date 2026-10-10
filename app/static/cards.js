// Manual commentary cards are kept separate from subtitle editing state.
(() => {
  const DEFAULT_STYLE = {
    side: "right", width_pct: 32, font_family: "Microsoft YaHei",
    title_size: 54, english_size: 34, body_size: 38,
    title_color: "#FFFFFF", english_color: "#DCE6F1", body_color: "#FFFFFF",
    title_bold: true, english_bold: false, body_bold: false,
    line_spacing: 1.25, padding: 30, background_opacity: 0.62,
    fade_in_ms: 800, fade_out_ms: 800,
  };
  let cards = [];
  let projectId = null;
  let selectedId = null;
  let isNew = false;
  let dirty = false;
  let tab = "subtitle";
  let draftAsset = null;
  let visualTimer = null;
  let requestNumber = 0;
  let assetUrls = new Map();
  let savedAssets = new Map();

  const field = (id) => document.getElementById(`card-${id}`);

  function setStatus(message, error = false) {
    field("save-state").textContent = message;
    const box = field("validation");
    box.textContent = error ? message : "";
    box.classList.toggle("hidden", !error);
  }

  function cardPayload() {
    const number = (id, scale = 1) => {
      const element = field(id);
      const value = Number(element.value);
      if (!element.value.trim() || !Number.isFinite(value)) {
        throw new Error(`${element.closest("label").firstChild.textContent.trim()} needs a number`);
      }
      const min = element.min === "" ? -Infinity : Number(element.min);
      const max = element.max === "" ? Infinity : Number(element.max);
      if (value < min || value > max) {
        throw new Error(`${element.closest("label").firstChild.textContent.trim()} is out of range`);
      }
      return Math.round(value * scale * 1000) / 1000;
    };
    const payload = {
      start_ms: Math.round(number("start", 1000)),
      end_ms: Math.round(number("end", 1000)),
      chinese_term: field("chinese").value.trim(),
      english_term: field("english").value.trim(),
      explanation: field("explanation").value.trim(),
      style: {
        side: field("side").value,
        width_pct: number("width"), font_family: field("font").value,
        title_size: number("title-size"), english_size: number("english-size"),
        body_size: number("body-size"),
        title_color: field("title-color").value.toUpperCase(),
        english_color: field("english-color").value.toUpperCase(),
        body_color: field("body-color").value.toUpperCase(),
        title_bold: field("title-bold").checked,
        english_bold: field("english-bold").checked,
        body_bold: field("body-bold").checked,
        line_spacing: number("line-spacing"), padding: number("padding"),
        background_opacity: number("bg-opacity") / 100,
        fade_in_ms: Math.round(number("fade-in", 1000)),
        fade_out_ms: Math.round(number("fade-out", 1000)),
      },
    };
    if (!payload.chinese_term || !payload.explanation) {
      throw new Error("Enter a Chinese term and explanation");
    }
    if (payload.end_ms <= payload.start_ms) throw new Error("End must be after start");
    if (payload.style.fade_in_ms + payload.style.fade_out_ms > payload.end_ms - payload.start_ms) {
      throw new Error("Fade-in and fade-out together exceed the card duration");
    }
    if (state.project?.duration_ms && payload.end_ms > state.project.duration_ms) {
      throw new Error("Card ends after the video");
    }
    return payload;
  }

  function fill(card) {
    field("start").value = (card.start_ms / 1000).toFixed(1);
    field("end").value = (card.end_ms / 1000).toFixed(1);
    field("chinese").value = card.chinese_term;
    field("english").value = card.english_term;
    field("explanation").value = card.explanation;
    const style = { ...DEFAULT_STYLE, ...card.style };
    field("side").value = style.side;
    field("width").value = style.width_pct;
    field("font").value = style.font_family;
    for (const [input, key] of [
      ["title-size", "title_size"], ["english-size", "english_size"],
      ["body-size", "body_size"], ["title-color", "title_color"],
      ["english-color", "english_color"], ["body-color", "body_color"],
      ["line-spacing", "line_spacing"], ["padding", "padding"],
    ]) field(input).value = style[key];
    field("bg-opacity").value = Math.round(style.background_opacity * 100);
    field("fade-in").value = (style.fade_in_ms / 1000).toFixed(1);
    field("fade-out").value = (style.fade_out_ms / 1000).toFixed(1);
    for (const [input, key] of [
      ["title-bold", "title_bold"], ["english-bold", "english_bold"],
      ["body-bold", "body_bold"],
    ]) field(input).checked = style[key];
    field("editor-title").textContent = isNew ? "New card" : `Card ${selectedId}`;
    field("delete").textContent = isNew ? "Discard" : "Delete";
    field("editor").classList.remove("hidden");
    setStatus(isNew ? "Unsaved new card" : "Saved");
  }

  function renderList() {
    const list = field("list");
    document.getElementById("cards-tab-count").textContent = cards.length ? `(${cards.length})` : "";
    if (!cards.length) {
      list.innerHTML = '<p class="card-empty">No cards yet. Add one at the current video time.</p>';
      return;
    }
    list.replaceChildren();
    for (const card of cards) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `card-list-item ${selectedId === card.id ? "selected" : ""}`;
      const title = document.createElement("strong");
      title.textContent = card.chinese_term;
      const times = document.createElement("span");
      times.textContent = `${formatTime(card.start_ms)} – ${formatTime(card.end_ms)}`;
      button.append(title, times);
      button.addEventListener("click", () => void selectCard(card.id));
      list.append(button);
    }
  }

  function setTab(next) {
    tab = next;
    $("#subtitle-pane").classList.toggle("hidden", next !== "subtitle");
    document.getElementById("cards-workspace").classList.toggle("hidden", next !== "cards");
    $("#scroll-to-active").classList.toggle("hidden", next === "cards");
    $("#loop-current").closest(".loop-control").classList.toggle("hidden", next === "cards");
    $("#save-state").classList.toggle("hidden", next === "cards");
    for (const [name, id] of [["subtitle", "subtitle-tab"], ["cards", "cards-tab"]]) {
      const button = document.getElementById(id);
      button.classList.toggle("active", next === name);
      button.setAttribute("aria-selected", String(next === name));
    }
  }

  function revokeAssets() {
    for (const url of assetUrls.values()) URL.revokeObjectURL(url);
    assetUrls.clear();
    savedAssets.clear();
    if (draftAsset?.url) URL.revokeObjectURL(draftAsset.url);
    draftAsset = null;
  }

  async function fetchVisual(card) {
    const requestedId = projectId;
    const response = await fetch(`/api/projects/${requestedId}/cards/${card.id}/visual`);
    if (!response.ok) throw new Error(await readApiError(response));
    const asset = {
      url: URL.createObjectURL(await response.blob()),
      x: Number(response.headers.get("X-Card-X")),
      y: Number(response.headers.get("X-Card-Y")),
      width: Number(response.headers.get("X-Card-Width")),
      height: Number(response.headers.get("X-Card-Height")),
    };
    if (requestedId !== projectId) {
      URL.revokeObjectURL(asset.url);
      return;
    }
    const old = assetUrls.get(card.id);
    if (old) URL.revokeObjectURL(old);
    assetUrls.set(card.id, asset.url);
    savedAssets.set(card.id, asset);
    sync($("#video").currentTime * 1000);
  }

  async function loadProject() {
    const requestedId = state.project?.id;
    requestNumber += 1;
    revokeAssets();
    field("overlay").classList.add("hidden");
    field("overlay").removeAttribute("src");
    field("overlay").dataset.url = "";
    projectId = requestedId;
    cards = [];
    selectedId = null;
    isNew = false;
    dirty = false;
    field("editor").classList.add("hidden");
    setTab("subtitle");
    if (!projectId) return;
    const loaded = await api(`/api/projects/${projectId}/cards`);
    if (requestedId !== state.project?.id) return;
    cards = loaded;
    renderList();
    renderProject();
    await Promise.all(cards.map((card) => fetchVisual(card).catch((error) => {
      console.warn("Card visual failed:", error);
    })));
    sync($("#video").currentTime * 1000);
  }

  function renderProject() {
    const locked = projectIsRenderLocked();
    field("add").disabled = locked || !state.project?.video_path;
    field("save").disabled = locked;
    field("delete").disabled = locked;
    document.getElementById("cards-render").disabled = locked || !["waiting_review", "completed", "failed"].includes(state.project?.status);
    document.getElementById("cards-render").textContent = state.project?.status === "completed"
      ? "Render new version" : "Approve all and render";
    field("render-status").textContent = state.project?.render_is_stale
      ? "Rendered video is outdated" : "";
  }

  async function selectCard(id) {
    if (!(await flush())) return;
    const card = cards.find((item) => item.id === id);
    if (!card) return;
    selectedId = id;
    isNew = false;
    dirty = false;
    fill(card);
    renderList();
    const video = $("#video");
    video.currentTime = card.start_ms / 1000;
    video.pause();
    sync(card.start_ms);
  }

  async function addCard() {
    if (!(await flush())) return;
    const requestedId = projectId;
    let style;
    try {
      style = await api(`/api/projects/${requestedId}/cards/default-style`, { cache: "no-store" });
    } catch (error) {
      showToast(error.message);
      return;
    }
    if (requestedId !== projectId) return;
    const duration = state.project?.duration_ms || 0;
    const start = Math.max(0, Math.min(Math.round($("#video").currentTime * 1000), duration - 2000));
    selectedId = null;
    isNew = true;
    dirty = true;
    fill({
      start_ms: start, end_ms: Math.min(duration, start + 10000),
      chinese_term: "", english_term: "", explanation: "", style,
    });
    field("chinese").focus();
    renderList();
  }

  async function save() {
    if (!dirty) return true;
    let payload;
    try {
      payload = cardPayload();
      field("save").disabled = true;
      setStatus("Saving…");
      const path = isNew ? "" : `/${selectedId}`;
      const saved = await api(`/api/projects/${projectId}/cards${path}`, {
        method: isNew ? "POST" : "PUT",
        body: JSON.stringify(payload),
      });
      cards = cards.filter((card) => card.id !== saved.id);
      cards.push(saved);
      cards.sort((a, b) => a.start_ms - b.start_ms || a.id - b.id);
      selectedId = saved.id;
      isNew = false;
      dirty = false;
      if (draftAsset?.url) URL.revokeObjectURL(draftAsset.url);
      draftAsset = null;
      state.project.card_revision = (state.project.card_revision || 0) + 1;
      state.project.card_count = cards.length;
      state.project.render_is_stale = state.project.rendered_revision !== null;
      fill(saved);
      renderList();
      renderProject();
      try {
        await fetchVisual(saved);
      } catch (error) {
        setStatus(`Saved; preview image unavailable: ${error.message}`, true);
      }
      if (state.exactPreview.open) scheduleExactPreview();
      showToast("Card saved");
      return true;
    } catch (error) {
      setStatus(error.message, true);
      return false;
    } finally {
      field("save").disabled = projectIsRenderLocked();
    }
  }

  async function flush() {
    return dirty ? save() : true;
  }

  async function deleteCard() {
    if (isNew) {
      dirty = false;
      isNew = false;
      field("editor").classList.add("hidden");
      if (draftAsset?.url) URL.revokeObjectURL(draftAsset.url);
      draftAsset = null;
      sync($("#video").currentTime * 1000);
      return;
    }
    if (!selectedId || !window.confirm("Delete this card?")) return;
    try {
      await api(`/api/projects/${projectId}/cards/${selectedId}`, { method: "DELETE" });
      const old = assetUrls.get(selectedId);
      if (old) URL.revokeObjectURL(old);
      assetUrls.delete(selectedId);
      savedAssets.delete(selectedId);
      cards = cards.filter((card) => card.id !== selectedId);
      selectedId = null;
      dirty = false;
      field("editor").classList.add("hidden");
      state.project.card_revision = (state.project.card_revision || 0) + 1;
      state.project.card_count = cards.length;
      state.project.render_is_stale = state.project.rendered_revision !== null;
      renderList();
      renderProject();
      sync($("#video").currentTime * 1000);
      if (state.exactPreview.open) scheduleExactPreview();
      showToast("Card deleted");
    } catch (error) {
      setStatus(error.message, true);
    }
  }

  async function previewDraft() {
    if (!dirty || !projectId) return;
    let payload;
    try {
      payload = cardPayload();
    } catch (error) {
      setStatus(error.message, true);
      if (draftAsset?.url) URL.revokeObjectURL(draftAsset.url);
      draftAsset = null;
      sync($("#video").currentTime * 1000);
      return;
    }
    const requestId = ++requestNumber;
    try {
      const response = await fetch(`/api/projects/${projectId}/cards/visual`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error(await readApiError(response));
      const url = URL.createObjectURL(await response.blob());
      if (requestId !== requestNumber || projectId !== state.project?.id) {
        URL.revokeObjectURL(url);
        return;
      }
      if (draftAsset?.url) URL.revokeObjectURL(draftAsset.url);
      draftAsset = {
        url, payload,
        x: Number(response.headers.get("X-Card-X")),
        y: Number(response.headers.get("X-Card-Y")),
        width: Number(response.headers.get("X-Card-Width")),
        height: Number(response.headers.get("X-Card-Height")),
      };
      setStatus("Unsaved changes · preview updated");
      sync($("#video").currentTime * 1000);
      if (state.exactPreview.open) scheduleExactPreview();
    } catch (error) {
      if (requestId === requestNumber) {
        setStatus(error.message, true);
        if (draftAsset?.url) URL.revokeObjectURL(draftAsset.url);
        draftAsset = null;
        sync($("#video").currentTime * 1000);
      }
    }
  }

  function fade(card, now) {
    if (now < card.start_ms || now >= card.end_ms) return 0;
    let opacity = 1;
    if (card.style.fade_in_ms) opacity = Math.min(opacity, (now - card.start_ms) / card.style.fade_in_ms);
    if (card.style.fade_out_ms) opacity = Math.min(opacity, (card.end_ms - now) / card.style.fade_out_ms);
    return Math.max(0, opacity);
  }

  function sync(now) {
    const overlay = field("overlay");
    if (!state.project || state.exactPreview.open) {
      overlay.classList.add("hidden");
      return;
    }
    let active = null;
    let asset = null;
    if (dirty && draftAsset && fade(draftAsset.payload, now) > 0) {
      active = draftAsset.payload;
      asset = draftAsset;
    } else {
      active = cards.find((card) => (!dirty || card.id !== selectedId) && fade(card, now) > 0);
      if (active) asset = savedAssets.get(active.id);
    }
    if (!active || !asset) {
      overlay.classList.add("hidden");
      return;
    }
    if (overlay.dataset.url !== asset.url) {
      overlay.src = asset.url;
      overlay.dataset.url = asset.url;
    }
    overlay.style.opacity = String(fade(active, now));
    overlay.dataset.x = asset.x;
    overlay.dataset.y = asset.y;
    overlay.dataset.width = asset.width;
    overlay.dataset.height = asset.height;
    overlay.classList.remove("hidden");
    layout();
  }

  function layout() {
    const overlay = field("overlay");
    const stage = document.querySelector(".video-stage");
    const video = $("#video");
    if (!stage || !video.videoWidth || !video.videoHeight || overlay.classList.contains("hidden")) return;
    const scale = Math.min(stage.clientWidth / video.videoWidth, stage.clientHeight / video.videoHeight);
    const offsetX = (stage.clientWidth - video.videoWidth * scale) / 2;
    const offsetY = (stage.clientHeight - video.videoHeight * scale) / 2;
    overlay.style.left = `${offsetX + Number(overlay.dataset.x) * scale}px`;
    overlay.style.top = `${offsetY + Number(overlay.dataset.y) * scale}px`;
    overlay.style.width = `${Number(overlay.dataset.width) * scale}px`;
    overlay.style.height = `${Number(overlay.dataset.height) * scale}px`;
  }

  function currentDraft() {
    if (!dirty || field("editor").classList.contains("hidden")) return null;
    return { id: selectedId, payload: cardPayload() };
  }

  document.getElementById("subtitle-tab").addEventListener("click", async () => {
    if (await flush()) setTab("subtitle");
  });
  document.getElementById("cards-tab").addEventListener("click", () => setTab("cards"));
  field("add").addEventListener("click", () => void addCard());
  field("save").addEventListener("click", () => void save());
  field("delete").addEventListener("click", () => void deleteCard());
  document.getElementById("cards-render").addEventListener("click", () => void approveAndRender());
  field("editor").addEventListener("input", (event) => {
    if (!event.target.matches("input, textarea, select")) return;
    dirty = true;
    requestNumber += 1;
    setStatus("Unsaved changes");
    clearTimeout(visualTimer);
    visualTimer = setTimeout(() => void previewDraft(), 350);
  });
  field("editor").addEventListener("change", (event) => {
    if (!event.target.matches("input, textarea, select")) return;
    dirty = true;
    clearTimeout(visualTimer);
    visualTimer = setTimeout(() => void previewDraft(), 100);
  });
  window.addEventListener("beforeunload", (event) => {
    if (!dirty) return;
    event.preventDefault();
    event.returnValue = "";
  });

  window.CardsUI = {
    loadProject, renderProject, sync, layout, flush, currentDraft,
    activeTab: () => tab,
  };
})();
