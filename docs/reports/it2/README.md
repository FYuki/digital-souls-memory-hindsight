# IT2 公開レポート

ここには監督が実施した実モデル試験の結果を置く。TAKT worker は実モデルを実行せず、
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

## 抽出・統合の品質と2モデル比較

監督はREADMEの事前条件を満たしたうえで、モデルごとに
`bash scripts/run-it2.sh tests/it2/test_extraction_quality.py`を実行する。
保存先は`IT2_ARTIFACT_DIR`で指定でき、未指定時は`.it2-artifacts/<実行名>/`。
`extraction-quality-<model>.json`にはモデル名・採点器版・全9行の試行結果・全体判定を記録する。
FAILでも結果を保存し、同じ保存先の同名ファイルは再実行で上書きしない。

同じ保存先の`extraction-quality-<model>.progress.jsonl`に、確定した試行ごとに1行を追記する。
途中の記録は全9行が終わっても残す。最終JSONがなく途中の記録だけがある実行は中断であり、
PASSではなく、比較表の入力にも使えない。最後の行が書き込み途中で切れている場合は、
その行を完了した行として扱わない。
途中の記録も上書きしないため、同じ保存先への再実行は拒否される。
新しい保存先で再実行する場合は、中断した実行の失敗行と中断の事実を
`costs_and_constraints`に記録し、後の成功で隠さない。

各行は`suite`、`trial`、`bank_id`、`completion`、`reason`、`elapsed_seconds`、
`cases`、`failures`、`metrics`、`memories`を持つ。`memories`は合成データ由来のSDK応答で、
本文・document・統合元を確認できる。未完了の行は全ケースFalse、metricsはnull。
完了後の読み取りなどに失敗した場合も、失敗行を残す。

公開用JSONは、モデルを`models.generation`に、保存された9行を`trials`に転記し、
上表の全項目を監督が補って既存のレポート生成器へ渡す。
`settings`には実際に使用した`recall_min_reranker_score`を記録する。
処理時間にはservice側のCharacterState生成などの負荷も影響し得るため、環境・制約を併記する。

生成済み公開レポートから比較表を作る。

```sh
uv run --no-sync python -m tests.it2.comparison \
  --report docs/reports/it2/<gemma-run-id>.json \
  --report docs/reports/it2/<luna-run-id>.json \
  --output docs/reports/it2/<comparison-name>.md
```

同じモデルの複数レポート、採点器版・ケース集合の不一致、不正な試行構造は拒否する。
片方のレポートだけでも生成でき、そのモデル以外はNOT RUNと表示する。
判定は全試行の完了・必須ケースとbank分離から再計算し、記載された判定や率では決めない。
比較表は試行別のケース結果、誤統合率・統合漏れ率、処理時間、失敗の内訳を含み、モデルを推奨しない。
分母0の率は「未定義」、未完了で計測できない率は「未測定」とする。
安全検査後に排他作成し、既存の比較表も上書きしない。
生成モデルの採用は本人がIssue 5-7で決め、両モデルとも不合格なら採用だけでPASSにはしない。

## 想起の閾値と性能

workerの実モデル結果は **IT2 NOT RUN**。監督は事前条件を確認し、次を別実行で行う。

```sh
IT2_ARTIFACT_DIR=/path/to/artifacts bash scripts/run-it2.sh tests/it2/test_recall_threshold.py::test_select_threshold
RECALL_MIN_RERANKER_SCORE=<選択値> IT2_ARTIFACT_DIR=/path/to/artifacts bash scripts/run-it2.sh tests/it2/test_recall_threshold.py::test_confirm_threshold
RECALL_MIN_RERANKER_SCORE=<選択値> IT2_ARTIFACT_DIR=/path/to/artifacts bash scripts/run-it2.sh tests/it2/test_recall_performance.py
```

同じ保存先を使い、選択JSONの値を確認実行へ渡す。閾値不一致なら投入前に拒否する。
runnerは各実行の前後に専用DBを消すため、確認は新しいbankで行われる。
`recall-threshold-selection-<model>.json`、`recall-threshold-confirmation-<model>.json`、
`recall-performance-<model>.json`と、それぞれの`.progress.jsonl`を保存する。
FAILも保存し、同名の結果・途中記録を上書きしない。中断時の扱いは抽出品質と同じ。

選択結果には全候補のtuning指標・試行別指標・必須通過・目安達成を保存する。
確認結果には全40問のSDK候補と実スコア、service応答、ケース結果を3試行とも残す。
Fact空と全体空、Anchor混入率を区別し、Anchor混入率は合否に使わない。
性能結果には実測sample、全条件、4種の件数、失敗率、Fact非空と全体非空の応答数、百分位、
目標達成、測定準備・別測定・生成カウンタ・traceの時間を保存する。
sampleの`fact_nonempty`はmemoriesの非空、`response_nonempty`は応答全体の非空を表す。
系列の`positive_fact_responses`・`fact_responses`と、全体非空の
`positive_nonempty_responses`・`nonempty_responses`を別に保存・表示する。
正例Fact非空0件なら、Anchor・原文区間・contextがあってもFAILとなる。
旧sampleの`nonempty`とpreparationなしの保存行は読み込まない。
生成カウンタ未取得は`not_measured`であり、処理増加なしの証拠にしない。
Lunaの外部生成呼び出しはローカル指標で測れないため未測定とする。
`counter`に`llamacpp:n_decode_total`、`counter_delta`に差分を記録する。
外部が未測定でも、取得できたローカルの差分は`local_counter_delta`へ残す。
これはdecode処理の回数で、HTTP呼び出し回数ではない。差分0は処理の増加なし、
正の差分は処理の増加を示す。整数以外の表記・欠落・取得失敗・減少は未測定にする。
固定image内の名前は確認済み、実metrics応答は未確認。詳細は採点契約を参照する。
準備・各測定の失敗時も、記録済みのwaits・sample・別測定を保持する。
例外の理由は`execution_error:<段階>:<型名>`とし、例外本文は保存しない。

```sh
uv run --no-sync python -m tests.it2.recall_report \
  --report /path/to/artifacts/recall-threshold-selection-gemma4-12b.json \
  --report /path/to/artifacts/recall-threshold-confirmation-gemma4-12b.json \
  --report /path/to/artifacts/recall-performance-gemma4-12b.json \
  --output docs/reports/it2/<run-id>-recall.md
```

入力は上記の規定ファイル名を使用する。保存済みverdictは信用せず、候補・全試行・sampleから
再判定する。入力がない試験はNOT RUN、分母0の指標は未定義とする。
安全検査後に排他作成し、既存のMarkdownを上書きしない。
公開時は実行許可・環境・revision・日時・モデルとイメージの版・費用と制約を監督が別途併記する。
正式閾値、既定値とREADMEの値の更新、モデル採用は監督・本人が行う。
