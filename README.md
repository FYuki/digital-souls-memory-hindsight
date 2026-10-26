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
uv run --no-sync pytest -m ut -q
```
