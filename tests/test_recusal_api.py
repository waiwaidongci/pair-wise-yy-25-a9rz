import json
import os
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(fd)
os.environ["CORPUS_DB"] = _db_path

import app as app_module


def _post(path, payload):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (RecusalApiTest.port, path),
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (RecusalApiTest.port, path)) as resp:
        return resp.status, json.loads(resp.read())


class RecusalApiTest(unittest.TestCase):
    server = None
    port = 0

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        db = app_module.Handler.db
        db.conn.execute("PRAGMA foreign_keys=OFF")
        db.conn.executescript(
            "DELETE FROM batch_freezes;DELETE FROM gold_records;DELETE FROM discussions;"
            "DELETE FROM adjudications;DELETE FROM recusals;DELETE FROM annotations;"
            "DELETE FROM assignments;DELETE FROM items;DELETE FROM batches;DELETE FROM guidelines;DELETE FROM users;"
        )
        db.conn.commit()
        db.conn.execute("PRAGMA foreign_keys=ON")
        _, r = _post("/api/users", {"name": "标甲", "role": "annotator"})
        self.a1 = r["id"]
        _, r = _post("/api/users", {"name": "标乙", "role": "annotator"})
        self.a2 = r["id"]
        _, r = _post("/api/users", {"name": "仲裁甲", "role": "arbitrator"})
        self.arb = r["id"]
        _, r = _post("/api/users", {"name": "仲裁乙", "role": "arbitrator"})
        self.arb2 = r["id"]
        _, r = _post("/api/users", {"name": "管理员", "role": "manager"})
        self.mgr = r["id"]
        _, r = _post("/api/guidelines", {"version": "g1", "rules": "规则内容足够长"})
        self.g = r["id"]
        _, r = _post("/api/batches", {"name": "接口批次", "guideline_id": self.g})
        self.batch = r["id"]
        _, r = _post("/api/batches/%d/items" % self.batch, {"ordinal": 1, "text": "接口测试文本"})
        self.item = r["id"]
        _post("/api/batches/%d/assign" % self.batch, {"item_id": self.item, "annotator_id": self.a1})
        _post("/api/batches/%d/assign" % self.batch, {"item_id": self.item, "annotator_id": self.a2})
        _post("/api/annotations", {"item_id": self.item, "annotator_id": self.a1, "label": "正向"})
        _post("/api/annotations", {"item_id": self.item, "annotator_id": self.a2, "label": "中性"})

    def test_recusal_end_to_end(self):
        # 仲裁甲先出结论
        status, r = _post("/api/adjudications", {
            "item_id": self.item, "arbitrator_id": self.arb,
            "final_label": "正向", "reason": "明确的正向表达倾向",
        })
        self.assertEqual(201, status)
        first_id = r["id"]

        # 状态区初始待复议为 0
        _, state = _get("/api/state")
        self.assertEqual(0, state["pending_reconsider_count"])

        # 登记回避：回避人、原因、处理人
        status, r = _post("/api/recusals", {
            "item_id": self.item, "arbitrator_id": self.arb,
            "reason": "与标注员存在师生关系", "handler_id": self.mgr,
        })
        self.assertEqual(201, status)
        self.assertIn("id", r)

        # 状态区能看到待复议数量
        _, state = _get("/api/state")
        self.assertEqual(1, state["pending_reconsider_count"])
        self.assertEqual(1, state["batches"][0]["pending_reconsider"])
        self.assertEqual("pending", state["recusals"][0]["status"])

        # 回避人仍可查看材料
        status, view = _get("/api/items/%d?user_id=%d" % (self.item, self.arb))
        self.assertEqual(200, status)
        self.assertTrue(view["viewing_recused"])
        self.assertEqual("接口测试文本", view["text"])

        # 回避人提交结论被拒绝
        status, r = _post("/api/adjudications", {
            "item_id": self.item, "arbitrator_id": self.arb,
            "final_label": "中性", "reason": "他尝试改判为中性标签",
        })
        self.assertEqual(400, status)
        self.assertIn("回避", r["error"])

        # 复议没结束前批次不能冻结
        status, r = _post("/api/batches/%d/freeze" % self.batch, {"manager_id": self.mgr})
        self.assertEqual(400, status)
        self.assertIn("复议", r["error"])

        # 另一位仲裁员重判
        status, r = _post("/api/adjudications", {
            "item_id": self.item, "arbitrator_id": self.arb2,
            "final_label": "中性", "reason": "另一位仲裁员复核后判中性",
        })
        self.assertEqual(201, status)
        second_id = r["id"]
        self.assertNotEqual(first_id, second_id)

        # 待复议清零，可以冻结
        _, state = _get("/api/state")
        self.assertEqual(0, state["pending_reconsider_count"])
        status, _ = _post("/api/batches/%d/freeze" % self.batch, {"manager_id": self.mgr})
        self.assertEqual(200, status)

        # 导出金标准注明最终采用哪份结论
        _, gold = _get("/api/batches/%d/gold" % self.batch)
        record = gold["records"][0]
        self.assertEqual("中性", record["label"])
        self.assertEqual(second_id, record["adjudication_id"])
        self.assertIn("第 2 份结论", record["adoption_note"])
        self.assertEqual(first_id, record["prior_adjudications"][0]["id"])

    def test_recusal_validation_errors(self):
        status, r = _post("/api/recusals", {
            "item_id": self.item, "arbitrator_id": self.a1,
            "reason": "x", "handler_id": self.mgr,
        })
        self.assertEqual(400, status)
        self.assertIn("仲裁员", r["error"])
        status, r = _post("/api/recusals", {
            "item_id": self.item, "arbitrator_id": self.arb,
            "reason": "x", "handler_id": 9999,
        })
        self.assertEqual(400, status)
        self.assertIn("处理人", r["error"])


if __name__ == "__main__":
    unittest.main()
