from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


class CorpusDB:
    """A small multi-annotator corpus governance service."""

    def __init__(self, path: str = "corpus.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('annotator','arbitrator','manager'))
            );
            CREATE TABLE IF NOT EXISTS guidelines (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              version TEXT NOT NULL UNIQUE,
              rules TEXT NOT NULL,
              active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
            );
            CREATE TABLE IF NOT EXISTS batches (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','annotating','frozen')),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS items (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              ordinal INTEGER NOT NULL,
              text TEXT NOT NULL,
              UNIQUE(batch_id, ordinal)
            );
            CREATE TABLE IF NOT EXISTS assignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'assigned' CHECK(status IN ('assigned','submitted')),
              UNIQUE(item_id, annotator_id)
            );
            CREATE TABLE IF NOT EXISTS annotations (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              annotator_id INTEGER NOT NULL REFERENCES users(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              comment TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(item_id, annotator_id, guideline_id)
            );
            CREATE TABLE IF NOT EXISTS adjudications (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              final_label TEXT NOT NULL,
              reason TEXT NOT NULL,
              arbitrator_id INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','pending_reconsider','superseded')),
              recusal_id INTEGER REFERENCES recusals(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recusals (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              arbitrator_id INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL,
              handler_id INTEGER NOT NULL REFERENCES users(id),
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','resolved')),
              resolved_adjudication_id INTEGER REFERENCES adjudications(id),
              created_at TEXT NOT NULL,
              resolved_at TEXT,
              UNIQUE(item_id, arbitrator_id)
            );
            CREATE TABLE IF NOT EXISTS discussions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
              author_id INTEGER NOT NULL REFERENCES users(id),
              body TEXT NOT NULL,
              contains_answer INTEGER NOT NULL DEFAULT 0 CHECK(contains_answer IN (0,1)),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gold_records (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
              item_id INTEGER NOT NULL UNIQUE REFERENCES items(id),
              guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
              label TEXT NOT NULL,
              source TEXT NOT NULL CHECK(source IN ('consensus','adjudication')),
              adjudication_id INTEGER REFERENCES adjudications(id),
              frozen_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batch_freezes (
              batch_id INTEGER PRIMARY KEY REFERENCES batches(id),
              metrics_json TEXT NOT NULL,
              frozen_by INTEGER NOT NULL REFERENCES users(id),
              frozen_at TEXT NOT NULL
            );
            """
        )
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(adjudications)")}
        if "status" not in cols:
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.execute("PRAGMA legacy_alter_table=ON")
            self.conn.executescript(
                """
                ALTER TABLE adjudications RENAME TO adjudications_old;
                CREATE TABLE adjudications (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                  guideline_id INTEGER NOT NULL REFERENCES guidelines(id),
                  final_label TEXT NOT NULL,
                  reason TEXT NOT NULL,
                  arbitrator_id INTEGER NOT NULL REFERENCES users(id),
                  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','pending_reconsider','superseded')),
                  recusal_id INTEGER REFERENCES recusals(id),
                  created_at TEXT NOT NULL
                );
                INSERT INTO adjudications(id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at)
                SELECT id,item_id,guideline_id,final_label,reason,arbitrator_id,created_at FROM adjudications_old;
                DROP TABLE adjudications_old;
                """
            )
            self.conn.execute("PRAGMA legacy_alter_table=OFF")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        a1 = self.add_user("标注员甲", "annotator")
        a2 = self.add_user("标注员乙", "annotator")
        arb = self.add_user("仲裁员", "arbitrator")
        arb2 = self.add_user("仲裁员乙", "arbitrator")
        mgr = self.add_user("管理员", "manager")
        guideline = self.add_guideline("v1", "标签仅可为 正向/负向/中性；先独立标注，不得查看他人答案。")
        batch = self.create_batch("情感标注示例", guideline)
        item1 = self.add_item(batch, 1, "这个更新让工作流畅了很多。")
        item2 = self.add_item(batch, 2, "功能没有变化，但也没有明显问题。")
        self.assign(item1, a1)
        self.assign(item1, a2)
        self.assign(item2, a1)
        self.assign(item2, a2)
        self.submit_annotation(item1, a1, "正向", "整体表达积极")
        self.submit_annotation(item1, a2, "中性", "描述较克制")
        self.submit_annotation(item2, a1, "中性")
        self.submit_annotation(item2, a2, "中性")

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"annotator", "arbitrator", "manager"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def add_guideline(self, version: str, rules: str) -> int:
        if not version.strip() or not rules.strip():
            raise DomainError("指南版本和规则不能为空")
        with self.transaction():
            cur = self.conn.execute("INSERT INTO guidelines(version,rules) VALUES(?,?)", (version.strip(), rules.strip()))
        return int(cur.lastrowid)

    def create_batch(self, name: str, guideline_id: int) -> int:
        if not name.strip() or not self.conn.execute("SELECT 1 FROM guidelines WHERE id=? AND active=1", (guideline_id,)).fetchone():
            raise DomainError("批次名称或指南无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO batches(name,guideline_id,created_at) VALUES(?,?,?)",
                (name.strip(), guideline_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def add_item(self, batch_id: int, ordinal: int, text: str) -> int:
        batch = self.conn.execute("SELECT status FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] == "frozen":
            raise DomainError("批次不存在或已经冻结")
        if ordinal <= 0 or not text.strip():
            raise DomainError("序号必须大于0且文本不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO items(batch_id,ordinal,text) VALUES(?,?,?)", (batch_id, ordinal, text.strip()))
            except sqlite3.IntegrityError as exc:
                raise DomainError("该批次序号已存在") from exc
        return int(cur.lastrowid)

    def assign(self, item_id: int, annotator_id: int) -> int:
        item = self.conn.execute("SELECT batch_id FROM items WHERE id=?", (item_id,)).fetchone()
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (annotator_id,)).fetchone()
        if not item or not user or user["role"] != "annotator":
            raise DomainError("条目不存在或用户不是标注员")
        with self.transaction():
            self.conn.execute("UPDATE batches SET status='annotating' WHERE id=? AND status='draft'", (item["batch_id"],))
            try:
                cur = self.conn.execute(
                    "INSERT INTO assignments(batch_id,item_id,annotator_id) VALUES(?,?,?)",
                    (item["batch_id"], item_id, annotator_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一标注员不能重复领取同一条目") from exc
        return int(cur.lastrowid)

    def submit_annotation(self, item_id: int, annotator_id: int, label: str, comment: str = "") -> int:
        if not label.strip():
            raise DomainError("标签不能为空")
        item = self.conn.execute(
            "SELECT i.*, b.guideline_id, b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        assignment = self.conn.execute(
            "SELECT * FROM assignments WHERE item_id=? AND annotator_id=?", (item_id, annotator_id)
        ).fetchone()
        if not item or not assignment:
            raise DomainError("只能提交已分配条目的标注")
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能修改标注")
        with self.transaction():
            self.conn.execute(
                "UPDATE recusals SET status='pending',resolved_adjudication_id=NULL,resolved_at=NULL "
                "WHERE item_id=? AND resolved_adjudication_id IS NOT NULL",
                (item_id,),
            )
            self.conn.execute("DELETE FROM adjudications WHERE item_id=?", (item_id,))
            try:
                cur = self.conn.execute(
                    "INSERT INTO annotations(item_id,annotator_id,guideline_id,label,comment,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, annotator_id, item["guideline_id"], label.strip(), comment.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError:
                cur = self.conn.execute(
                    "UPDATE annotations SET label=?,comment=?,created_at=? WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (label.strip(), comment.strip(), datetime.now().isoformat(), item_id, annotator_id, item["guideline_id"]),
                )
                annotation_id = self.conn.execute(
                    "SELECT id FROM annotations WHERE item_id=? AND annotator_id=? AND guideline_id=?",
                    (item_id, annotator_id, item["guideline_id"]),
                ).fetchone()["id"]
            else:
                annotation_id = int(cur.lastrowid)
            self.conn.execute("UPDATE assignments SET status='submitted' WHERE id=?", (assignment["id"],))
        return int(annotation_id)

    def add_discussion(self, item_id: int, author_id: int, body: str, contains_answer: bool = False) -> int:
        if not body.strip():
            raise DomainError("讨论内容不能为空")
        if not self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
            raise DomainError("条目不存在")
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (author_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO discussions(item_id,author_id,body,contains_answer,created_at) VALUES(?,?,?,?,?)",
                (item_id, author_id, body.strip(), int(contains_answer), datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def get_item_for_user(self, item_id: int, user_id: int) -> dict:
        item = self.conn.execute(
            "SELECT i.id,i.batch_id,i.ordinal,i.text,g.version AS guideline_version,g.rules "
            "FROM items i JOIN batches b ON b.id=i.batch_id JOIN guidelines g ON g.id=b.guideline_id WHERE i.id=?",
            (item_id,),
        ).fetchone()
        if not item:
            raise DomainError("条目不存在")
        own = self.conn.execute(
            "SELECT id,label,comment,created_at FROM annotations WHERE item_id=? AND annotator_id=?", (item_id, user_id)
        ).fetchone()
        revealed = own is not None
        discussions = []
        for row in self.conn.execute(
            "SELECT d.*,u.name FROM discussions d JOIN users u ON u.id=d.author_id WHERE d.item_id=? ORDER BY d.id", (item_id,)
        ).fetchall():
            if row["contains_answer"] and not revealed:
                discussions.append({"id": row["id"], "author": row["name"], "body": "提交自己的标注后才能查看此讨论", "hidden": True})
            else:
                discussions.append(dict(row))
        payload = dict(item)
        payload["own_annotation"] = dict(own) if own else None
        payload["discussions"] = discussions
        recusals = []
        for row in self.conn.execute(
            "SELECT r.id,r.arbitrator_id,r.reason,r.handler_id,r.status,r.created_at,u.name AS arbitrator_name "
            "FROM recusals r JOIN users u ON u.id=r.arbitrator_id WHERE r.item_id=? ORDER BY r.id", (item_id,)
        ).fetchall():
            entry = dict(row)
            entry["recused"] = row["arbitrator_id"] == user_id
            recusals.append(entry)
        payload["recusals"] = recusals
        payload["viewing_recused"] = any(r["recused"] for r in recusals)
        active = self.conn.execute(
            "SELECT a.*,u.name AS arbitrator_name FROM adjudications a JOIN users u ON u.id=a.arbitrator_id "
            "WHERE a.item_id=? AND a.status='active'", (item_id,)
        ).fetchone()
        payload["adjudication"] = dict(active) if active else None
        return payload

    def disagreements(self, batch_id: int) -> list[dict]:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        result = []
        for item in self.conn.execute("SELECT * FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall():
            rows = self.conn.execute(
                "SELECT a.*,u.name FROM annotations a JOIN users u ON u.id=a.annotator_id "
                "WHERE a.item_id=? AND a.guideline_id=? ORDER BY a.id",
                (item["id"], batch["guideline_id"]),
            ).fetchall()
            labels = {row["label"] for row in rows}
            adj = self.conn.execute(
                "SELECT * FROM adjudications WHERE item_id=? AND status='active'", (item["id"],)
            ).fetchone()
            if len(rows) >= 2 and len(labels) > 1 and not adj:
                result.append({
                    "item_id": item["id"], "ordinal": item["ordinal"], "text": item["text"],
                    "labels": [dict(row) for row in rows],
                })
        return result

    def register_recusal(self, item_id: int, arbitrator_id: int, reason: str, handler_id: int) -> int:
        item = self.conn.execute(
            "SELECT batch_id,status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (arbitrator_id,)).fetchone()
        handler = self.conn.execute("SELECT 1 FROM users WHERE id=?", (handler_id,)).fetchone()
        if not item:
            raise DomainError("条目不存在")
        if not user or user["role"] != "arbitrator":
            raise DomainError("回避人必须是仲裁员")
        if not handler:
            raise DomainError("处理人不存在")
        if not reason.strip():
            raise DomainError("回避原因不能为空")
        if item["status"] == "frozen":
            raise DomainError("批次已冻结，不能登记回避")
        now = datetime.now().isoformat()
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO recusals(item_id,arbitrator_id,reason,handler_id,status,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, arbitrator_id, reason.strip(), handler_id, "pending", now),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该仲裁员在此条目上已登记回避") from exc
            recusal_id = int(cur.lastrowid)
            # 已有结论先退回待复议，结论本身保留（新旧两份结果都留档）
            self.conn.execute(
                "UPDATE adjudications SET status='pending_reconsider' WHERE item_id=? AND status='active'",
                (item_id,),
            )
        return recusal_id

    def _recusal_guard(self, item_id: int, arbitrator_id: int) -> None:
        row = self.conn.execute(
            "SELECT status FROM recusals WHERE item_id=? AND arbitrator_id=?", (item_id, arbitrator_id)
        ).fetchone()
        if row:
            raise DomainError("该仲裁员与本条目标注存在利益关系，已登记回避，不能提交结论（仍可查看材料）")

    def adjudicate(self, item_id: int, final_label: str, reason: str, arbitrator_id: int) -> int:
        user = self.conn.execute("SELECT role FROM users WHERE id=?", (arbitrator_id,)).fetchone()
        item = self.conn.execute(
            "SELECT i.*,b.guideline_id,b.status FROM items i JOIN batches b ON b.id=i.batch_id WHERE i.id=?", (item_id,)
        ).fetchone()
        if not item or not user or user["role"] != "arbitrator":
            raise DomainError("条目或仲裁员无效")
        self._recusal_guard(item_id, arbitrator_id)
        if item["status"] == "frozen":
            raise DomainError("冻结批次不能重新仲裁")
        rows = self.conn.execute("SELECT label FROM annotations WHERE item_id=?", (item_id,)).fetchall()
        if len(rows) < 2:
            raise DomainError("至少需要两份标注才能仲裁")
        if not final_label.strip() or len(reason.strip()) < 5:
            raise DomainError("最终标签必填，仲裁理由至少5个字符")
        now = datetime.now().isoformat()
        with self.transaction():
            # 重判：原结论（含退回待复议的）保留为历史版本
            self.conn.execute(
                "UPDATE adjudications SET status='superseded' WHERE item_id=? AND status IN ('active','pending_reconsider')",
                (item_id,),
            )
            open_recusals = self.conn.execute(
                "SELECT id FROM recusals WHERE item_id=? AND status='pending'", (item_id,)
            ).fetchall()
            linked_recusal = open_recusals[0]["id"] if open_recusals else None
            cur = self.conn.execute(
                "INSERT INTO adjudications(item_id,guideline_id,final_label,reason,arbitrator_id,status,recusal_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (item_id, item["guideline_id"], final_label.strip(), reason.strip(), arbitrator_id,
                 "active", linked_recusal, now),
            )
            adjudication_id = int(cur.lastrowid)
            for recusal in open_recusals:
                self.conn.execute(
                    "UPDATE recusals SET status='resolved',resolved_adjudication_id=?,resolved_at=? WHERE id=?",
                    (adjudication_id, now, recusal["id"]),
                )
        return adjudication_id

    def consistency(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch:
            raise DomainError("批次不存在")
        items = self.conn.execute("SELECT id,ordinal FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        per_item, agreement_pairs, total_pairs = [], 0, 0
        label_totals: Counter[str] = Counter()
        all_annotation_count = 0
        for item in items:
            labels = [r["label"] for r in self.conn.execute(
                "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
            ).fetchall()]
            if len(labels) < 2:
                per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": None, "annotations": len(labels)})
                continue
            pairs = total = 0
            for i in range(len(labels)):
                for j in range(i + 1, len(labels)):
                    total += 1
                    pairs += labels[i] == labels[j]
            agreement = pairs / total
            agreement_pairs += pairs
            total_pairs += total
            all_annotation_count += len(labels)
            label_totals.update(labels)
            per_item.append({"item_id": item["id"], "ordinal": item["ordinal"], "agreement": round(agreement, 4), "annotations": len(labels)})
        pairwise = agreement_pairs / total_pairs if total_pairs else None
        expected = sum((count / all_annotation_count) ** 2 for count in label_totals.values()) if all_annotation_count else None
        kappa = None
        if pairwise is not None and expected is not None and expected < 1:
            kappa = (pairwise - expected) / (1 - expected)
        return {
            "batch_id": batch_id,
            "items_with_multiple_annotations": total_pairs and sum(1 for row in per_item if row["agreement"] is not None),
            "pairwise_agreement": round(pairwise, 4) if pairwise is not None else None,
            "fleiss_kappa": round(kappa, 4) if kappa is not None else None,
            "items": per_item,
        }

    def freeze_batch(self, batch_id: int, manager_id: int) -> dict:
        manager = self.conn.execute("SELECT role FROM users WHERE id=?", (manager_id,)).fetchone()
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or not manager or manager["role"] != "manager":
            raise DomainError("批次或管理员无效")
        if batch["status"] == "frozen":
            raise DomainError("批次已冻结")
        items = self.conn.execute("SELECT id FROM items WHERE batch_id=? ORDER BY ordinal", (batch_id,)).fetchall()
        if not items:
            raise DomainError("空批次不能冻结")
        item_ids = [row["id"] for row in items]
        placeholders = ",".join("?" for _ in item_ids)
        pending = self.conn.execute(
            f"SELECT COUNT(*) FROM recusals WHERE status='pending' AND item_id IN ({placeholders})", item_ids
        ).fetchone()[0]
        if pending:
            raise DomainError(f"仍有 {pending} 条回避复议未结束，不能冻结")
        disagreements = self.disagreements(batch_id)
        if disagreements:
            raise DomainError(f"仍有 {len(disagreements)} 条分歧未仲裁")
        missing = []
        for item in items:
            count = self.conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
            ).fetchone()[0]
            if count < 2:
                missing.append(item["id"])
        if missing:
            raise DomainError(f"条目缺少至少两份标注: {missing}")
        metrics = self.consistency(batch_id)
        frozen_at = datetime.now().isoformat()
        with self.transaction():
            for item in items:
                labels = [r["label"] for r in self.conn.execute(
                    "SELECT label FROM annotations WHERE item_id=? AND guideline_id=?", (item["id"], batch["guideline_id"])
                ).fetchall()]
                adj = self.conn.execute(
                    "SELECT * FROM adjudications WHERE item_id=? AND status='active'", (item["id"],)
                ).fetchone()
                if adj:
                    label, source, adj_id = adj["final_label"], "adjudication", adj["id"]
                else:
                    label, source, adj_id = labels[0], "consensus", None
                self.conn.execute(
                    "INSERT INTO gold_records(batch_id,item_id,guideline_id,label,source,adjudication_id,frozen_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, item["id"], batch["guideline_id"], label, source, adj_id, frozen_at),
                )
            self.conn.execute(
                "INSERT INTO batch_freezes(batch_id,metrics_json,frozen_by,frozen_at) VALUES(?,?,?,?)",
                (batch_id, json.dumps(metrics, ensure_ascii=False), manager_id, frozen_at),
            )
            self.conn.execute("UPDATE batches SET status='frozen' WHERE id=?", (batch_id,))
        return {"batch_id": batch_id, "metrics": metrics, "frozen_at": frozen_at}

    def export_gold(self, batch_id: int) -> dict:
        batch = self.conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not batch or batch["status"] != "frozen":
            raise DomainError("只有已冻结批次可以导出金标准")
        freeze = self.conn.execute("SELECT * FROM batch_freezes WHERE batch_id=?", (batch_id,)).fetchone()
        rows = self.conn.execute(
            "SELECT g.item_id,i.ordinal,i.text,g.label,g.source,g.frozen_at,"
            "g.adjudication_id,a.arbitrator_id,u.name AS arbitrator_name,a.created_at AS adjudicated_at "
            "FROM gold_records g JOIN items i ON i.id=g.item_id "
            "LEFT JOIN adjudications a ON a.id=g.adjudication_id "
            "LEFT JOIN users u ON u.id=a.arbitrator_id "
            "WHERE g.batch_id=? ORDER BY i.ordinal", (batch_id,)
        ).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            adopted_id = record.get("adjudication_id")
            history = []
            if adopted_id is not None:
                old_rows = self.conn.execute(
                    "SELECT a.id,a.final_label,a.reason,a.arbitrator_id,u.name AS arbitrator_name,a.status,a.created_at "
                    "FROM adjudications a LEFT JOIN users u ON u.id=a.arbitrator_id "
                    "WHERE a.item_id=? AND a.id!=? ORDER BY a.id",
                    (record["item_id"], adopted_id),
                ).fetchall()
                history = [dict(r) for r in old_rows]
                record["adopted_adjudication_id"] = adopted_id
                record["adoption_note"] = (
                    f"最终采用第 {len(history) + 1} 份结论（adjudication_id={adopted_id}，"
                    f"仲裁员：{record['arbitrator_name']}）；该条目共 {len(history) + 1} 份结论，旧版本已保留"
                )
            else:
                record["adoption_note"] = "标注一致，采用共识标签"
            record["prior_adjudications"] = history
            records.append(record)
        return {
            "batch_id": batch_id, "batch_name": batch["name"], "frozen_at": freeze["frozen_at"],
            "metrics": json.loads(freeze["metrics_json"]), "records": records,
        }

    def pending_reconsider_count(self, batch_id: int | None = None) -> int:
        if batch_id is None:
            return int(self.conn.execute(
                "SELECT COUNT(*) FROM recusals WHERE status='pending'"
            ).fetchone()[0])
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM recusals r JOIN items i ON i.id=r.item_id "
            "WHERE i.batch_id=? AND r.status='pending'", (batch_id,)
        ).fetchone()[0])

    def snapshot(self) -> dict:
        recusals = [dict(r) for r in self.conn.execute(
            "SELECT r.*,u.name AS arbitrator_name,h.name AS handler_name FROM recusals r "
            "JOIN users u ON u.id=r.arbitrator_id JOIN users h ON h.id=r.handler_id ORDER BY r.id"
        )]
        adjudications = [dict(r) for r in self.conn.execute(
            "SELECT a.id,a.item_id,a.final_label,a.reason,a.arbitrator_id,u.name AS arbitrator_name,"
            "a.status,a.recusal_id,a.created_at FROM adjudications a JOIN users u ON u.id=a.arbitrator_id ORDER BY a.id"
        )]
        batches = [dict(r) for r in self.conn.execute("SELECT * FROM batches ORDER BY id")]
        pending_by_batch = {
            row["batch_id"]: row["n"] for row in self.conn.execute(
                "SELECT i.batch_id,COUNT(*) AS n FROM recusals r JOIN items i ON i.id=r.item_id "
                "WHERE r.status='pending' GROUP BY i.batch_id"
            )
        }
        for batch in batches:
            batch["pending_reconsider"] = pending_by_batch.get(batch["id"], 0)
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "guidelines": [dict(r) for r in self.conn.execute("SELECT * FROM guidelines ORDER BY id")],
            "batches": batches,
            "items": [dict(r) for r in self.conn.execute("SELECT * FROM items ORDER BY batch_id,ordinal")],
            "adjudications": adjudications,
            "recusals": recusals,
            "pending_reconsider_count": sum(pending_by_batch.values()),
        }
