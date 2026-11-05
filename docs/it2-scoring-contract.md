# IT2 のケース・採点契約

Issue 5-1 で実行前に固定する契約。実モデルによる各 AC の試験本体は Issue 5-3〜5-6、
生成モデルの採用と閾値の決定は Issue 5-7 が担当する。共通論理の UT を実モデルの PASS にしない。

## 固定ケース

正本は `tests/it2/cases/` の JSON。`manifest.json` は schema_version=1、scorer_version=2、
trial_count=3、読み込む 4 ファイルとキャラ ID を固定する。
`cases_contract.load_cases` と `validate_cases` は、実行前に全件を検証する。
ケースを変更した場合は、採点器の版・revision とともに変更を記録し、実行後に期待結果を変えない。

各ケースは `id`、`acceptance_id`、`input`、`expected`、`scoring_unit` を持つ。
`input.sessions` は `session_id` と `mentions` の配列で、各言及に `id` と合成本文 `text` を置く。
`expected` はモデルへの指示に混ぜず、試験側の正解として保持する。

| 対象 | 必須条件と採点単位 |
| --- | --- |
| AC-M1 | 複数 Session に同じ意味の言及。単位は同一 Fact のグループ。統合 Fact が 1 つで、全 `source_mention_ids` への参照が残る |
| AC-M2 | `number`・`negation`・`subject`・`tense` の差を各 1 組以上。単位は別の Fact の組。`distinct_fact_count=2` と正解の `facts` を保持する |
| AC-R3 | `proper_name`・`date`・`verbatim` をそれぞれ固定。`extracted_fact` と `returned_source` を別のケース・採点単位とし、`expected.preserve` を各出力で評価する。原文が残るだけで抽出 Fact を PASS にしない |
| AC-R4 | `tuning`／`final` × `positive=true`／`false` の 4 区分。各 10 問を固定し、問い合わせを重複させない。単位は問い合わせ。負例では該当する記憶が空、正例では正解の記憶を返すことを求める |

「明らかな例」は、合成データの作者が差分の種類と正解を 1 つに決められる例をいう。
意味の照合を文字列の存在だけへ置き換えない。抽出・統合の品質は
`tests/it2/quality.py`で、以下の本文・系譜・返却元の照合を行う。
`required_case_ids` は tuning を除く全ケースを返す。最終確認用だけで閾値を調整しない。

### 抽出・統合の品質の照合

- AC-M1：`source_memory_ids`から抽出Factの`document_id`をたどり、対象Sessionの全言及元を
  1つのobservationが覆うことを確認する。serviceのrecallが同じobservation IDと全Turnを返すことも確認する。
  observationなし、対象の分裂、言及元欠落、検索失敗の順で理由を1つ選ぶ。
  検索だけの失敗は統合漏れの分子へ含めない。
- AC-M2（scorer_version=2、本人決定 2026-11-05）：同じdocumentに2言及をappendするため、
  document数やchunk数を差分の判定に使わない。採点器の固定`DIFFERENCE_FEATURES`で本文の側を分類する。
  差分のあるFactが別々に残れば合格とし、1つのFactに両側が入れば`false_merge`。
  片側が欠け、分類不能なFactがあれば`unresolved_fact`、なければ`fact_lost`。
  統合Factは後の値への更新と履歴の併記を許す。Hindsightの標準の統合（A6）がこう動くため。
  統合Factだけを読むと古い値が見えないことがあるが、Factと原文には残る。
  どちらの値にも分類できない統合Factは`observation_value_unresolved`で不合格。
  主体の差は更新として扱わず、統合Factの本文か系譜に両側が入れば`false_merge`。
  否定句など長い特徴語を先に照合して伏せ、肯定語の部分一致による誤分類を避ける。
  表にないケースは拒否する。表にない言い換えを合格と推測しない。
- AC-R3：`extracted_fact`は対象documentの抽出Factだけ、`returned_source`は対象Turnの
  `source_spans[].text`だけで`preserve`を評価する。observationや別の出典で代用しない。
  日付の`extracted_fact`は、同じ日付の固定の表記（`EQUIVALENT_DATES`）を合格にする（scorer_version=2、
  本人決定 2026-11-05）。`returned_source`は元の表記を求める。固有名と逐語表現の基準は変えない。

`test_extraction_quality.py`はAC-M1・M2・R3だけを実行する。共有言及は1回だけ投入し、
Session・Turn IDに試行ごとのキャラIDを付ける。`expected`は投入やqueryに使わない。
処理時間は最初のHTTP投入直前から完了判定が返るまでとし、読み取り・recall・採点の時間は含めない。
各試行の結果を残し、例外本文は記録せず型名だけを記録する。
照合規則やケースを実行結果に合わせて変更せず、変更が必要なら理由を報告して別PRにする。

