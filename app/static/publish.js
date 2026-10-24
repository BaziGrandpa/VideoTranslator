const $ = (selector) => document.querySelector(selector);
const projectId = location.pathname.match(/^\/publish\/([0-9a-f-]+)$/)?.[1];
const state = {
  data: null,
  ratio: "16x9",
  copyDirty: false,
  copyEditVersion: 0,
  designDirty: false,
  designEditVersion: 0,
  titleDirty: false,
  titleCommitPromise: null,
  copySaveTimer: null,
  copySavePromise: null,
  thumbnailDirty: false,
  thumbnailEditVersion: 0,
  thumbnailSaveTimer: null,
  thumbnailSavePromise: null,
  designSaveTimer: null,
  designSavePromise: null,
  previewTimer: null,
  previewRequest: 0,
  previewAbort: null,
  previewUrl: null,
  pollTimer: null,
  draggingText: false,
};

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      message = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

function endpoint(suffix = "") {
  return `/api/projects/${projectId}/publication${suffix}`;
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => box.classList.add("hidden"), 3500);
}

function showError(selector, message) {
  const element = $(selector);
  element.textContent = message;
  element.classList.toggle("hidden", !message);
}

function setData(data, { refreshCopy = true, refreshCover = false } = {}) {
  state.data = data;
  $("#project-name").textContent = data.project_title || "发布工作台";
  $("#back-link").href = `/?project=${projectId}`;
  if (refreshCopy) renderCopy();
  renderThumbnailTitles();
  if (refreshCover) renderRatio();
  renderBackgrounds();
  renderExportStatus();
}

function renderBackgrounds() {
  if (!state.data) return;
  const data = state.data;
  const active = ["queued", "running"].includes(data.background_status);
  const hasOldBackgrounds = Object.values(data.covers).some((cover) => cover.has_image);
  const labels = {
    idle: data.has_reference ?
      hasOldBackgrounds ? "参考图已更新；下方仍显示旧底图" : "参考图已保存，等待生成" :
      "等待参考图",
    queued: "等待 Codex CLI", running: "Codex 正在生成两张底图…",
    completed: "两张底图已就绪",
    failed: hasOldBackgrounds ? "生成失败；旧底图仍可用" : "底图生成失败",
  };
  $("#background-status").textContent = labels[data.background_status] || "等待参考图";
  showError("#background-error", data.background_error || "");
  const reference = $("#reference-preview");
  if (data.has_reference) {
    const url = `${data.reference_url}?v=${data.reference_revision}`;
    if (!reference.src.endsWith(url)) reference.src = url;
    reference.classList.remove("hidden");
    $("#reference-placeholder").classList.add("hidden");
  } else {
    reference.removeAttribute("src");
    reference.classList.add("hidden");
    $("#reference-placeholder").classList.remove("hidden");
  }
  $("#reference-status").textContent = active ? "生成中，暂不能更换参考图" :
    data.has_reference ? "参考图已保存" : "尚未上传";
  $("#select-reference").disabled = active;
  $("#paste-reference").disabled = active;
  $("#generate-backgrounds").disabled = active || !data.has_reference;
  $("#generate-backgrounds").textContent = active ? "正在生成…" :
    ["completed", "failed"].includes(data.background_status) ?
      "重新生成两张底图" : "生成两张底图";
  $("#select-image").disabled = active;
  $("#paste-image").disabled = active;
  $("#render-covers").disabled = active ||
    !Object.values(data.covers).every((cover) => cover.has_image);
  for (const ratio of ["16x9", "4x3"]) {
    const cover = data.covers[ratio];
    const image = $(`#background-${ratio}`);
    const placeholder = $(`#background-placeholder-${ratio}`);
    if (cover.has_image) {
      const url = `${cover.image_url}?v=${cover.revision}`;
      if (!image.src.endsWith(url)) image.src = url;
      image.classList.remove("hidden");
      placeholder.classList.add("hidden");
    } else {
      image.removeAttribute("src");
      image.classList.add("hidden");
      placeholder.classList.remove("hidden");
    }
  }
}

