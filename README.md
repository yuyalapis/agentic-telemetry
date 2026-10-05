# Agent Telemetry (alpha)

Agentic system 向けの小さな telemetry collector です。イベントを SQLite に保存し、run や agent 単位で参照できます。Python 標準ライブラリだけで動作します。

## 起動

```sh
python3 server.py
```

既定では `http://127.0.0.1:8000` で待ち受け、作業ディレクトリに `telemetry.db` を作成します。`TELEMETRY_HOST`、`TELEMETRY_PORT`、`TELEMETRY_DB` で変更できます。

起動後に `http://127.0.0.1:8000/` を開くとダッシュボードが表示されます。最新1,000件のイベントから run 数、agent 数、エラー、24時間のイベント量、agent 別の活動を確認でき、run/event type/テキストで絞り込めます。10秒ごとに自動更新します。

## イベントを送る

```sh
curl -X POST http://127.0.0.1:8000/v1/events \
  -H 'Content-Type: application/json' \
  -d '{
    "event_type": "tool_call.completed",
    "run_id": "run-123",
    "agent_id": "researcher",
    "parent_agent_id": "orchestrator",
    "task_id": "task-7",
    "status": "success",
    "attributes": {"tool": "web_search", "duration_ms": 240}
  }'
```

1件または最大100件の JSON 配列を受け付けます。`event_type` と `run_id` は必須です。`id` と `timestamp` は省略するとサーバーが生成します。イベント名は `agent.started`、`agent.handoff`、`tool_call.completed`、`llm.response` など、送信側で自由に定義できます。

## イベントを取得する

```sh
curl 'http://127.0.0.1:8000/v1/events?run_id=run-123&limit=100'
```

`run_id`、`agent_id`、`event_type`、`task_id`、`status` を組み合わせて絞り込めます。`limit` は既定100、最大1000です。`GET /health` は稼働確認用です。

## イベント形式

各イベントには `id`、`timestamp`、`event_type`、`run_id`、`agent_id`、`parent_agent_id`、`task_id`、`status`、`attributes` を含められます。agent 間の handoff は同じ `run_id` を使い、`parent_agent_id` と `attributes` に受け渡し元や内容を記録してください。

これは alpha 用の最小構成です。認証、保持期間管理、分散デプロイ、メトリクス集計は含みません。ローカル開発向けに loopback address で待ち受けます。
