import os
import tempfile
import unittest
from pathlib import Path

from database import CorpusDB, DomainError


class RecusalFlowTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.arb = self.db.add_user("仲裁", "arbitrator")
        self.arb2 = self.db.add_user("另一仲裁", "arbitrator")
        self.mgr = self.db.add_user("管理", "manager")
        self.g = self.db.add_guideline("v1", "独立标注")
        self.batch = self.db.create_batch("测试批次", self.g)
        self.item = self.db.add_item(self.batch, 1, "这个版本很快。")
        self.db.assign(self.item, self.a1)
        self.db.assign(self.item, self.a2)
        self.db.submit_annotation(self.item, self.a1, "正向")
        self.db.submit_annotation(self.item, self.a2, "中性")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _recuse(self):
        return self.db.register_recusal(self.item, self.arb, "仲裁员是标注员甲的亲属", self.mgr)

    def test_recused_arbitrator_can_view_but_cannot_submit(self):
        self.db.adjudicate(self.item, "正向", "速度描述构成明确正向倾向", self.arb)
        self._recuse()
        # 回避后仍可查看材料
        view = self.db.get_item_for_user(self.item, self.arb)
        self.assertTrue(view["viewing_recused"])
        self.assertEqual(1, len(view["recusals"]))
        self.assertEqual("这个版本很快。", view["text"])
        # 提交结论被拒绝
        with self.assertRaisesRegex(DomainError, "回避"):
            self.db.adjudicate(self.item, "中性", "我改判中性更稳妥一些", self.arb)

    def test_existing_conclusion_returned_to_pending_and_rejudged_keeps_both(self):
        first = self.db.adjudicate(self.item, "正向", "速度描述构成明确正向倾向", self.arb)
        self._recuse()
        self.assertEqual(1, self.db.pending_reconsider_count(self.batch))
        pending = self.db.conn.execute(
            "SELECT status FROM adjudications WHERE id=?", (first,)
        ).fetchone()["status"]
        self.assertEqual("pending_reconsider", pending)
        # 待复议期间不能冻结
        with self.assertRaisesRegex(DomainError, "复议"):
            self.db.freeze_batch(self.batch, self.mgr)
        # 旧结论不算已定稿，分歧重新出现
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        # 另一位仲裁员重判
        second = self.db.adjudicate(self.item, "中性", "措辞克制，中性更符合指南", self.arb2)
        self.assertNotEqual(first, second)
        self.assertEqual(0, self.db.pending_reconsider_count(self.batch))
        versions = self.db.conn.execute(
            "SELECT status,arbitrator_id FROM adjudications WHERE item_id=? ORDER BY id", (self.item,)
        ).fetchall()
        self.assertEqual([("superseded", self.arb), ("active", self.arb2)],
                         [(r["status"], r["arbitrator_id"]) for r in versions])
        # 冻结后金标准采用新结论并注明
        self.db.freeze_batch(self.batch, self.mgr)
        exported = self.db.export_gold(self.batch)
        record = exported["records"][0]
        self.assertEqual("中性", record["label"])
        self.assertEqual(second, record["adjudication_id"])
        self.assertIn("第 2 份结论", record["adoption_note"])
        self.assertEqual(1, len(record["prior_adjudications"]))
        self.assertEqual("正向", record["prior_adjudications"][0]["final_label"])
        self.assertEqual("superseded", record["prior_adjudications"][0]["status"])

    def test_recusal_before_any_conclusion_blocks_only_submission(self):
        rid = self._recuse()
        self.assertEqual(1, self.db.pending_reconsider_count())
        # 没有旧结论，分歧列表本来就存在；非回避仲裁员可直接裁决
        with self.assertRaisesRegex(DomainError, "回避"):
            self.db.adjudicate(self.item, "正向", "回避仲裁员尝试提交", self.arb)
        new_id = self.db.adjudicate(self.item, "正向", "另一位仲裁员裁决正向", self.arb2)
        recusal = self.db.conn.execute("SELECT * FROM recusals WHERE id=?", (rid,)).fetchone()
        self.assertEqual("resolved", recusal["status"])
        self.assertEqual(new_id, recusal["resolved_adjudication_id"])
        self.assertEqual(0, self.db.pending_reconsider_count())

    def test_recusal_validation_and_cannot_freeze_after(self):
        with self.assertRaisesRegex(DomainError, "仲裁员"):
            self.db.register_recusal(self.item, self.a1, "错误角色", self.mgr)
        with self.assertRaisesRegex(DomainError, "处理人"):
            self.db.register_recusal(self.item, self.arb, "原因", 9999)
        with self.assertRaisesRegex(DomainError, "原因"):
            self.db.register_recusal(self.item, self.arb, "  ", self.mgr)
        self._recuse()
        with self.assertRaisesRegex(DomainError, "已登记回避"):
            self._recuse()
        # 冻结后不允许登记回避
        self.db.adjudicate(self.item, "中性", "另一位仲裁员裁决中性", self.arb2)
        self.db.freeze_batch(self.batch, self.mgr)
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.db.register_recusal(self.item, self.arb2, "新发现的利益关系", self.mgr)

    def test_snapshot_exposes_recusals_and_pending_count(self):
        self._recuse()
        snap = self.db.snapshot()
        self.assertEqual(1, snap["pending_reconsider_count"])
        self.assertEqual(1, len(snap["recusals"]))
        self.assertEqual(1, snap["batches"][0]["pending_reconsider"])
        entry = snap["recusals"][0]
        self.assertEqual("仲裁", entry["arbitrator_name"])
        self.assertEqual("管理", entry["handler_name"])
        self.assertEqual("pending", entry["status"])


if __name__ == "__main__":
    unittest.main()