function renderCopy() {
  const data = state.data;
  if (!data) return;
  const status = data.copy_status;
  $("#copy-status").textContent = {
    queued: "排队等待 CLI", running: "正在生成候选", completed: "候选已保存", failed: "生成失败",
  }[status] || "未开始";
  showError("#copy-error", data.copy_error || "");
  $("#retry-copy").classList.toggle("hidden", status !== "failed");
  $("#copy-editor").classList.toggle("hidden", status !== "completed");
  if (status !== "completed" || state.copyDirty) return;
  $("#selected-title").value = data.selected_title || "";
  $("#selected-intro").value = data.selected_intro || "";
  updateFinalIntro();
  renderCandidates();
}

function renderThumbnailTitles() {
  if (!state.data) return;
  const status = state.data.thumbnail_status;
  $("#thumbnail-status").textContent = {
    queued: "排队等待 CLI", running: "正在生成封面短句", completed: "5 条候选已保存",
    failed: "生成失败",
  }[status] || "未开始";
  showError("#thumbnail-error", state.data.thumbnail_error || "");
  $("#retry-thumbnail-titles").classList.toggle("hidden", status !== "failed");
  $("#thumbnail-editor").classList.toggle("hidden", status !== "completed");
  if (status !== "completed" || state.thumbnailDirty) return;
  $("#selected-thumbnail-title").value = state.data.selected_thumbnail_title || "";
  renderThumbnailCandidates();
}

function renderThumbnailCandidates() {
  const list = $("#thumbnail-candidates");
  list.replaceChildren();
  (state.data.thumbnail_titles || []).forEach((title, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "candidate" +
      (title === $("#selected-thumbnail-title").value ? " selected" : "");
    const number = document.createElement("span");
    number.className = "candidate-index";
    number.textContent = String(index + 1).padStart(2, "0");
    const content = document.createElement("span");
    content.textContent = title;
    button.append(number, content);
    button.addEventListener("click", () => {
      $("#selected-thumbnail-title").value = title;
      markThumbnailDirty();
    });
    list.append(button);
  });
}

function markThumbnailDirty() {
  state.thumbnailDirty = true;
  state.thumbnailEditVersion += 1;
  $("#thumbnail-save-status").textContent = "未保存";
  renderThumbnailCandidates();
  clearTimeout(state.thumbnailSaveTimer);
  state.thumbnailSaveTimer = setTimeout(() => void saveThumbnailTitle(), 650);
}

async function saveThumbnailTitle() {
  clearTimeout(state.thumbnailSaveTimer);
  if (state.thumbnailSavePromise) {
    const previous = await state.thumbnailSavePromise;
    if (!previous || !state.thumbnailDirty) return previous;
  }
  if (!state.thumbnailDirty) return true;
  const version = state.thumbnailEditVersion;
  $("#thumbnail-save-status").textContent = "保存中…";
  const request = (async () => { try {
    const data = await api(endpoint("/thumbnail-title"), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("#selected-thumbnail-title").value }),
    });
    state.data = data;
    if (version === state.thumbnailEditVersion) {
      state.thumbnailDirty = false;
      $("#selected-thumbnail-title").value = data.selected_thumbnail_title;
      $("#thumbnail-save-status").textContent = "已保存";
      renderThumbnailCandidates();
    }
    return true;
  } catch (error) {
    $("#thumbnail-save-status").textContent = `保存失败：${error.message}`;
    return false;
  } })();
  state.thumbnailSavePromise = request;
  const result = await request;
  if (state.thumbnailSavePromise === request) state.thumbnailSavePromise = null;
  if (result && state.thumbnailDirty && version !== state.thumbnailEditVersion) {
    return saveThumbnailTitle();
  }
  return result;
}

