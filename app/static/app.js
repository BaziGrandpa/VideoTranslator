const state = {
  projects: [],
  project: null,
  segments: [],
  segmentDataProjectId: null,
  segmentLoadingProjectId: null,
  segmentLoadToken: 0,
  projectLoadToken: 0,
  reviewIssues: [],
  reviewMode: false,
  selectedId: null,
  activeId: null,
  pollTimer: null,
  previewToken: 0,
  subtitleStyle: null,
  previewResizeObserver: null,
  providerInfo: null,
  codexModels: [],
  styleDirty: false,
  styleSavePromise: null,
  contextDirty: false,
  contextSavePromise: null,
  rebuildParts: [],
  localRenderLockProjectId: null,
  pendingSaves: new Set(),
  unsavedSegmentFields: new Set(),
  segmentTextSaves: new Map(),
  independentTimingBoundaries: new Set(),
  segmentSaveFailed: false,
  exactPreview: {
    open: false,
    timestampMs: 0,
    segmentId: null,
    requestId: 0,
    controller: null,
    debounceTimer: null,
    objectUrl: null,
    cache: new Map(),
  },
  renamingProjectId: null,
  deletingProjectId: null,
};

const $ = (selector) => document.querySelector(selector);
const subtitleStyleClipboardKey = "videoTranslatorSubtitleStyleClipboardV1";

function ensureExactPreviewButton() {
  return $("#exact-preview-button");
}

ensureExactPreviewButton();
const stageNames = {
  queued: "Waiting for the pipeline worker",
  downloading: "Downloading video and subtitles",
  preparing_subtitles: "Preparing English subtitles",
  reconstructing_timeline: "Reconstructing the word-level timeline",
  restoring_punctuation: "Restoring English punctuation",
  transcribing: "No subtitles found; transcribing audio",
  building_translation_context: "Building global names and terminology",
  waiting_context_review: "Waiting for Pass 0 translation guidance review",
  translating: "Generating translations",
  translating_part: "Translating the complete Part as coherent text",
  aligning_cues: "Placing the Part translation onto cues",
  splitting_cues: "Splitting overlong Chinese cues",
  waiting_for_quota: "Paused at a saved checkpoint; waiting for CLI quota",
  validating_subtitles: "Validating timing and readability",
  rebuild_queued: "Queued for subtitle rebuilding",
  part_rebuild_queued: "Queued for selected-Part rebuilding",
  waiting_review: "Waiting for human review",
  render_queued: "Approved and queued for rendering",
  rendering: "Burning subtitles and rendering video",
  completed: "Video generated",
  failed: "Processing failed",
};

function formatPipelineDetail(detail) {
  if (!detail || typeof detail !== "object") return "";
  const items = [];
  if (Number.isInteger(detail.pass)) items.push(`Pass ${detail.pass}`);
  if (Number.isInteger(detail.part)) {
    items.push(`Part ${detail.part}${Number.isInteger(detail.part_count) ? `/${detail.part_count}` : ""}`);
  }
  if (Number.isInteger(detail.cue_count)) {
    items.push(`${detail.cue_count} cues`);
  }
  if (Number.isInteger(detail.start_token_id) && Number.isInteger(detail.end_token_id)) {
    items.push(`Tokens ${detail.start_token_id}–${detail.end_token_id}`);
  }
  if (detail.label) items.push(String(detail.label));
  if (detail.waiting_review) items.push("Waiting for approval");
  return items.join(" · ");
}

async function readApiError(response) {
  let detail = `${response.status} ${response.statusText}`;
  try {
    const body = await response.json();
    detail = Array.isArray(body.detail)
      ? body.detail.map((item) => {
          if (typeof item === "string") return item;
          const field = Array.isArray(item.loc) ? item.loc.at(-1) : "value";
          return `${field}: ${item.msg || "Invalid value"}`;
        }).join("; ")
      : body.detail || detail;
  } catch (_) {}
  return detail;
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    const error = new Error(await readApiError(response));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function formatTime(ms) {
  ms = Math.max(0, Number(ms) || 0);
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  const millis = Math.floor(ms % 1000);
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
}

function parseTime(value) {
  const match = String(value).trim().match(/^(?:(\d+):)?(\d{1,2})[.,](\d{1,3})$/);
  if (!match) throw new Error("Use the time format mm:ss.mmm");
  const minutes = Number(match[1] || 0);
  const seconds = Number(match[2]);
  const millis = Number(match[3].padEnd(3, "0"));
  if (seconds >= 60) throw new Error("Seconds cannot exceed 59");
  return minutes * 60000 + seconds * 1000 + millis;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.add("hidden"), 3500);
}

async function loadHealth() {
  try {
    const result = await api("/api/health");
    state.providerInfo = result;
    applySubtitleStyle(result.subtitle_style);
    renderProviderControl();
    const required = ["ffmpeg", "ffprobe", "yt_dlp", "node", "translation_cli"];
    const missing = required.filter((name) => !result.dependencies[name]);
    const messages = [];
    if (missing.length) messages.push(`Missing: ${missing.join(", ")}`);
    if (result.dependencies.translation_cli) {
      const active = result.translation_providers
        .filter((name) => name === "codex" ? result.dependencies.codex_cli :
          (["grok", "grokbuild"].includes(name) ? result.dependencies.grok_cli : true));
      messages.push(`Translation CLI: ${active.join(" → ")}`);
      if (active.includes("codex")) {
        messages.push(`Codex: ${result.codex_model} (${result.codex_reasoning_effort})`);
      }
    }
    const health = $("#health");
    health.textContent = messages.length ? messages.join(" · ") : "Environment check passed";
    health.classList.toggle("warning", missing.length > 0);
  } catch (error) {
    $("#health").textContent = `Environment check failed: ${error.message}`;
  }
}

async function loadCodexModels() {
  try {
    const result = await api("/api/codex-models");
    state.codexModels = result.models || [];
  } catch (_) {
    state.codexModels = [];
  }
  renderProviderControl();
}

function applySubtitleStyle(style) {
  if (!style?.chinese || !style?.english || !style?.layout) return;
  state.subtitleStyle = style;
  const root = document.documentElement;
  root.style.setProperty("--preview-chinese-font", `"${style.chinese.font}"`);
  root.style.setProperty("--preview-chinese-color", style.chinese.color);
  root.style.setProperty("--preview-english-font", `"${style.english.font}"`);
  root.style.setProperty("--preview-english-color", style.english.color);
  if (style.bottom_mask) {
    const maskOpacity = Math.min(1, Math.max(0, style.bottom_mask.opacity));
    root.style.setProperty("--bottom-mask-opacity", maskOpacity);
  }
  updateSubtitlePreviewGeometry();
}

function updateSubtitlePreviewGeometry() {
  const style = state.subtitleStyle;
  const stage = document.querySelector(".video-stage");
  const video = $("#video");
  if (!style?.layout || !stage || !video || !stage.clientWidth || !stage.clientHeight) return;

  const playResX = style.layout.play_res_x || 1920;
  const playResY = style.layout.play_res_y || 1080;
  const videoWidth = video.videoWidth || playResX;
  const videoHeight = video.videoHeight || playResY;
  const containScale = Math.min(stage.clientWidth / videoWidth, stage.clientHeight / videoHeight);
  const renderedWidth = videoWidth * containScale;
  const renderedHeight = videoHeight * containScale;
  const offsetX = (stage.clientWidth - renderedWidth) / 2;
  const offsetY = (stage.clientHeight - renderedHeight) / 2;
  const scaleX = renderedWidth / playResX;
  const scaleY = renderedHeight / playResY;
  const bottomMargin = Math.max(0, style.layout.bottom_margin);
  const activeSegment = state.segments.find((segment) => segment.id === state.activeId);
  const chineseMargin = state.project?.bilingual === false
    ? bottomMargin
    : bottomMargin + Math.max(0, style.layout.line_gap) + (activeSegment?.extra_line_gap ? 30 : 0);
  const horizontalMargin = Math.max(0, style.layout.horizontal_margin);
  const maskHeightRatio = Math.min(0.8, Math.max(0.05, style.bottom_mask?.height ?? 0.34));
  const root = document.documentElement;

  root.style.setProperty("--preview-left", `${offsetX + horizontalMargin * scaleX}px`);
  root.style.setProperty("--preview-right", `${offsetX + horizontalMargin * scaleX}px`);
  root.style.setProperty("--preview-chinese-bottom", `${offsetY + chineseMargin * scaleY}px`);
  root.style.setProperty("--preview-english-bottom", `${offsetY + bottomMargin * scaleY}px`);
  root.style.setProperty("--preview-chinese-size", `${style.chinese.size * scaleY}px`);
  root.style.setProperty("--preview-english-size", `${style.english.size * scaleY}px`);
  root.style.setProperty(
    "--preview-chinese-outline",
    `${(style.chinese.outline ?? 3) * scaleY}px`,
  );
  root.style.setProperty(
    "--preview-english-outline",
    `${(style.english.outline ?? 2) * scaleY}px`,
  );
  root.style.setProperty("--preview-chinese-spacing", `${scaleX}px`);
  root.style.setProperty("--mask-left", `${offsetX}px`);
  root.style.setProperty("--mask-bottom", `${offsetY}px`);
  root.style.setProperty("--mask-width", `${renderedWidth}px`);
  root.style.setProperty("--mask-height", `${renderedHeight * maskHeightRatio}px`);
  window.CardsUI?.layout();
}

function observeSubtitlePreviewGeometry() {
  if (state.previewResizeObserver) return;
  const stage = document.querySelector(".video-stage");
  const video = $("#video");
  if (!stage || !video) return;
  state.previewResizeObserver = new ResizeObserver(updateSubtitlePreviewGeometry);
  state.previewResizeObserver.observe(stage);
  video.addEventListener("loadedmetadata", updateSubtitlePreviewGeometry);
}

async function loadProjects() {
  state.projects = await api("/api/projects");
  renderProjectList();
}

