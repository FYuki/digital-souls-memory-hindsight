# digital-souls-memory-hindsight

[digital-souls-memory](https://github.com/FYuki/digital-souls-memory) のドメインモデル Phase 1（一貫性の土台）を、
[Hindsight](https://github.com/vectorize-io/hindsight) を記憶処理の中核にして実現できるかを示す試作です。

Hindsight はフォークせず、別プロセスのサーバーと公式クライアント（`hindsight-client==1.1.0`）として使います。
会話の正本（Evidence 台帳）は Hindsight の外側に追記専用で置き、Hindsight はエピソード記憶・意味記憶を抽出する仕組みとして使います。

## ドキュメント

- [受け入れ条件（草案）](docs/acceptance-criteria.md)：前提・受け入れ条件・既知の問題・未決事項
- [開発規約](CONTRIBUTING.md)

## セットアップ

Python **3.12.3**、uv **0.8.22** を使います。

```sh
python3 -m pip install uv==0.8.22
uv sync --frozen
uv run --no-sync pytest -m 'ut and not it2 and not st' -q
```

## サービスの起動

台帳用DBのURLを環境変数で渡します。プロジェクトはinstallせず、起動スクリプトが
絶対パスの `src` を `PYTHONPATH` に設定します。

```sh
export DATABASE_URL=postgresql://localhost/ledger
export EVIDENCE_CHARACTER_IDS='["char-a","char-b"]'
export HINDSIGHT_URL=http://localhost:8888
PYTHONPATH="$PWD/src" uv run --no-sync alembic upgrade head
bash scripts/run-service.sh
```

`GET http://localhost:8000/health` は `{"status":"ok"}` を返します。
healthはプロセスの応答確認であり、DB接続の確認はIT1で行います。
初期revisionは移行履歴だけを作り、続くrevisionがSession・Turn・DeliveryReceipt・SessionEndingの
4テーブルを台帳DBに作ります。通常のUPDATE・DELETE・TRUNCATEはDBでも拒否します。

キャラ判定には `EVIDENCE_CHARACTER_IDS` のJSON配列を使います。入力本文からは推測しません。
未設定・不正設定ではTurn登録を503にし、明示的な空配列 `[]` とは区別します。
この設定はmigrationとhealthの利用条件ではありません。設定変更はサービス再起動時に読み込みます。

## Evidence API

| 操作 | 経路 | 成功時 |
|---|---|---|
| Session登録・取得 | `POST /sessions`、`GET /sessions/{session_id}` | 201・200 |
| 終了報告・一覧 | `POST /sessions/{session_id}/endings`、`GET /sessions/{session_id}/endings` | 新規201・同一再送200・一覧200 |
| Turn登録・取得 | `POST /turns`、`GET /turns/{turn_id}` | 201・200 |
| Receipt登録・一覧 | `POST /turns/{turn_id}/receipts`、`GET /turns/{turn_id}/receipts` | 201・200 |
| 知覚したTurnの一覧 | `GET /characters/{character_id}/turns` | 200 |

Sessionは `session_id`、タイムゾーン付き `started_at`、`surface`、必須の真偽値
`reports_delivery`、任意の `ended_at` を受け付けます。宣言の変更はできません。
Turnは原文と知覚範囲を追記保存し、同じ `turn_id`・同内容の再送では元の `recorded_at` を返します。
異なる内容の再登録は409です。Turnの内容を更新するAPIはありません。
キャラの `address_mode` は必須で、利用者の省略・nullは宛先が空の場合だけbroadcastになります。
Receiptは `status`、文字数の `spoken_until`、`reported_by`、タイムゾーン付き `reported_at` を受け付け、
報告不要のSessionとthoughtには422を返します。原文・知覚範囲・宛先は変更しません。
終了報告は `ended_at`・`reported_by` を受け付け、同内容の再送は元の `reported_at` を保持します。
キャラ別の投影先と本文の選択は後続Issueの対象です。

## 配達範囲と期限処理

`0003_delivery_scope` はTurnとは別の追記専用テーブルに配達状態を保存します。
状態は `immediate`（即時確定）、`pending`（保留）、`receipt_confirmed`（Receipt確定）、
`assumed_confirmed`（みなし確定）です。thoughtには他キャラ向けの配達記録を作りません。

`EVIDENCE_DELIVERY_TIMEOUT_SECONDS` は正の整数で、未指定時は600秒です。
報告ありSessionのキャラ発話だけを保留し、利用者発話・observation・報告不要Sessionは
登録時に全文を即時確定します。期限は `recorded_at` を基準にし、期限到達時に明示的な
期限処理を呼ぶと未着のTurnを全文のみなし確定にします。

同じサービス内の投影処理やworkerは、次の内部関数を利用します。

```python
from digital_souls_memory_hindsight.app import ledger_service

scope = ledger_service.get_delivery_scope("synthetic-turn")
text = ledger_service.get_projection_text("synthetic-turn", "char-b")
confirmed_turn_ids = ledger_service.confirm_expired_deliveries()
```

問い合わせは保存済みの `perceived_by` を使い、発言者本人には全文、他キャラには確定範囲までを
返します。Receipt noneは空文字列、保留・知覚範囲外は `None` です。原文APIは全文を保持します。
Receiptと期限処理は同じTurnをロックし、既存の確定記録を上書きせず追記します。
複数・遅着Receiptは台帳の受理順で最新範囲に反映し、先行するみなし確定の履歴を残します。
配達記録がない既存Turnへのbackfillや全文確定の推測は行いません。

テストでは `EvidenceService(..., clock=clock, delivery_timeout_seconds=42)` で時計と待機時間を
注入できます。同じプロセス内のworkerが各周期で期限処理を呼びます。

## outbox と worker

`ledger_service.add_turn(turn_input)` または既存の HTTP API で Turn を登録すると、
知覚者ごとの投影操作を台帳DBの outbox に自動保存します。キャラ ID から bank ID、
Session ID から document ID を決定的に導き、初回の送信時に bank を作成します。
Turn・初期配達範囲・操作は同じトランザクションで保存されます。HTTP の入力は変更しません。
同じ Turn・bank・document の再送は同じ操作になり、本文の異なる再登録は拒否します。

FastAPI の lifespan が worker を開始し、停止時にタスクと公式SDKクライアントを解放します。
worker は `run_once()` で1回だけ処理できます。各周期でみなし確定も進め、DB処理は
別スレッドで実行します。取得は `FOR UPDATE SKIP LOCKED`、リースは60秒です。
停止後は期限切れの操作を再取得し、旧所有者による完了・失敗の更新を拒否します。

通常の retain は非同期 append として保存済みの operation_id・本文・occurred_at を送信します。
他キャラへの投影は配達範囲の確定まで保留し、本人には全文を送ります。
投影後の配達範囲が変わると、影響するキャラの Session 文書を台帳から replace で
作り直します。同じ範囲の Receipt では再構築しません。再構築の本文も保存済みの
snapshot を再送し、bank 内の先行操作・自動統合が完了するまで後続を進めません。
bank の mission・disposition を人格の正本には使いません。
受付だけでは完了にせず、operation の完了を確認します。処理中・通信失敗は1秒後に
同じ識別子で再試行し、試行回数・次回時刻・原因の分類を保存します。
Hindsight の failed／cancelled は outbox の failed に残し、自動で成功扱いにはしません。
`HINDSIGHT_URL` 未設定でもmigration・healthは利用できますが、取り込みは
`not_configured` として再試行待ちになります。本文や未加工の外部例外は失敗情報へ保存しません。

## IT1（実PostgreSQL・Hindsight、モデルはスタブ）

Docker EngineとDocker Composeを用意し、次を実行します。CIの `it1` も同じ入口を使います。

```sh
bash scripts/run-it1.sh
```

ビルド・pull・依存取得の後、PostgreSQL・Hindsight・スタブ・サービスと試験コンテナを
`internal: true` の専用ネットワークで起動します。ホストへのポート公開はしません。
台帳の `ledger` とHindsightの `hindsight` は同一PostgreSQLインスタンス上の別DBです。
DBのDockerfileとpackages.lockは `digital-souls-memory@a5e386c` から変更せず取り込んでいます。
Hindsightは指定digestの `1.1.0-slim`、全文検索はPGroonga、rerankerはrrfを使い、
LLM・embeddingだけを標準ライブラリ製のOpenAI互換スタブへ向けます。自動統合は有効のままです。

試験はhealth、Alembic upgrade、日本語のretain・recall、自動統合の要求記録、外部TCP接続の
失敗を確認します。スタブの成功は実モデルの精度・性能の証拠ではありません。実モデルは呼びません。
pytestの既定socket禁止を保ち、IT1だけ `--allow-hosts=postgres,hindsight,service` を指定します。

実行ごとに専用のComposeプロジェクトを作り、成功・失敗・通常の中断時にはログ取得後に
コンテナ・ネットワーク・volumeを削除します。ログは `.it1-artifacts/<実行名>/` に残ります。
保存先を変える場合は `IT1_ARTIFACT_DIR` を指定します。試験の非0終了はスクリプトにも伝播します。
強制終了（SIGKILL）やホスト停止時はtrapが動かないため、残った対象プロジェクトの削除が必要です。

試験コンテナには `DATABASE_URL`、`HINDSIGHT_DATABASE_URL`、`SERVICE_URL`、`HINDSIGHT_URL`、
`STUB_REQUEST_LOG` をComposeから渡します。要求ログは合成データ用の専用volumeに保存し、試験側は読み取り専用です。
