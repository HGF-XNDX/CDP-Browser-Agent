"""Immutable, hash-checked evidence scoped to one runtime/run directory."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4


class ArtifactStore:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, artifact_id):
        if not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_id):
            raise ValueError("Invalid artifact ID")
        path = self.root / (artifact_id + ".json")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Artifact escapes the current run")
        return path

    def save(self, value):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        content = text.encode("utf-8")
        identity = hashlib.sha256(content).hexdigest()
        path = self._path(identity)
        self.root.mkdir(parents=True, exist_ok=True)
        if path.exists():
            self._text(identity)  # Detect corruption; never overwrite old evidence.
        else:
            temp = self.root / ("tmp-" + uuid4().hex)
            try:
                with temp.open("xb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                # Same digest implies identical bytes, including concurrent writers.
                os.replace(temp, path)
            finally:
                temp.unlink(missing_ok=True)
        return {"artifact_id": identity, "sha256": identity, "bytes": len(content), "total_chars": len(text),
                "retrieval": "artifact_read(artifact_id, offset, limit) or artifact_search(artifact_id, query)"}

    def _text(self, identity):
        content = self._path(identity).read_bytes()
        if hashlib.sha256(content).hexdigest() != identity:
            raise ValueError("Artifact hash mismatch; original evidence was modified")
        return content.decode("utf-8")

    def load(self, identity):
        return json.loads(self._text(identity))

    def read(self, artifact_id, offset=0, limit=4000):
        text = self._text(artifact_id)
        if offset < 0 or not 1 <= limit <= 8000:
            raise ValueError("Invalid artifact slice")
        end = min(len(text), offset + limit)
        return {"ok": True, "artifact_id": artifact_id, "sha256": artifact_id, "text": text[offset:end],
                "offset": offset, "next_offset": end if end < len(text) else None, "total_chars": len(text)}

    def search(self, artifact_id, query, offset=0, limit=10):
        if not query or len(query) > 500 or offset < 0 or not 1 <= limit <= 20:
            raise ValueError("Search requires a literal query of 1-500 characters and valid paging")
        text = self._text(artifact_id)
        matches, cursor = [], offset
        while len(matches) < limit:
            pos = text.find(query, cursor)
            if pos < 0:
                break
            matches.append({"offset": pos, "text": text[max(0, pos-100):pos+len(query)+200]})
            cursor = pos + len(query)
        return {"ok": True, "artifact_id": artifact_id, "matches": matches,
                "next_offset": cursor if text.find(query, cursor) >= 0 else None, "total_chars": len(text)}