function renderProjectList() {
  const list = $("#project-list");
  if (!state.projects.length) {
    list.innerHTML = '<div class="muted" style="font-size:12px;padding:8px 4px">No projects yet</div>';
    return;
  }
  list.innerHTML = state.projects.map((project) => `
    <div class="project-item ${state.project?.id === project.id ? "selected" : ""}" data-id="${project.id}">
      <div class="project-item-head">
        <div class="project-item-title">${escapeHtml(projectDisplayName(project))}</div>
        <div class="project-item-actions">
          <button class="rename-project" type="button" aria-label="Rename project">Rename</button>
          <button class="delete-project" type="button" aria-label="Delete project" ${["queued", "running"].includes(project.status) ? 'disabled title="Wait for processing to finish"' : ""}>Delete</button>
        </div>
      </div>
      <div class="project-item-meta">
        <span><i class="status-dot ${project.status}"></i>${escapeHtml(stageNames[project.stage] || project.stage)}</span>
        <span>${project.progress}%</span>
      </div>
    </div>`).join("");
  list.querySelectorAll(".project-item").forEach((item) => {
    item.addEventListener("click", () => openProject(item.dataset.id));
    item.querySelector(".rename-project").addEventListener("click", (event) => {
      event.stopPropagation();
      openRenameProjectDialog(item.dataset.id);
    });
    item.querySelector(".delete-project").addEventListener("click", (event) => {
      event.stopPropagation();
      openDeleteProjectDialog(item.dataset.id);
    });
  });
}

function projectDisplayName(project) {
  return project?.display_name || project?.title || "Waiting for video metadata";
}

function reviewWorkspaceReady(project) {
  if (!project?.video_path || !project.segment_count) return false;
  if (["waiting_review", "completed"].includes(project.status)) return true;
  if (["render_queued", "rendering"].includes(project.stage)) return true;
  return project.status === "failed"
    && project.subtitle_revision > 0
    && !project.pending_rebuild_scope
    && Array.isArray(project.parts)
    && project.parts.length > 0
    && project.parts.every((part) => part.status === "completed");
}

function clearReviewData() {
  state.segmentLoadToken += 1;
  state.segmentDataProjectId = null;
  state.segmentLoadingProjectId = null;
  state.segments = [];
  state.reviewIssues = [];
  state.reviewMode = false;
  state.selectedId = null;
  state.activeId = null;
  state.previewToken += 1;
  const video = $("#video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  renderSegments();
  updatePreview();
}

async function openProject(projectId) {
  if (window.CardsUI && !(await window.CardsUI.flush())) return;
  if (
    state.contextDirty
    && !window.confirm("Discard unsaved Pass 0 translation guidance changes?")
  ) return;
  if (!(await flushPendingSegmentSaves())) return;
  if (!$("#subtitle-style-panel").classList.contains("hidden")) {
    const closed = await closeSubtitleStylePanel({ refreshExactPreview: false });
    if (!closed) return;
  }
  if (state.exactPreview.open) {
    await discardExactPreview();
  }
  const loadToken = ++state.projectLoadToken;
  $("#review-workspace").classList.add("hidden");
  $("#video").pause();
  try {
    const project = await api(`/api/projects/${projectId}`);
    if (loadToken !== state.projectLoadToken) return;
    state.project = project;
  } catch (error) {
    if (reloadAfterMissingProject(error)) return;
    if (loadToken === state.projectLoadToken) renderProject();
    throw error;
  }
  clearReviewData();
  state.unsavedSegmentFields.clear();
  state.segmentTextSaves.clear();
  state.independentTimingBoundaries.clear();
  state.segmentSaveFailed = false;
  state.contextDirty = false;
  state.contextSavePromise = null;
  updateSegmentSaveState();
  $("#subtitle-style-panel").classList.add("hidden");
  $("#welcome").classList.add("hidden");
  $("#project-view").classList.remove("hidden");
  const cardsLoad = window.CardsUI?.loadProject();
  renderProject();
  renderProviderControl();
  renderProjectList();
  if (reviewWorkspaceReady(state.project)) await loadSegments(projectId);
  await cardsLoad;
  syncPolling();
}

function projectIsRenderLocked(project = state.project) {
  return Boolean(
    project && (
      ["render_queued", "rendering"].includes(project.stage)
      || state.localRenderLockProjectId === project.id
    ),
  );
}

function renderProject() {
  const project = state.project;
  if (!project) return;
  const passesFinished = reviewWorkspaceReady(project);
  if (!passesFinished && (
    state.segmentDataProjectId || state.segments.length || $("#video").getAttribute("src")
  )) clearReviewData();
  const renderLocked = projectIsRenderLocked(project);
  const partialRebuildLocked = Boolean(project.pending_rebuild_scope);
  const subtitleLocked = renderLocked || partialRebuildLocked;
  applySubtitleStyle(project.subtitle_style);
  $("#project-kicker").textContent = `PROJECT ${project.id.slice(0, 8)}`;
  $("#project-title").textContent = projectDisplayName(project);
  const duration = project.duration_ms ? formatTime(project.duration_ms).slice(0, -4) : "--:--";
  $("#project-meta").textContent = `${duration} · ${project.target_language} · ${project.max_height}p · ${project.subtitle_source || "Subtitle source pending"}`;
  $("#stage-label").textContent = stageNames[project.stage] || project.stage;
  $("#progress-label").textContent = `${project.progress}%`;
  $("#progress-bar").style.width = `${project.progress}%`;
  const currentStep = $("#current-step");
  const currentStepText = formatPipelineDetail(project.pipeline_detail);
  currentStep.textContent = currentStepText;
  currentStep.classList.toggle("hidden", !currentStepText);
  const errorBox = $("#project-error");
  errorBox.textContent = project.error || "";
  errorBox.classList.toggle("hidden", !project.error);
  const partProgress = $("#part-progress");
  const parts = project.parts || [];
  partProgress.innerHTML = parts.map((part) =>
    `<span class="part-chip ${escapeHtml(part.status)}">Part ${part.part_number}/${parts.length}: ${escapeHtml(part.status)}</span>`
  ).join("");
  renderTranslationContextPanel(project);

  const actions = $("#project-actions");
  actions.innerHTML = "";
  actions.inert = renderLocked;
  if (["failed", "waiting_for_quota"].includes(project.status)) {
    const retry = document.createElement("button");
    retry.textContent = project.status === "waiting_for_quota" ? "Resume translation" : "Retry failed step";
    retry.className = "primary";
    retry.onclick = retryProject;
    actions.append(retry);
  } else if (project.status === "completed") {
    if (project.output_path) {
      const openFolder = document.createElement("button");
      openFolder.textContent = "Open output folder";
      openFolder.className = "primary";
      openFolder.onclick = openOutputFolder;
      actions.append(openFolder);
      const location = document.createElement("div");
      location.className = "output-location";
      location.textContent = `Saved to: ${project.output_path}`;
      actions.append(location);
    }
  }
  const canShowWorkspace = passesFinished && state.segmentDataProjectId === project.id;
  const canReview = canShowWorkspace && project.segment_count > 0;
  const approveButton = $("#approve-render");
  approveButton.textContent = project.status === "completed"
    ? "Render new version"
    : "Approve all and render";
  approveButton.disabled = subtitleLocked
    || !["waiting_review", "completed", "failed"].includes(project.status);
  const rebuildButton = $("#rebuild-subtitles");
  rebuildButton.classList.toggle(
    "hidden",
    !canShowWorkspace || !["waiting_review", "completed", "failed"].includes(project.status),
  );
  const styleButton = $("#subtitle-style-button");
  styleButton.classList.toggle("hidden", !canReview);
  styleButton.disabled = subtitleLocked;
  const exactPreviewButton = $("#exact-preview-button");
  if (exactPreviewButton) {
    exactPreviewButton.classList.toggle("hidden", !canShowWorkspace);
    exactPreviewButton.disabled = subtitleLocked || state.reviewMode;
    exactPreviewButton.textContent = state.exactPreview.open
      ? "Exit preview"
      : "Preview";
  }
  const reviewSuggestions = $("#review-suggestions");
  reviewSuggestions.classList.toggle("hidden", !canReview || !state.reviewIssues.length);
  reviewSuggestions.disabled = subtitleLocked;
  reviewSuggestions.textContent = state.reviewMode
    ? "All subtitles"
    : `Review suggestions (${state.reviewIssues.length})`;
  const workspace = $("#review-workspace");
  workspace.classList.toggle("hidden", !canShowWorkspace);
  workspace.classList.toggle("render-locked", renderLocked);
  workspace.inert = renderLocked;
  $("#subtitle-pane").inert = subtitleLocked;
  if (subtitleLocked) {
    $("#subtitle-style-panel").classList.add("hidden");
    if (state.exactPreview.open) void discardExactPreview();
  }
  if (canShowWorkspace) {
    const video = $("#video");
    video.controls = !subtitleLocked;
    const expected = `${location.origin}/api/projects/${project.id}/video`;
    if (video.src !== expected) {
      video.src = `/api/projects/${project.id}/video`;
      startVideoSync();
    }
  }
  window.CardsUI?.renderProject();
}

const translationContextCategories = [
  "person",
  "organization",
  "brand",
  "product",
  "place",
  "technical",
  "slang",
  "other",
];

function setTranslationContextDirty() {
  state.contextDirty = true;
  $("#context-save-state").textContent = "Unsaved changes";
}

function createTranslationContextTermRow(term = {}) {
  const row = document.createElement("div");
  row.className = "context-term-row";

  const source = document.createElement("input");
  source.className = "context-term-source";
  source.type = "text";
  source.maxLength = 300;
  source.placeholder = "Original term";
  source.value = term.source || "";

  const target = document.createElement("input");
  target.className = "context-term-target";
  target.type = "text";
  target.maxLength = 300;
  target.placeholder = "Preferred wording";
  target.value = term.target || "";

  const category = document.createElement("select");
  category.className = "context-term-category";
  translationContextCategories.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value.charAt(0).toUpperCase() + value.slice(1);
    option.selected = value === (term.category || "other");
    category.append(option);
  });

  const occurrence = document.createElement("span");
  occurrence.className = "context-term-count";
  const occurrenceCount = Math.max(0, Number(term.occurrence_count) || 0);
  row.dataset.occurrenceCount = String(occurrenceCount);
  occurrence.textContent = occurrenceCount ? String(occurrenceCount) : "Manual";
  occurrence.title = occurrenceCount
    ? "Case-insensitive whole-video phrase count"
    : "Manually added or edited source term";

  const mustUseLabel = document.createElement("label");
  mustUseLabel.className = "context-must-use";
  mustUseLabel.title = "Require all later translation passes to use this wording";
  const mustUse = document.createElement("input");
  mustUse.className = "context-term-must-use";
  mustUse.type = "checkbox";
  mustUse.checked = Boolean(term.must_use);
  mustUseLabel.append(mustUse);

  source.addEventListener("input", () => {
    row.dataset.occurrenceCount = "0";
    occurrence.textContent = "Edited";
    occurrence.title = "Source changed after Pass 0; occurrence count was cleared";
  });

  const remove = document.createElement("button");
  remove.className = "remove-context-term";
  remove.type = "button";
  remove.textContent = "×";
  remove.title = "Remove term";
  remove.addEventListener("click", () => {
    row.remove();
    if (!$("#context-terms").children.length) renderTranslationContextTerms([]);
    setTranslationContextDirty();
  });

  [source, target, category, mustUse].forEach((control) => {
    control.addEventListener("input", setTranslationContextDirty);
    control.addEventListener("change", setTranslationContextDirty);
  });
  row.append(source, target, occurrence, category, mustUseLabel, remove);
  return row;
}