function renderCandidates() {
  const titleList = $("#title-candidates");
  const introList = $("#intro-candidates");
  titleList.replaceChildren();
  introList.replaceChildren();
  (state.data.titles || []).forEach((title, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "candidate" + (title === $("#selected-title").value ? " selected" : "");
    const number = document.createElement("span");
    number.className = "candidate-index";
    number.textContent = String(index + 1).padStart(2, "0");
    const content = document.createElement("span");
    content.textContent = title;
    button.append(number, content);
    button.addEventListener("click", () => {
      $("#selected-title").value = title;
      markCopyDirty();
    });
    titleList.append(button);
  });
  (state.data.intros || []).forEach((intro, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "candidate" + (intro === $("#selected-intro").value ? " selected" : "");
    const number = document.createElement("span");
    number.className = "candidate-index";
    number.textContent = String.fromCharCode(65 + index);
    const content = document.createElement("span");
    content.textContent = intro;
    button.append(number, content);
    button.addEventListener("click", () => {
      $("#selected-intro").value = intro;
      markCopyDirty();
    });
    introList.append(button);
  });
}

function updateFinalIntro() {
  $("#final-intro").value = $("#selected-intro").value.trim() + (state.data?.intro_footer || "");
}

function markCopyDirty() {
  state.copyDirty = true;
  state.copyEditVersion += 1;
  $("#copy-save-status").textContent = "未保存";
  updateFinalIntro();
  renderCandidates();
  clearTimeout(state.copySaveTimer);
  state.copySaveTimer = setTimeout(() => void saveCopy(), 650);
}

async function saveCopy() {
  clearTimeout(state.copySaveTimer);
  if (state.copySavePromise) {
    const previous = await state.copySavePromise;
    if (!previous || !state.copyDirty) return previous;
  }
  if (!state.copyDirty) return true;
  const version = state.copyEditVersion;
  $("#copy-save-status").textContent = "保存中…";
  const request = (async () => { try {
    const data = await api(endpoint("/copy"), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: $("#selected-title").value, intro: $("#selected-intro").value }),
    });
    state.data = data;
    if (version === state.copyEditVersion) {
      state.copyDirty = false;
      $("#copy-save-status").textContent = "已保存";
      $("#selected-title").value = data.selected_title;
      $("#selected-intro").value = data.selected_intro;
      updateFinalIntro();
      renderCandidates();
    }
    return true;
  } catch (error) {
    $("#copy-save-status").textContent = `保存失败：${error.message}`;
    return false;
  } })();
  state.copySavePromise = request;
  const result = await request;
  if (state.copySavePromise === request) state.copySavePromise = null;
  if (result && state.copyDirty && version !== state.copyEditVersion) return saveCopy();
  return result;
}

async function copyText(value) {
  try {
    await navigator.clipboard.writeText(value);
    toast("已复制到剪贴板");
  } catch (error) {
    toast(`复制失败：${error.message}`);
  }
}

const fields = {
  highlight_terms: "#design-highlight", font_family: "#design-font",
  font_weight: "#design-weight", font_size: "#design-size", line_spacing: "#design-line-spacing",
  x_pct: "#design-x", y_pct: "#design-y", text_color: "#design-color",
  highlight_color: "#design-highlight-color", mask_solid_pct: "#mask-solid",
  mask_width_pct: "#mask-width", mask_opacity: "#mask-opacity",
  mask_gamma: "#mask-gamma", shadow_opacity: "#shadow-opacity",
  shadow_blur: "#shadow-blur", shadow_dx: "#shadow-dx", shadow_dy: "#shadow-dy",
};
const numericFields = new Set([
  "font_size", "line_spacing", "x_pct", "y_pct", "mask_solid_pct", "mask_width_pct",
  "mask_opacity", "mask_gamma", "shadow_opacity", "shadow_blur", "shadow_dx", "shadow_dy",
]);

function fillDesign(design) {
  $("#design-title").value = design.title;
  Object.entries(fields).forEach(([key, selector]) => {
    let value = design[key];
    if (key === "highlight_terms") value = value.join(", ");
    if (key === "mask_opacity" || key === "shadow_opacity") {
      value = Math.round(value * 100);
    }
    $(selector).value = value;
  });
}

