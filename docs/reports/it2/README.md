# IT2 公開レポート

ここには監督が実施した実モデル試験の結果を置く。Issue 5-1 の worker は実モデルを実行せず、
このディレクトリに架空の PASS レポートを作らない。Core からの ST は NOT RUN。

秘密を除いた構造化 JSON を、次の入口で検査・保存する。

```sh
uv run --no-sync python -m tests.it2.report --input /path/to/safe-input.json --output-dir docs/reports/it2
```

入力の全項目は以下のとおり。欠落と未知の最上位キーは拒否する。

| キー | 許可する内容 |
| --- | --- |
| run_id | 半角英数字で始まり、半角英数字・ハイフン・アンダースコアだけの実行 ID（128 文字以内） |
| authorization | 実行許可と適用条件 |
| environment | GPU・OS など対象環境の概要。環境変数の一覧は入れない |
| revision | 実行した revision |
| timestamp | 実行日時 |
| models | モデル名・sha256・revision |
| images | 使用イメージの固定 digest |
| settings | 試験に使った設定。秘密や認証情報は含めない |
| scorer_version | 使用した採点器の版 |
| completion | 完了判定の結果 |
| trials | 全 3 試行それぞれの bank・必須ケース・指標・結果 |
| costs_and_constraints | 費用と制約 |
| synthetic_inputs | 秘密を除いた合成入力 |

出力は `<run_id>.json`。パス区切りを含む ID と既存の ID を拒否する。
最終 JSON に対し sk-・ghp_・github_pat_・Bearer・JWT 形式のトークン様文字列、
個人のホームパスを検査し、検出時は値を出力せず失敗にする。digest と git revision は受理する。
JSON で改行がエスケープされた値も見落とさないよう、元の文字列も再帰的に検査する。
検査はレポート作成者による秘密除去の代替ではない。

`docker inspect`、環境変数一覧、認証ディレクトリ、未加工ログを回収・入力しない。
runner が保存する GPU 使用量は非公開の `.it2-artifacts/` に置き、Hindsight のログを収集しない。
ログを転載して公開レポートを作らず、許可した項目だけを構造化して渡す。
FAIL・SKIP・NOT RUN を PASS と区別し、成功するまでの再実行で過去の試行を隠さない。
