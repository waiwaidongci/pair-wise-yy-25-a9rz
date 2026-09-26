# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、仲裁、一致性指标、金标准冻结与导出，并以“提交前不可查看含答案讨论”的方式隔离讨论区答案。

## 启动

```bash
python app.py
```

默认地址 <http://127.0.0.1:8112>，默认数据库为 `corpus.db`。首次启动会写入两位标注员、一位仲裁员和一个含分歧的示例批次。

```bash
PORT=9002 CORPUS_DB=/tmp/corpus.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试包括：分配、标注、发现分歧、阻止提前冻结、仲裁、计算一致性、冻结和导出；另一条测试验证提交答案前后讨论可见性变化，以及错误角色不能领取标注任务。

## 接口

- `POST /api/users`、`POST /api/guidelines`、`POST /api/batches`
- `POST /api/batches/{id}/items`、`POST /api/batches/{id}/assign`
- `POST /api/annotations`、`POST /api/adjudications`、`POST /api/recusals`
- `GET /api/items/{id}?user_id=`
- `GET /api/batches/{id}/disagreements`
- `GET /api/batches/{id}/consistency`
- `POST /api/batches/{id}/freeze`
- `GET /api/batches/{id}/gold`

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、没有未仲裁分歧；冻结后不能修改标注，导出结果来自不可变的 `gold_records`。

## 仲裁回避与复议

- `POST /api/recusals` 为争议条目登记回避：`item_id`、`arbitrator_id`（回避人，须为仲裁员）、`reason`（原因）、`handler_id`（处理人，须为管理员）。
- 登记后该仲裁员仍可查看条目材料，但提交结论会被拒绝；若他已有生效结论，该结论退回为 `returned`（待复议）。
- 由另一位仲裁员重判，旧结论保留为 `superseded`，新结论为 `active`，新旧两份均留档。
- 批次内存在待复议结论时不能冻结；导出金标准的每条记录通过 `adjudication_id` 与 `arbitrator` 注明最终采用了哪份结论。
- `/api/state` 返回 `pending_reviews`（待复议数量）、`recusals` 和 `adjudications` 历史，页面状态区可见。