function collectDesign() {
  const design = { ...state.data.covers[state.ratio].design };
  Object.entries(fields).forEach(([key, selector]) => {
    let value = $(selector).value;
    if (key === "highlight_terms") {
      value = value.split(",").map((item) => item.trim()).filter(Boolean);
    } else if (numericFields.has(key)) {
      if (value === "") {
        const label = $(selector).closest("label")?.firstChild?.textContent?.trim() || key;
        throw new Error(`请填写${label}`);
      }
      value = Number(value);
      if (!Number.isFinite(value)) throw new Error("设计参数必须是数字");
      if (key === "mask_opacity" || key === "shadow_opacity") {
        value /= 100;
      }
    }
    design[key] = value;
  });
  return design;
}

function updateTitleStatus() {
  if (!state.data) return;
  const lines = $("#design-title").value.replace(/\r\n?/g, "\n").split("\n").length;
  const slot = Math.min(lines, 4);
  const cover = state.data.covers[state.ratio];
  $("#title-confirm-status").textContent = state.titleDirty ?
    `${lines} 行待确认，按 Enter 加载 ${slot} 行样式` :
    `当前：${state.ratio.replace("x", ":")} · ${cover.line_count} 行样式` +
      (cover.title_line_count > 4 ? `（标题实际 ${cover.title_line_count} 行）` : "");
  Object.values(fields).forEach((selector) => { $(selector).disabled = state.titleDirty; });
}

function markTitleDirty() {
  state.titleDirty = $("#design-title").value !== state.data.covers[state.ratio].design.title;
  updateTitleStatus();
  updateDragRegion();
  renderExportStatus();
}

async function confirmTitle() {
  if (state.titleCommitPromise) return state.titleCommitPromise;
  if (!state.titleDirty) return true;
  const field = $("#design-title");
  field.disabled = true;
  const request = (async () => {
    if (!(await saveDesign())) return false;
    try {
      const data = await api(endpoint(`/covers/${state.ratio}/title`), {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: field.value }),
      });
      state.data = data;
      state.titleDirty = false;
      renderRatio();
      renderExportStatus();
      return true;
    } catch (error) {
      $("#title-confirm-status").textContent = `确认失败：${error.message}`;
      return false;
    }
  })();
  state.titleCommitPromise = request;
  try { return await request; }
  finally {
    state.titleCommitPromise = null;
    field.disabled = false;
  }
}

function updateDragRegion() {
  const region = $("#text-drag-region");
  const canvas = $("#cover-canvas");
  if (state.titleDirty || !state.data?.covers[state.ratio].has_image ||
      !state.data.covers[state.ratio].design.title.trim()) {
    region.classList.add("hidden");
    return;
  }
  region.classList.remove("hidden");
  const scale = canvas.clientWidth / 1536;
  const size = Number($("#design-size").value) || 104;
  region.style.left = `${Number($("#design-x").value) || 0}%`;
  region.style.top = `${Number($("#design-y").value) || 0}%`;
  region.style.fontSize = `${Math.max(10, size * scale)}px`;
  region.style.fontFamily = $("#design-font").value;
  region.style.fontWeight = $("#design-weight").value === "bold" ? "700" : "400";
  region.style.lineHeight = String(Number($("#design-line-spacing").value) || 1.04);
  $("#text-drag-measure").textContent = state.data.covers[state.ratio].design.title;
}

function cancelPreview() {
  clearTimeout(state.previewTimer);
  state.previewTimer = null;
  state.previewRequest += 1;
  state.previewAbort?.abort();
  state.previewAbort = null;
}

function resetPreviewToBase() {
  const cover = state.data?.covers[state.ratio];
  if (!cover?.has_image) return;
  $("#cover-preview").src = `${cover.image_url}?v=${cover.revision}`;
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
}

