# IT2 のケース・採点契約

Issue 5-1 で実行前に固定する契約。実モデルによる各 AC の試験本体は Issue 5-3〜5-6、
生成モデルの採用と閾値の決定は Issue 5-7 が担当する。共通論理の UT を実モデルの PASS にしない。

## 固定ケース

正本は `tests/it2/cases/` の JSON。抽出・想起用の `manifest.json` は schema_version=1、scorer_version=2、
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
閾値候補は実行前に0.00〜1.00の0.05刻みに固定する。候補表の再現率・棄権率はtuningの
3試行合算から算出し、試行別の値も残す。finalの必須ケース全通過を制約とし、順位付けには
tuningだけを使う。再現率90%以上では棄権率、再現率、低い閾値の順に選ぶ。
目安に届く候補がなければ、必須通過候補の再現率、棄権率、低い閾値の順に選び、
`guideline_met=false`を記録する。候補なしと試行未完了は区別する。

llama.cppのrerankは生logitを返す。Hindsightは全スコアが[0,1]ならそのまま使い、
1つでも範囲外なら全てへsigmoidを適用する。同じlogitでも同時処理の候補によって
比較値が変わり得る。この性質は監督の合成1問の実測を出典とし、試験では
`scores.reranker`の実値を記録・評価する。Hindsightは変更しない。

選択時にもfinalを必須通過制約として使うため、finalは完全な未見データではない。
最終確認は選択閾値でサービスを再起動し、新しいbankの3試行をservice応答から採点する。
正解の系譜には、`expected.facts`と同じ入力本文を持つ全言及を含める。

## 性能の測定契約