### 採点器の版

scorer_version=1 では、統合Factが両側に触れるだけで誤統合とし、日付は元の表記だけを合格にした。
両モデルとも同じ差分で不合格となり、本人の決定で版 2 に変えた。版 1 の結果は
`docs/reports/it2/2026-11-05-5-3-*-attempt-*.json` に残す。
`tests/it2/rescore.py`は、保存した試行データからAC-M2と抽出側のAC-R3だけを再採点する。
想起の結果を要するケースは保存していないので、記録された結果のままとする。

## 統合の完了

`completion.read_completion_snapshot(client, database_url, bank_id)` は公式 SDK の操作一覧を
limit=100 で全ページ取得し、bank stats は `refresh=True` で読み、同じ bank の service outbox を
読み取り専用の SQL で取得する。操作の `id`・`task_type`・`status` だけを判定用に写し、
`error_message`・本文・outbox の `last_error` は取得結果へ含めない。
固定サーバーが返す retain 親操作の `batch_retain` も retain として正規化する。
親・子の両操作を一覧に残し、どちらの未完了・失敗も見落とさない。
一覧が進まない、通信・DB 取得に失敗した、統合状態を取得できない場合は例外を成功へ変換しない。

`evaluate_completion` が complete を返すのは次の全条件を満たす場合だけ。

- outbox が 1 件以上あり、全件 completed。
- delete 以外の各 outbox の operation_id に対応する retain が completed。
- 一覧に pending・processing がなく、completed の consolidation が存在する。
- pending_consolidation と failed_consolidation がともに 0。
- fresh な bank stats の pending_operations と failed_operations も 0。一覧取得後の新しい未処理操作を成功として隠さない。

failed・cancelled・未知状態は failed とする。複数の失敗では操作 ID 順で最初の失敗を選び、
その操作 ID と状態から理由を作る。別の失敗の理由と混ぜず、未加工の外部エラーを含めない。
`wait_for_completion(snapshot=..., clock=..., sleep=...)` は毎回取得した状態を判定し、
待機上限 1800 秒、ポーリング間隔最大 1 秒で待つ。固定時間が過ぎたことを完了の根拠にしない。
失敗時は直ちに戻り、操作を再投入しない。超過は timeout で FAIL とする。
取得中に待機上限を超えた場合も、その後に届いた成功を受け入れない。
snapshot は毎回新しい状態を返す責務を持つ。非同期 SDK の取得は呼び出し側の event loop で行う。
品質・性能の測定は complete の後に開始し、`score_trial` も未完了なら metrics を None にする。

## 指標

`scoring.calculate_metrics` は照合済みの件数から次を計算する。分母が 0 なら None（未定義）を
記録し、0% や 100% の成功として扱わない。

| 指標 | 分子 / 分母 |
| --- | --- |
| 誤統合率 | 1 つの統合 Fact に混ざった別 Fact の組 / 別 Fact の全組 |
| 統合漏れ率 | 1 つに統合されなかった同一 Fact のグループ / 同一 Fact の全グループ |
| 再現率 | 正解の記憶を返した正例問い合わせ / 全正例問い合わせ |
| 棄権率 | 該当する記憶が空だった負例問い合わせ / 全負例問い合わせ |
| Anchor 混入率 | 負例で返った無関係な Anchor / 負例で返った全 Anchor |

Fact だけが空の応答数 `empty_fact_responses` と、応答全体が空の数 `empty_responses` は
別々に記録する。本文・Anchor が残っている応答を、全体が空だった応答として数えない。
誤統合率・統合漏れ率・Anchor 混入率は記録のみ。再現率 90% は棄権閾値選択の目安であって合否条件ではない。

## 固定 3 試行

各 suite のキャラ ID は `it2-<suite>-t1`〜`t3` とし、キャラから導く bank を試行ごとに分ける。
manifest の一覧と Compose の EVIDENCE_CHARACTER_IDS が一致することを UT で検証する。
runner は実行前後に専用 DB volume を消し、前の実行の bank を引き継がない。
`summarize_trials` は、ちょうど 3 試行、異なる bank、全試行の完了、全必須ケースの合格を
すべて満たす場合だけ PASS を返す。指標の値は合否の入力に使わず、失敗した試行も出力に残す。
失敗・SKIP・NOT RUN を後の成功で置き換えない。

必須ケースを通る棄権閾値がなければ AC-R4 を FAIL として本人へ戻す。
本人が両モデルの結果を見て採用し、そのモデルで受け入れを判定する。
両モデルとも不合格なら採用決定だけで PASS にしない。

## 公開レポート

[公開レポートの手順](reports/it2/README.md) に従う。生成器は診断情報を自動収集せず、
許可項目の入力だけを受け取る。安全検査を通る前にファイルを作らず、既存の実行 ID を上書きしない。