function renderRatio() {
  if (!state.data) return;
  cancelPreview();
  const cover = state.data.covers[state.ratio];
  document.querySelectorAll(".ratio-tabs button").forEach((button) => {
    const active = button.dataset.ratio === state.ratio;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  fillDesign(cover.design);
  updateTitleStatus();
  const image = $("#cover-preview");
  if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
  state.previewUrl = null;
  if (cover.has_image) {
    image.src = `${cover.image_url}?v=${cover.revision}`;
    $("#cover-canvas").classList.remove("hidden");
    $("#preview-placeholder").classList.add("hidden");
    $("#image-status").textContent = "底图已保存";
    schedulePreview();
  } else {
    image.removeAttribute("src");
    $("#cover-canvas").classList.add("hidden");
    $("#preview-placeholder").classList.remove("hidden");
    $("#image-status").textContent = "尚未上传";
    $("#preview-status").textContent = "";
  }
  updateDragRegion();
  $("#cover-save-status").textContent = state.designDirty ? "设计未保存" : "设计已保存";
}

function schedulePreview() {
  clearTimeout(state.previewTimer);
  if (state.draggingText) return;
  if (!state.data.covers[state.ratio].has_image) return;
  state.previewTimer = setTimeout(() => void previewDesign(), 350);
}

async function previewDesign() {
  if (state.draggingText) return;
  const ratio = state.ratio;
  const requestId = ++state.previewRequest;
  let design;
  try { design = collectDesign(); } catch (error) {
    $("#preview-status").textContent = error.message;
    return;
  }
  $("#preview-status").textContent = "正在更新预览…";
  const controller = new AbortController();
  state.previewAbort = controller;
  try {
    const response = await fetch(endpoint(`/covers/${ratio}/preview`), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(design),
      signal: controller.signal,
    });
    if (!response.ok) {
      const body = await response.json();
      throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail));
    }
    const blob = await response.blob();
    if (requestId !== state.previewRequest || ratio !== state.ratio || state.draggingText) return;
    const url = URL.createObjectURL(blob);
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = url;
    $("#cover-preview").src = url;
    $("#preview-status").textContent = "预览已更新";
  } catch (error) {
    if (error.name !== "AbortError" && requestId === state.previewRequest) {
      $("#preview-status").textContent = `预览失败：${error.message}`;
    }
  } finally {
    if (state.previewAbort === controller) state.previewAbort = null;
  }
}

function markDesignDirty() {
  cancelPreview();
  state.designDirty = true;
  state.designEditVersion += 1;
  $("#cover-save-status").textContent = "设计未保存";
  renderExportStatus();
  updateDragRegion();
  schedulePreview();
  clearTimeout(state.designSaveTimer);
  state.designSaveTimer = setTimeout(() => void saveDesign(), 700);
}

async function saveDesign() {
  clearTimeout(state.designSaveTimer);
  if (state.designSavePromise) {
    const previous = await state.designSavePromise;
    if (!previous || !state.designDirty) return previous;
  }
  if (!state.designDirty) return true;
  const ratio = state.ratio;
  const version = state.designEditVersion;
  let design;
  try { design = collectDesign(); } catch (error) {
    $("#cover-save-status").textContent = error.message;
    return false;
  }
  $("#cover-save-status").textContent = "保存设计中…";
  const request = (async () => { try {
    const data = await api(endpoint(`/covers/${ratio}/design`), {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(design),
    });
    state.data = data;
    if (version === state.designEditVersion && ratio === state.ratio) {
      state.designDirty = false;
      $("#cover-save-status").textContent = "设计已保存";
    }
    renderExportStatus();
    return true;
  } catch (error) {
    $("#cover-save-status").textContent = `保存失败：${error.message}`;
    return false;
  } })();
  state.designSavePromise = request;
  const result = await request;
  if (state.designSavePromise === request) state.designSavePromise = null;
  if (result && state.designDirty && version !== state.designEditVersion) return saveDesign();
  return result;
}

