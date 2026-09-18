import React, { useEffect, useState } from "react";
import {
  getDocumentByUrl,
  moveDocument,
  saveWebPage,
  type DocumentByUrl,
  type Highlight,
  type SaveResult,
} from "@/lib/api";
import {
  getSelectedFolderPath,
  getSelectedKnowledgeBaseId,
  normalizeFolderPath,
  setSelectedFolderPath,
  setSelectedKnowledgeBaseId,
} from "@/lib/settings";
import { normalizePdfSourceUrl } from "@/lib/pdf";
import { runPdfSaveJob } from "@/lib/pdf-save-jobs";
import { detectActivePage, type ActivePageTab } from "@/lib/active-tab";
import { capturePageHtml } from "@/lib/page-capture";
import { tabMessageWithDeadline, withDeadline } from "@/lib/deadline";
import KBPicker from "./KBPicker";
import StatusFeedback, { type Status } from "./StatusFeedback";
import { canonicalize } from "@/lib/url";

function safeHost(href: string): string {
  try {
    return new URL(href).hostname.replace(/^www\./, "");
  } catch {
    return href;
  }
}

function slugifyFilename(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^\w\s.-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .slice(0, 80)
    .replace(/^[-_.]+|[-_.]+$/g, "") || "web-clip";
}

interface Props {
  apiUrl: string;
  accessToken: string | null;
}

type PageState =
  | { status: "loading" }
  | { status: "ready"; tab: ActivePageTab }
  | { status: "unavailable"; message: string };

export const SAVED_DESTINATION_TIMEOUT_MS = 2_000;

