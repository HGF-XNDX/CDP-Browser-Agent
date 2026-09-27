from __future__ import annotations

import re
from pathlib import Path

import yaml


class SkillCatalog:
    """Discover metadata; load instructions and bounded UTF-8 resources on demand."""

    def __init__(self, paths: list[str], max_chars: int = 20000, active_budget: int = 30000):
        if max_chars < 512 or active_budget < max_chars:
            raise ValueError("Skill budgets require active_budget >= max_chars >= 512")
        self.max_chars = max_chars
        self.active_budget = active_budget
        self.skills: dict[str, dict] = {}
        self.active: dict[str, dict] = {}
        for raw in paths:
            root = Path(raw).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"Skill directory does not exist: {root}")
            candidates = [root / "SKILL.md"] if (root / "SKILL.md").is_file() else sorted(root.glob("*/SKILL.md"))
            for path in candidates:
                resolved = path.resolve()
                if not resolved.is_relative_to(root):
                    raise ValueError(f"Skill path escapes configured root: {path}")
                metadata, _ = self._parse(resolved)
                name = metadata.get("name")
                description = metadata.get("description")
                if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
                        or len(name) > 64 or name != path.parent.name):
                    raise ValueError(f"Invalid skill name/directory: {path}")
                if not isinstance(description, str) or not 1 <= len(description.strip()) <= 1024:
                    raise ValueError(f"Invalid skill description: {path}")
                if name in self.skills:
                    raise ValueError(f"Duplicate skill: {name}")
                self.skills[name] = {"name": name, "description": description, "path": resolved}

    def _parse(self, path: Path) -> tuple[dict, str]:
        if path.stat().st_size > self.max_chars * 4:
            raise ValueError(f"Skill file too large: {path.name}")
        text = path.read_text(encoding="utf-8-sig")
        if len(text) > self.max_chars:
            raise ValueError(f"Skill exceeds {self.max_chars} characters: {path.name}")
        match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)(.*)\Z", text, re.S)
        if not match:
            raise ValueError(f"Missing YAML frontmatter: {path}")
        metadata = yaml.safe_load(match[1])
        if not isinstance(metadata, dict):
            raise ValueError(f"Skill metadata must be an object: {path}")
        return metadata, match[2].strip()

    def catalog(self, query: str = "", offset: int = 0, limit: int = 30) -> dict:
        values = [{"name": s["name"], "description": s["description"]}
                  for s in self.skills.values()
                  if query.lower() in (s["name"] + " " + s["description"]).lower()]
        limit, offset = max(1, min(50, limit)), max(0, offset)
        return {"skills": values[offset:offset + limit], "total": len(values),
                "next_offset": offset + limit if offset + limit < len(values) else None}

    def load(self, name: str) -> dict:
        skill = self.skills[name]
        _, body = self._parse(skill["path"])
        used = sum(len(s["instructions"]) for n, s in self.active.items() if n != name)
        if used + len(body) > self.active_budget:
            raise ValueError("Active skill context budget exceeded; unload an unused skill first")
        self.active[name] = {"name": name, "instructions": body}
        return self.active[name]

    def read(self, name: str, path: str, offset: int = 0, limit: int = 8000) -> dict:
        if name not in self.active:
            raise ValueError("Load the skill before reading its resources")
        root = self.skills[name]["path"].parent
        relative = Path(path)
        target = (root / relative).resolve()
        if relative.is_absolute() or not target.is_relative_to(root) or not target.is_file():
            raise ValueError("Resource must be a file inside the selected skill")
        if target.stat().st_size > 1024 * 1024:
            raise ValueError("Resource exceeds 1 MiB; use an external tool for large or binary files")
        text = target.read_text(encoding="utf-8-sig")
        offset, limit = max(0, offset), max(1, min(8000, limit))
        return {"name": name, "path": path, "text": text[offset:offset + limit],
                "next_offset": offset + limit if offset + limit < len(text) else None}
