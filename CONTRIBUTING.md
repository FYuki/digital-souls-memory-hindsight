# 開発規約

## 正本と言語

この文書を開発規約の正本とし、AGENTS.md はエージェント向けの入口とします。
試作の範囲と判定基準は [受け入れ条件](docs/acceptance-criteria.md) を、ドメインの定義は
[digital-souls-memory のドメインモデル](https://github.com/FYuki/digital-souls-memory/blob/main/docs/domain-model.md) を参照します。
人間向けの文書・PR 本文は日本語、コミットメッセージは日本語の Conventional Commits
（`feat:` / `infra:` / `docs:` / `test:` / `fix:` / `chore:` など）で書きます。
受け入れ条件の未決事項は、本人の決定があるまで決定として扱いません。

## ブランチ・レビュー・merge

実装はタスク単位で行い、`main` へは epic 単位で取り込みます。

1. 最新の `origin/main` から `epic/*` を作ります。
2. epic からタスクごとの作業ブランチを切り、別 worktree で変更します。
3. 作業ブランチから対応する epic 宛ての Draft PR を出します。
4. 作業 PR → epic は **最終 head の CI グリーン**を条件とします。
5. epic → main は **最終 head の CI グリーンと実際の CodeRabbit レビュー完了・指摘対応**を条件とします。

bot の概要・review disabled 通知は CodeRabbit レビュー完了ではありません。
blocking 指摘を残したまま merge しません。作業ブランチから main への直接 PR は作りません。
これらの条件は merge 権限を与えません。依頼で許可された担当だけが merge します。
branch protection・ruleset・GitHub App などの設定は別途の判断なしに変更しません。
push 前に remote・対象ブランチ・差分を確認し、秘密情報・私的ログ・実データを含めません。

## 固定ツールチェーンと品質ゲート

Python **3.12.3**（`.python-version`）と uv **0.8.22** を使います。
直接依存は `pyproject.toml` で固定し、推移依存・配布物ハッシュは `uv.lock` に記録します（受け入れ条件 AC-O1）。
Hindsight のクライアントは `hindsight-client==1.1.0` に固定し、フォーク・改変しません（A1）。
TDD に従い、振る舞いを検証する失敗テストを先に書いてから実装します。
依存更新は明示的に行い、lockfile の差分をレビューします。

```sh
python3 -m pip install uv==0.8.22
uv lock --check
uv sync --frozen
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy
uv run --no-sync pytest -m ut -q
git diff --check
```

CI は全 PR と `main` / `epic/**` への push で `Checks / quality` を実行します。Actions は SHA 固定、
権限は `contents: read` のみとし、実モデル・secrets・`pull_request_target` は使いません。
IT2 / ST は CI で実行しません。required check 設定自体は所有者の判断です。

## テスト区分と結果

| marker | 区分・依存 | CI |
| --- | --- | --- |
| `ut` | 単体。ネットワーク・実 DB・実モデルを使わない | 必須 |
| `it1` | 実 PostgreSQL と Hindsight を使い、LLM・embedding は fixture にした結合。外部ネットワークに接続しない（AC-O2） | 必須（試験の追加時に CI へ加える） |
| `it2` | 明示的に承認された実モデルによる結合 | 実行しない |
| `st` | Core から呼ぶ E2E | 実行しない |

UT は pytest-socket でネットワーク socket を禁止します。
既定の pytest 実行は IT2 / ST を除外します。
必須 suite は `tests/conftest.py` により skip / xfail / xpass / 0 件を非 0 終了にします。
marker で全件除外した場合も失敗です。FAIL・SKIP・NOT RUN は PASS ではありません。
不足する検証は明示し、fixture の成功を実モデルの成功にしません。
IT2 / ST は実行許可・対象環境・revision・日時・秘密を除いた入力と結果・費用や制約を記録します。
