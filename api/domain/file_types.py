"""Shared file-type classification for local mode (watcher, processor, upload)."""

PDF_TYPES = frozenset({"pdf"})
OFFICE_TYPES = frozenset({"pptx", "ppt", "docx", "doc"})
SPREADSHEET_TYPES = frozenset({"xlsx", "xls"})
IMAGE_TYPES = frozenset({"png", "jpg", "jpeg", "webp", "gif"})
HTML_TYPES = frozenset({"html", "htm"})

# Read inline and chunked as plain text — no extraction backend needed.
SIMPLE_TEXT_TYPES = frozenset({
    "md", "txt", "csv", "svg", "json", "xml",
    "yaml", "yml", "toml", "ini", "cfg", "rst", "tex", "latex",
})

# Need an extraction/processing backend before they're searchable. HTML is here
# (not in SIMPLE_TEXT_TYPES) because it goes through the webmd parser.
EXTRACTION_TYPES = PDF_TYPES | OFFICE_TYPES | SPREADSHEET_TYPES | HTML_TYPES

# Files routed through the local background processor. Images do not need text
# extraction, but the processor still finalizes their metadata/status, so they
# must participate in upload dispatch and interrupted-work reconciliation.
PROCESSING_TYPES = EXTRACTION_TYPES | IMAGE_TYPES
