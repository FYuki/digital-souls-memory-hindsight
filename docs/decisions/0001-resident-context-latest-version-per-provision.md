# ADR 0001：常駐コンテキストは提供ごとの最新版を使う

## 状態・日付

採用。本人決定：2026-11-03（Epic #43 / Issue #44）。

## 背景

常駐コンテキストは、セッション途中の人格の登録・設定変更を次の提供から反映する必要がある。
従来の AC-I4 にあるセッション開始時点の版固定では、この要求を満たせない。
また、版を固定しなくても各応答で渡した内容を後から辿れるよう、提供時点の版と内容を記録する必要がある。

A9 の既存エンドポイント・スキーマへの準拠は比較用の定めであり、受け入れ条件の要求により合う方法があればそれを採ってよい。
本決定は [受け入れ条件 AC-I4](../acceptance-criteria.md#36-identityvoice状態api) に反映する。

## 決定

- 常駐コンテキスト API は、提供のたびにその時点で登録済みの最新版の CoreIdentity・Temperament・PersonalityProfile と、その時点で利用可能な代表発話・CharacterProfile を返す。セッション単位では固定せず、途中の登録・設定変更は次の提供から反映する。
- resident の ProvisionRecord に、返した内容と一致する人格の版・代表発話 ID・CharacterProfile の revision と返却項目を保存し、commit 後に成功を返す。記録の目的は版の固定ではなく、各提供で渡した内容の追跡である。
- ターン想起は `resident_provision_id` で常駐の提供記録へ辿る。
- Memory は渡した内容だけを記録する。Core で実際に使用した内容の記録は Core 側の責務とする。
- 自動の人格更新は Phase 3 の対象とする。

## 正本ドメインモデル・既存実装との違い

比較対象の正本は [digital-souls-memory `4c84939:docs/domain-model.md`](https://github.com/FYuki/digital-souls-memory/blob/4c8493916fa8350cd610a97b68351a5a14f22a92/docs/domain-model.md) とする。
Issue #44 が指定する正本・既存実装との差分は次のとおり。

| 項目 | 今回の決定 |
| --- | --- |
| 人格の版 | セッション単位の版固定を廃止し、途中の登録・設定変更を次の提供から反映する。提供した版は resident の提供記録に残す |
| VoiceExemplar | `trait_version` を廃止する。提供した代表発話は resident の提供記録の ID で辿る |
| CharacterProfile の上限 | トークン数ではなくコードポイント数で数える |
| CharacterProfile の項目 | 原文区間に限る。Hindsight の memory ID は document の作り直しで変わるため、項目の参照先に使わない |

## 不採用案

Session 登録時に人格の版を固定する案は採らない。
参加キャラの列挙が必要であり、セッション途中で切り替える要求に合わないためである。

## 再検討条件

セッション内での一貫性が評価要件になったとき、版の選び方を再検討する。

## 影響

AC-I4 の判定は IT1 とし、ST は Epic 5 で扱う。
今回の変更は受け入れ条件と設計記録の同期に限る。
常駐コンテキスト API・ProvisionRecord・`resident_provision_id` などのコード変更は4-1以降で行う。
