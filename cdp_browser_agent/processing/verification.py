"""Deterministic operator contracts; never a claim of general semantic accuracy."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from ..workflows.spec import digest


def field(value, path):
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def validate_checks(checks):
    if not isinstance(checks, list) or len(checks) > 32:
        raise ValueError("verification must be a list of at most 32 operator checks")
    for check in checks:
        if not isinstance(check, dict) or check.get("kind") not in {"equals_input", "contained_in_input", "nonempty", "range"}:
            raise ValueError("Unknown processing verification check")
        for key in (["output", "input"] if check["kind"] in {"equals_input", "contained_in_input"} else ["output"]):
            if not isinstance(check.get(key), str) or not check[key] or len(check[key]) > 200:
                raise ValueError("Verification field paths must be nonempty strings")
        if check["kind"] == "range":
            if not all(isinstance(check.get(k), (int, float)) and not isinstance(check[k], bool)
                       and math.isfinite(check[k]) for k in ("min", "max")) or check["min"] > check["max"]:
                raise ValueError("Verification range requires finite min <= max")


def verify_data(checks, data, source):
    results = []
    for check in checks:
        try:
            value = field(data, check["output"])
            kind = check["kind"]
            if kind == "equals_input":
                ok = digest(value) == digest(field(source, check["input"]))
            elif kind == "contained_in_input":
                original = field(source, check["input"])
                ok = isinstance(value, str) and bool(value.strip()) and isinstance(original, str) and value in original
            elif kind == "nonempty":
                ok = value is not None and (bool(value.strip()) if isinstance(value, str) else value not in ([], {}))
            else:
                ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and check["min"] <= value <= check["max"]
            results.append({"check": check, "ok": bool(ok)})
        except (KeyError, IndexError, TypeError, ValueError):
            results.append({"check": check, "ok": False, "error": "Required field absent or wrong type"})
    return {"ok": bool(checks) and all(r["ok"] for r in results), "basis": "operator_checks",
            "checks": results, "source_sha256": digest(source), "output_sha256": digest(data),
            "contract_sha256": digest(checks), "semantic_accuracy_verified": False}


class ContractFailure(ValueError):
    def __init__(self, report):
        self.report = report
        failed = [r for r in report["checks"] if not r["ok"]]
        super().__init__("Operator checks failed: " + json.dumps(failed, ensure_ascii=False))


def verify_delivery(summary, root, *, require_contract=True, min_records=1):
    """Verify persisted exports and per-record receipts against their source inputs."""
    from .engine import validate_candidate
    report = {"ok": False, "basis": "persisted_processing_contract", "profile": summary.get("profile")}
    try:
        folder = Path(summary["records_path"]).resolve().parent
        if not folder.is_relative_to(Path(root).resolve()):
            raise ValueError("Processing output is outside configured storage")
        manifest = json.loads((folder / "delivery.json").read_text(encoding="utf-8"))
        if digest(manifest) != summary["delivery_sha256"]:
            raise ValueError("Delivery manifest changed")
        for filename, expected in manifest["files"].items():
            path = (folder / filename).resolve()
            if not path.is_relative_to(folder) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("An exported artifact changed or is missing")
        rows = json.loads((folder / "validated-records.json").read_text(encoding="utf-8"))
        method = json.loads((folder / "method.json").read_text(encoding="utf-8"))
        if method["method_hash"] != summary["method_hash"]:
            raise ValueError("Processing method changed")
        if summary.get("failed_count") or len(rows) < min_records:
            raise ValueError("Required records have not all passed")
        for row in rows:
            source = json.loads((folder / "items" / (row["item_id"] + ".input.json")).read_text(encoding="utf-8"))
            if digest(source) != row["input_sha256"]:
                raise ValueError("Source input changed")
            validation = validate_candidate(method["profile"], row, source["data"])
            if require_contract and not validation["ok"]:
                raise ValueError("Operator result checks are required")
            if digest({"data": row["data"], "evidence": row["evidence"]}) != row["output_sha256"]:
                raise ValueError("Validated output changed")
        report.update(ok=True, validated_count=len(rows), method_hash=method["method_hash"],
                      delivery_sha256=summary["delivery_sha256"], semantic_accuracy_verified=False)
    except Exception as exc:
        report["error"] = str(exc)[:1000]
    return report