function renderExportStatus() {
  if (!state.data) return;
  const covers = Object.values(state.data.covers);
  $("#export-status").textContent = state.titleDirty || state.designDirty ||
    covers.some((cover) => cover.export_stale) ?
    "设计已改动，需重新渲染" :
    covers.every((cover) => cover.export_in_output) ? "两张成品已保存在输出文件夹" :
      "点击渲染后，成品会保存到输出文件夹";
}

async function uploadImage(file) {
  if (!file || !file.type.startsWith("image/")) {
    toast("剪贴板中没有可上传的图片");
    return;
  }
  const ratio = state.ratio;
  if (!(await confirmTitle()) || !(await saveDesign())) return;
  $("#image-status").textContent = "正在保存底图…";
  try {
    const data = await api(endpoint(`/covers/${ratio}/image`), {
      method: "PUT", headers: { "Content-Type": file.type }, body: file,
    });
    setData(data, { refreshCopy: false, refreshCover: true });
    toast(`${ratio.replace("x", ":")} 底图已保存`);
  } catch (error) {
    $("#image-status").textContent = `上传失败：${error.message}`;
  }
}

async function uploadReference(file) {
  if (!file || !file.type.startsWith("image/")) {
    toast("剪贴板中没有可上传的图片");
    return;
  }
  $("#reference-status").textContent = "正在保存参考图…";
  try {
    setData(await api(endpoint("/reference"), {
      method: "PUT", headers: { "Content-Type": file.type }, body: file,
    }), { refreshCopy: false });
    toast("参考图已保存，可生成两张底图");
  } catch (error) {
    $("#reference-status").textContent = `上传失败：${error.message}`;
  }
}

async function pasteImage(target) {
  try {
    const items = await navigator.clipboard.read();
    for (const item of items) {
      const type = item.types.find((entry) => entry.startsWith("image/"));
      if (type) {
        const file = await item.getType(type);
        return target === "cover" ? uploadImage(file) : uploadReference(file);
      }
    }
    toast("剪贴板中没有图片，可使用上传底图按钮");
  } catch (error) {
    toast(`无法读取图片：${error.message}。可在上传区按 Ctrl+V`);
  }
}

function syncPolling() {
  const active = [state.data?.copy_status, state.data?.thumbnail_status,
    state.data?.background_status]
    .some((status) => ["queued", "running"].includes(status));
  if (!active && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  } else if (active && !state.pollTimer) {
    state.pollTimer = setInterval(async () => {
      try {
        const previousCopy = state.data.copy_status;
        const previousThumbnail = state.data.thumbnail_status;
        const previousBackground = state.data.background_status;
        const previousCovers = Object.fromEntries(
          Object.entries(state.data.covers).map(([ratio, cover]) => [ratio, cover.revision])
        );
        const data = await api(endpoint());
        state.data = data;
        renderCopy();
        renderThumbnailTitles();
        renderBackgrounds();
        renderExportStatus();
        const coverChanged = Object.entries(data.covers)
          .some(([ratio, cover]) => cover.revision !== previousCovers[ratio]);
        if (coverChanged || previousBackground !== data.background_status) {
          if (!state.designDirty && !state.titleDirty) renderRatio();
          else schedulePreview();
        }
        if (previousCopy !== data.copy_status && data.copy_status === "completed") toast("文案候选已生成");
        if (previousThumbnail !== data.thumbnail_status && data.thumbnail_status === "completed") {
          toast("5 条封面短句已生成");
        }
        if (previousBackground !== data.background_status && data.background_status === "completed") {
          toast("两张无字底图已生成，请审核后编辑封面");
        }
        syncPolling();
      } catch (error) {
        $("#copy-status").textContent = `状态刷新失败：${error.message}`;
      }
    }, 2000);
  }
}

