/*!
 * Corpus chat widget - drop-in "chat with your documents" for any website.
 *
 *   <script src="https://YOUR-API/widget.js"
 *           data-api-key="crp_..." data-collection="COLLECTION_ID" defer></script>
 *
 * Or place <corpus-chat api-url=".." api-key=".." collection=".."></corpus-chat> yourself
 * (add mode="inline" to render the panel in the page instead of a floating launcher).
 * Use a *widget* key: it is public by nature, so it only allows search and chat, on one
 * collection, from the origins it was issued for.
 *
 * No dependencies, no build step. Shadow DOM isolates styles both ways.
 */
(function () {
  "use strict";

  // ------------------------------------------------------------------ pure helpers (tested)
  /** Split a Server-Sent Events buffer into complete events and the unfinished remainder. */
  function parseSSE(buffer) {
    const events = [];
    const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
    const rest = blocks.pop();
    for (const block of blocks) {
      let event = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (data.length) {
        try {
          events.push({ event, data: JSON.parse(data.join("\n")) });
        } catch (_) {
          events.push({ event, data: data.join("\n") });
        }
      }
    }
    return { events, rest };
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  /** Escape first, then add the tiny bit of formatting we support: paragraphs, line breaks,
   *  **bold**, and [n] citations that point at a source actually shown to the model. */
  function renderAnswer(text, sourceCount) {
    const html = escapeHtml(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\[(\d{1,3})\]/g, (match, n) => {
        const i = Number(n);
        return i >= 1 && i <= sourceCount
          ? `<button type="button" class="cite" data-source="${i}" aria-label="Source ${i}">${i}</button>`
          : match;
      });
    return html
      .split(/\n{2,}/)
      .map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`)
      .join("");
  }

  function pages(source) {
    return source.page_start === source.page_end
      ? `p. ${source.page_start}`
      : `p. ${source.page_start}-${source.page_end}`;
  }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { parseSSE, escapeHtml, renderAnswer, pages };
  }
  if (typeof HTMLElement === "undefined" || typeof customElements === "undefined") return;
  if (customElements.get("corpus-chat")) return;

  // ------------------------------------------------------------------ styles
  const STYLES = `
    :host {
      --corpus-accent: #2b59c3;
      --corpus-bg: #ffffff;
      --corpus-surface: #f4f5f7;
      --corpus-text: #16181d;
      --corpus-muted: #5c6370;
      --corpus-border: #e1e4e8;
      --corpus-radius: 14px;
      --ease-out: cubic-bezier(0.23, 1, 0.32, 1);
      all: initial;
      font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
      color: var(--corpus-text);
    }
    @media (prefers-color-scheme: dark) {
      :host(:not([theme="light"])) {
        --corpus-bg: #17191e; --corpus-surface: #22252c; --corpus-text: #eceef2;
        --corpus-muted: #9aa1ad; --corpus-border: #2f333b; --corpus-accent: #7aa2ff;
      }
    }
    :host([theme="dark"]) {
      --corpus-bg: #17191e; --corpus-surface: #22252c; --corpus-text: #eceef2;
      --corpus-muted: #9aa1ad; --corpus-border: #2f333b; --corpus-accent: #7aa2ff;
    }
    * { box-sizing: border-box; }
    button { font: inherit; cursor: pointer; }

    .launcher {
      position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;
      width: 56px; height: 56px; border-radius: 50%; border: 0;
      background: var(--corpus-accent); color: #fff;
      box-shadow: 0 8px 24px rgb(0 0 0 / 0.18);
      display: grid; place-items: center;
      transition: transform 160ms var(--ease-out);
    }
    .launcher:active { transform: scale(0.94); }
    .launcher svg { width: 26px; height: 26px; }

    .panel {
      display: flex; flex-direction: column;
      background: var(--corpus-bg); color: var(--corpus-text);
      border: 1px solid var(--corpus-border); border-radius: var(--corpus-radius);
      overflow: hidden;
    }
    :host(:not([mode="inline"])) .panel {
      position: fixed; right: 20px; bottom: 88px; z-index: 2147483000;
      width: min(400px, calc(100vw - 24px)); height: min(600px, calc(100dvh - 120px));
      box-shadow: 0 16px 48px rgb(0 0 0 / 0.2);
      transform-origin: bottom right;
      transition: transform 200ms var(--ease-out), opacity 200ms var(--ease-out);
    }
    :host(:not([mode="inline"])) .panel[hidden] {
      display: flex; opacity: 0; transform: scale(0.96) translateY(8px); pointer-events: none;
      visibility: hidden;
    }
    :host([mode="inline"]) .panel { height: 100%; min-height: 420px; }
    @media (max-width: 480px) {
      :host(:not([mode="inline"])) .panel {
        left: 12px; right: 12px; width: auto; bottom: 84px; height: calc(100dvh - 108px);
      }
      .launcher { right: 16px; bottom: max(16px, env(safe-area-inset-bottom)); }
    }

    header {
      display: flex; align-items: center; gap: 10px;
      padding: 14px 16px; border-bottom: 1px solid var(--corpus-border);
    }
    header .title { font-weight: 600; flex: 1; }
    header .close {
      border: 0; background: none; color: var(--corpus-muted); padding: 6px; border-radius: 8px;
      display: grid; place-items: center;
    }
    :host([mode="inline"]) header .close { display: none; }

    .log { flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 14px; }
    .empty { color: var(--corpus-muted); margin: auto 0; text-align: center; }
    .suggestions { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 12px; }
    .suggestions button {
      border: 1px solid var(--corpus-border); background: var(--corpus-bg); color: var(--corpus-text);
      border-radius: 16px; padding: 6px 12px; font-size: 13px; text-align: center;
      transition: transform 160ms var(--ease-out), border-color 160ms ease;
    }
    .suggestions button:active { transform: scale(0.97); }

    .msg { max-width: 88%; padding: 10px 14px; border-radius: 12px; overflow-wrap: anywhere; }
    .msg p { margin: 0 0 8px; } .msg p:last-child { margin: 0; }
    .user { align-self: flex-end; background: var(--corpus-accent); color: #fff; border-bottom-right-radius: 4px; }
    .assistant { align-self: flex-start; background: var(--corpus-surface); border-bottom-left-radius: 4px; }
    .assistant.pending::after {
      content: ""; display: inline-block; width: 6px; height: 6px; margin-left: 4px;
      border-radius: 50%; background: var(--corpus-muted); animation: pulse 900ms ease-in-out infinite;
    }
    .error {
      align-self: stretch; color: #d92d20;
      background: color-mix(in srgb, #f04438 10%, transparent);
      border: 1px solid color-mix(in srgb, #f04438 28%, transparent);
    }
    @keyframes pulse { 50% { opacity: 0.25; } }

    .cite {
      border: 0; border-radius: 6px; padding: 0 5px; margin: 0 1px;
      font-size: 11px; font-weight: 600; line-height: 18px; vertical-align: 1px;
      background: color-mix(in srgb, var(--corpus-accent) 16%, transparent); color: var(--corpus-accent);
      transition: transform 160ms var(--ease-out);
    }
    .cite:active { transform: scale(0.94); }
    .sources { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .sources button {
      border: 1px solid var(--corpus-border); background: var(--corpus-bg); color: var(--corpus-muted);
      border-radius: 8px; padding: 3px 8px; font-size: 12px; max-width: 100%;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    }

    .excerpt {
      margin: 0 16px 12px; padding: 12px 14px; border-radius: 10px; font-size: 13px;
      background: var(--corpus-surface); border: 1px solid var(--corpus-border);
      max-height: 40%; overflow-y: auto;
    }
    .excerpt header { padding: 0 0 6px; border: 0; font-weight: 600; font-size: 13px; gap: 6px; }
    .excerpt header .close { padding: 2px; }
    .excerpt blockquote { margin: 0; color: var(--corpus-muted); white-space: pre-wrap; }

    form { display: flex; gap: 8px; padding: 12px; border-top: 1px solid var(--corpus-border); }
    textarea {
      flex: 1; resize: none; border: 1px solid var(--corpus-border); border-radius: 10px;
      padding: 10px 12px; font: inherit; font-size: 16px; /* 16px: no zoom on iOS */
      background: var(--corpus-bg); color: var(--corpus-text); max-height: 120px;
    }
    textarea:focus-visible, button:focus-visible { outline: 2px solid var(--corpus-accent); outline-offset: 2px; }
    .send {
      border: 0; border-radius: 10px; padding: 0 14px; background: var(--corpus-accent); color: #fff;
      font-weight: 600; transition: transform 160ms var(--ease-out), opacity 160ms ease;
    }
    .send:active { transform: scale(0.97); }
    .send:disabled { opacity: 0.5; cursor: default; }
    .footer { text-align: center; font-size: 11px; color: var(--corpus-muted); padding: 0 0 8px; }

    @media (hover: hover) and (pointer: fine) {
      .suggestions button:hover, .sources button:hover { border-color: var(--corpus-accent); }
      header .close:hover { background: var(--corpus-surface); }
    }
    @media (prefers-reduced-motion: reduce) {
      .panel, .launcher, .send, .cite { transition: opacity 150ms ease !important; transform: none !important; }
      .assistant.pending::after { animation: none; }
    }
  `;

  const ICON_CHAT = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>`;
  const ICON_CLOSE = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>`;

  // ------------------------------------------------------------------ component
  class CorpusChat extends HTMLElement {
    static get observedAttributes() {
      return ["heading", "placeholder", "accent"];
    }

    constructor() {
      super();
      this.history = [];
      this.busy = false;
      this.root = this.attachShadow({ mode: "open" });
    }

    get apiUrl() {
      return (this.getAttribute("api-url") || "").replace(/\/$/, "");
    }

    connectedCallback() {
      if (this.root.childElementCount) return;
      const inline = this.getAttribute("mode") === "inline";
      const title = this.getAttribute("heading") || "Ask the documentation";
      this.root.innerHTML = `
        <style>${STYLES}</style>
        ${inline ? "" : `<button class="launcher" type="button" aria-label="Open chat" aria-expanded="false">${ICON_CHAT}</button>`}
        <section class="panel" role="dialog" aria-label="${escapeHtml(title)}" ${inline ? "" : "hidden"}>
          <header><span class="title">${escapeHtml(title)}</span>
            <button class="close" type="button" aria-label="Close chat">${ICON_CLOSE}</button></header>
          <div class="log" aria-live="polite">
            <div class="empty"><div>Answers come from the documents, with their sources.</div>
              <div class="suggestions"></div></div>
          </div>
          <div class="excerpt" hidden></div>
          <form>
            <textarea rows="1" maxlength="4000" aria-label="Your question"
              placeholder="${escapeHtml(this.getAttribute("placeholder") || "Ask a question...")}"></textarea>
            <button class="send" type="submit">Send</button>
          </form>
          <div class="footer">Answers can contain mistakes: check the sources.</div>
        </section>`;
      this.$ = (sel) => this.root.querySelector(sel);
      this.applyAccent();
      this.renderSuggestions();

      const launcher = this.$(".launcher");
      if (launcher) launcher.addEventListener("click", () => this.toggle());
      this.$(".close").addEventListener("click", () => this.toggle(false));
      this.$("form").addEventListener("submit", (e) => {
        e.preventDefault();
        this.ask(this.$("textarea").value);
      });
      this.$("textarea").addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
          e.preventDefault();
          this.ask(e.target.value);
        }
      });
      this.$("textarea").addEventListener("input", (e) => {
        e.target.style.height = "auto";
        e.target.style.height = `${e.target.scrollHeight}px`;
      });
      this.root.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !inline) this.toggle(false);
      });
      // Citations and source chips are created dynamically: one delegated listener.
      this.$(".log").addEventListener("click", (e) => {
        const target = e.target.closest("[data-source]");
        if (target) this.showSource(target.closest(".msg"), Number(target.dataset.source));
      });
    }

    attributeChangedCallback() {
      if (this.root.childElementCount) this.applyAccent();
    }

    applyAccent() {
      const accent = this.getAttribute("accent");
      if (accent) this.style.setProperty("--corpus-accent", accent);
    }

    renderSuggestions() {
      const box = this.$(".suggestions");
      const raw = this.getAttribute("suggestions");
      if (!box || !raw) return;
      for (const text of raw.split("|").map((s) => s.trim()).filter(Boolean).slice(0, 4)) {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = text;
        b.addEventListener("click", () => this.ask(text));
        box.append(b);
      }
    }

    toggle(open) {
      const panel = this.$(".panel");
      const launcher = this.$(".launcher");
      const show = open === undefined ? panel.hidden : open;
      panel.hidden = !show;
      if (launcher) launcher.setAttribute("aria-expanded", String(show));
      if (show) this.$("textarea").focus();
      else if (launcher) launcher.focus();
    }

    append(cls, html) {
      const empty = this.$(".empty");
      if (empty) empty.remove();
      const el = document.createElement("div");
      el.className = `msg ${cls}`;
      el.innerHTML = html;
      this.$(".log").append(el);
      el.scrollIntoView({ block: "end" });
      return el;
    }

    async ask(rawQuestion) {
      const question = rawQuestion.trim();
      if (!question || this.busy) return;
      const collection = this.getAttribute("collection");
      const key = this.getAttribute("api-key");
      if (!collection || !key) {
        this.append("error", "This widget is missing its <code>collection</code> or <code>api-key</code>.");
        return;
      }
      this.busy = true;
      this.$(".send").disabled = true;
      this.$("textarea").value = "";
      this.$("textarea").style.height = "auto";
      this.$(".excerpt").hidden = true;
      this.append("user", escapeHtml(question));
      const bubble = this.append("assistant pending", "");
      const state = { text: "", sources: [], cited: [] };
      bubble.state = state;

      try {
        const response = await fetch(`${this.apiUrl}/v1/collections/${encodeURIComponent(collection)}/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-API-Key": key },
          body: JSON.stringify({ question, history: this.history.slice(-10) }),
        });
        if (!response.ok) throw new Error(await this.describeError(response));

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const parsed = parseSSE(buffer);
          buffer = parsed.rest;
          for (const { event, data } of parsed.events) this.onEvent(bubble, state, event, data);
        }
        if (state.text) {
          this.history.push({ role: "user", content: question }, { role: "assistant", content: state.text });
        }
      } catch (err) {
        bubble.remove();
        this.append("error", escapeHtml(err.message || "Something went wrong."));
      } finally {
        bubble.classList.remove("pending");
        this.busy = false;
        this.$(".send").disabled = false;
      }
    }

    onEvent(bubble, state, event, data) {
      if (event === "sources") state.sources = data;
      else if (event === "token") {
        state.text += data.text;
        bubble.innerHTML = renderAnswer(state.text, state.sources.length);
        bubble.scrollIntoView({ block: "end" });
      } else if (event === "done") {
        state.cited = data.cited || [];
        this.renderSources(bubble, state);
      } else if (event === "error") {
        throw new Error(data.code === "refusal" ? "This question cannot be answered." : "The answer could not be generated.");
      }
    }

    renderSources(bubble, state) {
      if (!state.cited.length) return;
      const list = document.createElement("div");
      list.className = "sources";
      for (const n of state.cited) {
        const s = state.sources[n - 1];
        if (!s) continue;
        const b = document.createElement("button");
        b.type = "button";
        b.dataset.source = String(n);
        b.title = `${s.filename}, ${pages(s)}`;
        b.textContent = `[${n}] ${s.filename} · ${pages(s)}`;
        list.append(b);
      }
      bubble.append(list);
    }

    showSource(bubble, n) {
      const source = bubble && bubble.state && bubble.state.sources[n - 1];
      const box = this.$(".excerpt");
      if (!source) return;
      box.innerHTML = `<header><span class="title">[${n}] ${escapeHtml(source.filename)} · ${pages(source)}</span>
          <button class="close" type="button" aria-label="Close excerpt">${ICON_CLOSE}</button></header>
        <blockquote>${escapeHtml(source.content)}</blockquote>`;
      box.querySelector(".close").addEventListener("click", () => {
        box.hidden = true;
      });
      box.hidden = false;
    }

    async describeError(response) {
      if (response.status === 429) {
        return `Too many questions right now. Try again in ${response.headers.get("Retry-After") || "a few"} seconds.`;
      }
      if (response.status === 401 || response.status === 403) return "This widget is not authorised for this site.";
      if (response.status === 503) return "The service is busy. Please try again in a moment.";
      return `Unexpected error (${response.status}).`;
    }
  }

  customElements.define("corpus-chat", CorpusChat);

  // ------------------------------------------------------------------ script-tag auto-mount
  const script = document.currentScript;
  if (script && script.dataset.collection && script.dataset.apiKey) {
    const mount = () => {
      const el = document.createElement("corpus-chat");
      el.setAttribute("api-url", script.dataset.apiUrl || new URL(script.src).origin);
      el.setAttribute("api-key", script.dataset.apiKey);
      el.setAttribute("collection", script.dataset.collection);
      for (const attr of ["heading", "placeholder", "accent", "theme", "suggestions"]) {
        if (script.dataset[attr]) el.setAttribute(attr, script.dataset[attr]);
      }
      document.body.append(el);
    };
    if (document.body) mount();
    else document.addEventListener("DOMContentLoaded", mount);
  }
})();
