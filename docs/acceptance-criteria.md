# 受け入れ条件：Hindsightを中核にしたMemory試作（Phase 1）

- 状態：**草案**。未決の項目（6章）が残っている。以降の質疑はこのリポジトリで行い、未決事項を解消してから確定する。
- 作成：2026-10-26
- 正本となるドメインモデル：[digital-souls-memory `docs/domain-model.md`](https://github.com/FYuki/digital-souls-memory/blob/4c8493916fa8350cd610a97b68351a5a14f22a92/docs/domain-model.md)（epic/phase1-consistency `4c84939`）
- 根拠とした調査：[digital-souls-memory `docs/research/hindsight-domain-fit.md`](https://github.com/FYuki/digital-souls-memory/blob/a18a9a4c7e35c7846f1e7054fe20d1da14574713/docs/research/hindsight-domain-fit.md)と[確認結果JSON](https://github.com/FYuki/digital-souls-memory/blob/a18a9a4c7e35c7846f1e7054fe20d1da14574713/docs/reports/2026-10-26-phase1-oss/hindsight-domain-results.json)（研究ブランチ `research/phase1-oss-reassessment`、`a18a9a4`）

## 1. 目的

ドメインモデルのPhase 1（一貫性の土台）を、Hindsightを記憶処理の中核に使って実現できることを、試作で示す。
評価の対象はドメインモデルの実現であり、既存のdigital-souls-memory実装との互換性・移行費用は対象外とする。

## 2. 前提

| # | 前提 | 状態 |
| --- | --- | --- |
| A1 | Hindsightはフォークせず、別プロセスのサーバー＋公式クライアントとして利用する。サーバーはイメージ `ghcr.io/vectorize-io/hindsight:1.1.0-slim`（digestも固定）か、pipの `hindsight-api-slim==1.1.0` を使う。クライアントは `hindsight-client==1.1.0` に固定する。上流の修正が必要なら、外側で回避するか上流へ報告する | 決定（2026-10-26、本人） |
| A2 | 会話の正本（Evidence台帳）はHindsightの外側に置き、追記専用にする。Hindsightは台帳から投影した本文をもとに、エピソード記憶と意味記憶を抽出する仕組みとして使う。Hindsightのdocumentは作り直せる検索用のコピーとする | 決定（2026-10-26、本人） |
| A3 | キャラごとの知覚範囲（M6）は、1キャラ＝1 bankとし、そのキャラが知覚した本文だけを各bankへ投影して表す（案A） | 決定（2026-10-26、本人） |
| A4 | Turn単位の厳密な出典（source_turn_idsの精度）は求めない。会話を削除したら、関連する記憶をさかのぼって消せればよい。作り直し方式でかまわない | 本人が明示 |
| A5 | 再強化（スコアの加算・last_reinforced_at）は求めない。同じFactへの複数言及が、非同期の統合によって1つの事実として扱えればよい | 本人が明示 |
| A6 | Hindsightの統合（consolidation）は、標準の自動実行（取り込みのたびに非同期で実行）をそのまま使う。内省と記憶整理（Phase 2）はHindsightの外側で別途行う | 決定（2026-10-26、本人） |
| A7 | 試験は合成データで行う。実モデルを使う試験（IT2・ST）は、別途許可を得てから実施する | 既定 |
| A8 | 投影の単位は、Hindsightの想定に合わせて「1キャラbank × 1 Session ＝ 1 document」とし、知覚したTurnをappendで追加する。1往復単位にはこだわらない。Turnを削除したときは、台帳に残ったTurnからそのSessionのdocumentをreplaceで作り直す | 決定（2026-10-26、本人がHindsightに合わせると明示。具体の単位はAIの提案） |
| A9 | Coreが呼ぶHTTP APIは、既存digital-souls-memoryのエンドポイントとスキーマに合わせ、内部の実装は新しく作る。Core側も開発途中のため、STでの接続確認はCoreで実装済みの機能に限り、それ以外はIT1で確認する | 決定（2026-10-27、本人） |
| A10 | IT1では、外部ネットワークに接続しないコンテナ網に、OpenAI互換のスタブ（LLM・embedding）とCohere互換のスタブrerankerを置き、Hindsightサーバーの接続先をそこへ向ける。棄権はrerankerのスコアで判定する。rrfとスタブembeddingでは棄権を判定できないため変更した。サーバーはslimイメージを使う（スタブで動くことを確認済み、7章） | 決定（2026-10-29、本人。slimは2026-10-27の委任に基づきAIが動作確認で判断） |
| A11 | PostgreSQLは台帳とHindsightで同じインスタンスを使い、データベースを分ける。イメージはpgvectorとPGroongaを入れたものをこのリポジトリでビルドし、版とdigestを固定する。Hindsightの全文検索は `HINDSIGHT_API_TEXT_SEARCH_EXTENSION=pgroonga` とする | 決定（2026-10-27、本人） |
| A12 | 台帳からHindsightへの投影・削除などの非同期処理は、PostgreSQLのoutboxテーブルと、同じサービス内のworkerで行う | 決定（2026-10-27、本人） |
| A13 | 他キャラへのappendは配達範囲の確定順になり、occurred_atの順序とずれることを許容する。retainにはoccurred_atを時刻（timestamp）として渡し、時間の順序をappendの順に頼らない | 決定（2026-10-27、本人。U7の解決） |
| A14 | IT2・STの実モデルはllama.cppで動かす（Hindsightの接続方式は `llamacpp`／OpenAI互換、rerankerはCohere互換）。embeddingはbge-m3、rerankerはbge-reranker-v2-m3とし、digital-souls-memoryの検索比較（memory-v2.1、IT2）の結果を参考にする。フレームワークによる差を見るため、digital-souls-memoryの自作実装との比較も行ってよい | 決定（2026-10-27、本人。U5・U6の解決。抽出・統合に使う生成モデルはU8） |
| A15 | 投影後に他キャラ向けの配達範囲が変わった場合は、影響を受けるキャラのbankで、そのSessionのdocumentを台帳から作り直す（replace）。範囲が変わらないReceiptでは作り直さない | 決定（2026-10-28、本人） |

## 3. 受け入れ条件

判定はすべて自動試験で行う。区分の意味は次のとおり。

- UT：単体試験
- IT1：実PostgreSQLとHindsightを使い、LLM・embeddingはfixtureにした結合試験
- IT2：実モデルを使った結合試験
- ST：Coreから呼ぶE2E試験

FAIL・SKIP・NOT RUNはPASSと区別して報告する。

### 3.1 Evidence（会話台帳）

| ID | 条件 | 試験 |
| --- | --- | --- |
| AC-E1 | Turnは追記のみ。内容の変更APIを持たず、同じturn_idで内容の違う再登録は拒否する。同じ内容の再送は冪等にする | UT, IT1 |
| AC-E2 | Turnの登録時に、kind・perceived_by・addressees・address_mode・reply_to_turn_id の規則（domain-model のTurn定義）を検証し、違反は拒否する | UT |
| AC-E3 | Sessionは reports_delivery を必須とし、後から変更できない。DeliveryReceiptは、宣言がfalseのSessionとkind=thoughtに対して422で拒否する | UT, IT1 |
| AC-E4 | 他のキャラへの投影には、配達範囲（spoken_until）までの本文だけを使う。Receiptが届かない場合は既定10分でみなし確定とし、即時確定・保留・Receipt確定・みなし確定を区別して記録する | UT, IT1 |
| AC-E5 | 常駐コンテキストとターン想起の応答ごとに、返した内容をProvisionRecordとして記録する | IT1 |

### 3.2 投影とキャラごとの知覚（M6）

| ID | 条件 | 試験 |
| --- | --- | --- |
| AC-P1 | 各キャラのbankには、そのキャラがperceived_byに含まれるTurnの本文だけを投影する。thoughtは本人のbankだけに入れ、perceived_byが空のTurnはどのbankにも入れない | IT1 |
| AC-P2 | キャラA・Bと「途中まで届いた相手」の3パターンで、範囲外の本文・抽出Fact・統合済みFact・chunkが、recall・文書取得・reflectのどの経路でも返らない | IT1 |
| AC-P3 | 台帳への追記からbankへの投影はoutboxを通し、再試行してもappendを二重に行わない。turn_idから導いたoperation_idによるHindsightの冪等化が使えるかをまず検証し、使えない場合は外側で冪等にする | IT1 |
| AC-P4 | **対象外（A3で案Aを採用したため）。** 記録として残す：共有bank（案B）を採る場合に限り、observation_scopesはキャラ単位（per_tag、または明示のcustom）とし、combined・sharedは使わない。読み書きの範囲は拡張で強制する | なし（案Bの場合のみIT1） |

### 3.3 派生記憶と同じFactの統合

| ID | 条件 | 試験 |
| --- | --- | --- |
| AC-M1 | 同じFactを複数回言及した場合、統合の実行後は1つの事実として検索・利用でき、言及元をすべて保持する | IT1（fixture）, IT2 |
| AC-M2 | 数値・否定・主体・時制が違うFactを誤って統合しない。IT2では誤統合率と統合漏れ率を別々に測る | IT1, IT2 |
| AC-M3 | 統合が実行されるまでの間も、新しいFactを想起できる。重複した表示は許容する | IT1 |
| AC-M4 | 推論で作られた情報（統合済みのFactを含む）は、繰り返されても信頼区分を自動で昇格しない。信頼の出自はHindsightの外側で保持する | UT, IT1 |
| AC-M5 | 記憶への追加にゲート（M5）を設け、自己生成物を無選別に追加しない | UT |

### 3.4 想起

| ID | 条件 | 試験 |
| --- | --- | --- |
| AC-R1 | 派生記憶をキーとして使い、知覚範囲内の原文区間を値として返す（M2） | IT1 |
| AC-R2 | 約束・記念日の問い合わせには、VerbatimAnchorの原文を必ず含める | IT1 |
| AC-R3 | 日本語の固有名・日付・逐語表現を保つ。全文検索にはPGroongaを使う（Hindsightのtext search extension、A11） | IT1, IT2 |
| AC-R4 | 該当する記憶がない場合は、空の結果を返せる（棄権できる）。Hindsightのrecallは該当がなくても上位の結果を返すため（7章）、`min_scores` か外側の閾値で判定する | IT1, IT2 |
| AC-R5 | ターン想起は、100件と1,000件の記憶で必達p95 600ms以内に収まる。目標値p50 150ms／p95 300msも計測して記録する。対象は、原文の取得・提供記録の保存・通信を含む全経路 | IT2（性能） |

### 3.5 削除（作者による削除）

| ID | 条件 | 試験 |
| --- | --- | --- |
| AC-D1 | 削除要求を出せるのは作者だけ。利用者を指定した場合は、actorがその利用者のTurnと、addresseesにその利用者を含むTurnの和集合を対象にする | UT |
| AC-D2 | 削除後、対象の本文とそこから作られた記憶が、全キャラのbank・統合済みFact・recall・chunk・ProvisionRecordから消える。他の根拠で支えられる記憶は作り直され、残る | IT1 |
| AC-D3 | 削除した内容が復活しない。少なくとも次の4経路で確認する：①取り込み処理の最中に削除した場合、②統合処理の最中に削除した場合、③削除後に同じTurnが再送・再投影された場合、④削除後に再処理（reprocess）した場合 | IT1 |
| AC-D4 | 削除の受付・処理中・完了・失敗を区別して記録する。失敗または処理中の間は、対象の内容を提供しない。結果はcascade_resultとして記録する | UT, IT1 |

### 3.6 Identity・Voice・状態・API

| ID | 条件 | 試験 |
| --- | --- | --- |
| AC-I1 | CoreIdentity・Temperament・PersonalityProfile（author_seedと記述文）は、登録APIでのみ版が上がる。記憶や統合の処理から書き換えられない。Hindsightのbank設定（mission・disposition）を正本にしない | UT, IT1 |
| AC-I2 | VoiceExemplarとVerbatimAnchorは逐語で保存し、言い換えを禁止し、登録には承認を必要とする | UT |
| AC-I3 | CharacterStateは窓口・セッション単位の揮発状態として扱う | UT |
| AC-I4 | 常駐コンテキストAPIは、セッション開始時点の人格版を返し、自動の更新は次のセッションから適用する | IT1, ST |
| AC-I5 | 登録時に整合チェックを1回実行し、結果を作者に返す | IT2 |

### 3.7 運用と再現性

| ID | 条件 | 試験 |
| --- | --- | --- |
| AC-O1 | Hindsightとその依存の版を固定する（lockファイル） | CI |
| AC-O2 | IT1では外部ネットワークへの接続を遮断し、実モデルを呼ばない | CI |
| AC-O3 | 統合の自動実行を有効にしたまま、削除の流れ（AC-D3）と競合しない。削除の前に、対象bankで実行中の取り込みと統合の完了を待つ（または一時的に自動実行を止める） | IT1 |
| AC-O4 | workerが停止・再起動しても、取り込み・統合・削除の操作が失われず、二重にも実行されない | IT1 |

## 4. 既知の問題と、試作で必須とする回避策

調査（Hindsight 1.1.0、`f29ba70c`）で、実PostgreSQL上で再現した問題。各問題に回帰試験を用意する。

| # | 問題 | 回避策 | 対応するAC |
| --- | --- | --- | --- |
| K1 | 取り込み処理の最中に同じ文書を削除すると、処理の完了後に文書と記憶が復活する | 削除済みの印（tombstone）を台帳に持つ。削除の前に、対象文書の取り込み操作の完了を待つか取り消す。削除後に残存を検査する。本人がこの対策で様子を見ると決定（2026-10-26） | AC-D3① |
| K2 | 削除後に同じIDの文書を再送すると、そのまま再登録される | tombstoneがあるTurnの投影を外側で拒否する | AC-D3③ |
| K3 | 統合処理が複数の出典を読んでいる最中に、その一部が削除されると、削除した内容を含む本文が統合済みFactとして残る | 統合は自動実行のまま（A6）なので、K1と同じ削除の流れで対象bankの実行中の統合を待ってから削除する。削除後の残存検査で見つかった場合は、該当する統合済みFactを消して作り直す | AC-D3②, AC-O3 |
| K4 | combined scopeでは、Aだけが知る詳細が共有の統合済みFactに混ざり、Bへ返る | 案Aを採ったので該当しない | AC-P2 |
| K5 | shared scopeでは、strictで読むと統合済みFactが見えず、既定のanyで読むと他キャラの情報が見える | 複数キャラの共有bankではsharedを使わない（キャラ別bank内のセッション横断なら可） | AC-P4 |
| K6 | 同じ内容のappendを再送すると、本文が二重に追記される | A8でappendを使うため必須。turn_idから導いたoperation_idで冪等にできるかを検証し、できなければ外側で冪等にする | AC-P3 |

## 5. 範囲外（Phase 2以降）

- Reflection・Belief・Motivationの状態機械と、内省の根拠検証（Phase 2）
- TraitChangeProposal・ChangeBudget・審査エージェント・ProvenanceGraphによる完全な削除連鎖（Phase 3）
- 視聴者・利用者本人からの削除要求の窓口（Phase 3）

## 6. 未決事項（質疑中）

- 解決済み：U1（利用形態 → A1）、U2（案A → A3）、U3（投影の単位 → A8）、U4（統合の実行 → A6）、台帳の位置づけ（→ A2）、AC-P4の扱い（案Aでは対象外）、U5・U6（実モデル → A14）、U7（appendの順序 → A13）
- U8：抽出・統合に使う生成モデル（llama.cppで動かすGGUF）。digital-souls-memoryの調査では生成モデルを比較していない
- U9：IT2・STの実行許可。実行の時期・対象環境を本人が確認してから行う（A7）

## 7. 引継ぎメモ

- 2026-10-27までの決定（A1〜A3・A6・A8〜A14、AC-P4の扱い、K1の対策）を反映した版。以降の質疑と決定は、このファイルとこのリポジトリのIssue・PRで管理する。
- 調査の詳細・再現用probe・結果JSONは「根拠とした調査」を参照する。

### 2026-10-27 サーバー構成の動作確認（スパイク）

試験用の使い捨てコンテナで行った確認。合成データだけを使い、実モデルは使っていない。

- 構成：`ghcr.io/vectorize-io/hindsight:1.1.0-slim`（`sha256:fbc37d194bce007b6cce2fd9f66cd7f8c050bd18b80e662090a92835d9cb191d`）、`hindsight-client==1.1.0`、OpenAI互換スタブ（LLM・embedding）、外部ネットワークに接続しないDockerネットワーク
- 起動時の疎通確認、retain（抽出→Fact保存）、自動の統合、recallが、スタブだけで通った（PASS）。抽出はjson_objectモードで、Factの形はプロンプト内のJSON Schemaで指定される。スタブはそれに合わせて応答する必要がある
- 既定の全文検索（native、english）では、日本語の部分一致（「お茶」）が返らなかった
- pgvectorとPGroongaを入れたイメージ（digital-souls-memoryの `docker/retrieval-postgres` と同じ構成）で `HINDSIGHT_API_TEXT_SEARCH_EXTENSION=pgroonga` にすると、「お茶」「東京タワー」「土曜日の約束」が該当する記憶を返した（PASS）
- 該当する記憶のない問い合わせ（「コーヒー」）にも結果が返った。棄権（AC-R4）はHindsightの外側か `min_scores` で扱う必要がある
- `retain` は `operation_id` を引数に取れる。K6の冪等化に使えるかは未検証（NOT RUN）
- fullイメージは試していない（slimで足りるため、NOT RUN）

### 2026-10-28 operation_id の実測（K6）

`tests/it1/test_hindsight_operation_id.py` を既存の IT1 runner で実行し、3件 PASS。
サーバーは上記と同じ固定digestの `1.1.0-slim`、公式クライアントは `1.1.0`。
実PostgreSQL・OpenAI互換スタブと内部ネットワークを使い、合成データだけを送信した。

- `retain_async=True`、`update_mode="append"` と turn・bank・document から導いた
  UUID の `operation_id`、固定の `timestamp` を指定した。
- 最初の operation が `completed` になった後、同じ識別子・本文を再送した。
  再送後も `completed` を確認し、document の `original_text` は変化せず、本文は1回だけ存在した（PASS）。
- 同じ本文でも別 Turn の識別子では2回追記され、同じ Turn の別 bank では各文書に1回ずつ存在した（PASS）。
- 以上はHindsight側の能力確認であり、本番outbox経由の停止復旧とは別の試験である。
  同期retainや実モデルの試験結果には読み替えない。

### 2026-10-28 処理中の operation_id 再送の実測（P1）

`tests/it1/test_outbox_worker.py::test_processing_operation_resend_preserves_append_identity`
で、上記と同じ固定版サーバー・公式クライアント・実PostgreSQLを使用した。
合成本文に固有マーカーを含め、その要求に対するモデルスタブの応答だけを
制御用volumeの解除通知まで待機させた。retainの応答やoperationの状態は置き換えていない。

- 台帳登録からoutboxへの保存、取得、実retain、deferを通した後、同じworkerの
  次周期で同じ保存操作を再送した。実SDKへ委譲するspyで2回の送信を確認し、
  operation_id・bank・document・本文・timestampが同じであることを検証した。
- 最初のoperationは再送前後とも実状態が `pending` だった。解除後にoperationと
  outboxが `completed` となり、documentの `original_text` に本文は1回だけ存在した（PASS）。
- 比較試験ではbank・document・本文・timestampと初回の処理中条件を固定し、
  2回目のoperation_idだけを別Turn由来へ変えた。最初のoperationは2回目の送信前後とも
  `pending` で、解除後に両operationの完了を待つと本文は2回存在した（PASS）。
- 再送前に `completed` となった場合は前提不成立として失敗する。
  この処理中再送の結果は、既存の完了後再送の確認と区別する。
  本実測では重複する欠陥は観測されず、製品コードの変更は行っていない。