document.querySelectorAll(".ratio-tabs button").forEach((button) => {
  button.addEventListener("click", async () => {
    if (button.dataset.ratio === state.ratio) return;
    if (!(await confirmTitle()) || !(await saveDesign())) return;
    state.previewRequest += 1;
    state.ratio = button.dataset.ratio;
    renderRatio();
  });
});
Object.values(fields).forEach((selector) => {
  $(selector).addEventListener("input", markDesignDirty);
  $(selector).addEventListener("change", markDesignDirty);
});
$("#design-title").addEventListener("input", markTitleDirty);
$("#design-title").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  void confirmTitle();
});
const dragRegion = $("#text-drag-region");
let dragStart = null;
dragRegion.addEventListener("pointerdown", (event) => {
  if (event.button !== 0 || !state.data?.covers[state.ratio].has_image) return;
  event.preventDefault();
  event.stopPropagation();
  cancelPreview();
  resetPreviewToBase();
  $("#preview-status").textContent = "拖动中，松开后更新预览";
  state.draggingText = true;
  dragStart = {
    pointerId: event.pointerId, clientX: event.clientX, clientY: event.clientY,
    x: Number($("#design-x").value), y: Number($("#design-y").value),
  };
  dragRegion.setPointerCapture(event.pointerId);
  dragRegion.classList.add("dragging");
});
dragRegion.addEventListener("pointermove", (event) => {
  if (!dragStart || event.pointerId !== dragStart.pointerId) return;
  event.preventDefault();
  const canvas = $("#cover-canvas");
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (!width || !height) return;
  const maxX = Math.min(90, Math.max(0, (width - dragRegion.offsetWidth - 8) / width * 100));
  const maxY = Math.min(90, Math.max(0, (height - dragRegion.offsetHeight - 8) / height * 100));
  const x = Math.max(0, Math.min(maxX, dragStart.x + (event.clientX - dragStart.clientX) / width * 100));
  const y = Math.max(0, Math.min(maxY, dragStart.y + (event.clientY - dragStart.clientY) / height * 100));
  $("#design-x").value = x.toFixed(1);
  $("#design-y").value = y.toFixed(1);
  markDesignDirty();
});
function finishTextDrag(event) {
  if (!dragStart || event.pointerId !== dragStart.pointerId) return;
  dragStart = null;
  state.draggingText = false;
  dragRegion.classList.remove("dragging");
  if (dragRegion.hasPointerCapture(event.pointerId)) dragRegion.releasePointerCapture(event.pointerId);
  schedulePreview();
  if (state.designDirty) void saveDesign();
}
dragRegion.addEventListener("pointerup", finishTextDrag);
dragRegion.addEventListener("pointercancel", finishTextDrag);
$("#cover-preview").addEventListener("load", updateDragRegion);
window.addEventListener("resize", updateDragRegion);
$("#selected-title").addEventListener("input", markCopyDirty);
$("#selected-intro").addEventListener("input", markCopyDirty);
$("#selected-thumbnail-title").addEventListener("input", markThumbnailDirty);
$("#copy-title").addEventListener("click", async () => {
  if (await saveCopy()) await copyText($("#selected-title").value);
});
$("#copy-intro").addEventListener("click", async () => {
  if (await saveCopy()) await copyText($("#final-intro").value);
});
$("#retry-copy").addEventListener("click", async () => {
  try {
    setData(await api(endpoint("/retry-copy"), { method: "POST" }));
    syncPolling();
  } catch (error) { showError("#copy-error", error.message); }
});
$("#retry-thumbnail-titles").addEventListener("click", async () => {
  try {
    setData(await api(endpoint("/retry-thumbnail-titles"), { method: "POST" }), {
      refreshCopy: false,
    });
    syncPolling();
  } catch (error) { showError("#thumbnail-error", error.message); }
});
$("#copy-thumbnail-title").addEventListener("click", async () => {
  if (await saveThumbnailTitle()) await copyText($("#selected-thumbnail-title").value);
});
$("#apply-thumbnail-title").addEventListener("click", async () => {
  if (!(await saveThumbnailTitle())) return;
  $("#design-title").value = $("#selected-thumbnail-title").value;
  markTitleDirty();
  if (await confirmTitle()) toast("已填入当前画幅，可用 Shift+Enter 换行");
});
$("#use-video-title").addEventListener("click", async () => {
  const title = $("#selected-title").value.replace(/【中英字幕】$/, "").trim();
  if (!title) return toast("先选择或填写视频标题");
  $("#design-title").value = title;
  markTitleDirty();
  await confirmTitle();
});
$("#select-image").addEventListener("click", () => $("#image-file").click());
$("#image-file").addEventListener("change", (event) => {
  void uploadImage(event.target.files[0]);
  event.target.value = "";
});
$("#paste-image").addEventListener("click", () => void pasteImage("cover"));
$("#select-reference").addEventListener("click", () => $("#reference-file").click());
$("#reference-file").addEventListener("change", (event) => {
  void uploadReference(event.target.files[0]);
  event.target.value = "";
});
$("#paste-reference").addEventListener("click", () => void pasteImage("reference"));
$("#reference-zone").addEventListener("click", () => $("#reference-zone").focus());
$("#reference-zone").addEventListener("dragover", (event) => {
  event.preventDefault();
  event.currentTarget.classList.add("dragging");
});
$("#reference-zone").addEventListener("dragleave", (event) => event.currentTarget.classList.remove("dragging"));
$("#reference-zone").addEventListener("drop", (event) => {
  event.preventDefault();
  event.currentTarget.classList.remove("dragging");
  void uploadReference(event.dataTransfer.files[0]);
});
$("#generate-backgrounds").addEventListener("click", async () => {
  if (!(await confirmTitle()) || !(await saveDesign())) return;
  try {
    setData(await api(endpoint("/generate-backgrounds"), { method: "POST" }), {
      refreshCopy: false,
    });
    syncPolling();
    toast("已提交两张底图的 Codex 生成任务");
  } catch (error) { showError("#background-error", error.message); }
});
$("#paste-zone").addEventListener("click", () => $("#paste-zone").focus());
$("#paste-zone").addEventListener("dragover", (event) => {
  event.preventDefault();
  event.currentTarget.classList.add("dragging");
});
$("#paste-zone").addEventListener("dragleave", (event) => event.currentTarget.classList.remove("dragging"));
$("#paste-zone").addEventListener("drop", (event) => {
  event.preventDefault();
  event.currentTarget.classList.remove("dragging");
  void uploadImage(event.dataTransfer.files[0]);
});
document.addEventListener("paste", (event) => {
  if (event.target.closest("input, textarea")) return;
  const item = Array.from(event.clipboardData?.items || []).find((entry) => entry.type.startsWith("image/"));
  if (item) {
    event.preventDefault();
    void (document.activeElement?.closest("#paste-zone") ?
      uploadImage(item.getAsFile()) : uploadReference(item.getAsFile()));
  }
});
$("#render-covers").addEventListener("click", async () => {
  if (!(await confirmTitle()) || !(await saveDesign())) return;
  const button = $("#render-covers");
  button.disabled = true;
  button.textContent = "正在渲染两张封面…";
  showError("#export-error", "");
  try {
    setData(await api(endpoint("/render"), { method: "POST" }), { refreshCopy: false });
    toast("两张封面已保存到视频输出文件夹");
  } catch (error) { showError("#export-error", error.message); }
  finally {
    button.textContent = "渲染最终封面";
    renderBackgrounds();
  }
});
$("#open-output-folder").addEventListener("click", async () => {
  try {
    await api(`/api/projects/${projectId}/open-output-folder`, { method: "POST" });
  } catch (error) { showError("#export-error", `打开文件夹失败：${error.message}`); }
});
window.addEventListener("beforeunload", (event) => {
  if (!state.copyDirty && !state.thumbnailDirty && !state.designDirty && !state.titleDirty) return;
  event.preventDefault();
  event.returnValue = "";
});

(async () => {
  if (!projectId) return showError("#copy-error", "无效的项目地址");
  try {
    setData(await api(endpoint("/start"), { method: "POST" }), { refreshCover: true });
    syncPolling();
  } catch (error) {
    showError("#copy-error", error.message);
    $("#copy-status").textContent = "无法打开工作台";
  }
})();