function renderTranslationContextTerms(terms) {
  const container = $("#context-terms");
  container.innerHTML = "";
  if (!terms.length) {
    const empty = document.createElement("div");
    empty.className = "context-empty";
    empty.textContent = "No terminology entries. Add one if a name or phrase needs a fixed translation.";
    container.append(empty);
    return;
  }
  terms.forEach((term) => container.append(createTranslationContextTermRow(term)));
}

function renderTranslationContextPanel(project) {
  const panel = $("#translation-context-panel");
  const isWaiting = project.stage === "waiting_context_review"
    && project.translation_context;
  panel.classList.toggle("hidden", !isWaiting);
  if (!isWaiting || state.contextDirty) return;
  const context = project.translation_context;
  panel.dataset.projectId = project.id;
  $("#context-overview").value = context.overview || "";
  $("#context-style").value = context.style || "";
  $("#context-provider").textContent = project.context_provider
    ? `Generated by ${project.context_provider}`
    : "Generated context";
  $("#context-save-state").textContent = "Review required before Pass 1";
  $("#context-error").classList.add("hidden");
  renderTranslationContextTerms(Array.isArray(context.terms) ? context.terms : []);
}

function readTranslationContextPanel() {
  const overview = $("#context-overview").value.trim();
  if (!overview) throw new Error("Project overview cannot be empty");
  const terms = [];
  const seen = new Set();
  $("#context-terms").querySelectorAll(".context-term-row").forEach((row) => {
    const source = row.querySelector(".context-term-source").value.trim();
    const target = row.querySelector(".context-term-target").value.trim();
    if (!source && !target) return;
    if (!source || !target) throw new Error("Every terminology row needs both fields");
    const key = source.toLocaleLowerCase();
    if (seen.has(key)) throw new Error(`Duplicate terminology source: ${source}`);
    seen.add(key);
    terms.push({
      source,
      target,
      category: row.querySelector(".context-term-category").value,
      must_use: row.querySelector(".context-term-must-use").checked,
      occurrence_count: Number(row.dataset.occurrenceCount) || 0,
    });
  });
  return { overview, style: $("#context-style").value.trim(), terms };
}

async function saveTranslationContext({ approve = false } = {}) {
  if (!state.project || state.contextSavePromise) return false;
  const task = performSaveTranslationContext(approve);
  state.contextSavePromise = task;
  try {
    return await task;
  } finally {
    if (state.contextSavePromise === task) state.contextSavePromise = null;
  }
}

async function performSaveTranslationContext(approve) {
  const errorBox = $("#context-error");
  const saveButton = $("#save-context-draft");
  const approveButton = $("#approve-context");
  errorBox.classList.add("hidden");
  saveButton.disabled = true;
  approveButton.disabled = true;
  $("#context-save-state").textContent = approve ? "Saving and queuing Pass 1…" : "Saving…";
  try {
    const payload = readTranslationContextPanel();
    const suffix = approve ? "/approve" : "";
    const result = await api(
      `/api/projects/${state.project.id}/translation-context${suffix}`,
      { method: approve ? "POST" : "PATCH", body: JSON.stringify(payload) },
    );
    state.project.translation_context = approve ? result.context : result;
    state.contextDirty = false;
    if (approve) {
      const partial = Boolean(state.project.pending_rebuild_scope);
      state.project.context_approved = true;
      state.project.status = "queued";
      state.project.stage = partial ? "part_rebuild_queued" : "queued";
      state.project.progress = 68;
      const listed = state.projects.find((project) => project.id === state.project.id);
      if (listed) {
        listed.status = "queued";
        listed.stage = partial ? "part_rebuild_queued" : "queued";
        listed.progress = 68;
      }
      renderProject();
      renderProjectList();
      syncPolling();
      showToast(partial
        ? "Translation guidance approved; selected-Part Pass 1 is queued"
        : "Translation guidance approved; Pass 1 is queued");
    } else {
      $("#context-save-state").textContent = "Draft saved";
      showToast("Translation guidance draft saved");
    }
    return true;
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
    $("#context-save-state").textContent = "Save failed";
    return false;
  } finally {
    saveButton.disabled = false;
    approveButton.disabled = false;
  }
}

function selectedProvider() {
  const saved = localStorage.getItem("videoTranslatorProvider");
  return ["auto", "codex", "grok"].includes(saved) ? saved : "auto";
}

function selectedCodexModel() {
  return localStorage.getItem("videoTranslatorCodexModel") || "";
}

function renderProviderControl() {
  const select = $("#translation-provider");
  const mode = state.project?.translation_provider || selectedProvider();
  select.value = mode;
  const isProcessing = Boolean(
    state.project && (
      ["queued", "running"].includes(state.project.status)
      || projectIsRenderLocked(state.project)
    ),
  );
  select.disabled = isProcessing;

  const modelSelect = $("#codex-model");
  const configuredDefault = state.providerInfo?.codex_model || "CLI default";
  const selected = state.project?.codex_model ?? selectedCodexModel();
  modelSelect.innerHTML = [
    `<option value="">Default: ${escapeHtml(configuredDefault)}</option>`,
    ...state.codexModels.map(
      (model) => `<option value="${escapeHtml(model.id)}">${escapeHtml(model.name)}</option>`,
    ),
  ].join("");
  modelSelect.value = selected;
  if (modelSelect.value !== selected) modelSelect.value = "";
  modelSelect.disabled = isProcessing || mode === "grok";
}

async function changeTranslationProvider(event) {
  const mode = event.target.value;
  localStorage.setItem("videoTranslatorProvider", mode);
  if (!state.project) {
    renderProviderControl();
    return;
  }
  try {
    const result = await api(`/api/projects/${state.project.id}/translation-provider`, {
      method: "PATCH",
      body: JSON.stringify({ translation_provider: mode }),
    });
    state.project.translation_provider = result.translation_provider;
    renderProviderControl();
    showToast(`Translation provider changed to ${event.target.selectedOptions[0].textContent}`);
  } catch (error) {
    event.target.value = state.project.translation_provider || "auto";
    showToast(error.message);
  }
}

async function changeCodexModel(event) {
  const model = event.target.value;
  localStorage.setItem("videoTranslatorCodexModel", model);
  if (!state.project) return;
  try {
    const result = await api(`/api/projects/${state.project.id}/codex-model`, {
      method: "PATCH",
      body: JSON.stringify({ codex_model: model }),
    });
    state.project.codex_model = result.codex_model;
    showToast(`Codex model changed to ${event.target.selectedOptions[0].textContent}`);
  } catch (error) {
    event.target.value = state.project.codex_model || "";
    showToast(error.message);
  }
}

async function loadSegments(projectId = state.project?.id) {
  if (!projectId || !reviewWorkspaceReady(state.project)) return;
  if (state.segmentLoadingProjectId === projectId) return;
  const loadToken = state.segmentLoadToken;
  state.segmentLoadingProjectId = projectId;
  try {
    const [segments, reviewIssues] = await Promise.all([
      api(`/api/projects/${projectId}/segments`),
      api(`/api/projects/${projectId}/review-issues`),
    ]);
    if (loadToken !== state.segmentLoadToken
      || state.project?.id !== projectId
      || !reviewWorkspaceReady(state.project)) return;
    state.segments = segments;
    state.reviewIssues = reviewIssues;
    state.segmentDataProjectId = segments.length ? projectId : null;
    if (!state.reviewIssues.length) state.reviewMode = false;
    if (!state.selectedId && state.segments.length) state.selectedId = state.segments[0].id;
    renderSegments();
    renderProject();
    updatePreview();
  } finally {
    if (loadToken === state.segmentLoadToken) state.segmentLoadingProjectId = null;
  }
}

function renderSegments() {
  const reviewed = state.segments.filter((segment) => segment.status === "reviewed").length;
  const flaggedIds = new Set(state.reviewIssues.map((issue) => issue.segment_id));
  const flagged = flaggedIds.size;
  $("#segment-summary").textContent = state.reviewMode
    ? `${state.reviewIssues.length} issues · all review states`
    : `${reviewed}/${state.segments.length} reviewed${flagged ? ` · ${flagged} need attention` : ""}`;
  $(".subtitle-table-head span:nth-child(2)").textContent = state.reviewMode
    ? "English / Current translation / Review note"
    : "English / Translation";
  const list = $("#segment-list");
  if (state.reviewMode) {
    renderReviewIssues(list);
    return;
  }
  list.innerHTML = state.segments.map((segment) => `
    <div class="segment-row ${segment.id === state.selectedId ? "selected" : ""} ${segment.id === state.activeId ? "active" : ""}" data-id="${segment.id}">
      ${timeFieldsMarkup(segment, flaggedIds.has(segment.id))}
      <div class="text-fields">
        <textarea class="source" aria-label="English subtitle">${escapeHtml(segment.source_text)}</textarea>
        <textarea class="translation" aria-label="Translated subtitle">${escapeHtml(segment.target_text)}</textarea>
      </div>
      <label class="review-check"><input class="reviewed" type="checkbox" ${segment.status === "reviewed" ? "checked" : ""} aria-label="Reviewed" /></label>
    </div>`).join("");

  list.querySelectorAll(".segment-row").forEach((row) => {
    const id = Number(row.dataset.id);
    row.addEventListener("click", (event) => {
      if (!event.target.matches("input,textarea,button")) selectSegment(id, true);
      else selectSegment(id, false);
    });
    for (const [selector, field] of [[".source", "source_text"], [".translation", "target_text"]]) {
      const input = row.querySelector(selector);
      input.addEventListener("input", (event) => editSegmentText(id, field, event.target.value));
      input.addEventListener("blur", (event) => void saveSegmentText(id, field, event.target.value));
      input.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        void saveSegmentText(id, field, event.target.value);
      });
    }
    bindTimingControls(row, id);
    row.querySelector(".reviewed").addEventListener("change", (event) => saveSegment(id, { status: event.target.checked ? "reviewed" : "edited" }, true));
  });
}

