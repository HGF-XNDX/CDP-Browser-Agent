# HTTP collection and dataset handoff

Inspect a representative page or user-supplied selectors. Use `web_fetch(content_format="html")`
to read bounded HTML slices and attributes; its default format is plain text. Use offsets
for additional HTML. Do not invent selectors from plain-text snippets. If no field layout
is needed, omit `fields` to collect each page's full extracted text and title.

MCP and inner agent tools have the same four names:

- `web_crawl(spec=..., page_budget=10)` starts a job; `web_crawl(crawl_id=..., page_budget=10)` resumes.
- `web_crawl_status(crawl_id)` reports status, scope, failures and exports.
- `web_crawl_read(crawl_id, offset=0, limit=4000)` reads saved JSON by character offsets.
- `web_crawl_pause(crawl_id)` interrupts reads cooperatively and preserves the frontier.

Supply either `spec` or `crawl_id`, never both. Example spec:

```json
{
  "seed_urls": ["https://example.com/catalog"],
  "max_pages": 10, "max_depth": 0, "max_records": 100,
  "item_selector": "article.item", "next_selector": "a.next",
  "fields": {"title": {"selector": "h2"}, "id": {"attribute": "data-id"}},
  "key_fields": ["id"]
}
```

These selectors are illustrative, not a claim about example.com. Ordinary `link_selector`
links add one depth; `next_selector` links stay at the current depth. Traversal stays on
seed origins, including redirects. `include_patterns`/`exclude_patterns` filter full URLs;
`record_patterns` limits which visited pages yield records. Defaults: 20 pages, depth 1,
1000 records, further capped by operator configuration. Empty selector results fail unless
`allow_empty=true` was intentionally supplied. Fields are strings or null, with required=true
by default. This version has no XPath, nested field schemas, JavaScript or browser cookies.
Field selectors search descendants of each item; omit selector or use `:scope` for the
item itself. For `<article class="item" data-id="a">`, id extraction is `{"attribute":"data-id"}`;
`{"selector":"[data-id]","attribute":"data-id"}` would instead search inside that article.

`paused` needs a later resume of the same ID. Honor `next_retry_at` on retry_backoff;
do not repeatedly call the tool before that time. `completed` means the bounded frontier
finished, not full-site coverage. `incomplete` preserves partial results and failure receipts.
To change selectors/scope or retry a terminal failed source, start a new specification;
the previous job stays immutable. `browser_fallback` lists dynamic/auth/challenge pages
for authorized browser handling. Robots denial and rate limits are not browser fallback.

After completion, pass `crawl_id` to `browser_worker_start(profile, crawl_id=...)` in MCP,
or `delegate_processing(profile, crawl_id=...)` inside the agent. The host loads all
hash-checked records, not only the first source summaries. The profile's max_records
still applies (default 500); match the crawl budget to the intended processing profile.
Partial/active datasets are refused by this handoff. For deliberate partial processing,
read/export the partial records and submit them explicitly with their coverage stated.
Use the worker's `output_preview` when describing processed values. It contains complete
validated rows (up to 8 and 4000 characters), total_records and truncated. A truncated
preview is a sample; report the actual output file and count without inventing remaining values.

CLI uses `--crawl-spec spec.json`, `--crawl-resume ID`, `--crawl-status ID`,
`--crawl-read ID --crawl-offset N --crawl-limit N`, and `--crawl-pause ID`.
Use `--crawl-page-budget 1` for one-page checkpoints. `--config` controls state directory,
proxies, allowed private hosts, robots, delays and limits; tool arguments cannot change these.
The export contains records.json, records.jsonl, records.csv, pages.json and manifest.json.
Use the records.json file with `--worker-start PROFILE --processing-input PATH` for CLI processing.
Registered workflows support `{"id":"collect","type":"http_crawl","spec":{...}}`
followed by a `process` step whose `input_step` is `collect`.
