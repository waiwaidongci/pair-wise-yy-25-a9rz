import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CorpusDB, DomainError


class CorpusFlowTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.arb = self.db.add_user("仲裁", "arbitrator")
        self.mgr = self.db.add_user("管理", "manager")
        self.g = self.db.add_guideline("v1", "独立标注")
        self.batch = self.db.create_batch("测试批次", self.g)
        self.item1 = self.db.add_item(self.batch, 1, "这个版本很快。")
        self.item2 = self.db.add_item(self.batch, 2, "没有明显变化。")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_full_annotation_disagreement_adjudication_freeze_flow(self):
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "中性")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        self.assertEqual(1, len(self.db.disagreements(self.batch)))
        with self.assertRaisesRegex(DomainError, "分歧"):
            self.db.freeze_batch(self.batch, self.mgr)
        self.db.adjudicate(self.item1, "正向", "速度描述构成明确正向倾向", self.arb)
        result = self.db.freeze_batch(self.batch, self.mgr)
        self.assertIsNotNone(result["metrics"]["pairwise_agreement"])
        exported = self.db.export_gold(self.batch)
        self.assertEqual(2, len(exported["records"]))
        self.assertEqual("adjudication", exported["records"][0]["source"])

    def test_answer_isolation_and_role_validation(self):
        self.db.assign(self.item1, self.a1)
        self.db.assign(self.item1, self.a2)
        self.db.add_discussion(self.item1, self.a2, "我认为是正向", True)
        secret = self.db.get_item_for_user(self.item1, self.a1)
        self.assertTrue(secret["discussions"][0]["hidden"])
        self.db.submit_annotation(self.item1, self.a1, "负向")
        visible = self.db.get_item_for_user(self.item1, self.a1)
        self.assertFalse(visible["discussions"][0].get("hidden", False))
        with self.assertRaisesRegex(DomainError, "标注员"):
            self.db.assign(self.item2, self.arb)

    def test_recusal_return_readjudicate_freeze_and_export(self):
        arb2 = self.db.add_user("仲裁二", "arbitrator")
        for item in (self.item1, self.item2):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
        self.db.submit_annotation(self.item1, self.a1, "正向")
        self.db.submit_annotation(self.item1, self.a2, "负向")
        self.db.submit_annotation(self.item2, self.a1, "中性")
        self.db.submit_annotation(self.item2, self.a2, "中性")
        first = self.db.adjudicate(self.item1, "正向", "描述明确表达满意", self.arb)

        # 登记回避后，已有结论退回待复议，状态区可见数量
        self.db.register_recusal(self.item1, self.arb, "与条目作者存在利益关系", self.mgr)
        self.assertEqual(1, self.db.snapshot()["pending_reviews"])

        # 回避后仍可查看材料，但提交结论被拒绝
        self.assertEqual(self.item1, self.db.get_item_for_user(self.item1, self.arb)["id"])
        with self.assertRaisesRegex(DomainError, "回避"):
            self.db.adjudicate(self.item1, "负向", "回避后仍尝试提交结论", self.arb)

        # 复议未结束不能冻结
        with self.assertRaisesRegex(DomainError, "待复议"):
            self.db.freeze_batch(self.batch, self.mgr)

        # 另一位仲裁员重判，新旧两份结论都保留
        second = self.db.adjudicate(self.item1, "负向", "复核后应判为负向", arb2)
        statuses = [r["status"] for r in self.db.conn.execute(
            "SELECT status FROM adjudications WHERE item_id=? ORDER BY id", (self.item1,)
        )]
        self.assertEqual(["superseded", "active"], statuses)
        self.assertEqual(0, self.db.snapshot()["pending_reviews"])

        # 冻结后导出注明最终采用的结论
        self.db.freeze_batch(self.batch, self.mgr)
        record = self.db.export_gold(self.batch)["records"][0]
        self.assertEqual("adjudication", record["source"])
        self.assertEqual(second, record["adjudication_id"])
        self.assertNotEqual(first, record["adjudication_id"])
        self.assertEqual("仲裁二", record["arbitrator"])

    def test_recusal_registration_validation(self):
        with self.assertRaisesRegex(DomainError, "仲裁员"):
            self.db.register_recusal(self.item1, self.a1, "利益关系", self.mgr)
        with self.assertRaisesRegex(DomainError, "管理员"):
            self.db.register_recusal(self.item1, self.arb, "利益关系", self.a1)
        with self.assertRaisesRegex(DomainError, "原因"):
            self.db.register_recusal(self.item1, self.arb, "  ", self.mgr)
        self.db.register_recusal(self.item1, self.arb, "利益关系", self.mgr)
        with self.assertRaisesRegex(DomainError, "已登记"):
            self.db.register_recusal(self.item1, self.arb, "重复登记", self.mgr)


if __name__ == "__main__":
    unittest.main()
