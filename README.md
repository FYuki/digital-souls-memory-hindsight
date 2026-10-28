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
PYTHONPATH="$PWD/src" uv run --no-sync alembic upgrade head
bash scripts/run-service.sh
```

`GET http://localhost:8000/health` は `{"status":"ok"}` を返します。
healthはプロセスの応答確認であり、DB接続の確認はIT1で行います。
初期revisionは移行履歴だけを作り、業務テーブル・APIは後続Issueで追加します。

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
