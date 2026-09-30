# Full-document processing

Use generic document tools for structured exports from HTML/XML. Work by source_id
and job_id; do not copy a long file into the model context or transcribe its content.

1. document_open(url) retains full source and returns a structure inventory.
2. document_inspect(source_id, selector?, offset?, limit?) checks actual nodes.
   HTML uses CSS; XML uses case-sensitive ElementTree paths such as .//Entry;
   JSON uses RFC 6901 pointers. Inspect first/middle/last and metadata.
3. If a JSON string embeds another document, document_decode uses a pointer and
   base64/text encoding. Parent bytes and metadata remain part of the source chain.
4. document_preview(source_id, spec) tests a declarative rule over the whole source.
   elements selects disjoint complete records. sections selects ordered units and
   starts groups with heading_selector or start_pattern. exclude_pattern ends a
   group; body_selector separates body from notes. Relative title_selector and
   body_selector are available in elements mode. key_pattern capture 1 can extract
   labels and key_separator can separate multiple labels sharing one source.
   Capture the full grouped label before splitting; key_separator cannot recover
   text outside the capture. Inspect key_diagnostics rather than assuming a split
   occurred. id is the unique source-bound identity; key is a display label.
5. Review counts, full-source boundaries, continuations, duplicate labels, tables,
   annotations and remainder; revise the rule when evidence disagrees. An empty
   query is not permission to repeat it: inspect actual tags/case and change it.
   Agent tasks add a separate bounded candidate review. Read its cited evidence:
   display truncation is not lost data, and retained remainder is not discarded.
   An inconclusive reviewer decision is not evidence of a candidate defect.
   A quoted-issues-only rejection is a request to revise cited problems; it does
   not certify the rest of the checklist. Acceptance requires all displayed checks.
   A review-policy change requires refreshing the saved candidate; old decisions
   are retained for audit but cannot authorize a new export.
6. document_export(job_id) replays the frozen candidate and verifies hashes before
   writing. Keep the returned output path and verification receipt.

Pass a verified export by document_job_id to browser_worker_start (or internal
delegate_processing) with a registered profile for further semantic processing.
The complete records go to the worker without passing through parent context.

The output collection key is configurable (default records). Each record contains
key, heading, text, paragraphs, annotations, source_paths and a text hash. fragments
and remainder together preserve the entire parsed source tree; raw response bytes
are also retained. Duplicate labels in different source scopes have distinct IDs.
Planner-supplied metadata is marked separately from actual source metadata.

Report scope and source-version limitations. Structural conservation proves no
parsed tree content disappeared; it does not establish semantic completeness,
currentness, or correct field/record boundaries. Artifact completion gates verify
files and hashes only. Do not treat an exported candidate as universal task success.

These tools do not execute arbitrary code, OCR PDFs or import dynamic browser DOMs.
Use browser/registered external capabilities when necessary, within existing access
rules. Document network calls inherit web proxy/private-host/origin restrictions.
