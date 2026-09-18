import { RUNTIME_MESSAGE_TIMEOUT_MS, withDeadline } from "./deadline";

export async function isPdfTab(
  tabId: number,
  url: string,
  title: string | null | undefined,
): Promise<boolean> {
  if (hasPdfSuffix(url, title)) return true;
  if (!/^https?:/.test(url)) return false;
  return tabReportsPdfContentType(tabId);
}

export function hasPdfSuffix(url: string, title: string | null | undefined): boolean {
  if (pathnameOf(url).endsWith(".pdf")) return true;
  return title?.toLowerCase().trim().endsWith(".pdf") ?? false;
}

/**
 * Normalize a PDF tab URL for lookup and deduplication without stripping query
 * parameters. Signed and authenticated PDF links often depend on their full
 * query string; only the viewer-only fragment (for example #page=12) is safe
 * to remove.
 */
export function normalizePdfSourceUrl(href: string): string {
  const value = href.trim();
  try {
    const url = new URL(value);
    url.hash = "";
    return url.toString();
  } catch {
    return value.split("#", 1)[0];
  }
}

function pathnameOf(url: string): string {
  try {
    return new URL(url).pathname.toLowerCase();
  } catch {
    return url.toLowerCase();
  }
}

// Chrome's built-in PDF viewer reports application/pdf as the top document's
// contentType even when the URL has no .pdf suffix (e.g. arxiv.org/pdf/<id>).
async function tabReportsPdfContentType(tabId: number): Promise<boolean> {
  try {
    const [{ result }] = await withDeadline(
      chrome.scripting.executeScript({
        target: { tabId },
        func: () => document.contentType,
      }),
      RUNTIME_MESSAGE_TIMEOUT_MS,
      "PDF detection did not finish",
    );
    return result === "application/pdf";
  } catch {
    return false;
  }
}
