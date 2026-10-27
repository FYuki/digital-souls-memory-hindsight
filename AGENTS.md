# リポジトリ内の作業指示

変更前に [CONTRIBUTING.md](CONTRIBUTING.md) と [受け入れ条件](docs/acceptance-criteria.md) を読んでください。
開発規約の正本は CONTRIBUTING で、このファイルはエージェント向けの入口です。

- タスクごとの作業ブランチから、対応する `epic/*` 宛ての Draft PR を作成します。
- 作業 PR → epic は最終 head の CI、epic → main は最終 head の CI と実際の CodeRabbit レビューが必要です。merge 権限は依頼で確認します。
- TDD を守り、依頼された実装範囲を超えないでください。
- Hindsight をフォーク・改変しません（受け入れ条件 A1）。上流の修正が必要なら、外側で回避するか上流への報告を提案します。
- 合成データだけを使い、実モデル・実環境の実行権限をこの文書から推定しません。
- このリポジトリは public です。秘密情報・私的ログ・実データを書きません。
- FAIL・SKIP・NOT RUN は PASS ではありません。結果を区別して報告してください。