function issueLabel(issue) {
  const types = issue.issue_types || [];
  if (types.includes("pass3_invalid_output")) return "Pass 3 split failed";
  if (types.includes("target_text_too_long")) return "Overlong subtitle";
  if (types.includes("very_long_duration")) return "Long duration";
  if (types.includes("very_short_duration")) return "Short duration";
  if (types.includes("high_target_reading_speed")) return "Fast reading speed";
  return "Needs review";
}

function renderReviewIssues(list) {
  list.innerHTML = state.reviewIssues.map((issue) => {
    const segment = state.segments.find((item) => item.id === issue.segment_id);
    const row = segment ? `
        <div class="segment-row review-issue-row ${segment.id === state.selectedId ? "selected" : ""}" data-id="${segment.id}" data-issue-id="${escapeHtml(issue.id)}">
          ${timeFieldsMarkup(segment, false)}
          <div class="text-fields review-text-fields">
            <textarea class="source" aria-label="English subtitle" readonly>${escapeHtml(segment.source_text)}</textarea>
            <textarea class="translation" aria-label="Current translated subtitle">${escapeHtml(segment.target_text)}</textarea>
            <div class="suggestion-row diagnostic">
              <div class="suggestion-text"><span class="suggestion-label">Review note</span>${escapeHtml(issue.reason)}</div>
            </div>
          </div>
          <label class="review-check"><input class="reviewed" type="checkbox" ${segment.status === "reviewed" ? "checked" : ""} aria-label="Reviewed" /></label>
        </div>` : `<div class="review-issue-missing">The subtitle has changed.</div>`;
    return `
      <section class="review-issue" data-issue-id="${escapeHtml(issue.id)}">
        <div class="review-issue-head">
          <span class="issue-kind">${escapeHtml(issueLabel(issue))}</span>
          <span class="issue-types">${escapeHtml((issue.issue_types || []).map((value) => value.replaceAll("_", " ")).join(" · "))}</span>
          <p>${escapeHtml(issue.reason)}</p>
        </div>
        ${row}
      </section>`;
  }).join("");

  list.querySelectorAll(".segment-row").forEach((row) => {
    const id = Number(row.dataset.id);
    row.addEventListener("click", (event) => {
      if (!event.target.matches("input,textarea,button")) selectSegment(id, true);
      else if (!event.target.matches("button")) selectSegment(id, false);
    });
    const translation = row.querySelector(".translation");
    translation.addEventListener("input", (event) => editSegmentText(id, "target_text", event.target.value));
    translation.addEventListener("blur", (event) => void saveSegmentText(id, "target_text", event.target.value));
    translation.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      void saveSegmentText(id, "target_text", event.target.value);
    });
    bindTimingControls(row, id);
    row.querySelector(".reviewed").addEventListener("change", (event) => saveSegment(id, { status: event.target.checked ? "reviewed" : "edited" }, true));
  });
}

function timeFieldsMarkup(segment, showWarning) {
  const issue = state.reviewIssues.find((item) => item.segment_id === segment.id);
  const warning = showWarning && issue
    ? `<span class="quality-warning" title="${escapeHtml((issue.issue_types || []).map((flag) => flag.replaceAll("_", " ")).join(", "))}">Check readability</span>`
    : "";
  const independentStart = state.independentTimingBoundaries.has(`${segment.id}:start`);
  const independentEnd = state.independentTimingBoundaries.has(`${segment.id}:end`);
  return `
    <div class="time-fields">
      <div class="time-field-row">
        <input class="start-time" aria-label="Start time" value="${formatTime(segment.start_ms)}" />
        <button class="time-adjust" type="button" data-boundary="start" data-delta="-100" aria-label="Move start 100 milliseconds earlier" title="Start −100 ms">−</button>
        <button class="time-adjust" type="button" data-boundary="start" data-delta="100" aria-label="Move start 100 milliseconds later" title="Start +100 ms">+</button>
        <label class="independent-time-toggle" title="Adjust start without changing the previous subtitle">
          <input type="checkbox" data-boundary="start" ${independentStart ? "checked" : ""} aria-label="Adjust start independently" />
          <span aria-hidden="true"></span>
        </label>
      </div>
      <div class="time-field-row">
        <input class="end-time" aria-label="End time" value="${formatTime(segment.end_ms)}" />
        <button class="time-adjust" type="button" data-boundary="end" data-delta="-100" aria-label="Move end 100 milliseconds earlier" title="End −100 ms">−</button>
        <button class="time-adjust" type="button" data-boundary="end" data-delta="100" aria-label="Move end 100 milliseconds later" title="End +100 ms">+</button>
        <label class="independent-time-toggle" title="Adjust end without changing the next subtitle">
          <input type="checkbox" data-boundary="end" ${independentEnd ? "checked" : ""} aria-label="Adjust end independently" />
          <span aria-hidden="true"></span>
        </label>
      </div>
      ${warning}
      <label class="extra-line-gap-toggle" title="Add 30 to this subtitle's line gap">
        <input class="extra-line-gap" type="checkbox" ${segment.extra_line_gap ? "checked" : ""} aria-label="Extra line gap for this subtitle" />
        <span class="extra-line-gap-track" aria-hidden="true"></span>
        <span>Line gap +30</span>
      </label>
    </div>`;
}

function bindTimingControls(row, id) {
  row.querySelector(".extra-line-gap-toggle").addEventListener("click", (event) => event.stopPropagation());
  row.querySelector(".extra-line-gap").addEventListener("change", async (event) => {
    const toggle = event.target;
    toggle.disabled = true;
    const saved = await saveSegment(id, { extra_line_gap: toggle.checked }, true);
    if (saved && state.exactPreview.open) scheduleExactPreview();
  });
  row.querySelectorAll(".independent-time-toggle").forEach((toggle) => {
    toggle.addEventListener("click", (event) => event.stopPropagation());
  });
  row.querySelector(".start-time").addEventListener("change", (event) => {
    void saveTimeInput(id, "start", event.target);
  });
  row.querySelector(".end-time").addEventListener("change", (event) => {
    void saveTimeInput(id, "end", event.target);
  });
  row.querySelectorAll(".time-adjust").forEach((button) => {
    button.addEventListener("click", () => {
      void updateCueTiming(id, {
        boundary: button.dataset.boundary,
        delta_ms: Number(button.dataset.delta),
      });
    });
  });
  row.querySelectorAll(".independent-time-toggle input").forEach((toggle) => {
    toggle.addEventListener("change", () => {
      const key = `${id}:${toggle.dataset.boundary}`;
      if (toggle.checked) state.independentTimingBoundaries.add(key);
      else state.independentTimingBoundaries.delete(key);
    });
  });
}

function editSegmentText(id, field, value) {
  const segment = state.segments.find((item) => item.id === id);
  if (segment) {
    segment[field] = value;
    segment.status = "edited";
    state.unsavedSegmentFields.add(`${id}:${field}`);
    state.segmentSaveFailed = false;
    updateSegmentSaveState();
    updatePreview();
  }
}

function updateSegmentSaveState() {
  const save = $("#save-state");
  if (!save) return;
  if (state.segmentSaveFailed) {
    save.textContent = "Save failed";
    save.classList.remove("saving");
  } else if (state.pendingSaves.size || state.segmentTextSaves.size) {
    save.textContent = "Saving…";
    save.classList.add("saving");
  } else if (state.unsavedSegmentFields.size) {
    save.textContent = "Unsaved";
    save.classList.add("saving");
  } else {
    save.textContent = "Saved";
    save.classList.remove("saving");
  }
}

async function saveSegmentText(id, field, value) {
  const key = `${id}:${field}`;
  if (!state.unsavedSegmentFields.has(key)) return true;
  const existing = state.segmentTextSaves.get(key);
  if (existing) {
    await existing;
    if (state.segmentTextSaves.get(key) === existing) {
      state.segmentTextSaves.delete(key);
    }
    const segment = state.segments.find((item) => item.id === id);
    if (!state.unsavedSegmentFields.has(key)) return true;
    return saveSegmentText(id, field, segment?.[field] ?? value);
  }
  const task = (async () => {
    const saved = await saveSegment(id, { [field]: value, status: "edited" });
    const segment = state.segments.find((item) => item.id === id);
    if (saved && segment?.[field] === value) state.unsavedSegmentFields.delete(key);
    if (saved && state.exactPreview.open && segment
      && state.exactPreview.timestampMs >= segment.start_ms
      && state.exactPreview.timestampMs < segment.end_ms) {
      scheduleExactPreview();
    }
    return saved;
  })();
  state.segmentTextSaves.set(key, task);
  updateSegmentSaveState();
  try {
    return await task;
  } finally {
    if (state.segmentTextSaves.get(key) === task) state.segmentTextSaves.delete(key);
    updateSegmentSaveState();
  }
}

async function saveTimeInput(id, boundary, input) {
  const segment = state.segments.find((item) => item.id === id);
  const field = boundary === "start" ? "start_ms" : "end_ms";
  if (!segment) return;
  try {
    const value = parseTime(input.value);
    await updateCueTiming(id, { boundary, value_ms: value });
  } catch (error) {
    showToast(error.message);
    input.value = formatTime(segment[field]);
  }
}

function applyTimingSegments(segments) {
  for (const updated of segments) {
    const index = state.segments.findIndex((item) => item.id === updated.id);
    if (index >= 0) state.segments[index] = updated;
  }
}

function updateCueTiming(id, payload) {
  const projectId = state.project.id;
  const requestPayload = {
    ...payload,
    sync_neighbor: !state.independentTimingBoundaries.has(`${id}:${payload.boundary}`),
  };
  const previous = state.timingChangeQueue || Promise.resolve();
  const task = previous.catch(() => {}).then(async () => {
    try {
      const result = await api(`/api/projects/${projectId}/segments/${id}/timing`, {
        method: "PATCH",
        body: JSON.stringify(requestPayload),
      });
      if (state.project?.id === projectId) {
        applyTimingSegments(result.segments || []);
        state.segmentSaveFailed = false;
        renderSegments();
        updatePreview();
        showToast("Timing updated");
      }
      return true;
  } catch (error) {
    if (state.project?.id === projectId) {
      state.segmentSaveFailed = false;
      renderSegments();
    }
      showToast(error.message);
      return false;
    }
  });
  state.timingChangeQueue = task;
  state.pendingSaves.add(task);
  task.finally(() => {
    state.pendingSaves.delete(task);
    if (state.timingChangeQueue === task) state.timingChangeQueue = null;
    updateSegmentSaveState();
  });
  updateSegmentSaveState();
  return task;
}

