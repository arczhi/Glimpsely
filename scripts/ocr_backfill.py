"""Backfill ocr_text for historical events with PaddleOCR, then re-index vectors.

Usage: .venv/bin/python scripts/ocr_backfill.py [--db data/glimpsely.db]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from glimpsely.memory import MemoryStore  # noqa: E402
from glimpsely.ocr import ocr_image_text  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/glimpsely.db")
    ap.add_argument("--only-empty", action="store_true",
                    help="只处理 ocr_text 为空的记录")
    args = ap.parse_args()

    store = MemoryStore(ROOT / args.db)
    sql = ("SELECT id, media_path, ocr_text FROM events"
           " WHERE media_path IS NOT NULL")
    if args.only_empty:
        sql += " AND (ocr_text IS NULL OR length(ocr_text) < 5)"
    rows = store.conn.execute(sql).fetchall()
    print(f"待回填 {len(rows)} 条")

    from glimpsely.embeddings import embed_one
    done, skipped = 0, 0
    for r in rows:
        path = Path(r["media_path"])
        if not path.is_absolute():
            path = ROOT / path
        if not path.exists():
            print(f"[{r['id']}] 媒体缺失，跳过: {r['media_path']}")
            skipped += 1
            continue
        text = ocr_image_text(path)
        if not text:
            print(f"[{r['id']}] OCR 无文字结果，保留原值")
            skipped += 1
            continue
        old_len = len(r["ocr_text"] or "")
        store.conn.execute("UPDATE events SET ocr_text=? WHERE id=?",
                           (text, r["id"]))
        # 重建该事件的向量索引（ocr 参与嵌入文本）
        ev = store.conn.execute(
            "SELECT title, memory_note, entities_json FROM events WHERE id=?",
            (r["id"],)).fetchone()
        embed_text = " ".join(filter(None, [
            ev["title"], ev["memory_note"] or "", text,
            ev["entities_json"] or ""]))
        store.conn.execute("DELETE FROM event_vecs WHERE id=?", (r["id"],))
        store.conn.execute(
            "INSERT INTO event_vecs (id, embedding) VALUES (?,?)",
            (r["id"], embed_one(embed_text)))
        store.conn.commit()
        done += 1
        print(f"[{r['id']}] 回填 {len(text)} 字 (旧 {old_len} 字): {text[:50]}…")
    print(f"\n完成：回填 {done} 条，跳过 {skipped} 条")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
