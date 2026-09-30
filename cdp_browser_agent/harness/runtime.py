from __future__ import annotations

import os
import hashlib
import re
from pathlib import Path
from uuid import uuid4
from contextlib import AsyncExitStack
from dataclasses import replace

import httpx2
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from .skills import SkillCatalog
from .tools import Tool, ToolRegistry
from .artifacts import ArtifactStore
from ..web.tools import WebTools
from ..documents.engine import DocumentTools
from ..crawler.engine import Crawler
from ..processing.engine import ProcessingEngine
from ..processing.sessions import ProcessingSessions, WorkerBusy
from ..processing.learning import ProcedureStore, ReplayCatalog, replay_experience


def object_schema(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": required or [], "additionalProperties": False}


class ExtensionRuntime:
    def __init__(self, config: dict, tools: list[Tool] | None = None):
        settings = config.get("harness", {})
        self.settings = settings
        self.config = config
        self.task_state = {}
        self.processing = ProcessingEngine(config)
        self.workers = ProcessingSessions(config)
        self.registry = ToolRegistry(float(settings.get("tool_timeout_seconds", 60)),
                                     int(settings.get("max_tool_result_chars", 12000)),
                                     ArtifactStore(Path(settings.get("artifact_dir") or Path(config.get("agent", {}).get("log_dir", "logs/browser-agent")) / "artifacts") / uuid4().hex))
        self.skills = SkillCatalog(settings.get("skill_paths", []),
                                   int(settings.get("max_skill_chars", 20000)),
                                   int(settings.get("active_skill_budget_chars", 30000)))
        self.stack = AsyncExitStack()
        self.unavailable_servers = {}
        self.web = WebTools(config.get("web", {}), self.registry.max_result_chars)
        self.crawl_allowed_origins = None
        self._register_builtins()
        self.web_tool_names = []
        self.document_tool_names = []
        if config.get("documents", {}).get("enabled", True):
            self.documents = DocumentTools(config, self.web)
            for tool in self.documents.definitions():
                async def document_call(_handler=tool.handler, _name=tool.name, **args):
                    task = self.task_state.get('task')
                    review_required = bool(task and self.config.get('documents', {}).get('review_candidates', True))
                    recovery = self.document_recovery() if task else None
                    if recovery:
                        blocked = recovery.before(_name, args)
                        if blocked:
                            return blocked
                    if _name == 'document_preview':
                        required_key = self.config.get('agent', {}).get('completion_documents', {}).get('collection_key')
                        if required_key and args['spec'].get('collection_key', 'records') != required_key:
                            raise ValueError(f'Task output contract requires collection_key={required_key}; correct the recipe')
                    if _name == 'document_export' and review_required:
                        from ..documents.review import review_policy_id
                        reviewed = self.task_state.get('document_reviews', {}).get(args['job_id'], {})
                        if not reviewed.get('accepted') or reviewed.get('policy_id') != review_policy_id(self.config):
                            raise ValueError('Candidate has no accepted independent review for this task. Run document_preview and address its review issues before exporting.')
                    try:
                        result = await _handler(**args)
                    except Exception as exc:
                        if recovery:
                            recovery.failure(args, str(exc))
                        raise
                    if _name == 'document_preview':
                        self.task_state.setdefault('document_candidates', {})[result['job_id']] = {
                            k: result[k] for k in ('job_id', 'source_id', 'status', 'record_count', 'validation') if k in result}
                    revision = recovery.candidate(result) if recovery and _name == 'document_preview' else None
                    if _name == 'document_preview' and review_required and result.get('validation', {}).get('ok'):
                        from ..documents.review import review_candidate, repair_guidance
                        from ..documents.review import review_policy_id
                        previous = self.task_state.get('document_reviews', {}).get((revision or {}).get('previous_job_id'), {})
                        if revision and not revision['effective_change'] and previous.get('issues') and previous.get('policy_id') == review_policy_id(self.config):
                            review = {**previous, 'accepted': False, 'kind': 'unchanged_output_rejection',
                                'candidate_sha256': self.documents._job(result['job_id'])[1]['candidate_sha256'],
                                'previous_review_id': previous.get('review_id'),
                                'message': 'This candidate produces the same record fields as the rejected candidate. Its cited defects remain unresolved; a cosmetic recipe/metadata change is not an effective repair.'}
                        else:
                            review = await review_candidate(self.config, task, self.documents, result['job_id'])
                        review = {**review, 'repair_options': repair_guidance(review.get('issues', []))}
                        self.task_state.setdefault('document_reviews', {})[result['job_id']] = review
                        if recovery:
                            recovery.decision(result['job_id'], review)
                    if _name in {'document_preview', 'document_review'} and review_required and result.get('job_id'):
                        review = self.task_state.get('document_reviews', {}).get(result['job_id'])
                        if review:
                            from ..documents.review import repair_guidance
                            review = {**review, 'repair_options': repair_guidance(review.get('issues', []))}
                            result['review'] = review
                            if not review['accepted']:
                                result['status'] = review.get('status', 'needs_revision')
                                result['message'] = review.get('message') or 'This candidate remains rejected. Reading samples does not approve it. Revise document_preview using the cited evidence and generic repair_options before export.'
                    self.task_state.setdefault("collected_files", []).extend(
                        p for p in result.get("artifact_paths", []) if p not in self.task_state.get("collected_files", []))
                    if result.get('ok') and result.get('source_id') and result.get('format') and result.get('artifact_paths'):
                        self.task_state.setdefault('document_sources', {})[result['source_id']] = {
                            k: result[k] for k in ('source_id', 'url', 'format', 'bytes', 'parent_source_id') if k in result}
                    if result.get('job_id') and result.get('coverage'):
                        candidates = self.task_state.setdefault('document_candidates', {})
                        candidates[result['job_id']] = {**candidates.get(result['job_id'], {}), **{
                            k: result[k] for k in ('job_id', 'source_id', 'status', 'record_count', 'validation', 'review') if k in result}}
                    if result.get("status") == "exported" and result.get("output_path"):
                        exports = self.task_state.setdefault("document_exports", {})
                        exports[result["job_id"]] = result
                    if recovery:
                        recovery.after(_name, args, result)
                    return result
                self.registry.register(replace(tool, handler=document_call))
                self.document_tool_names.append(tool.name)
            async def retry_review(job_id, expected_review_id):
                from ..documents.review import review_candidate, repair_guidance
                task = self.task_state.get('task')
                if not task:
                    raise ValueError('Review retry requires a running task with its original request')
                recovery = self.document_recovery()
                blocked = recovery.before('document_review_retry', {'job_id': job_id})
                if blocked:
                    return blocked
                review = await review_candidate(self.config, task, self.documents, job_id,
                    retry_inconclusive=True, expected_review_id=expected_review_id)
                review = {**review, 'repair_options': repair_guidance(review.get('issues', []))}
                self.task_state.setdefault('document_reviews', {})[job_id] = review
                source_id = recovery.source_for({'job_id': job_id})
                result = {'ok': True, 'job_id': job_id, 'source_id': source_id, 'review': review,
                          'status': 'reviewed' if review.get('accepted') else review.get('status', 'needs_revision')}
                self.task_state['document_candidates'][job_id].update(status=result['status'], review=review)
                recovery.decision(job_id, review)
                recovery.after('document_review_retry', {'job_id': job_id}, result)
                return result
            identity = {'type': 'string', 'pattern': '^[0-9a-f]{64}$'}
            self.registry.register(Tool('document_review_retry',
                'Retry a saved inconclusive transient model review with job_id and its current expected_review_id. Uses the unchanged source/candidate, preserves prior attempts, and has a per-candidate attempt limit. Reading document_review only reads samples. A cited semantic rejection requires a recipe repair, not this retry.',
                object_schema({'job_id': identity, 'expected_review_id': identity}, ['job_id', 'expected_review_id']), retry_review))
            self.document_tool_names.append('document_review_retry')
        if config.get("web", {}).get("enabled", True):
            for tool in self.web.definitions():
                self.registry.register(tool)
                self.web_tool_names.append(tool.name)
            if config.get("crawler", {}).get("enabled", True):
                for tool in Crawler(config).definitions():
                    async def crawl_call(_name=tool.name, **args):
                        service = self.crawler()
                        handler = next(t.handler for t in service.definitions() if t.name == _name)
                        result = await handler(**args)
                        self.task_state.setdefault("collected_files", []).extend(
                            p for p in result.get("artifact_paths", []) if p not in self.task_state.get("collected_files", []))
                        return result
                    self.registry.register(replace(tool, handler=crawl_call))
                    self.web_tool_names.append(tool.name)
        for tool in tools or []:
            self.registry.register(tool)
        for name in settings.get("active_skills", []):
            self.skills.load(name)

    def _register_builtins(self) -> None:
        text = {"type": "string"}
        paging = {"query": text, "offset": {"type": "integer", "minimum": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 50}}

        async def skill_list(**args):
            return self.skills.catalog(**args)

        async def skill_load(name):
            return self.skills.load(name)

        async def skill_unload(name):
            self.skills.active.pop(name, None)
            return {"unloaded": name}

        async def skill_read(**args):
            return self.skills.read(**args)

        async def tool_list(**args):
            return self.registry.catalog(**args)

        async def tool_describe(name):
            return self.registry.describe(name)

        async def artifact_read(**args):
            args["limit"] = min(args.get("limit", 4000), max(1, (self.registry.max_result_chars - 400) // 6))
            return self.registry.artifacts.read(**args)

        async def artifact_search(**args):
            args["limit"] = min(args.get("limit", 10), max(1, (self.registry.max_result_chars - 300) // 900))
            result = self.registry.artifacts.search(**args)
            for match in result["matches"]:
                match["text"] = match["text"][:max(1, (self.registry.max_result_chars - 350) // (6 * len(result["matches"])))]
            return result

        async def history_read(action_id):
            for item in self.task_state.get("history", []):
                if item["actionId"] == action_id:
                    return item
            raise ValueError("Action ID is absent from the current run")

        def remember_worker(result):
            identity = result["worker_session_id"]
            old = self.task_state.setdefault("processing_results", [])
            self.task_state["processing_results"] = [r for r in old if r.get("worker_session_id") != identity] + [result]
            self.task_state.setdefault("collected_files", []).extend(p for p in result.get("artifact_paths", []) if p not in self.task_state.get("collected_files", []))
            return result

        async def processing_continue(worker_session_id, feedback=None, expected_turn=None):
            result = await self.workers.run(worker_session_id, feedback=feedback, expected_turn=expected_turn,
                parent_id=self.task_state.get("run_id"), origin="parent_agent")
            return remember_worker(result)

        async def processing_status(worker_session_id):
            return self.workers.status(worker_session_id, parent_id=self.task_state.get("run_id"))

        async def processing_cancel(worker_session_id):
            return self.workers.cancel(worker_session_id, parent_id=self.task_state.get("run_id"))

        async def experience_list(profile=None):
            with ProcedureStore(self.config) as learned:
                return {"experiences": learned.list(profile), "replay_suites": ReplayCatalog(self.config).catalog()}

        async def experience_replay(experience_id, suite):
            return await replay_experience(self.config, experience_id, suite)

        async def delegate_processing(profile, crawl_id=None, document_job_id=None):
            if crawl_id and document_job_id:
                raise ValueError('Choose crawl_id or document_job_id, not both')
            records = []
            for source in ([] if crawl_id or document_job_id else self.task_state.get("sources", [])):
                if source.get("kind") == "search_result":
                    continue
                text = source.get("snippet", "")
                coverage = "observed_excerpt"
                if source.get("observation_artifact"):
                    original = self.registry.artifacts.load(source["observation_artifact"]["artifact_id"])
                    text = original.get("fullText") or original.get("visibleText") or text
                    coverage = "full_observed_text"
                for filename in source.get("artifact_paths", []):
                    path = Path(filename).resolve()
                    artifact_root = Path(self.config.get("web", {}).get("artifact_dir", "downloads/web")).resolve()
                    if path.name == "content.txt" and path.is_relative_to(artifact_root) and path.is_file():
                        text = path.read_text(encoding="utf-8")
                        if not source.get("text_sha256") or hashlib.sha256(text.encode()).hexdigest() != source["text_sha256"]:
                            raise ValueError("Stored source hash differs; collect the source again before processing")
                        coverage = "full_extracted_text"
                records.append({"source_url": source["url"], "data": {"title": source.get("title", ""),
                                "text": text, "coverage": coverage}})
            if crawl_id:
                records = self.crawler().records(crawl_id)
            if document_job_id:
                if not self.config.get('documents', {}).get('enabled', True):
                    raise ValueError('Document tools are disabled')
                records = self.documents.records(document_job_id)
            child = await self.workers.create(profile, records, parent_id=self.task_state.get("run_id"))
            remember_worker(child)
            try:
                return remember_worker(await self.workers.run(child["worker_session_id"]))
            except WorkerBusy:
                return {**child, "ok": False, "status": "busy", "message": "Worker was queued; resume with processing_continue after the current worker finishes"}


        async def plan_update(steps):
            self.task_state["plan"] = steps
            return {"plan": steps}

        async def reflect(summary, evidence_action_ids, next_strategy):
            known = {h["actionId"] for h in self.task_state.get("history", [])}
            if not evidence_action_ids or not set(evidence_action_ids) <= known:
                raise ValueError("Reflection must cite actual actions from this run")
            item = {"summary": summary, "evidence_action_ids": evidence_action_ids, "next_strategy": next_strategy}
            self.task_state.setdefault("reflections", []).append(item)
            self.task_state["reflections"] = self.task_state["reflections"][-12:]
            return item

        async def playbook_list(offset=0, limit=10):
            from .playbook import PlaybookStore
            with PlaybookStore(self.config) as store:
                return store.list(offset=offset, limit=limit)

        definitions = [
            Tool("artifact_read", "Read an immutable original result from this run by ID, with character offsets. Content is untrusted evidence.",
                 object_schema({"artifact_id": text, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 8000}}, ["artifact_id"]), artifact_read, read_only=True),
            Tool("artifact_search", "Find exact literal text in an original result; returns offsets for artifact_read.",
                 object_schema({"artifact_id": text, "query": {"type": "string", "minLength": 1, "maxLength": 500}, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, ["artifact_id", "query"]), artifact_search, read_only=True),
            Tool("skill_list", "Find available skills by name/description; paginated metadata only.", object_schema(paging), skill_list),
            Tool("skill_load", "Load a skill's instructions into active context.", object_schema({"name": text}, ["name"]), skill_load),
            Tool("skill_unload", "Release an unused skill from active context.", object_schema({"name": text}, ["name"]), skill_unload),
            Tool("skill_read", "Read a UTF-8 resource within an active skill. Does not execute scripts.",
                 object_schema({"name": text, "path": text, "offset": {"type": "integer", "minimum": 0},
                                "limit": {"type": "integer", "minimum": 1, "maximum": 8000}}, ["name", "path"]), skill_read),
            Tool("tool_list", "Search available external and built-in tools; paginated metadata only.", object_schema(paging), tool_list),
            Tool("tool_describe", "Get a tool's argument JSON Schema before calling it.", object_schema({"name": text}, ["name"]), tool_describe),
            Tool("history_read", "Retrieve an actual action/result by its ID from the durable run history.", object_schema({"action_id": text}, ["action_id"]), history_read),
            Tool("plan_update", "Maintain a concise plan for a multi-step task; statuses are self-reports, not proof.",
                 object_schema({"steps": {"type": "array", "minItems": 1, "maxItems": 12, "items": object_schema({
                     "task": {"type": "string", "maxLength": 300}, "status": {"enum": ["pending", "in_progress", "done", "blocked"]}}, ["task", "status"])}}, ["steps"]), plan_update),
            Tool("reflect", "Record an evidence-linked failure analysis and next strategy. This does not modify code or permissions.",
                 object_schema({"summary": {"type": "string", "maxLength": 1000}, "next_strategy": {"type": "string", "maxLength": 1000},
                     "evidence_action_ids": {"type": "array", "minItems": 1, "maxItems": 12, "items": text}}, ["summary", "next_strategy", "evidence_action_ids"]), reflect),
        ]
        self.builtin_names = [t.name for t in definitions]
        if self.config.get("learning", {}).get("enabled"):
            tool = Tool("playbook_list", "Inspect versioned procedural memory. Only active, unexpired matching advice is eligible for recall; candidates are unverified.",
                        object_schema({"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}), playbook_list, read_only=True)
            definitions.append(tool)
            self.builtin_names.append(tool.name)
        for tool in definitions:
            self.registry.register(tool)
        self.processing_tool_names = []
        if self.processing.catalog.profiles:
            tool = Tool("delegate_processing", "Send page evidence, a completed crawl by crawl_id, OR a verified document export by document_job_id to a processing worker using an operator profile. Full records bypass model context.",
                        object_schema({"profile": {"enum": list(self.processing.catalog.profiles)}, "crawl_id": text, "document_job_id": text}, ["profile"]), delegate_processing)
            self.registry.register(tool)
            self.processing_tool_names.append(tool.name)
            extra = [
                Tool("processing_continue", "Continue the same worker with feedback and expected_turn, or resume its pending turn without feedback. Frozen source/method and bounded prior drafts are retained.",
                     object_schema({"worker_session_id": text, "feedback": {"type": "string", "minLength": 1, "maxLength": 4000}, "expected_turn": {"type": "integer", "minimum": 1}}, ["worker_session_id"]), processing_continue),
                Tool("processing_status", "Inspect an existing child worker and its recent events.", object_schema({"worker_session_id": text}, ["worker_session_id"]), processing_status, read_only=True),
                Tool("processing_cancel", "Cancel the current child turn; preserve its prior results and receipts.", object_schema({"worker_session_id": text}, ["worker_session_id"]), processing_cancel),
                Tool("experience_list", "Inspect processing experience candidates, active advice and operator replay suites.", object_schema({"profile": text}), experience_list, read_only=True),
            ]
            if self.config.get("processing", {}).get("replay_paths"):
                extra.append(Tool("experience_replay", "Run bounded paired evaluation on an operator-held-out suite. Only passing candidate results with a baseline improvement can promote advice; this calls the configured model.",
                    object_schema({"experience_id": text, "suite": text}, ["experience_id", "suite"]), experience_replay))
            for item in extra:
                self.registry.register(item)
                self.processing_tool_names.append(item.name)


    async def __aenter__(self):
        try:
            servers = self.settings.get("mcp_servers", {})
            if not isinstance(servers, dict):
                raise ValueError("harness.mcp_servers must be an object")
            for name, settings in servers.items():
                if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name):
                    raise ValueError(f"Invalid MCP server name: {name}")
                if not isinstance(settings.get("allow_tools"), list) or not all(isinstance(x, str) for x in settings["allow_tools"]):
                    raise ValueError(f"MCP server {name} requires an explicit allow_tools list")
                # An empty allowlist disables the server without launching a process.
                if not settings["allow_tools"]:
                    continue
                connection = AsyncExitStack()
                try:
                    discovered = []
                    transport = settings.get("transport", "stdio")
                    if transport == "stdio":
                        env = dict(settings.get("env", {}))
                        for variable in settings.get("env_from", []):
                            env[variable] = os.environ[variable]
                        target = StdioServerParameters(command=settings["command"], args=settings.get("args", []),
                                                       cwd=settings.get("cwd"), env=env)
                    elif transport == "streamable-http":
                        headers = {header: os.environ[variable] for header, variable in settings.get("headers_from_env", {}).items()}
                        http = await connection.enter_async_context(httpx2.AsyncClient(
                            headers=headers, timeout=self.registry.timeout, trust_env=False))
                        target = streamable_http_client(settings["url"], http_client=http)
                    else:
                        raise ValueError(f"Unsupported MCP transport: {transport}")
                    client = await connection.enter_async_context(Client(target, read_timeout_seconds=self.registry.timeout))
                    cursor = None
                    seen_cursors = set()
                    for _ in range(100):
                        page = await client.list_tools(cursor=cursor)
                        for remote in page.tools:
                            if remote.name not in settings["allow_tools"]:
                                continue
                            discovered.append(remote)
                        cursor = page.next_cursor
                        if not cursor:
                            break
                        if cursor in seen_cursors:
                            raise ValueError(f"Repeated tools/list cursor from {name}")
                        seen_cursors.add(cursor)
                    else:
                        raise ValueError(f"Too many tools/list pages from {name}")
                    # Validate against the complete registry, not only its first catalog page.
                    for allowed in settings["allow_tools"]:
                        if not any(t.name == allowed for t in discovered):
                            raise ValueError(f"Configured tool unavailable: {name}.{allowed}")
                    staged = ToolRegistry(self.registry.timeout, self.registry.max_result_chars)
                    for remote in discovered:
                        staged.register(self._remote_tool(name, remote, client))
                    if set(staged._tools) & set(self.registry._tools):
                        raise ValueError("External MCP tool conflicts with an already registered tool")
                    for tool in staged._tools.values():
                        self.registry.register(tool)
                    self.stack.push_async_exit(connection.pop_all())
                except Exception as exc:
                    await connection.aclose()
                    if settings.get("required", False):
                        raise
                    self.unavailable_servers[name] = {"status": "unavailable", "error_type": type(exc).__name__}
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    def _remote_tool(self, server, remote, client):
        async def invoke(**arguments):
            result = await client.call_tool(remote.name, arguments)
            # Never feed binary/base64 content into a text planner.
            content = [{"type": block.type, **({"text": block.text} if block.type == "text" else {"omitted": True})}
                       for block in result.content]
            value = {"ok": not result.is_error, "content": content,
                     "structured_content": result.structured_content}
            if any(block.type != "text" for block in result.content):
                value["artifact"] = self.registry.artifacts.save(result.model_dump(mode="json", by_alias=True))
            return value
        return Tool(f"mcp.{server}.{remote.name}", remote.description or remote.name,
                    remote.input_schema, invoke)

    async def __aexit__(self, *exc):
        return await self.stack.__aexit__(*exc)

    def crawler(self):
        return Crawler(self.config, parent_id=self.task_state.get("run_id"), allowed_origins=self.crawl_allowed_origins)

    def document_recovery(self):
        from ..documents.recovery import DocumentRecovery
        return DocumentRecovery(self.task_state, getattr(self, 'documents', None), self.config.get('documents', {}))

    def _document_review_refresh(self):
        """Expose policy changes without inventing a replacement extraction recipe."""
        result = {'actions': [], 'errors': []}
        if not self.document_tool_names or not self.task_state.get('task') or not self.config.get('documents', {}).get('review_candidates', True):
            return result
        from ..documents.review import review_policy_id
        policy_id = review_policy_id(self.config)
        result['policy_id'] = policy_id
        candidates = self.task_state.get('document_candidates', {})
        reviews = self.task_state.get('document_reviews', {})
        latest = {c['source_id']: c for c in candidates.values() if c.get('source_id')}
        for candidate in latest.values():
            job_id = candidate['job_id']
            reviewed = reviews.get(job_id)
            if not reviewed or reviewed.get('policy_id') == policy_id:
                continue
            stale = {'accepted': False, 'status': 'review_required', 'policy_id': reviewed.get('policy_id'),
                'pending_policy_id': policy_id, 'previous_review_id': reviewed.get('review_id') or reviewed.get('previous_review_id'),
                'semantic_accuracy_verified': False, 'issues': [], 'required_changes': [],
                'message': 'Review rules or configuration changed since this checkpoint. The previous decision is stale. Refresh the saved preview before treating this source as accepted or blocked.'}
            reviews[job_id] = stale
            candidate.update(status='review_required', review=stale)
            try:
                _, receipt, _ = self.documents._job(job_id)
                result['actions'].append({'name': 'document_preview', 'job_id': job_id,
                    'arguments': {'source_id': receipt['source_id'], 'spec': receipt['spec']}})
            except Exception as exc:
                result['errors'].append({'job_id': job_id, 'error': str(exc)[:500]})
        if result['actions'] or result['errors']:
            result['message'] = 'Review policy changed. Refresh the listed previews using their exact saved recipes, then act on the new review. These recipes were already generated in this task; they have not been repaired by the host.'
        return result

    def context(self) -> dict:
        review_refresh = self._document_review_refresh()
        sources = list(self.task_state.get('document_sources', {}).values())[-20:]
        candidates = self.task_state.get('document_candidates', {})
        ledgers = self.task_state.get('document_recovery', {})
        active = self.task_state.get('active_document_source_id')
        latest = {c['source_id']: c for c in candidates.values() if c.get('source_id')}
        focused = latest.get(active)
        recipe = None
        if focused:
            recipe = self.documents._job(focused['job_id'])[1]['spec']
            review = focused.get('review')
            if review:
                focused = {**focused, 'review': {k: review[k] for k in ('accepted', 'status', 'issues', 'required_changes', 'repair_options', 'message', 'semantic_accuracy_verified', 'review_id', 'retryable', 'review_attempt', 'max_review_attempts') if k in review}}
        focus = {'active_source_id': active, 'active_candidate': focused, 'recipe': recipe,
            'pending_url': self.task_state.get('pending_document_url'),
            'retrieval': self.document_recovery().target(self.task_state['pending_document_url']) if self.task_state.get('pending_document_url') else None,
            'recovery': self.document_recovery().view(ledgers[active]) if active in ledgers else None,
            'other_sources': [{**s, 'stage': ledgers.get(s['source_id'], {}).get('stage', 'inspect'),
                'latest_job_id': latest.get(s['source_id'], {}).get('job_id')} for s in sources if s['source_id'] != active]}
        return {"builtin_tools": [self.registry.describe(n) for n in self.builtin_names + self.web_tool_names + self.processing_tool_names + self.document_tool_names],
                "document_focus": focus,
                "document_targets": [{k: t[k] for k in ('target_id', 'url', 'stage', 'source_id', 'attempts', 'last_failure', 'limitation', 'next_retry_at') if k in t}
                    for t in self.task_state.get('document_targets', {}).values()],
                "document_review_refresh": review_refresh,
                "document_exports": [{k: e[k] for k in ('source_id', 'job_id', 'status', 'record_count', 'output_path', 'output_sha256', 'artifact_paths') if k in e}
                    for e in self.task_state.get("document_exports", {}).values()],
                "document_sources": sources,
                "document_candidates": [{k: c[k] for k in ('source_id', 'job_id', 'status', 'record_count', 'validation') if k in c} |
                    ({'review': focused['review']} if c['source_id'] == active and focused and focused.get('review') else {}) for c in latest.values()][-20:],
                "processing_profiles": self.processing.catalog.catalog(),
                "child_workers": self.workers.list(self.task_state["run_id"]) if self.task_state.get("run_id") else [],
                "crawls": self.crawler().list() if self.task_state.get("run_id") else [],
                "completion_processing": self.config.get("agent", {}).get("completion_processing", []),
                "completion_documents": self.config.get("agent", {}).get("completion_documents", {}),
                "skills": self.skills.catalog(limit=15),
                "active_skills": list(self.skills.active.values()),
                "unavailable_servers": self.unavailable_servers,
                "external_tool_count": len(self.registry._tools) - len(self.builtin_names) - len(self.web_tool_names) - len(self.processing_tool_names) - len(self.document_tool_names)}