100件と1,000件は、統合完了後のworld・experience・observationの合計で判定する。
同じbankへ25言及ずつ追加し、各段階は目標の2倍の言及で停止する。Session数、入力Fact数、
抽出Fact数、統合Fact数を保存する。IDの番号は段階をまたいで継続する。
投入完了とCharacterStateキューの消化を待ち、固定20件のAnchor登録後に測定する。
正例・負例を交互に送り、並列数1、ウォームアップ20件、測定200件、固定文字数、assembled形式を使う。
モデル・offload・context長を系列へ保存し、条件が変われば別条件として扱う。
HTTP送信直前から本文受信直後までを測り、JSON解析は除く。nearest-rankの百分位を使い、
p95が600ms以内、失敗0件、正例のFact非空応答が1件以上、記憶数充足、
`preparation.status == "complete"`でPASSとする。Anchor・原文区間・contextだけが
返る応答ではFact非空を満たさない。sampleは`fact_nonempty`と`response_nonempty`を
別々に保存し、系列もFact非空件数と全体非空件数を正例・全体それぞれに記録する。
記録行・総合判定・公開表の再計算は同じ系列判定を使う。
p50 150ms・p95 300msの目標達成は別に記録する。
常駐取得、取り込み待ち、CharacterStateの完了待ちは想起系列へ混ぜない。
CharacterStateの別測定は新しい合成TurnのHTTP送信からキュー完了までであり、待ち時間も含む。
生成処理は`llamacpp:n_decode_total`の前後差分で観測する。これはllama_decode()の
実行回数であり、HTTP要求の呼び出し回数ではない。固定digest
`sha256:69019445c94c970496c8f6d6447214b837508162dfe1152768942c51237e3ab7`の
`/app/libllama-server-impl.so`内で名前を確認した。実HTTP応答と数値表記はIT2未実行のため未確認。
半角数字の整数だけを読み、指数・小数表記、欠落、取得失敗、カウンタ減少は
`not_measured`として扱い、0差分に置き換えない。差分0は測定中にdecode処理が増えず、
正の差分は処理が増えたことを示す。要求ごとの回数や処理との一対一対応は保証しない。
この差分は記録のみで合否に使わない。Lunaの外部生成はローカル指標では未測定。
指標の意味は[llama.cppのmetrics一覧](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#available-metrics)を参照する。
traceの数値duration項目はパスとともに記録し、取得できなければ未取得として扱う。
本人が両モデルの結果を見て採用し、そのモデルで受け入れを判定する。
両モデルとも不合格なら採用決定だけで PASS にしない。

## 登録時の整合チェック（AC-I5）

`consistency.json`はmanifestから独立した合成ケースで、`scorer_version`は
`scoring.SCORER_VERSION`と照合し、`trial_count=3`を実行前に検証する。
各ケースは`id`、`level`（`reviewer`または`path`）、`operation`、`input`、`expected.consistent`を持つ。
人格のseedと口調の入力は、それぞれ既存の`SeedRequest`、`VoiceRequest`で検証する。
重複ID、不正な識別子、必須の操作・合否区分の不足は実行前に拒否する。
`expected`はモデルへ渡さない。ケースと期待結果を実行後に書き換えない。

| ケースID | 期待する結果 |
| --- | --- |
| `reviewer-personality-consistent-001`、`-002` | 固定の人格文を直接審査しtrue、理由あり |
| `reviewer-personality-contradiction-001`、`-002` | 固定の人格文を直接審査しfalse、理由あり |
| `reviewer-voice-consistent-001`、`-002` | 固定の発話を直接審査しtrue、理由あり |
| `reviewer-voice-contradiction-001`、`-002` | 固定の発話を直接審査しfalse、理由あり |
| `path-seed-consistent-001` | 201、review=true、Profile版1を保存 |
| `path-seed-contradiction-001` | 422、consistency_review=false、Profile版1は404、current不変 |
| `path-temperament-consistent-001` | Profile準備後の更新が201、review=true、Temperament版2を保存 |
| `path-temperament-contradiction-001` | Profile準備後の更新が422、consistency_review=false、版2は404、current不変 |
| `path-voice-first-consistent-001` | 201、consistency_review=true、代表発話を保存 |
| `path-voice-first-contradiction-001` | 201、consistency_review=false、代表発話を保存 |
| `path-voice-no-rereview-001` | 同じキャラで初回登録→元Turn削除→再登録→後続登録→アプリ再生成後の登録。初回だけ審査 |

全経路では、試験プロセス内の`create_app`へ実OpenAI互換部品を記録付きで注入し、
TestClientから実ルートと実PostgreSQLを通す。人格の読み取り応答にはreviewを保存しない。
Profileのない気質登録はreview=null、生成・審査0回とする。
口調の後続登録はconsistency_review=null、生成・審査の累計は各1回、
初回の`voice_consistency_reviews`行は削除・再生成の前後で不変とする。
試験プロセスのWorkerは停止し、元Turnの削除はserviceの実Workerに任せる。
削除のcompletedを最大1800秒、最大1秒間隔で待ち、一覧が空になったことを確認する。
再起動の観測範囲は同じDBを使うアプリの再生成であり、コンテナの実再起動ではない。

手順の分類順は、過剰な部品呼び出しを`contract_violation`、生成の例外・空白を
`generation_failed`、審査例外を`review_failed`、完了した審査の合否違いを`misjudged`、
HTTP・理由・保存・呼び出し回数などの不一致を`contract_violation`、全一致を`correct`とする。
部品以外の例外・待機超過は`execution_error:<段階>:<型名>`を理由として記録する。
ケースは実行順で最初の非correct手順の分類と理由を使い、実行した手順の記録を保持する。
前提の準備に失敗した場合、別の状態で対象操作を代用せず、その準備の失敗を残す。
生成文・理由の文面一致で合否を決めない。生成文と理由は監督の確認用に保存する。

試行ごとのキャラIDは`it2-consistency-<case-id>-t1`〜`t3`とし、
口調の連続シナリオを各試行の最後に置く。3試行すべてで全必須ケースがcorrectの場合だけPASS。
Reviewer単体と全経路の判定も別々に記録する。再試行して最後の成功だけを採用しない。
`MEMORY_LLM_*`を試験入口で解決し、モデル名に特定の名前を要求しない。

seed矛盾ケースの人格文は固定文ではなく実モデルの生成結果である。
モデルが矛盾を和らげた場合も固定の期待を変更せず、結果をFAILとして生成文とともに記録する。
Reviewer単体の固定文評価と合わせて監督が生成と審査を確認し、必要な修正は別PRにする。
削除の実時間、実モデルでの合否はworkerでは未確認（IT2 NOT RUN）。

## 代替E2E（AC-I4）

想定する Core の呼び出し順で、Core の検証ではない。
固定入力は`tests/it2/cases/e2e.json`とし、schema・採点器版・固定3試行・人格入力・
削除対象と対照を区別する合成語を実行前に検証する。モデル名を限定しない。

1. CoreIdentity、Temperament、PersonalityProfile、代表発話、出典Turnを持つCharacterProfileを登録する。
2. Session、利用者・キャラのTurn、DeliveryReceiptを登録し、GETで保存値を照合する。
   別Session・別surfaceの対照Turnと、TurnなしのSessionも準備する。
3. 1回目の常駐応答とkind=residentの記録を、版3種・代表発話IDの順序・revision・返却区間で照合する。
4. 投影・統合の完了後、同じSessionでCore・Temperament・代表発話・CharacterProfileを変更する。
   2回目の常駐提供がCore版2・Temperament版2・Personality版1と新しい設定を返し、
   記録が一致することを確認する。1回目の提供記録は変わらず残る。
5. 完了判定後にTurn指定で想起し、記憶と原文が非空で、本Sessionの出典を含むことを確認する。
   原文区間を台帳と比較し、提供記録のID・区間・信頼区分と常駐参照先を照合する。
6. 完了後に受領だけで別Session・別surface・本Sessionの状態生成を順に起こす。
   有効な3項目、世代一致、本Sessionの再更新による世代・生成日時の増加、対照の不変を確認する。
   空Sessionと未使用surfaceには状態・保存行がない。受領で投影outboxの件数が増えない。
7. 作者の削除を要求しcompletedを待ってから、対象Turn・受領・一覧・想起・常駐応答、
   派生記憶・統合元ID・提供記録・状態に対象が残らないことを確認する。
   状態の世代増加でTTL切れと区別し、出典Turn・最初の常駐記録・別Sessionの状態は残る。

判定用の状態生成は統合完了後に行い、新しいTurnを追加しない。完了前の生成結果も記録するが、
その成否を状態生成の判定に使わない。製品の生成上限30秒は保持し、試験側の状態待機は最大600秒、
削除待機は最大1800秒、間隔は最大1秒とする。状態生成のfailed・timeoutは手順6をFalseにし、削除へ進む。
状態の文章の完全一致では品質を判定しない。対照との前後比較では保存値とHTTP応答を比較する。

照合不一致はその手順をFalseにして続行する。完了判定の未完了、例外、想定外のHTTP状態は試行を中断し、
残りをFalse・`not_reached`にする。例外理由には段階と型名だけを残し、例外本文と認可ヘッダーは保存しない。
試行の理由は実行順で最初のFalseの理由を使い、他の理由も保持する。

固定3回を新しいbankで実行し、全回の全7手順がTrueかつ完了判定がcompleteの場合だけPASS。
失敗後も次の試行を実行し、成功までの再試行はしない。結果は`e2e-<IT2_GENERATION_MODEL>.json`、
確定試行は`.progress.jsonl`へ保存する。モデル名・部品モデル名・採点器版・想起閾値・全試行を記録し、
安全検査と保存の後に合否assertionを行う。既存の結果・進捗は実行前に拒否する。
実モデルIT2は監督が実行し、CoreからのSTはNOT RUNのまま残す。

## 公開レポート

[公開レポートの手順](reports/it2/README.md) に従う。生成器は診断情報を自動収集せず、
許可項目の入力だけを受け取る。安全検査を通る前にファイルを作らず、既存の実行 ID を上書きしない。