export default function SaveForm({ apiUrl, accessToken }: Props) {
  const [page, setPage] = useState<PageState>({ status: "loading" });
  const [title, setTitle] = useState("");
  const [knowledgeBaseId, setKnowledgeBaseId] = useState<string | null>(null);
  const [selectionReady, setSelectionReady] = useState(false);
  const [folderPath, setFolderPath] = useState("/webclipper/");
  const [showMore, setShowMore] = useState(false);
  const [existingDoc, setExistingDoc] = useState<DocumentByUrl | null>(null);
  const [checkingExisting, setCheckingExisting] = useState(false);
  const [status, setStatus] = useState<Status>({ type: "idle" });
  const tab = page.status === "ready" ? page.tab : null;

  useEffect(() => {
    let cancelled = false;
    void detectCurrentPage(() => cancelled);
    void loadSavedDestination();

    async function loadSavedDestination() {
      const [savedKnowledgeBaseId, savedFolderPath] = await Promise.all([
        withDeadline(
          getSelectedKnowledgeBaseId(),
          SAVED_DESTINATION_TIMEOUT_MS,
          "Saved knowledge base lookup timed out",
        ).catch(() => null),
        withDeadline(
          getSelectedFolderPath(),
          SAVED_DESTINATION_TIMEOUT_MS,
          "Saved folder lookup timed out",
        ).catch(() => "/webclipper/"),
      ]);
      if (cancelled) return;
      if (savedKnowledgeBaseId) {
        setKnowledgeBaseId((current) => current ?? savedKnowledgeBaseId);
      }
      setFolderPath(savedFolderPath);
      setSelectionReady(true);
    }

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!tab) return;

    let cancelled = false;

    async function checkExistingDocument() {
      if (!tab) return;
      setCheckingExisting(true);
      setExistingDoc(null);
      try {
        const sourceUrl = tab.isPdf
          ? normalizePdfSourceUrl(tab.url)
          : canonicalize(tab.url);
        const doc = await getDocumentByUrl(apiUrl, accessToken, sourceUrl);
        if (cancelled) return;
        if (doc) {
          setExistingDoc(doc);
          setKnowledgeBaseId(doc.knowledge_base_id);
          setFolderPath(doc.path || "/webclipper/");
          setSelectedKnowledgeBaseId(doc.knowledge_base_id).catch(() => {});
          if (doc.path) setSelectedFolderPath(doc.path).catch(() => {});
          tabMessageWithDeadline(tab.tabId, {
            type: "DOCUMENT_SAVED",
            documentId: doc.id,
          }, 1_500).catch(() => {
            // Content script may not be present on restricted pages.
          });
        }
      } catch {
        // A miss is normal for new pages. Other failures should not block saving.
      } finally {
        if (!cancelled) setCheckingExisting(false);
      }
    }

    checkExistingDocument();

    return () => {
      cancelled = true;
    };
  }, [apiUrl, accessToken, tab]);

  async function detectCurrentPage(isCancelled: () => boolean = () => false) {
    if (isCancelled()) return;
    setPage({ status: "loading" });
    const result = await detectActivePage();
    if (isCancelled()) return;
    if (result.status !== "ready") {
      setPage(result);
      return;
    }

    const activeTab = result.tab;
    setPage(result);
    setTitle(activeTab.title);

    // Inject the highlighter under the activeTab grant so saved highlights
    // overlay and the user can annotate. No-op on PDFs / restricted pages.
    if (!activeTab.isPdf) {
      chrome.scripting
        .executeScript({ target: { tabId: activeTab.tabId }, files: ["content-scripts/content.js"] })
        .catch(() => {});
    }
  }

  async function handleSave() {
    if (!tab || !knowledgeBaseId) return;
    try {
      if (tab.isPdf) {
        await handleSavePdf();
      } else {
        await handleSaveWeb();
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : "Save failed";
      setStatus({ type: "error", message });
    }
  }

  async function handleSaveWeb() {
    if (!tab || !knowledgeBaseId) return;

    setStatus({ type: "saving", message: "Extracting page..." });

    let html: string;
    try {
      const [{ result }] = await withDeadline(
        chrome.scripting.executeScript({
          target: { tabId: tab.tabId },
          func: capturePageHtml,
        }),
        15_000,
        "Page extraction did not finish",
      );
      if (typeof result !== "string" || !result) {
        throw new Error("The page returned no content.");
      }
      html = result;
    } catch (error) {
      const detail = error instanceof Error ? error.message : "Unknown extraction error";
      throw new Error(`Could not extract page content. ${detail}`);
    }

    let highlights: Highlight[] = [];
    try {
      const reply = await tabMessageWithDeadline<{ highlights?: unknown }>(
        tab.tabId,
        { type: "GET_PAGE_HIGHLIGHTS" },
        1_500,
      );
      if (reply?.highlights && Array.isArray(reply.highlights)) {
        highlights = reply.highlights as Highlight[];
      }
    } catch {
      // Content script may not be present (e.g. PDF, restricted page). Ignore.
    }

    setStatus({ type: "saving", message: "Saving to LLM Wiki..." });

    const canonicalUrl = canonicalize(tab.url);
    const normalizedFolderPath = normalizeFolderPath(folderPath);

    const result = await saveWebPage(apiUrl, accessToken, knowledgeBaseId, {
      url: canonicalUrl,
      title: title || tab.title,
      path: normalizedFolderPath,
      html,
      highlights: highlights.length ? highlights : undefined,
    });

    // Saving is already complete. Do not hold the popup open for this
    // best-effort content-script refresh.
    void tabMessageWithDeadline(
      tab.tabId,
      {
        type: "DOCUMENT_SAVED",
        documentId: result.id,
      },
      1_500,
    ).catch(() => {
      // Page might be closed or content script unavailable — fine.
    });

    setExistingDoc({
      id: result.id,
      knowledge_base_id: knowledgeBaseId,
      title: title || tab.title,
      path: normalizedFolderPath,
      filename: "",
      version: result.version ?? 1,
      highlights: result.highlights ?? highlights,
    });
    setSelectedKnowledgeBaseId(knowledgeBaseId).catch(() => {});
    setSelectedFolderPath(normalizedFolderPath).catch(() => {});
    setStatus({ type: "success" });
  }

  async function handleSavePdf() {
    if (!tab || !knowledgeBaseId) return;

    const normalizedFolderPath = normalizeFolderPath(folderPath);
    const sourceUrl = normalizePdfSourceUrl(tab.url);
    setStatus({ type: "saving", message: "Saving PDF..." });

    // The offscreen document survives popup focus changes. Only this small job
    // request and its status cross runtime messaging; the PDF stays a Blob.
    const saved: SaveResult = await runPdfSaveJob({
      url: sourceUrl,
      apiUrl,
      accessToken,
      knowledgeBaseId,
      path: normalizedFolderPath,
    });
    setExistingDoc({
      id: saved.id,
      knowledge_base_id: saved.knowledge_base_id ?? knowledgeBaseId,
      title: saved.title ?? (title || tab.title),
      path: saved.path ?? normalizedFolderPath,
      filename: saved.filename ?? "document.pdf",
      version: saved.version ?? 1,
      highlights: saved.highlights ?? [],
    });

    setSelectedKnowledgeBaseId(knowledgeBaseId).catch(() => {});
    setSelectedFolderPath(normalizedFolderPath).catch(() => {});
    setStatus({ type: "success" });
  }

  async function handleKnowledgeBaseChange(id: string) {
    setKnowledgeBaseId(id);
    setSelectedKnowledgeBaseId(id).catch(() => {});
    // If the page is already saved (instant-save just ran), changing the KB
    // moves the document rather than selecting a target for a future save.
    if (existingDoc && existingDoc.knowledge_base_id !== id) {
      try {
        await moveDocument(apiUrl, accessToken, existingDoc.id, id);
        setExistingDoc({ ...existingDoc, knowledge_base_id: id });
      } catch {
        setStatus({ type: "error", message: "Couldn't move to that knowledge base." });
      }
    }
  }

  if (page.status === "loading") {
    return (
      <div className="flex items-center justify-center py-6">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-zinc-200 border-t-zinc-800" />
      </div>
    );
  }

  if (page.status === "unavailable") {
    return (
      <div className="space-y-3 py-2">
        <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-5 text-amber-800">
          {page.message}
        </div>
        <button
          onClick={() => void detectCurrentPage()}
          className="h-9 w-full rounded-md bg-zinc-950 px-3 text-xs font-medium text-white hover:bg-zinc-800"
        >
          Retry active page
        </button>
      </div>
    );
  }

  const isSaving = status.type === "saving";
  const isAlreadySaved = !!existingDoc;
  const canSave = !!knowledgeBaseId
    && selectionReady
    && !checkingExisting
    && !isSaving
    && !isAlreadySaved
    && status.type !== "success";

  return (
    <div className="space-y-3">
      {/* Title */}
      <div>
        <label className="mb-1.5 block text-xs font-medium text-zinc-700">Title</label>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="h-9 w-full rounded-md border border-zinc-200 bg-white px-3 text-sm
                     text-zinc-950 shadow-sm outline-none transition-colors
                     placeholder:text-zinc-400 focus:border-zinc-400 focus:ring-2
                     focus:ring-zinc-950/10"
          placeholder="Page title"
        />
      </div>

      {/* KB picker */}
      {selectionReady ? (
        <KBPicker
          apiUrl={apiUrl}
          accessToken={accessToken}
          value={knowledgeBaseId}
          onChange={handleKnowledgeBaseChange}
        />
      ) : (
        <div className="py-1 text-xs text-zinc-500">Loading saved destination...</div>
      )}

      {/* Folder/More section disabled for v0 — re-enable when folder picker is ready.
      <div className="rounded-md border border-zinc-200 bg-zinc-50/60">
        <button
          type="button"
          onClick={() => setShowMore((v) => !v)}
          className="flex h-8 w-full items-center justify-between px-3 text-xs font-medium text-zinc-600 transition-colors hover:text-zinc-950"
        >
          <span>More</span>
          <span className="text-zinc-400">{showMore ? "-" : "+"}</span>
        </button>
        {showMore && (
          <div className="space-y-2 border-t border-zinc-200 px-3 py-3">
            <div>
              <label className="mb-1.5 block text-xs font-medium text-zinc-700">Folder</label>
              <input
                list="llmwiki-folder-suggestions"
                value={folderPath}
                onChange={(e) => setFolderPath(e.target.value)}
                onBlur={() => setFolderPath(normalizeFolderPath(folderPath))}
                className="h-8 w-full rounded-md border border-zinc-200 bg-white px-2.5 text-xs text-zinc-950 shadow-sm outline-none transition-colors placeholder:text-zinc-400 focus:border-zinc-400 focus:ring-2 focus:ring-zinc-950/10"
                placeholder="/webclipper/"
              />
              <datalist id="llmwiki-folder-suggestions">
                <option value="/webclipper/" />
                <option value="/articles/" />
                <option value="/research/" />
                <option value="/inbox/" />
              </datalist>
            </div>
            <div className="min-w-0 text-[11px] text-zinc-500">
              <span className="font-medium text-zinc-600">Filename</span>{" "}
              <span className="break-all">{normalizedFolderPath}{filenamePreview}</span>
            </div>
          </div>
        )}
      </div>
      */}

      {/* Save button — hidden when the page is already saved */}
      {!isAlreadySaved && (
        <button
          onClick={handleSave}
          disabled={!canSave}
          className="h-9 w-full rounded-md bg-zinc-950 px-4 text-sm font-medium text-zinc-50
                     shadow-sm transition-colors hover:bg-zinc-800
                     focus-visible:outline-none focus-visible:ring-2
                     focus-visible:ring-zinc-950 focus-visible:ring-offset-2
                     disabled:cursor-not-allowed disabled:opacity-50"
        >
          {isSaving ? "Saving..." : "Save to LLM Wiki"}
        </button>
      )}

      {checkingExisting && (
        <p className="text-xs text-zinc-500">Checking saved status...</p>
      )}
      {isAlreadySaved && status.type !== "success" && (
        <p className="text-xs text-emerald-700">
          This page is already in LLM Wiki.
        </p>
      )}

      <StatusFeedback status={status} />
    </div>
  );
}