function saveSegment(id, patch, rerender = false) {
  const projectId = state.project.id;
  const task = (async () => {
    try {
      const updated = await api(`/api/projects/${projectId}/segments/${id}`, {
        method: "PATCH",
        body: JSON.stringify(patch),
      });
      if (state.project?.id === projectId) {
        const index = state.segments.findIndex((item) => item.id === id);
        if (index >= 0) {
          const local = state.segments[index];
          for (const field of ["source_text", "target_text"]) {
            if (state.unsavedSegmentFields.has(`${id}:${field}`)) {
              updated[field] = local[field];
            }
          }
          if (
            state.unsavedSegmentFields.has(`${id}:source_text`)
            || state.unsavedSegmentFields.has(`${id}:target_text`)
          ) {
            updated.status = local.status;
          }
          state.segments[index] = updated;
        }
        state.segmentSaveFailed = false;
        if (rerender) renderSegments();
        updatePreview();
      }
      return true;
    } catch (error) {
      if (state.project?.id === projectId) {
        state.segmentSaveFailed = true;
      }
      showToast(error.message);
      return false;
    }
  })();
  state.pendingSaves.add(task);
  task.finally(() => {
    state.pendingSaves.delete(task);
    updateSegmentSaveState();
  });
  updateSegmentSaveState();
  return task;
}

async function flushPendingSegmentSaves() {
  const active = document.activeElement;
  if (active?.matches?.(".segment-row input, .segment-row textarea")) active.blur();
  await Promise.resolve();
  const textSaves = [...state.segmentTextSaves.values()];
  if (textSaves.length) await Promise.all(textSaves);
  const pending = [...state.pendingSaves];
  if (!pending.length) {
    return !state.segmentSaveFailed && !state.unsavedSegmentFields.size;
  }
  const results = await Promise.all(pending);
  if (
    results.every(Boolean)
    && !state.segmentSaveFailed
    && !state.unsavedSegmentFields.size
  ) return true;
  showToast("Fix the subtitle save error before continuing");
  return false;
}

function selectSegment(id, seek) {
  state.selectedId = id;
  document.querySelectorAll(".segment-row").forEach((row) => row.classList.toggle("selected", Number(row.dataset.id) === id));
  const segment = selectedSegment();
  if (!segment) return;
  if (state.exactPreview.open) {
    const midpoint = Math.round((segment.start_ms + segment.end_ms) / 2);
    state.exactPreview.timestampMs = midpoint;
    state.exactPreview.segmentId = segment.id;
    $("#video").currentTime = midpoint / 1000;
    scheduleExactPreview();
  } else if (seek) {
    $("#video").currentTime = segment.start_ms / 1000;
    if (state.reviewMode) void $("#video").play();
  }
}

function selectedSegment() {
  return state.segments.find((segment) => segment.id === state.selectedId);
}

