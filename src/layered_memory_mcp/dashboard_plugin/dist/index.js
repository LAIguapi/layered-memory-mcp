/**
 * 分层记忆框架 Dashboard Plugin
 *
 * 展示 L0/L1/L2/L3 四层记忆架构的设计思想与工作流程。
 * 3 个 Tab: 记忆地图 / 知识生命周期 / 健康诊断
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React } = SDK;
  const h = React.createElement;
  const {
    Card, CardContent, CardHeader, CardTitle,
    Badge, Input, Label, Select, SelectOption,
    Separator, Tabs, TabsList, TabsTrigger,
  } = SDK.components;
  const { useState, useEffect, useCallback } = SDK.hooks;
  const { cn } = SDK.utils;
  const { fetchJSON } = SDK;

  const API_BASE = "/api/plugins/layered-memory-dashboard";

  // ── postJSON: POST with proper Content-Type so FastAPI parses the body as JSON ──
  function postJSON(endpoint, payload) {
    return fetchJSON(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {}),
    });
  }


  // ── Button (native, theme-token only — never SDK Button which injects bg-midground) ──
  function Button(props) {
    const { variant, className, children, ...rest } = props || {};
    const base = "inline-flex items-center justify-center rounded-md px-3 py-1.5 text-sm font-medium transition-colors focus:outline-none disabled:opacity-50 disabled:pointer-events-none cursor-pointer";
    // variant only sets defaults; explicit className wins via cn() ordering
    const variantCls =
      variant === "ghost"
        ? "bg-transparent text-text-secondary hover:bg-surface-hover hover:text-text-primary"
        : "border border-border bg-transparent text-text-secondary hover:bg-surface-hover hover:text-text-primary";
    return h("button", Object.assign({ className: cn(base, variantCls, className) }, rest), children);
  }


  // ── Icons ───────────────────────────────────────────────────────────
  const Icons = {
    Layers: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("polygon", { points: "12 2 2 7 12 12 22 7 12 2" }),
      h("polyline", { points: "2 17 12 22 22 17" }),
      h("polyline", { points: "2 12 12 17 22 12" })
    ),
    Map: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("polygon", { points: "1 6 1 22 8 18 16 22 21 18 21 2 16 6 8 2 1 6" }),
      h("line", { x1: 8, y1: 2, x2: 8, y2: 18 }),
      h("line", { x1: 16, y1: 6, x2: 16, y2: 22 })
    ),
    RefreshCw: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("path", { d: "M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" }),
      h("path", { d: "M3 3v5h5" }),
      h("path", { d: "M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16" }),
      h("path", { d: "M16 16h5v5" })
    ),
    Search: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("circle", { cx: 11, cy: 11, r: 8 }),
      h("path", { d: "m21 21-4.3-4.3" })
    ),
    FileText: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("path", { d: "M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z" }),
      h("polyline", { points: "14 2 14 8 20 8" })
    ),
    Check: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("path", { d: "M20 6 9 17l-5-5" })
    ),
    X: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("path", { d: "M18 6 6 18" }),
      h("path", { d: "m6 6 12 12" })
    ),
    AlertTriangle: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("path", { d: "m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" }),
      h("path", { d: "M12 9v4" }),
      h("path", { d: "M12 17h.01" })
    ),
    HeartPulse: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("path", { d: "M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z" }),
      h("path", { d: "M3.22 12H9.5l.5-1 2 4.5 2-3 1.5 2.5h5.27" })
    ),
    ListTodo: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("rect", { x: 3, y: 5, width: 6, height: 6, rx: 1 }),
      h("path", { d: "m3 17 2 2 4-4" }),
      h("path", { d: "M13 6h8" }),
      h("path", { d: "M13 12h8" }),
      h("path", { d: "M13 18h8" })
    ),
    Compass: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("circle", { cx: 12, cy: 12, r: 10 }),
      h("polygon", { points: "16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76" })
    ),
    Database: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("ellipse", { cx: 12, cy: 5, rx: 9, ry: 3 }),
      h("path", { d: "M3 5V19A9 3 0 0 0 21 19V5" }),
      h("path", { d: "M3 12A9 3 0 0 0 21 12" })
    ),
    Cpu: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("rect", { x: 4, y: 4, width: 16, height: 16, rx: 2 }),
      h("rect", { x: 9, y: 9, width: 6, height: 6 }),
      h("path", { d: "M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2" })
    ),
    Archive: () => h("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("rect", { x: 2, y: 3, width: 20, height: 5, rx: 1 }),
      h("path", { d: "M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" }),
      h("path", { d: "M10 12h4" })
    ),
    ArrowDown: () => h("svg", { width: 20, height: 20, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("path", { d: "M12 5v14" }),
      h("path", { d: "m19 12-7 7-7-7" })
    ),
    ExternalLink: () => h("svg", { width: 14, height: 14, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: 2 },
      h("path", { d: "M15 3h6v6" }),
      h("path", { d: "M10 14 21 3" }),
      h("path", { d: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" })
    ),
  };

  // ── Markdown renderer (simple, theme-aware) ─────────────────────────
  function renderMarkdown(content) {
    if (!content) return h("div", { className: "text-text-tertiary italic" }, "No content");

    let html = content
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/^#{1,6}\s+(.+)$/gm, '<h3 class="text-lg font-semibold mt-4 mb-2 text-text-primary">$1</h3>')
      .replace(/\*\*(.+?)\*\*/g, '<strong class="text-text-primary">$1</strong>')
      .replace(/\*(.+?)\*/g, '<em class="text-text-secondary">$1</em>')
      .replace(/```[\s\S]*?```/g, (m) => {
        const code = m.slice(3, -3).replace(/</g, "&lt;").replace(/>/g, "&gt;");
        return `<pre class="bg-surface p-3 rounded text-sm overflow-x-auto my-2 text-text-secondary font-mono">${code}</pre>`;
      })
      .replace(/`([^`]+)`/g, '<code class="bg-surface px-1 py-0.5 rounded text-sm text-text-secondary font-mono">$1</code>')
      .replace(/^>\s+(.+)$/gm, '<blockquote class="border-l-2 border-border pl-3 my-2 text-text-tertiary italic">$1</blockquote>')
      .replace(/^[-*]\s+(.+)$/gm, '<li class="ml-4 text-text-secondary">$1</li>')
      .replace(/\n\n+/g, '</p><p class="text-text-secondary my-2">')
      .replace(/\n/g, '<br>');

    return h("div", {
      className: "prose break-words",
      dangerouslySetInnerHTML: { __html: `<p class="text-text-secondary my-2">${html}</p>` }
    });
  }

  // ── useAsyncData hook ─────────────────────────────────────────────────
  function useAsyncData(fetchFn, deps) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState(null);

    useEffect(() => {
      let cancelled = false;
      setLoading(true);
      setError(null);
      fetchFn().then(result => {
        if (!cancelled) { setData(result); setLoading(false); }
      }).catch(err => {
        if (!cancelled) { setError(err.message || String(err)); setLoading(false); }
      });
      return () => { cancelled = true; };
    }, deps || []);

    return { data, loading, error, refetch: () => setData(null) };
  }

  // ── Loading / Error helpers ───────────────────────────────────────────
  function LoadingSpinner() {
    return h("div", { className: "flex items-center justify-center p-8 text-text-secondary" },
      h("div", { className: "animate-spin mr-2" }, h(Icons.RefreshCw)),
      "Loading..."
    );
  }

  function ErrorBox({ message }) {
    return h("div", { className: "bg-red-500/10 border border-red-500/20 rounded-lg p-4 text-red-400" },
      h("div", { className: "flex items-center gap-2 font-semibold mb-1" },
        h(Icons.AlertTriangle), "Error"
      ),
      message
    );
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Tab 0: Architecture Overview (design philosophy)
  // ═══════════════════════════════════════════════════════════════════════
  function ArchitectureOverviewTab() {
    // one layer card in the L0→L1→L2→L3 flow
    function LayerCard(opts) {
      return h("div", {
        className: cn(
          "rounded-lg border p-4 transition-colors",
          opts.accent
        )
      },
        h("div", { className: "flex items-center gap-3" },
          h("div", { className: "flex items-center justify-center w-9 h-9 rounded-md bg-background/40 shrink-0" }, opts.icon),
          h("div", { className: "min-w-0 flex-1" },
            h("div", { className: "flex items-center gap-2 flex-wrap" },
              h("span", { className: "font-bold text-text-primary" }, opts.tag),
              h("span", { className: "text-sm text-text-secondary" }, opts.title),
              opts.badge && h("span", {
                className: "text-[10px] px-1.5 py-0.5 rounded bg-background/50 text-text-tertiary border border-border"
              }, opts.badge)
            ),
            h("div", { className: "text-xs text-text-tertiary mt-1 leading-relaxed" }, opts.desc),
            opts.store && h("div", { className: "text-[11px] text-text-tertiary mt-1 font-mono opacity-70" }, opts.store),
            opts.link && h("a", {
              href: opts.link,
              onClick: (e) => { e.preventDefault(); window.location.href = opts.link; },
              className: "inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300 mt-2 cursor-pointer"
            }, opts.linkLabel, h(Icons.ExternalLink))
          )
        )
      );
    }

    const arrow = h("div", { className: "flex justify-center py-1 text-text-tertiary" }, h(Icons.ArrowDown));

    return h("div", { className: "max-w-3xl mx-auto" },
      // philosophy header
      h("div", { className: "mb-5" },
        h("h2", { className: "text-lg font-semibold text-text-primary flex items-center gap-2" },
          h(Icons.Compass), "设计哲学：分层记忆，按需加载"
        ),
        h("p", { className: "text-sm text-text-secondary mt-2 leading-relaxed" },
          "把知识按「访问频率」与「抽象层级」分成四层，每轮对话只把最轻的 L0 索引常驻上下文，更重的内容按需逐层拉取。这样既让 Agent 始终\"知道有什么\"，又不会用大量细节撑爆 token 预算。"
        )
      ),

      // the four-layer flow
      h("div", { className: "space-y-0" },
        LayerCard({
          tag: "L0", title: "目录索引层",
          badge: "每轮常驻",
          icon: h(Icons.Compass),
          accent: "bg-blue-500/5 border-blue-500/30",
          desc: "一行一个领域的摘要指针，告诉 Agent「有哪些知识、在哪个文件」。体积极小，每轮注入上下文，是整张知识地图的入口。",
          store: "~/.layered-memory/L0.md",
        }),
        arrow,
        LayerCard({
          tag: "L1", title: "知识内容层",
          badge: "按需检索",
          icon: h(Icons.Database),
          accent: "bg-emerald-500/5 border-emerald-500/30",
          desc: "领域知识正文，按 ## 分区。命中 L0 后用 recall_knowledge / 语义搜索按需拉取。写入时自动三写：L1 正文 + L0 索引 + 向量库，三者永不脱节。",
          store: "~/.layered-memory/knowledge/*.md",
        }),
        arrow,
        LayerCard({
          tag: "L2", title: "技能方法论层",
          badge: "Hermes 托管",
          icon: h(Icons.Cpu),
          accent: "bg-purple-500/5 border-purple-500/30",
          desc: "可复用的操作流程与方法论（skills）。由 Hermes 原生管理，用 skill_view 按需加载。本框架不重复造轮子，直接复用 Hermes 技能系统。",
          link: "/skills", linkLabel: "在 Hermes 面板查看 SKILLS",
        }),
        arrow,
        LayerCard({
          tag: "L3", title: "原始会话层",
          badge: "Hermes 托管",
          icon: h(Icons.Archive),
          accent: "bg-amber-500/5 border-amber-500/30",
          desc: "完整历史对话，是所有上层知识的源头。需要回溯细节时用 session_search 检索。同样由 Hermes 原生提供，本框架直接复用。",
          link: "/sessions", linkLabel: "在 Hermes 面板查看 SESSIONS",
        })
      ),

      // knowledge lifecycle strip
      h("div", { className: "mt-6 rounded-lg border border-border bg-surface p-4" },
        h("div", { className: "font-semibold text-text-primary flex items-center gap-2 mb-3" },
          h(Icons.RefreshCw), "知识全生命周期"
        ),
        h("div", { className: "flex items-center flex-wrap gap-2 text-xs" },
          ["提取 extract", "审核 review", "注入 inject", "压缩 compact", "防腐 audit"].map((s, i, arr) =>
            h("span", { key: s, className: "flex items-center gap-2" },
              h("span", { className: "px-2 py-1 rounded bg-background border border-border text-text-secondary" }, s),
              i < arr.length - 1 && h("span", { className: "text-text-tertiary" }, "→")
            )
          )
        ),
        h("p", { className: "text-xs text-text-tertiary mt-3 leading-relaxed" },
          "知识从会话中被提取，经审核后注入 L1；冗余内容定期压缩下沉，并通过防腐体检（audit_rot）检测衰退。在「知识生命周期」与「健康诊断」页可操作其中的各环节。"
        )
      ),

      // design principles
      h("div", { className: "mt-4 grid grid-cols-1 md:grid-cols-3 gap-3" },
        h("div", { className: "rounded-lg border border-border bg-surface p-3" },
          h("div", { className: "text-sm font-medium text-text-primary mb-1" }, "省 token"),
          h("div", { className: "text-xs text-text-tertiary leading-relaxed" }, "只有 L0 常驻上下文，细节按需拉取，避免全量知识撑爆预算。")
        ),
        h("div", { className: "rounded-lg border border-border bg-surface p-3" },
          h("div", { className: "text-sm font-medium text-text-primary mb-1" }, "三写一致"),
          h("div", { className: "text-xs text-text-tertiary leading-relaxed" }, "写入自动同步 L1+L0+向量库，删除自动级联清理，框架自负副作用。")
        ),
        h("div", { className: "rounded-lg border border-border bg-surface p-3" },
          h("div", { className: "text-sm font-medium text-text-primary mb-1" }, "职责分明"),
          h("div", { className: "text-xs text-text-tertiary leading-relaxed" }, "L0/L1 由本框架管理，L2/L3 复用 Hermes 原生系统，不重复造轮子。")
        )
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Tab 1: Memory Map
  // ═══════════════════════════════════════════════════════════════════════
  function MemoryMapTab() {
    const [selectedFile, setSelectedFile] = useState(null);
    const [searchQuery, setSearchQuery] = useState("");
    const [searchResults, setSearchResults] = useState(null);
    const [searching, setSearching] = useState(false);

    const l0Data = useAsyncData(() => fetchJSON(`${API_BASE}/l0-index`), []);
    const fileData = useAsyncData(() => {
      if (!selectedFile) return Promise.resolve(null);
      return fetchJSON(`${API_BASE}/knowledge-file/${encodeURIComponent(selectedFile)}`);
    }, [selectedFile]);

    const handleSearch = useCallback(() => {
      if (!searchQuery.trim()) { setSearchResults(null); return; }
      setSearching(true);
      postJSON(`${API_BASE}/semantic-search`, { query: searchQuery, top_n: 5 })
        .then(r => { setSearchResults(r); setSearching(false); })
        .catch(e => { setSearchResults({ error: e.message }); setSearching(false); });
    }, [searchQuery]);

    return h("div", { className: "flex gap-4 h-full" },
      // Left: L0 Index Tree
      h("div", { className: "w-1/3 min-w-[280px] flex flex-col" },
        h("div", { className: "mb-3" },
          h("div", { className: "flex items-center gap-2 mb-2" },
            h(Icons.Map), h("h2", { className: "text-lg font-semibold text-text-primary" }, "L0 知识索引")
          ),
          h("div", { className: "flex gap-2" },
            h(Input, {
              placeholder: "语义搜索...",
              value: searchQuery,
              onChange: e => setSearchQuery(e.target.value),
              className: "flex-1 bg-surface border-border text-text-primary",
              onKeyDown: e => e.key === "Enter" && handleSearch()
            }),
            h(Button, {
              variant: "outline",
              className: "border-border text-text-secondary hover:text-text-primary hover:bg-surface-hover",
              onClick: handleSearch,
              disabled: searching
            }, searching ? "..." : h(Icons.Search))
          )
        ),

        // Search results overlay
        searchResults && h("div", { className: "mb-3 bg-surface border border-border rounded-lg p-3" },
          searchResults.error
            ? h(ErrorBox, { message: searchResults.error })
            : h("div", null,
                h("div", { className: "text-sm text-text-tertiary mb-2" },
                  `搜索结果: ${searchResults.total || 0} 条`
                ),
                (searchResults.results || []).map(r =>
                  h("div", {
                    key: r.id,
                    className: "p-2 rounded cursor-pointer hover:bg-surface-hover border-b border-border last:border-0",
                    onClick: () => { setSelectedFile(r.domain + ".md"); setSearchResults(null); }
                  },
                    h("div", { className: "font-medium text-text-primary" }, r.domain),
                    h("div", { className: "text-sm text-text-secondary truncate" }, r.summary),
                    h("div", { className: "text-xs text-text-tertiary" }, `score: ${r.score}`)
                  )
                )
              )
        ),

        // L0 Entries list
        h("div", { className: "flex-1 overflow-auto border border-border rounded-lg bg-surface" },
          l0Data.loading ? h(LoadingSpinner)
            : l0Data.error ? h(ErrorBox, { message: l0Data.error })
            : !(l0Data.data && l0Data.data.entries) ? h("div", { className: "p-4 text-text-tertiary" }, "No L0 index found")
            : h("div", null,
                h("div", { className: "p-2 text-xs text-text-tertiary border-b border-border" },
                  `${l0Data.data.total} domains`
                ),
                (l0Data.data.entries || []).map(entry => {
                  const fname = entry.filename || (entry.domain + ".md");
                  return h("div", {
                    key: fname,
                    className: cn(
                      "p-2 cursor-pointer border-b border-border hover:bg-surface-hover transition-colors",
                      selectedFile === fname && "bg-surface-hover border-l-2 border-l-blue-500"
                    ),
                    onClick: () => setSelectedFile(fname)
                  },
                    h("div", { className: "font-medium text-text-primary text-sm" }, entry.domain),
                    h("div", { className: "text-xs text-text-tertiary truncate" }, entry.summary)
                  );
                })
              )
        )
      ),

      // Right: File Preview
      h("div", { className: "flex-1 flex flex-col min-w-0" },
        !selectedFile
          ? h("div", { className: "flex-1 flex items-center justify-center text-text-tertiary" },
              h("div", { className: "text-center" },
                h(Icons.FileText, { className: "mx-auto mb-2 opacity-50" }),
                "Select a domain from the L0 index to view its L1 knowledge file"
              )
            )
          : h("div", { className: "flex-1 flex flex-col min-w-0" },
              h("div", { className: "flex items-center justify-between mb-2 pb-2 border-b border-border" },
                h("div", { className: "flex items-center gap-2" },
                  h(Icons.FileText),
                  h("span", { className: "font-semibold text-text-primary" }, selectedFile)
                ),
                fileData.data && h("span", { className: "text-xs text-text-tertiary" },
                  `${fileData.data.size} bytes · ${new Date(fileData.data.mtime).toLocaleString()}`
                )
              ),
              h("div", { className: "flex-1 overflow-auto bg-surface border border-border rounded-lg p-4" },
                fileData.loading ? h(LoadingSpinner)
                  : fileData.error ? h(ErrorBox, { message: fileData.error })
                  : renderMarkdown(fileData.data && fileData.data.content)
              )
            )
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Tab 2: Knowledge Lifecycle
  // ═══════════════════════════════════════════════════════════════════════
  function KnowledgeLifecycleTab() {
    const [activeSubTab, setActiveSubTab] = useState("reviews");
    const [preview, setPreview] = useState(null);
    const [executing, setExecuting] = useState(false);

    // Inject form state
    const [injectDomain, setInjectDomain] = useState("");
    const [injectSection, setInjectSection] = useState("");
    const [injectContent, setInjectContent] = useState("");

    const reviewsData = useAsyncData(() => fetchJSON(`${API_BASE}/pending-reviews`), []);

    const handleDryRun = useCallback((endpoint, body) => {
      setExecuting(true);
      setPreview(null);
      postJSON(`${API_BASE}/${endpoint}`, { ...body, dry_run: true })
        .then(r => {
        setPreview({ type: "preview", data: r });
        setExecuting(false);
      }).catch(e => {
        setPreview({ type: "error", message: e.message });
        setExecuting(false);
      });
    }, []);

    const handleExecute = useCallback((endpoint, body) => {
      setExecuting(true);
      postJSON(`${API_BASE}/${endpoint}`, { ...body, dry_run: false })
        .then(r => {
        setPreview({ type: "result", data: r });
        setExecuting(false);
        // Refresh data
        if (endpoint === "approve-knowledge" || endpoint === "reject-knowledge") {
          reviewsData.refetch();
        }
      }).catch(e => {
        setPreview({ type: "error", message: e.message });
        setExecuting(false);
      });
    }, [reviewsData]);

    return h("div", { className: "flex flex-col gap-4" },
      // Sub-tab navigation
      h("div", { className: "flex gap-2 border-b border-border pb-2" },
        ["reviews", "inject", "compact"].map(tab =>
          h(Button, {
            key: tab,
            variant: activeSubTab === tab ? "outline" : "ghost",
            className: cn(
              "border-border text-text-secondary hover:text-text-primary hover:bg-surface-hover",
              activeSubTab === tab && "bg-surface text-text-primary"
            ),
            onClick: () => { setActiveSubTab(tab); setPreview(null); }
          },
            tab === "reviews" && "待审核队列",
            tab === "inject" && "知识注入",
            tab === "compact" && "记忆压缩"
          )
        )
      ),

      // Preview panel
      preview && h("div", { className: "bg-surface border border-border rounded-lg p-4" },
        preview.type === "error" && h(ErrorBox, { message: preview.message }),
        preview.type === "preview" && h("div", null,
          h("div", { className: "flex items-center gap-2 text-text-secondary mb-2" },
            h(Icons.AlertTriangle), "Preview — 确认后执行"
          ),
          h("pre", { className: "bg-background p-3 rounded text-sm text-text-secondary overflow-auto max-h-40" },
            JSON.stringify(preview.data, null, 2)
          ),
          h("div", { className: "flex gap-2 mt-3" },
            h(Button, {
              variant: "outline",
              className: "border-border text-text-secondary hover:text-text-primary hover:bg-surface-hover",
              onClick: () => setPreview(null)
            }, "Cancel"),
            h(Button, {
              variant: "outline",
              className: "border-emerald-500/50 text-emerald-400 hover:bg-emerald-500/10",
              onClick: () => {
                const endpoint = preview.data.action === "approve" ? "approve-knowledge"
                  : preview.data.action === "reject" ? "reject-knowledge"
                  : preview.data.domain ? "inject-knowledge"
                  : "compact-memory";
                const body = preview.data.entry_id ? { entry_id: preview.data.entry_id }
                  : preview.data.domain ? { domain: preview.data.domain, section: preview.data.section, content: injectContent }
                  : {};
                handleExecute(endpoint, body);
              },
              disabled: executing
            }, executing ? "Executing..." : "Confirm Execute")
          )
        ),
        preview.type === "result" && h("div", null,
          h("div", { className: "flex items-center gap-2 text-emerald-400 mb-2" },
            h(Icons.Check), "Executed successfully"
          ),
          h("pre", { className: "bg-background p-3 rounded text-sm text-text-secondary overflow-auto max-h-40" },
            JSON.stringify(preview.data, null, 2)
          )
        )
      ),

      // Reviews sub-tab
      activeSubTab === "reviews" && h("div", null,
        h("div", { className: "text-xs text-text-tertiary bg-background border border-border rounded-md p-3 mb-3 leading-relaxed" },
          "此队列仅显示由 ", h("span", { className: "font-mono text-text-secondary" }, "extract_session_knowledge"),
          " 从历史会话自动提取、且置信度低于阈值的候选知识，需人工审核后才入库。手动用「知识注入」写入的知识不经过此队列，因此通常为空属正常。"
        ),
        reviewsData.loading ? h(LoadingSpinner)
          : reviewsData.error ? h(ErrorBox, { message: reviewsData.error })
          : !(reviewsData.data && reviewsData.data.items && reviewsData.data.items.length)
            ? h("div", { className: "text-center p-8 text-text-tertiary" }, "No pending reviews")
            : h("div", { className: "grid gap-3" },
                (reviewsData.data.items || []).map(item =>
                  h(Card, { key: item.id, className: "bg-surface border-border" },
                    h(CardHeader, { className: "pb-2" },
                      h(CardTitle, { className: "text-sm flex items-center justify-between" },
                        h("span", null, `${item.domain} · ${item.section}`),
                        h(Badge, { className: "bg-amber-500/10 text-amber-400 border-amber-500/20" },
                          `confidence: ${item.confidence}`
                        )
                      )
                    ),
                    h(CardContent, null,
                      h("div", { className: "text-sm text-text-secondary mb-3" }, item.summary),
                      h("div", { className: "flex gap-2" },
                        h(Button, {
                          variant: "outline",
                          className: "border-emerald-500/50 text-emerald-400 hover:bg-emerald-500/10 text-xs",
                          onClick: () => handleDryRun("approve-knowledge", { entry_id: item.id }),
                          disabled: executing
                        }, h(Icons.Check, { className: "mr-1" }), "Approve"),
                        h(Button, {
                          variant: "outline",
                          className: "border-red-500/50 text-red-400 hover:bg-red-500/10 text-xs",
                          onClick: () => handleDryRun("reject-knowledge", { entry_id: item.id }),
                          disabled: executing
                        }, h(Icons.X, { className: "mr-1" }), "Reject")
                      )
                    )
                  )
                )
              )
      ),

      // Inject sub-tab
      activeSubTab === "inject" && h("div", { className: "bg-surface border border-border rounded-lg p-4" },
        h("div", { className: "text-xs text-text-tertiary bg-background border border-border rounded-md p-3 mb-3 leading-relaxed" },
          "注入将一次写入三处：", h("span", { className: "text-emerald-400" }, "L1 正文"),
          "（", h("span", { className: "font-mono" }, "knowledge/<domain>.md"), " 的 ", h("span", { className: "font-mono" }, "## <section>"), "）、",
          h("span", { className: "text-blue-400" }, "L0 索引"), "（自动同步一行摘要）、",
          h("span", { className: "text-purple-400" }, "向量库"), "（供语义搜索）。三者由框架自动保持一致。"
        ),
        h("div", { className: "space-y-3" },
          h("div", null,
            h(Label, { className: "text-text-secondary text-sm" }, "Domain"),
            h(Input, {
              value: injectDomain,
              onChange: e => setInjectDomain(e.target.value),
              placeholder: "e.g., infra, dev, stock-analysis",
              className: "bg-background border-border text-text-primary mt-1"
            })
          ),
          h("div", null,
            h(Label, { className: "text-text-secondary text-sm" }, "Section"),
            h(Input, {
              value: injectSection,
              onChange: e => setInjectSection(e.target.value),
              placeholder: "e.g., WSL Proxy, API Design",
              className: "bg-background border-border text-text-primary mt-1"
            })
          ),
          h("div", null,
            h(Label, { className: "text-text-secondary text-sm" }, "Content"),
            h("textarea", {
              value: injectContent,
              onChange: e => setInjectContent(e.target.value),
              placeholder: "Knowledge content (markdown supported)...",
              rows: 6,
              className: "w-full bg-background border border-border rounded-md p-2 text-text-primary text-sm mt-1 resize-y"
            })
          ),
          h(Button, {
            variant: "outline",
            className: "border-border text-text-secondary hover:text-text-primary hover:bg-surface-hover",
            onClick: () => handleDryRun("inject-knowledge", {
              domain: injectDomain,
              section: injectSection,
              content: injectContent
            }),
            disabled: executing || !injectDomain || !injectSection || !injectContent
          }, "Preview Injection")
        )
      ),

      // Compact sub-tab
      activeSubTab === "compact" && h("div", { className: "bg-surface border border-border rounded-lg p-4" },
        h("div", { className: "text-text-secondary mb-4" },
          "Memory compaction scans the agent's built-in memory for bloat entries ",
          "and migrates them to L1 knowledge files. This frees up memory space ",
          "and keeps the knowledge base organized."
        ),
        h(Button, {
          variant: "outline",
          className: "border-border text-text-secondary hover:text-text-primary hover:bg-surface-hover",
          onClick: () => handleDryRun("compact-memory", {}),
          disabled: executing
        }, "Preview Compaction")
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Tab 3: Health & Diagnostics
  // ═══════════════════════════════════════════════════════════════════════
  function HealthDiagnosticsTab() {
    const healthData = useAsyncData(() => fetchJSON(`${API_BASE}/health`), []);
    const todosData = useAsyncData(() => fetchJSON(`${API_BASE}/todos?status=pending`), []);

    const [todoUpdating, setTodoUpdating] = useState(null);

    const handleTodoStatus = useCallback((todoId, newStatus) => {
      setTodoUpdating(todoId);
      postJSON(`${API_BASE}/update-todo`, { todo_id: todoId, status: newStatus, dry_run: false })
        .then(() => {
        todosData.refetch();
        setTodoUpdating(null);
      }).catch(() => setTodoUpdating(null));
    }, [todosData]);

    const rot = healthData.data && healthData.data.rot;
    const healthScore = rot ? rot.health_score : null;
    // findings is a dict keyed by pathology, each value an array.
    const f = (rot && rot.findings) || {};
    const penalties = [
      { key: "oversized", label: "文件过大", count: (f.oversized || []).length, per: 4, cap: 24 },
      { key: "garbled_heading", label: "标题乱码", count: (f.garbled_heading || []).length, per: 6, cap: 24 },
      { key: "stale", label: "过期内容", count: (f.stale || []).length, per: 3, cap: 18 },
      { key: "cross_dup", label: "跨文件重复", count: (f.cross_dup || []).length, per: 5, cap: 30 },
      { key: "same_file_dup", label: "文件内重复", count: (f.same_file_dup || []).length, per: 5, cap: 24 },
    ];
    const activePenalties = penalties.filter(p => p.count > 0).map(p => ({
      ...p, deducted: Math.min(p.count * p.per, p.cap)
    }));
    const totalFindings = activePenalties.reduce((s, p) => s + p.count, 0);
    // Flatten findings dict → array for the detailed list below
    const findingsList = [];
    Object.keys(f).forEach(cat => {
      (f[cat] || []).forEach(item => findingsList.push({ category: cat, ...item }));
    });

    return h("div", { className: "flex flex-col gap-4" },
      // Health Score Dashboard
      h("div", { className: "grid grid-cols-1 md:grid-cols-3 gap-4" },
        // Score card (spans 1 col but content explains the score)
        h(Card, { className: "bg-surface border-border" },
          h(CardHeader, { className: "pb-2" },
            h(CardTitle, { className: "text-sm flex items-center gap-2" },
              h(Icons.HeartPulse), "健康分数"
            )
          ),
          h(CardContent, null,
            healthData.loading ? h("div", { className: "text-text-tertiary" }, "Loading...")
              : healthScore === null ? h("div", { className: "text-text-tertiary" }, "N/A")
              : h("div", null,
                  // single-line score, no wrap
                  h("div", { className: "flex items-baseline gap-1 whitespace-nowrap" },
                    h("span", {
                      className: cn(
                        "text-4xl font-bold leading-none",
                        healthScore >= 80 ? "text-emerald-400"
                          : healthScore >= 60 ? "text-amber-400"
                          : "text-red-400"
                      )
                    }, healthScore),
                    h("span", { className: "text-lg text-text-tertiary leading-none" }, "/ 100")
                  ),
                  // explanation
                  h("div", { className: "text-xs text-text-secondary mt-3" },
                    healthScore === 100
                      ? "知识库健康，未检测到衰退信号。"
                      : `共扣 ${100 - healthScore} 分（${totalFindings} 处问题）。明细：`
                  ),
                  activePenalties.length > 0 && h("div", { className: "mt-2 space-y-1" },
                    activePenalties.map(p =>
                      h("div", { key: p.key, className: "flex justify-between text-xs" },
                        h("span", { className: "text-text-secondary" }, `${p.label} × ${p.count}`),
                        h("span", { className: "text-red-400 font-medium" }, `-${p.deducted}`)
                      )
                    )
                  )
                )
          )
        ),

        // Vector Store card
        h(Card, { className: "bg-surface border-border" },
          h(CardHeader, { className: "pb-2" },
            h(CardTitle, { className: "text-sm" }, "Vector Store")
          ),
          h(CardContent, null,
            healthData.loading ? h("div", { className: "text-text-tertiary" }, "Loading...")
              : !healthData.data || !healthData.data.vector
                ? h("div", { className: "text-text-tertiary" }, "N/A")
                : h("div", null,
                    h("div", { className: "flex justify-between text-sm" },
                      h("span", { className: "text-text-secondary" }, "Entries:"),
                      h("span", { className: "text-text-primary font-medium" },
                        healthData.data.vector.total_entries || 0
                      )
                    ),
                    h("div", { className: "flex justify-between text-sm mt-1" },
                      h("span", { className: "text-text-secondary" }, "Fitted:"),
                      h("span", { className: "text-text-primary font-medium" },
                        healthData.data.vector.is_fitted ? "Yes" : "No"
                      )
                    )
                  )
          )
        ),

        // L0 Consistency card
        h(Card, { className: "bg-surface border-border" },
          h(CardHeader, { className: "pb-2" },
            h(CardTitle, { className: "text-sm" }, "L0 Index")
          ),
          h(CardContent, null,
            healthData.loading ? h("div", { className: "text-text-tertiary" }, "Loading...")
              : !healthData.data || !healthData.data.consistency
                ? h("div", { className: "text-text-tertiary" }, "N/A")
                : h("div", null,
                    h("div", { className: "flex justify-between text-sm" },
                      h("span", { className: "text-text-secondary" }, "Exists:"),
                      h("span", { className: "text-text-primary font-medium" },
                        healthData.data.consistency.l0_exists ? "Yes" : "No"
                      )
                    ),
                    h("div", { className: "flex justify-between text-sm mt-1" },
                      h("span", { className: "text-text-secondary" }, "Size:"),
                      h("span", { className: "text-text-primary font-medium" },
                        `${healthData.data.consistency.l0_size} bytes`
                      )
                    )
                  )
          )
        )
      ),

      // Rot Findings
      findingsList.length > 0 && h("div", { className: "bg-surface border border-border rounded-lg p-4" },
        h("div", { className: "font-semibold text-text-primary mb-3 flex items-center gap-2" },
          h(Icons.AlertTriangle), `衰退信号 (${findingsList.length})`
        ),
        h("div", { className: "space-y-2" },
          findingsList.map((f, i) =>
            h("div", {
              key: i,
              className: "p-2 rounded border text-sm bg-amber-500/10 border-amber-500/20 text-amber-400"
            },
              h("div", { className: "font-medium" },
                `${f.category}: ${f.file || f.heading || f.section || "—"}`
              ),
              (f.details || f.size || f.similarity) && h("div", { className: "text-xs mt-1 opacity-80" },
                f.details
                  || (f.size ? `size: ${f.size}` : "")
                  || (f.similarity ? `similarity: ${f.similarity}` : "")
              )
            )
          )
        )
      ),

      // TODO List
      h("div", { className: "bg-surface border border-border rounded-lg p-4" },
        h("div", { className: "font-semibold text-text-primary mb-3 flex items-center gap-2" },
          h(Icons.ListTodo), "Pending TODOs"
        ),
        todosData.loading ? h(LoadingSpinner)
          : todosData.error ? h(ErrorBox, { message: todosData.error })
          : !(todosData.data && todosData.data.items && todosData.data.items.length)
            ? h("div", { className: "text-text-tertiary text-center py-4" }, "No pending TODOs")
            : h("div", { className: "space-y-2" },
                (todosData.data.items || []).map(todo =>
                  h("div", {
                    key: todo.id,
                    className: "flex items-center justify-between p-2 rounded border border-border hover:bg-surface-hover"
                  },
                    h("div", { className: "flex-1 min-w-0" },
                      h("div", { className: "text-sm text-text-primary font-medium truncate" },
                        todo.title || todo.content
                      ),
                      h("div", { className: "text-xs text-text-tertiary" },
                        `${todo.domain} · ${todo.created_at}`
                      )
                    ),
                    h("div", { className: "flex gap-1 ml-2" },
                      h(Button, {
                        variant: "outline",
                        className: "border-emerald-500/50 text-emerald-400 hover:bg-emerald-500/10 text-xs px-2 py-1 h-auto",
                        onClick: () => handleTodoStatus(todo.id, "completed"),
                        disabled: todoUpdating === todo.id
                      }, h(Icons.Check, { width: 12, height: 12 })),
                      h(Button, {
                        variant: "outline",
                        className: "border-red-500/50 text-red-400 hover:bg-red-500/10 text-xs px-2 py-1 h-auto",
                        onClick: () => handleTodoStatus(todo.id, "cancelled"),
                        disabled: todoUpdating === todo.id
                      }, h(Icons.X, { width: 12, height: 12 }))
                    )
                  )
                )
              )
      )
    );
  }

  // ═══════════════════════════════════════════════════════════════════════
  // Main Plugin Component
  // ═══════════════════════════════════════════════════════════════════════
  function LayeredMemoryPlugin() {
    const [activeTab, setActiveTab] = useState("overview");

    const tabBtn = (id, icon, label) => h(Button, {
      variant: "outline",
      className: cn(
        "border-border text-text-secondary hover:text-text-primary hover:bg-surface-hover",
        activeTab === id && "bg-surface text-text-primary border-border-hover"
      ),
      onClick: () => setActiveTab(id)
    }, h("span", { className: "flex items-center gap-1" }, h(icon), label));

    return h("div", { className: "p-4 h-full flex flex-col" },
      // Header
      h("div", { className: "flex items-center justify-between mb-4 pb-3 border-b border-border" },
        h("div", { className: "flex items-center gap-2" },
          h(Icons.Layers),
          h("h1", { className: "text-xl font-bold text-text-primary" }, "分层记忆框架")
        ),
        h("div", { className: "text-xs text-text-tertiary" }, "Layered Memory MCP v2.x")
      ),

      // Tab navigation (simple button style, no Tabs component)
      h("div", { className: "flex gap-2 mb-4 border-b border-border pb-2 flex-wrap" },
        tabBtn("overview", Icons.Compass, "架构总览"),
        tabBtn("map", Icons.Map, "记忆地图"),
        tabBtn("lifecycle", Icons.RefreshCw, "知识生命周期"),
        tabBtn("health", Icons.HeartPulse, "健康诊断")
      ),

      // Tab content
      h("div", { className: "flex-1 overflow-auto" },
        activeTab === "overview" && h(ArchitectureOverviewTab),
        activeTab === "map" && h(MemoryMapTab),
        activeTab === "lifecycle" && h(KnowledgeLifecycleTab),
        activeTab === "health" && h(HealthDiagnosticsTab)
      )
    );
  }

  // ── Register plugin ───────────────────────────────────────────────────
  if (window.__HERMES_PLUGINS__) {
    window.__HERMES_PLUGINS__.register("layered-memory-dashboard", LayeredMemoryPlugin);
  }
})();
