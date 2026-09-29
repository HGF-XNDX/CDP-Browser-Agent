---
name: patent-law-collection
description: Collect and split the configured Chinese, US and Japanese patent statutes into source-linked JSON using the optional patent-laws MCP workflow.
---

Use the configured `patent-laws` MCP tools for this three-country collection.
Discover the tools with `tool_list`, then inspect their schemas. This skill does
not grant shell execution; the operator must register the MCP server.

For each requested country (`CN`, `US`, `JP`), call `patent_source_fetch` and then
`patent_articles_export`. An existing source or completed export is reused after
hash verification. The export tool reads the whole saved source and uses a
deterministic processing worker; do not page through or transcribe the full text.
If a tool fails, report its actual failure and do not claim the JSON exists.

Finish with `patent_collection_status` and report its verified paths, counts and
versions. Each `law.json` is one law object; `articles` contains one article per
element, with original-language text, paragraphs, identity and source locator.

Scope distinctions that affect the result:

- CN is the 2020 amended Patent Law, with 82 consecutively numbered articles.
- US is the official GovInfo **2024 edition**, current through the date in the
  downloaded header. Do not describe it as the latest 2026 consolidation.
  Preserve repealed/renumbered markers, split grouped section numbers, and keep
  editorial/historical annotations separate from statutory paragraphs.
- JP uses e-Gov API v2 with the operator's `as_of` date. Preserve article suffixes,
  deletion markers, supplementary article scopes and abridgment attributes.
  Supplementary paragraphs without Article nodes remain in supplementary groups.
  Attached tables remain in `appendices` with their complete row/cell structure;
  article counts alone are not a complete-document check.

Validation establishes source coverage and exact copying, not legal interpretation
or a comparison with every later amending law. Report that boundary accurately.
