"""Small, inspectable local Markdown retrieval; no network or business writes."""

import hashlib
import sqlite3
import time
import math
import re
from collections import Counter
from pathlib import Path


def terms(text):
    text = text.lower()
    for triggers, expansion in [
        (("晾", "晒"), " 晾晒 阴干 晒干"),
        (("洗", "清洁"), " 清洗 洗护 护理"),
        (("肩膀", "肩部"), " 肩部 肩膀"),
        (("退钱", "退款"), " 退款 退钱"),
        (("换码", "尺码不合"), " 换码 尺码 退换"),
    ]:
        if any(trigger in text for trigger in triggers):
            text += expansion
    words = re.findall(r"[a-z0-9]+", text)
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(part[i:i + 2] for i in range(len(part) - 1))
    return Counter(words)


class Knowledge:
    def __init__(self, extra_dir=None, db_path=None):
        self.extra_dir = extra_dir
        self.db_path = str(db_path) if db_path else None
        self.chunks = []
        self.documents = []
        directories = [Path(__file__).parent / "knowledge"]
        if extra_dir:
            directory = Path(extra_dir)
            if not directory.is_dir():
                raise ValueError("WANG_KNOWLEDGE_DIR 必须指向存在的资料目录。")
            directories.append(directory)
        for directory in directories:
            for path in sorted(directory.glob("*.md"))[:100]:
                if path.is_symlink() or path.stat().st_size > 100_000:
                    continue
                text = path.read_text(encoding="utf-8-sig")
                heading = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
                title = heading.group(1).strip()[:120] if heading else path.stem
                doc_id = f"K{len(self.documents) + 1:03}"
                self.documents.append({"id": doc_id, "title": title, "file": path.name})
                # Keep headings with each paragraph and bound context volume.
                for paragraph in re.split(r"\n\s*\n", text):
                    paragraph = paragraph.strip()
                    if not paragraph or paragraph.startswith("# ") and "\n" not in paragraph:
                        continue
                    for offset in range(0, len(paragraph), 1000):
                        content = paragraph[offset:offset + 1000]
                        self.chunks.append({"id": doc_id, "title": title, "content": content,
                                            "terms": terms(title + " " + content)})
        if self.db_path:
            with sqlite3.connect(self.db_path) as db:
                db.execute("CREATE TABLE IF NOT EXISTS kb_documents (id TEXT PRIMARY KEY, title TEXT, content TEXT, product_id TEXT, created REAL)")
                rows = db.execute("SELECT id,title,content,product_id FROM kb_documents ORDER BY created,id").fetchall()
            for doc_id, title, text, product_id in rows:
                self.documents.append({"id": doc_id, "title": title, "file": title,
                                       "product_id": product_id, "imported": True})
                for offset in range(0, len(text), 700):
                    content = text[offset:offset + 850]
                    self.chunks.append({"id": doc_id, "title": title, "content": content,
                                        "product_id": product_id, "terms": terms(title + " " + content)})
        self.frequency = Counter()
        for chunk in self.chunks:
            self.frequency.update(chunk["terms"].keys())

    def import_document(self, title, content, product_id=""):
        if not self.db_path:
            raise ValueError("知识库持久化未配置。")
        title, content = title.strip(), content.strip()
        if not title or len(title) > 120 or not content or len(content) > 100_000 or "\x00" in content:
            raise ValueError("资料标题1～120字，正文1～100000字，不能含空字符。")
        doc_id = "K" + hashlib.sha256((product_id + "\0" + title + "\0" + content).encode()).hexdigest()[:16]
        with sqlite3.connect(self.db_path) as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM kb_documents WHERE id=?", (doc_id,)).fetchone():
                if db.execute("SELECT COUNT(*) FROM kb_documents").fetchone()[0] >= 100:
                    raise ValueError("最多导入100份资料，请先移除旧资料。")
                db.execute("INSERT INTO kb_documents VALUES (?,?,?,?,?)", (doc_id, title, content, product_id, time.time()))
        return doc_id

    def remove_document(self, doc_id):
        if not self.db_path:
            raise ValueError("知识库持久化未配置。")
        with sqlite3.connect(self.db_path) as db:
            cursor = db.execute("DELETE FROM kb_documents WHERE id=?", (doc_id,))
            if not cursor.rowcount:
                raise ValueError("导入资料不存在；内置资料不能在这里移除。")

    def search(self, query, limit=4, product_id=None):
        query_terms = terms(query)
        eligible = [c for c in self.chunks if not c.get("product_id") or c["product_id"] == product_id]
        average = sum(sum(c["terms"].values()) for c in eligible) / max(len(eligible), 1)
        ranked = []
        for chunk in eligible:
            length = sum(chunk["terms"].values())
            # BM25-style term frequency saturation and document-length normalization.
            score = sum(
                math.log(1 + (len(self.chunks) - self.frequency[word] + .5) / (self.frequency[word] + .5))
                * chunk["terms"][word] * 2.2
                / (chunk["terms"][word] + 1.2 * (.25 + .75 * length / max(average, 1)))
                for word in query_terms if chunk["terms"][word]
            )
            if score > 0:
                ranked.append((score, chunk))
        ranked.sort(key=lambda row: row[0], reverse=True)
        return [{k: v for k, v in chunk.items() if k != "terms"} for _, chunk in ranked[:limit]]
