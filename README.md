# 语料标注与争议仲裁

项目使用 Python 标准库、SQLite 和 `http.server`，实现批次、指南版本、重复标注、分歧检测、仲裁、仲裁员回避与复议、一致性指标、金标准冻结与导出，并以“提交前不可查看含答案讨论”的方式隔离讨论区答案。

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

一致性同时返回逐条成对一致率和 Fleiss Kappa。冻结要求每条至少有两人标注、没有未仲裁分歧、没有未结束的回避复议；冻结后不能修改标注，导出结果来自不可变的 `gold_records`。

## 仲裁员回避

`POST /api/recusals` 为某条目登记回避，参数：`item_id`、`arbitrator_id`（被回避仲裁员）、`reason`、`handler_id`（处理人）。

- 回避后该仲裁员仍可查看条目材料与讨论，但提交仲裁结论会被拒绝；
- 若其已有生效结论，该结论退回“待复议”（状态 `pending_reconsider`，记录保留），分歧重新出现；
- 由另一位仲裁员重新裁决后，旧结论标记为 `superseded` 并留档，新结论为 `active`，同一条目的新旧两份结论都可追溯；
- 复议未结束（存在 `pending` 回避登记）前批次不能冻结；
- 导出金标准时每条记录带 `adoption_note` 注明最终采用第几份结论（`adjudication_id` 与仲裁员），`prior_adjudications` 列出所有历史结论；
- `GET /api/state` 返回 `pending_reconsider_count`（全局待复议数量）与每个批次的 `pending_reconsider`，页面状态区以徽标实时显示。
