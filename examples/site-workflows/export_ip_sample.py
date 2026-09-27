"""Export an inspected IPC court workflow run; no network and no legal inference.

This adapter belongs to the site example, not the general browser package.
Usage: python examples/site-workflows/export_ip_sample.py RUN_DIRECTORY NEW_OUTPUT_DIRECTORY
"""
from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import zipfile


def export(run_dir: Path, output: Path):
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    if result["status"] != "completed" or result["workflow"] != "ipc-judgments-sample":
        raise ValueError("Expected a completed ipc-judgments-sample run")
    records = [json.loads(line) for line in (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()]
    prepared = []
    for i, record in enumerate(records, 1):
        data, text = record["data"], record["data"]["full_text"]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not ("判决书" in re.sub(r"\s+", "", text[:100]) and data["title"] in text[:300]
                and "判决如下" in text and "本判决为终审判决" in text[-2000:] and len(text) > 500):
            raise ValueError(f"Document structure requires manual review: {data['title']}")
        for key in ("page_sha256", "detail_sha256"):
            path = run_dir / "snapshots" / (record[key] + ".html")
            if hashlib.sha256(path.read_bytes()).hexdigest() != record[key]:
                raise ValueError("Source snapshot hash mismatch")
        context = next((line for line in lines if "纠纷一案" in line), "")
        # Retain exact source wording, without guessing unknown case metadata.
        subject = next((term for term in (
            "发明专利临时保护期使用费纠纷", "外观设计专利权无效行政纠纷",
            "实用新型专利权无效行政纠纷", "侵害植物新品种权纠纷", "滥用市场支配地位纠纷"
        ) if term in context), None)
        dates = re.findall(r"[二一〇○零十百千一二三四五六七八九0-9]+年[十一二三四五六七八九0-9]+月[十一二三四五六七八九0-9]+日", text[-500:])
        scope = "competition_related" if subject == "滥用市场支配地位纠纷" else ("intellectual_property" if subject else "needs_review")
        prepared.append({"id": f"{i:02d}", "case_number": data["title"], "court": lines[0],
            "document_type": lines[1], "subject_exact": subject, "scope": scope,
            "decision_date_raw": dates[-1] if dates else None,
            "published_date": data["published_date"], "source_name": "最高人民法院知识产权法庭",
            "source_url": record["source_url"], "list_url": record["list_url"],
            "captured_at": record["captured_at"], "characters": len(text), "full_text": text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "detail_sha256": record["detail_sha256"], "subject_source_paragraph": context,
            "text_file": f"texts/{i:02d}.txt"})

    output.mkdir(parents=True, exist_ok=False)
    (output / "texts").mkdir()
    main = [r for r in prepared if r["scope"] == "intellectual_property"]
    related = [r for r in prepared if r["scope"] != "intellectual_property"]
    for record in prepared:
        (output / record["text_file"]).write_bytes(record["full_text"].encode("utf-8"))
    for name, selected in (("judgments.jsonl", main), ("related.jsonl", related)):
        (output / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in selected), encoding="utf-8")
    fields = [key for key in prepared[0] if key not in {"full_text", "subject_source_paragraph"}]
    with (output / "index.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(prepared)
    provenance = output / "provenance"
    provenance.mkdir()
    for name in ("records.jsonl", "pages.json", "workflow.json", "result.json"):
        shutil.copy2(run_dir / name, provenance / name)
    shutil.copytree(run_dir / "snapshots", provenance / "snapshots")
    summary = {"run_id": result["run_id"], "collected": len(prepared), "ip_judgments": len(main),
        "related_judgments": len(related), "characters_total": sum(r["characters"] for r in prepared),
        "characters_ip": sum(r["characters"] for r in main), "scope": "first listing page, bounded sample",
        "source": "https://ipc.court.gov.cn/zh-cn/news/more-5-25.html",
        "wenshu_records": 0, "wenshu_status": "login required after normal search",
        "verification": "HTML snapshot hashes, exact text hashes, document heading, matching case number, disposition and signature date",
        "limitations": ["Text only; illustrations are not downloaded or OCRed", "No completeness or representativeness claim", "Publication date is not decision date"]}
    (output / "receipt.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    table = "\n".join(f"| [{r['case_number']}]({r['text_file']}) | {r['subject_exact']} | {r['decision_date_raw']} | {r['characters']:,} | [原文]({r['source_url']}) |" for r in prepared)
    (output / "README.md").write_text(f"""# 知识产权判决书首批样本

实际浏览器工作流采集 {len(prepared)} 篇判决书的正文文本，其中知识产权案件 {len(main)} 篇，竞争相关案件 {len(related)} 篇。
来源为最高人民法院知识产权法庭官网；**这不是裁判文书网采集结果**。
文书网正常检索后要求登录，当前 0 篇；其检索记录保留在项目日志。

| 案号 / 本地正文 | 原文案由 | 裁判日期（原文） | 字符数 | 来源 |
|---|---|---|---:|---|
{table}

`judgments.jsonl` 是知识产权样本，`related.jsonl` 单独保存竞争相关案件，`index.csv` 为全部目录。
裁判日期摘自落款，网页发布日期单列。官方隐去的姓名保持原样，不推测补全。
`provenance/` 保存工作流、原始记录和页面 DOM 快照；快照文件名是 SHA-256。
`manifest.json` 可校验文件是否被改变，`receipt.json` 记录采集范围和核验方法。

本批是第一页的限定样本，不代表文书全集。保存了网页全部可见正文文本，未下载/OCR 判决中的图示。
模型只负责进入列表；字段和正文由项目浏览器工作流直接提取，导出时核对案号、判决书标题、
判决主文、终审落款及哈希。未进行法律评价或结论推断。
""", encoding="utf-8")
    manifest = {str(path.relative_to(output)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in output.rglob("*") if path.is_file()}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "x", zipfile.ZIP_DEFLATED) as z:
        for path in output.rglob("*"):
            if path.is_file():
                z.write(path, path.relative_to(output.parent))
    print(json.dumps({**summary, "output_dir": str(output.resolve()), "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    export(Path(sys.argv[1]), Path(sys.argv[2]))