function updatePreview() {
  const active = state.segments.find((segment) => segment.id === state.activeId);
  const preview = $("#subtitle-preview");
  if (!active) {
    preview.classList.add("hidden");
    return;
  }
  updateSubtitlePreviewGeometry();
  $("#preview-cn").textContent = active.target_text.replace(
    /[，。,.—–―⸺⸻]+(?=\s*["'”’》」』】）)]*\s*$)/u,
    "",
  );
  $("#preview-en").textContent = state.project?.bilingual ? active.source_text : "";
  preview.classList.remove("hidden");
}

function startVideoSync() {
  observeSubtitlePreviewGeometry();
  updateSubtitlePreviewGeometry();
  const token = ++state.previewToken;
  const video = $("#video");
  const tick = () => {
    if (token !== state.previewToken) return;
    const now = video.currentTime * 1000;
    window.CardsUI?.sync(now);
    const active = state.segments.find((segment) => now >= segment.start_ms && now < segment.end_ms) || null;
    const nextId = active?.id ?? null;
    if (state.activeId !== nextId) {
      state.activeId = nextId;
      document.querySelectorAll(".segment-row").forEach((row) => row.classList.toggle(
        "active",
        !state.reviewMode && Number(row.dataset.id) === nextId,
      ));
      updatePreview();
    }
    const selected = selectedSegment();
    if (window.CardsUI?.activeTab() !== "cards"
      && $("#loop-current").checked && selected && now >= selected.end_ms) {
      video.currentTime = selected.start_ms / 1000;
      video.play();
    }
    if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(tick);
    else requestAnimationFrame(tick);
  };
  if (video.requestVideoFrameCallback) video.requestVideoFrameCallback(tick);
  else requestAnimationFrame(tick);
}

function toggleVideoPlaybackFromKeyboard(event) {
  if (event.code !== "Space" || event.repeat || event.defaultPrevented) return;
  if (event.target.closest("input, textarea, select, button, [contenteditable='true']")) return;
  const workspace = $("#review-workspace");
  const video = $("#video");
  if (
    !state.project
    || workspace.classList.contains("hidden")
    || workspace.inert
    || state.exactPreview.open
    || !video.currentSrc
  ) return;
  event.preventDefault();
  if (video.paused) void video.play();
  else video.pause();
}

function focusFirstInvalidSegmentFromError(message) {
  const video = $("#video");
  const workspace = $("#review-workspace");
  if (
    !video.paused
    || state.reviewMode
    || workspace.classList.contains("hidden")
    || workspace.inert
  ) return;
  const subtitleNumbers = [];
  for (const match of String(message).matchAll(/\bSubtitles?\s+(\d+)(?:\s+and\s+(\d+))?/gi)) {
    subtitleNumbers.push(Number(match[1]));
    if (match[2]) subtitleNumbers.push(Number(match[2]));
  }
  const firstSubtitleNumber = Math.min(...subtitleNumbers.filter(Number.isInteger));
  const segment = state.segments[firstSubtitleNumber - 1];
  if (!segment) return;
  selectSegment(segment.id, false);
  scrollSegmentListToCenter(segment.id);
}

async function approveAndRender() {
  if (!state.project || projectIsRenderLocked()) return;
  if (window.CardsUI && !(await window.CardsUI.flush())) return;
  const projectId = state.project.id;
  if (!$("#subtitle-style-panel").classList.contains("hidden")) {
    if (!(await closeSubtitleStylePanel({ refreshExactPreview: false }))) return;
  }
  if (state.exactPreview.open) await discardExactPreview();
  if (!(await flushPendingSegmentSaves())) return;
  state.localRenderLockProjectId = projectId;
  if (state.project?.id === projectId) {
    renderProject();
    renderProviderControl();
  }
  try {
    await api(`/api/projects/${projectId}/approve-and-render`, { method: "POST", body: "{}" });
    if (state.localRenderLockProjectId === projectId) state.localRenderLockProjectId = null;
    showToast("Render queued; the new version will be saved to the project output folder");
    if (state.project?.id === projectId) await refreshCurrentProject();
    else await loadProjects();
  } catch (error) {
    if (state.localRenderLockProjectId === projectId) state.localRenderLockProjectId = null;
    if (state.project?.id === projectId) {
      renderProject();
      renderProviderControl();
      focusFirstInvalidSegmentFromError(error.message);
    }
    showToast(error.message);
  }
}

async function openOutputFolder() {
  try {
    const result = await api(`/api/projects/${state.project.id}/open-output-folder`, {
      method: "POST",
      body: "{}",
    });
    showToast(`Opened: ${result.path}`);
  } catch (error) {
    showToast(error.message);
  }
}

async function retryProject() {
  try {
    await api(`/api/projects/${state.project.id}/retry`, { method: "POST", body: "{}" });
    await refreshCurrentProject();
  } catch (error) { showToast(error.message); }
}

function selectedRebuildMode() {
  return document.querySelector('input[name="rebuild-mode"]:checked')?.value || "selected";
}

function updateRebuildDialog() {
  const mode = selectedRebuildMode();
  const selected = [...document.querySelectorAll(".rebuild-part-input:checked")];
  $("#rebuild-part-panel").classList.toggle("hidden", mode !== "selected");
  $("#rebuild-full-warning").classList.toggle("hidden", mode !== "full");
  $("#rebuild-skip-pass0-option").classList.toggle("hidden", mode !== "full");
  $("#rebuild-selection-summary").textContent = `${selected.length} Part${selected.length === 1 ? "" : "s"} selected`;
  $("#confirm-rebuild-subtitles").textContent = mode === "full"
    ? "Delete and rebuild everything"
    : "Rebuild selected Parts";
}

function renderRebuildParts(parts) {
  state.rebuildParts = parts;
  $("#rebuild-part-list").innerHTML = parts.map((part) => `
    <label class="rebuild-part-option">
      <input type="checkbox" class="rebuild-part-input" data-part-id="${part.part_id}" />
      <span class="rebuild-part-copy">
        <span class="rebuild-part-meta"><strong>Part ${part.part_number}</strong> · ${part.cue_count} cues · ${formatTime(part.start_ms).slice(0, -4)}–${formatTime(part.end_ms).slice(0, -4)}</span>
        <span class="rebuild-part-preview">${escapeHtml(part.preview)}</span>
      </span>
    </label>`).join("");
  document.querySelectorAll(".rebuild-part-input").forEach((input) => {
    input.addEventListener("change", updateRebuildDialog);
  });
  updateRebuildDialog();
}

async function rebuildSubtitles() {
  const errorBox = $("#rebuild-dialog-error");
  errorBox.classList.add("hidden");
  try {
    const result = await api(`/api/projects/${state.project.id}/rebuild-parts`);
    renderRebuildParts(result.parts || []);
    const selectedRadio = document.querySelector('input[name="rebuild-mode"][value="selected"]');
    const fullRadio = document.querySelector('input[name="rebuild-mode"][value="full"]');
    const skipPass0 = $("#rebuild-skip-pass0");
    selectedRadio.disabled = !result.selected_rebuild_available;
    selectedRadio.checked = Boolean(result.selected_rebuild_available);
    fullRadio.checked = !result.selected_rebuild_available;
    skipPass0.checked = false;
    skipPass0.disabled = !result.pass0_available;
    updateRebuildDialog();
    $("#rebuild-subtitles-dialog").showModal();
  } catch (error) {
    showToast(error.message);
  }
}

async function submitRebuildSubtitles(event) {
  event.preventDefault();
  const mode = selectedRebuildMode();
  const skipPass0 = mode === "full" && $("#rebuild-skip-pass0").checked;
  const partIds = [...document.querySelectorAll(".rebuild-part-input:checked")].map((input) => (
    Number(input.dataset.partId)
  ));
  const errorBox = $("#rebuild-dialog-error");
  errorBox.classList.add("hidden");
  if (mode === "selected" && !partIds.length) {
    errorBox.textContent = "Select at least one Part.";
    errorBox.classList.remove("hidden");
    return;
  }
  if (!(await flushPendingSegmentSaves())) return;
  if (!$("#subtitle-style-panel").classList.contains("hidden")) {
    if (!(await closeSubtitleStylePanel({ refreshExactPreview: false }))) return;
  }
  if (state.exactPreview.open) await discardExactPreview();
  try {
    await api(`/api/projects/${state.project.id}/rebuild-subtitles`, {
      method: "POST",
      body: JSON.stringify({ mode, part_ids: partIds, skip_pass0: skipPass0 }),
    });
    $("#rebuild-subtitles-dialog").close();
    showToast(mode === "full"
      ? (skipPass0
        ? "Full rebuild queued; existing Pass 0 will be reused"
        : "Old subtitle data cleared; full rebuild queued")
      : "Selected Parts queued; current subtitles remain until the new result is ready");
    await refreshCurrentProject();
  } catch (error) {
    errorBox.textContent = error.message;
    errorBox.classList.remove("hidden");
  }
}

const styleInputs = {
  chinese_subtitle_size: "#style-chinese-size",
  english_subtitle_size: "#style-english-size",
  chinese_subtitle_outline: "#style-chinese-outline",
  english_subtitle_outline: "#style-english-outline",
  subtitle_bottom_margin: "#style-bottom-margin",
  subtitle_line_gap: "#style-line-gap",
  subtitle_horizontal_margin: "#style-horizontal-margin",
  bottom_mask_height: "#style-mask-height",
  bottom_mask_opacity: "#style-mask-opacity",
};

const styleColorInputs = {
  chinese_subtitle_color: "#style-chinese-color",
  english_subtitle_color: "#style-english-color",
};

function fillSubtitleStylePanel() {
  const style = state.project?.subtitle_style || state.subtitleStyle;
  if (!style) return;
  $("#style-show-english").checked = state.project?.bilingual !== false;
  $(styleInputs.chinese_subtitle_size).value = style.chinese.size;
  $(styleInputs.english_subtitle_size).value = style.english.size;
  $(styleColorInputs.chinese_subtitle_color).value = style.chinese.color;
  $(styleColorInputs.english_subtitle_color).value = style.english.color;
  $(styleInputs.chinese_subtitle_outline).value = style.chinese.outline;
  $(styleInputs.english_subtitle_outline).value = style.english.outline;
  $(styleInputs.subtitle_bottom_margin).value = style.layout.bottom_margin;
  $(styleInputs.subtitle_line_gap).value = style.layout.line_gap;
  $(styleInputs.subtitle_horizontal_margin).value = style.layout.horizontal_margin;
  $(styleInputs.bottom_mask_height).value = Math.round(style.bottom_mask.height * 100);
  $(styleInputs.bottom_mask_opacity).value = Math.round(style.bottom_mask.opacity * 100);
  $("#style-save-state").textContent = "";
  state.styleDirty = false;
  updateStyleClipboardButtons();
}

function exactPreviewTarget() {
  const now = Math.round($("#video").currentTime * 1000);
  return { timestampMs: now };
}

async function openSubtitleStylePanel() {
  if (!state.project || !state.segments.length || projectIsRenderLocked()) return;
  if (!(await flushPendingSegmentSaves())) return;
  fillSubtitleStylePanel();
  $("#subtitle-style-panel").classList.remove("hidden");
}

async function openExactPreview() {
  if (!state.project || !state.project.video_path || projectIsRenderLocked() || state.reviewMode) return;
  if (!(await flushPendingSegmentSaves())) return;
  try {
    window.CardsUI?.currentDraft();
  } catch (error) {
    showToast(error.message);
    return;
  }
  const target = exactPreviewTarget();
  if (!target) return;
  state.exactPreview.open = true;
  state.exactPreview.timestampMs = target.timestampMs;
  state.exactPreview.segmentId = null;
  $("#exact-preview-time").value = (target.timestampMs / 1000).toFixed(1);
  $("#video").pause();
  $("#video").currentTime = target.timestampMs / 1000;
  document.querySelector(".video-stage").classList.add("exact-mode");
  $("#exact-preview-badge").textContent = `Exact FFmpeg preview · ${formatTime(target.timestampMs)}`;
  $("#exact-preview-badge").classList.remove("hidden");
  const exactPreviewButton = $("#exact-preview-button");
  if (exactPreviewButton) exactPreviewButton.textContent = "Exit preview";
  await renderExactPreview();
}

async function toggleExactPreview() {
  if (state.exactPreview.open) await discardExactPreview();
  else await openExactPreview();
}

async function toggleReviewMode() {
  if (!state.project || !state.reviewIssues.length || projectIsRenderLocked()) return;
  if (!(await flushPendingSegmentSaves())) return;
  if (!state.reviewMode && state.exactPreview.open) await discardExactPreview();
  const leavingReview = state.reviewMode;
  const paused = $("#video").paused;
  const selectedId = state.selectedId;
  const selectedInIssues = state.reviewIssues.some((issue) => issue.segment_id === selectedId);
  state.reviewMode = !state.reviewMode;
  state.activeId = leavingReview && paused ? selectedId : null;
  if (state.activeId == null) {
    document.querySelectorAll(".segment-row").forEach((row) => row.classList.remove("active"));
  }
  renderSegments();
  renderProject();
  const jumpToSelected = paused && selectedId != null && (leavingReview || selectedInIssues);
  if (jumpToSelected) scrollSegmentListToCenter(selectedId, { instant: true });
}

function readSubtitleStylePanel() {
  const values = Object.fromEntries(
    Object.entries(styleInputs).map(([name, selector]) => {
      const input = $(selector);
      const value = Number(input.value);
      if (input.value.trim() === "" || !Number.isFinite(value)) {
        throw new Error(`${input.closest("label").firstChild.textContent.trim()} requires a number`);
      }
      const minimum = Number(input.min);
      const maximum = Number(input.max);
      if (value < minimum || value > maximum) {
        throw new Error(
          `${input.closest("label").firstChild.textContent.trim()} must be between ${minimum} and ${maximum}`,
        );
      }
      return [name, value];
    }),
  );
  values.bottom_mask_height /= 100;
  values.bottom_mask_opacity /= 100;
  Object.entries(styleColorInputs).forEach(([name, selector]) => {
    values[name] = $(selector).value.toUpperCase();
  });
  values.show_english_subtitles = $("#style-show-english").checked;
  return values;
}

function validateSubtitleStyleClipboard(values) {
  if (!values || typeof values !== "object") {
    throw new Error("The copied subtitle style is invalid");
  }
  const normalized = {};
  Object.entries(styleInputs).forEach(([name, selector]) => {
    let value = Number(values[name]);
    if (name === "bottom_mask_height" || name === "bottom_mask_opacity") value *= 100;
    const input = $(selector);
    const minimum = Number(input.min);
    const maximum = Number(input.max);
    if (!Number.isFinite(value) || value < minimum || value > maximum) {
      throw new Error(`The copied value for ${name} is invalid`);
    }
    normalized[name] = value;
  });
  Object.entries(styleColorInputs).forEach(([name]) => {
    const value = String(values[name] || "").toUpperCase();
    if (!/^#[0-9A-F]{6}$/.test(value)) {
      throw new Error(`The copied value for ${name} is invalid`);
    }
    normalized[name] = value;
  });
  if (
    values.show_english_subtitles !== undefined
    && typeof values.show_english_subtitles !== "boolean"
  ) {
    throw new Error("The copied value for show_english_subtitles is invalid");
  }
  normalized.show_english_subtitles = values.show_english_subtitles !== false;
  return normalized;
}

function readSubtitleStyleClipboard() {
  const raw = localStorage.getItem(subtitleStyleClipboardKey);
  if (!raw) return null;
  const clipboard = JSON.parse(raw);
  if (clipboard?.version !== 1) throw new Error("The copied subtitle style is incompatible");
  return validateSubtitleStyleClipboard(clipboard.values);
}

function updateStyleClipboardButtons() {
  const paste = $("#paste-subtitle-style");
  if (!paste) return;
  try {
    paste.disabled = !readSubtitleStyleClipboard() || Boolean(state.styleSavePromise);
  } catch (_) {
    paste.disabled = true;
  }
}

function copySubtitleStyle() {
  try {
    const values = readSubtitleStylePanel();
    localStorage.setItem(
      subtitleStyleClipboardKey,
      JSON.stringify({ version: 1, values }),
    );
    updateStyleClipboardButtons();
    $("#style-save-state").textContent = "Style copied";
    showToast("Subtitle style copied");
  } catch (error) {
    showToast(error.message);
  }
}

async function pasteSubtitleStyle() {
  if ($("#subtitle-style-panel").classList.contains("hidden") || state.styleSavePromise) return;
  try {
    const values = readSubtitleStyleClipboard();
    if (!values) throw new Error("Copy a subtitle style first");
    Object.entries(styleInputs).forEach(([name, selector]) => {
      $(selector).value = values[name];
    });
    Object.entries(styleColorInputs).forEach(([name, selector]) => {
      $(selector).value = values[name];
    });
    $("#style-show-english").checked = values.show_english_subtitles;
    state.styleDirty = true;
    $("#style-save-state").textContent = "Pasted; saving…";
    await saveSubtitleStyle();
  } catch (error) {
    $("#style-save-state").textContent = "Paste failed";
    showToast(error.message);
  }
}

function exactPreviewCacheKey(draft) {
  const active = state.segments.filter((segment) =>
    state.exactPreview.timestampMs >= segment.start_ms
      && state.exactPreview.timestampMs < segment.end_ms,
  );
  return JSON.stringify({
    projectId: state.project.id,
    timestampMs: state.exactPreview.timestampMs,
    bilingual: state.project.bilingual,
    style: state.project.subtitle_style,
    subtitleRevision: state.project.subtitle_revision,
    cardRevision: state.project.card_revision,
    active,
    draft,
  });
}

function showExactPreviewUrl(url) {
  const image = $("#exact-preview-image");
  state.exactPreview.objectUrl = url;
  image.src = url;
  image.classList.remove("hidden");
}

function cacheExactPreview(key, url) {
  const replacedUrl = state.exactPreview.cache.get(key);
  state.exactPreview.cache.delete(key);
  state.exactPreview.cache.set(key, url);
  while (state.exactPreview.cache.size > 6) {
    const oldestKey = [...state.exactPreview.cache.keys()].find(
      (item) => state.exactPreview.cache.get(item) !== state.exactPreview.objectUrl,
    );
    if (oldestKey === undefined) break;
    const oldestUrl = state.exactPreview.cache.get(oldestKey);
    state.exactPreview.cache.delete(oldestKey);
    URL.revokeObjectURL(oldestUrl);
  }
  return replacedUrl && replacedUrl !== url ? replacedUrl : null;
}

async function renderExactPreview() {
  if (!state.exactPreview.open || !state.project) return false;
  let draft;
  try {
    draft = window.CardsUI?.currentDraft() || null;
  } catch (error) {
    showToast(error.message);
    return false;
  }
  const key = exactPreviewCacheKey(draft);
  const cached = state.exactPreview.cache.get(key);
  if (cached && !state.exactPreview.controller) {
    state.exactPreview.cache.delete(key);
    state.exactPreview.cache.set(key, cached);
    state.exactPreview.requestId += 1;
    state.exactPreview.controller?.abort();
    state.exactPreview.controller = null;
    showExactPreviewUrl(cached);
    $("#exact-preview-loading").classList.add("hidden");
    $("#exact-preview-badge").textContent = `Exact FFmpeg preview · ${formatTime(state.exactPreview.timestampMs)}`;
    return true;
  }

  const requestId = ++state.exactPreview.requestId;
  state.exactPreview.controller?.abort();
  const controller = new AbortController();
  state.exactPreview.controller = controller;
  const loading = $("#exact-preview-loading");
  loading.textContent = "Rendering exact preview…";
  loading.classList.remove("hidden");
  try {
    const response = await fetch(`/api/projects/${state.project.id}/exact-preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        timestamp_ms: state.exactPreview.timestampMs,
        ...(draft ? { card_id: draft.id, card_draft: draft.payload } : {}),
      }),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(await readApiError(response));
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    if (!state.exactPreview.open || requestId !== state.exactPreview.requestId) {
      URL.revokeObjectURL(url);
      return false;
    }
    const replacedUrl = cacheExactPreview(key, url);
    showExactPreviewUrl(url);
    if (replacedUrl) URL.revokeObjectURL(replacedUrl);
    $("#exact-preview-badge").textContent = `Exact FFmpeg preview · ${formatTime(state.exactPreview.timestampMs)}`;
    return true;
  } catch (error) {
    if (error.name !== "AbortError" && requestId === state.exactPreview.requestId) {
      $("#exact-preview-badge").textContent = `Exact preview failed · ${error.message}`;
      showToast(error.message);
    }
    return false;
  } finally {
    if (requestId === state.exactPreview.requestId) {
      loading.classList.add("hidden");
      state.exactPreview.controller = null;
    }
  }
}

function scheduleExactPreview() {
  if (!state.exactPreview.open) return;
  clearTimeout(state.exactPreview.debounceTimer);
  state.exactPreview.debounceTimer = setTimeout(() => {
    state.exactPreview.debounceTimer = null;
    void renderExactPreview();
  }, 250);
}

function stepExactPreview(direction) {
  if (!state.exactPreview.open) return;
  const maximum = Math.max(0, (state.project?.duration_ms || 0) - 1);
  state.exactPreview.timestampMs = Math.max(0, Math.min(
    maximum, state.exactPreview.timestampMs + direction * 100,
  ));
  $("#exact-preview-time").value = (state.exactPreview.timestampMs / 1000).toFixed(1);
  scheduleExactPreview();
}

function scrollSegmentListTo(segmentId) {
  const list = $("#segment-list");
  const row = list.querySelector(`.segment-row[data-id="${segmentId}"]`);
  if (!row) return;
  const listBounds = list.getBoundingClientRect();
  const rowBounds = row.getBoundingClientRect();
  if (rowBounds.top < listBounds.top) {
    list.scrollTop -= listBounds.top - rowBounds.top;
  } else if (rowBounds.bottom > listBounds.bottom) {
    list.scrollTop += rowBounds.bottom - listBounds.bottom;
  }
}

function scrollSegmentListToCenter(segmentId, { toastIfMissing = false, instant = false } = {}) {
  const list = $("#segment-list");
  const row = list.querySelector(`.segment-row[data-id="${segmentId}"]`);
  if (!row) {
    if (toastIfMissing) showToast("The current subtitle is not visible in this list");
    return;
  }
  const target = row.closest(".review-issue") || row;
  const listBounds = list.getBoundingClientRect();
  const rowBounds = target.getBoundingClientRect();
  const centeredTop = Math.max(
    0,
    list.scrollTop
      + rowBounds.top
      - listBounds.top
      - (list.clientHeight - rowBounds.height) / 2,
  );
  if (instant) list.scrollTop = centeredTop;
  else list.scrollTo({ top: centeredTop, behavior: "smooth" });
}

function scrollToActiveSegment() {
  if (state.activeId === null) {
    showToast("No subtitle is active at the current playback time");
    return;
  }
  scrollSegmentListToCenter(state.activeId, { toastIfMissing: true });
}

async function discardExactPreview() {
  const projectId = state.project?.id;
  const restoreMs = state.exactPreview.timestampMs;
  state.exactPreview.open = false;
  clearTimeout(state.exactPreview.debounceTimer);
  state.exactPreview.debounceTimer = null;
  state.exactPreview.requestId += 1;
  state.exactPreview.controller?.abort();
  state.exactPreview.controller = null;
  if (projectId) {
    try {
      await api(`/api/projects/${projectId}/exact-preview/cancel`, {
        method: "POST",
        body: "{}",
      });
    } catch (_) {}
  }
  const objectUrls = new Set(state.exactPreview.cache.values());
  if (state.exactPreview.objectUrl) objectUrls.add(state.exactPreview.objectUrl);
  for (const url of objectUrls) URL.revokeObjectURL(url);
  state.exactPreview.cache.clear();
  state.exactPreview.objectUrl = null;
  state.exactPreview.segmentId = null;
  const image = $("#exact-preview-image");
  image.removeAttribute("src");
  image.classList.add("hidden");
  $("#exact-preview-badge").classList.add("hidden");
  $("#exact-preview-loading").classList.add("hidden");
  document.querySelector(".video-stage").classList.remove("exact-mode");
  const exactPreviewButton = $("#exact-preview-button");
  if (exactPreviewButton) exactPreviewButton.textContent = "Preview";
  const video = $("#video");
  video.currentTime = restoreMs / 1000;
  video.pause();
  state.activeId = state.segments.find(
    (segment) => restoreMs >= segment.start_ms && restoreMs < segment.end_ms,
  )?.id ?? null;
  updatePreview();
  window.CardsUI?.sync(restoreMs);
}

async function closeSubtitleStylePanel({ refreshExactPreview = true } = {}) {
  if (
    state.styleDirty
    && !(await saveSubtitleStyle({
      renderPreview: refreshExactPreview && state.exactPreview.open,
    }))
  ) {
    return false;
  }
  $("#subtitle-style-panel").classList.add("hidden");
  return true;
}

async function saveSubtitleStyle(options = {}) {
  if (state.styleSavePromise) return state.styleSavePromise;
  const task = performSaveSubtitleStyle(options);
  state.styleSavePromise = task;
  try {
    return await task;
  } finally {
    if (state.styleSavePromise === task) state.styleSavePromise = null;
    updateStyleClipboardButtons();
  }
}

async function performSaveSubtitleStyle({ renderPreview = true } = {}) {
  if (!state.project) return false;
  if (!(await flushPendingSegmentSaves())) return false;
  const button = $("#save-subtitle-style");
  const copyButton = $("#copy-subtitle-style");
  const pasteButton = $("#paste-subtitle-style");
  const saveState = $("#style-save-state");
  button.disabled = true;
  copyButton.disabled = true;
  pasteButton.disabled = true;
  saveState.textContent = "Saving…";
  try {
    const payload = readSubtitleStylePanel();
    const result = await api(`/api/projects/${state.project.id}/subtitle-style`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
    state.project.subtitle_style = result.subtitle_style;
    state.project.bilingual = result.bilingual;
    state.project.subtitle_revision += 1;
    state.project.render_is_stale = state.project.rendered_revision !== null;
    applySubtitleStyle(result.subtitle_style);
    updatePreview();
    state.styleDirty = false;
    saveState.textContent = renderPreview && state.exactPreview.open
      ? "Saved; rendering exact preview…"
      : "Saved";
    if (renderPreview && state.exactPreview.open) {
      const rendered = await renderExactPreview();
      saveState.textContent = rendered
        ? "Saved and exact preview refreshed"
        : "Style saved, but exact preview failed";
    }
    showToast("Subtitle style saved");
    return true;
  } catch (error) {
    saveState.textContent = "Save failed";
    showToast(error.message);
    return false;
  } finally {
    button.disabled = false;
    copyButton.disabled = false;
    updateStyleClipboardButtons();
  }
}

async function refreshCurrentProject() {
  if (!state.project) return;
  const projectId = state.project.id;
  const loadToken = state.projectLoadToken;
  const previousCount = state.project.segment_count;
  const previousRevision = state.project.subtitle_revision;
  let project;
  try {
    project = await api(`/api/projects/${projectId}`);
  } catch (error) {
    if (reloadAfterMissingProject(error)) return;
    throw error;
  }
  if (loadToken !== state.projectLoadToken || state.project?.id !== projectId) return;
  const needsSegments = reviewWorkspaceReady(project) && (
    previousCount !== project.segment_count
    || previousRevision !== project.subtitle_revision
    || (state.segmentDataProjectId !== projectId
      && state.segmentLoadingProjectId !== projectId)
  );
  if (needsSegments) clearReviewData();
  state.project = project;
  renderProject();
  renderProviderControl();
  await loadProjects();
  if (state.project?.id !== projectId || loadToken !== state.projectLoadToken) return;
  if (needsSegments) await loadSegments(projectId);
  syncPolling();
}

function reloadAfterMissingProject(error) {
  if (error.status !== 404) return false;
  // The server may have restarted with a fresh database while this tab stayed open.
  if (state.pollTimer) clearInterval(state.pollTimer);
  state.pollTimer = null;
  window.location.reload();
  return true;
}

function syncPolling() {
  const shouldPoll = state.project && ["queued", "running"].includes(state.project.status);
  if (!shouldPoll && state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  } else if (shouldPoll && !state.pollTimer) {
    state.pollTimer = setInterval(() => {
      void refreshCurrentProject().catch((error) => console.warn("Project refresh failed:", error));
    }, 2000);
  }
}

window.addEventListener("focus", () => {
  const refresh = state.project ? refreshCurrentProject() : loadProjects();
  void refresh.catch((error) => console.warn("Project refresh failed:", error));
});

function openCreateDialog() {
  $("#form-error").classList.add("hidden");
  $("#new-project-dialog").showModal();
}

function openRenameProjectDialog(projectId) {
  const project = state.projects.find((item) => item.id === projectId);
  if (!project) return;
  state.renamingProjectId = projectId;
  $("#project-display-name").value = project.display_name || project.title || "";
  $("#rename-project-error").classList.add("hidden");
  $("#rename-project-dialog").showModal();
  $("#project-display-name").select();
}

function closeRenameProjectDialog() {
  state.renamingProjectId = null;
  $("#rename-project-dialog").close();
}

function openDeleteProjectDialog(projectId) {
  const project = state.projects.find((item) => item.id === projectId);
  if (!project) return;
  state.deletingProjectId = projectId;
  $("#delete-project-name").textContent = projectDisplayName(project);
  $("#delete-project-error").classList.add("hidden");
  $("#delete-project-dialog").showModal();
}

function closeDeleteProjectDialog() {
  state.deletingProjectId = null;
  $("#delete-project-dialog").close();
}

$("#new-project-button").addEventListener("click", openCreateDialog);
$("#welcome-new-project").addEventListener("click", openCreateDialog);
$("#close-dialog").addEventListener("click", () => $("#new-project-dialog").close());
$("#cancel-dialog").addEventListener("click", () => $("#new-project-dialog").close());
$("#close-rename-dialog").addEventListener("click", closeRenameProjectDialog);
$("#cancel-rename-dialog").addEventListener("click", closeRenameProjectDialog);
$("#close-delete-dialog").addEventListener("click", closeDeleteProjectDialog);
$("#cancel-delete-dialog").addEventListener("click", closeDeleteProjectDialog);
$("#delete-project-dialog").addEventListener("close", () => { state.deletingProjectId = null; });
$("#close-rebuild-dialog").addEventListener("click", () => $("#rebuild-subtitles-dialog").close());
$("#cancel-rebuild-dialog").addEventListener("click", () => $("#rebuild-subtitles-dialog").close());
$("#rebuild-subtitles-form").addEventListener("submit", submitRebuildSubtitles);
document.querySelectorAll('input[name="rebuild-mode"]').forEach((input) => {
  input.addEventListener("change", updateRebuildDialog);
});
$("#select-all-rebuild-parts").addEventListener("click", () => {
  document.querySelectorAll(".rebuild-part-input").forEach((input) => { input.checked = true; });
  updateRebuildDialog();
});
$("#clear-rebuild-parts").addEventListener("click", () => {
  document.querySelectorAll(".rebuild-part-input").forEach((input) => { input.checked = false; });
  updateRebuildDialog();
});
$("#scroll-to-active").addEventListener("click", scrollToActiveSegment);
$("#approve-render").addEventListener("click", approveAndRender);
$("#rebuild-subtitles").addEventListener("click", rebuildSubtitles);
$("#review-suggestions").addEventListener("click", () => void toggleReviewMode());
$("#add-context-term").addEventListener("click", () => {
  const container = $("#context-terms");
  if (container.querySelectorAll(".context-term-row").length >= 40) {
    showToast("The global consistency list is limited to 40 terms");
    return;
  }
  container.querySelector(".context-empty")?.remove();
  const row = createTranslationContextTermRow();
  container.append(row);
  row.querySelector(".context-term-source").focus();
  setTranslationContextDirty();
});
$("#context-overview").addEventListener("input", setTranslationContextDirty);
$("#context-style").addEventListener("input", setTranslationContextDirty);
$("#save-context-draft").addEventListener("click", () => void saveTranslationContext());
$("#approve-context").addEventListener("click", () => void saveTranslationContext({ approve: true }));
ensureExactPreviewButton()?.addEventListener("click", () => void toggleExactPreview());
$("#exact-preview-refresh").addEventListener("click", () => {
  if (!state.project?.video_path) return;
  const seconds = Number($("#exact-preview-time").value);
  if (!Number.isFinite(seconds) || seconds < 0 || seconds * 1000 >= state.project.duration_ms) {
    showToast("Enter a time within the video duration");
    return;
  }
  const timestampMs = Math.round(seconds * 1000);
  if (state.exactPreview.open) {
    state.exactPreview.timestampMs = timestampMs;
    void renderExactPreview();
  } else {
    $("#video").currentTime = seconds;
    void openExactPreview();
  }
});
$("#exact-preview-time").addEventListener("keydown", (event) => {
  if (event.key === "Enter") $("#exact-preview-refresh").click();
});
$("#subtitle-style-button").addEventListener("click", () => void openSubtitleStylePanel());
$("#close-subtitle-style").addEventListener("click", () => void closeSubtitleStylePanel());
$("#save-subtitle-style").addEventListener("click", () => void saveSubtitleStyle());
$("#copy-subtitle-style").addEventListener("click", copySubtitleStyle);
$("#paste-subtitle-style").addEventListener("click", () => void pasteSubtitleStyle());
$("#exact-preview-image").addEventListener("click", (event) => {
  const bounds = event.currentTarget.getBoundingClientRect();
  const direction = event.clientX < bounds.left + bounds.width / 2 ? -1 : 1;
  stepExactPreview(direction);
});
Object.values({ ...styleInputs, ...styleColorInputs }).forEach((selector) => {
  $(selector).addEventListener("input", () => {
    if ($("#subtitle-style-panel").classList.contains("hidden")) return;
    state.styleDirty = true;
    $("#style-save-state").textContent = "Unsaved changes";
  });
});
$("#style-show-english").addEventListener("change", () => {
  if ($("#subtitle-style-panel").classList.contains("hidden")) return;
  state.styleDirty = true;
  $("#style-save-state").textContent = "Unsaved changes";
});
$("#subtitle-style-panel").addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !event.target.matches("input")) return;
  event.preventDefault();
  if (!$("#save-subtitle-style").disabled) void saveSubtitleStyle();
});
$("#translation-provider").addEventListener("change", changeTranslationProvider);
$("#codex-model").addEventListener("change", changeCodexModel);
document.addEventListener("keydown", toggleVideoPlaybackFromKeyboard);

window.addEventListener("beforeunload", (event) => {
  if (
    !state.unsavedSegmentFields.size
    && !state.pendingSaves.size
    && !state.styleDirty
    && !state.contextDirty
  ) return;
  event.preventDefault();
  event.returnValue = "";
});

$("#new-project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.target.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const project = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        source_url: $("#source-url").value,
        target_language: $("#target-language").value,
        max_height: Number($("#max-height").value),
        translation_provider: $("#translation-provider").value,
        codex_model: $("#codex-model").value,
      }),
    });
    $("#new-project-dialog").close();
    event.target.reset();
    await loadProjects();
    await openProject(project.id);
  } catch (error) {
    const box = $("#form-error");
    box.textContent = error.message;
    box.classList.remove("hidden");
  } finally {
    submit.disabled = false;
  }
});

$("#rename-project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const projectId = state.renamingProjectId;
  if (!projectId) return;
  const submit = event.target.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const result = await api(`/api/projects/${projectId}/name`, {
      method: "PATCH",
      body: JSON.stringify({ display_name: $("#project-display-name").value }),
    });
    const project = state.projects.find((item) => item.id === projectId);
    if (project) project.display_name = result.display_name;
    if (state.project?.id === projectId) {
      state.project.display_name = result.display_name;
      $("#project-title").textContent = result.effective_name;
    }
    closeRenameProjectDialog();
    renderProjectList();
    showToast("Project renamed");
  } catch (error) {
    const box = $("#rename-project-error");
    box.textContent = error.message;
    box.classList.remove("hidden");
  } finally {
    submit.disabled = false;
  }
});

$("#delete-project-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const projectId = state.deletingProjectId;
  if (!projectId) return;
  const submit = event.target.querySelector('button[type="submit"]');
  submit.disabled = true;
  const isCurrent = state.project?.id === projectId;
  try {
    if (isCurrent) {
      await Promise.allSettled([
        ...state.pendingSaves,
        state.contextSavePromise,
        state.styleSavePromise,
      ].filter(Boolean));
      if (state.exactPreview.open) await discardExactPreview();
      const video = $("#video");
      video.pause();
      video.removeAttribute("src");
      video.load();
    }
    await api(`/api/projects/${projectId}`, { method: "DELETE" });
    closeDeleteProjectDialog();
    if (isCurrent) {
      state.project = null;
      state.segments = [];
      state.reviewIssues = [];
      state.reviewMode = false;
      state.selectedId = null;
      state.activeId = null;
      state.contextDirty = false;
      state.styleDirty = false;
      state.segmentSaveFailed = false;
      state.unsavedSegmentFields.clear();
      state.segmentTextSaves.clear();
      state.independentTimingBoundaries.clear();
      if (state.localRenderLockProjectId === projectId) state.localRenderLockProjectId = null;
      $("#subtitle-style-panel").classList.add("hidden");
      syncPolling();
    }
    await loadProjects();
    if (isCurrent) {
      if (state.projects.length) await openProject(state.projects[0].id);
      else {
        $("#project-view").classList.add("hidden");
        $("#welcome").classList.remove("hidden");
      }
    }
    showToast("Project and all its files deleted");
  } catch (error) {
    if (isCurrent && state.project?.id === projectId) renderProject();
    const box = $("#delete-project-error");
    box.textContent = error.message;
    box.classList.remove("hidden");
  } finally {
    submit.disabled = false;
  }
});

Promise.all([loadHealth(), loadCodexModels(), loadProjects()]).catch((error) => showToast(error.message));
