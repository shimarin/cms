# ADR-002: vhostが存在する場合はトップレベルのdefaults.jsonを読まない

## 状況

`defaults.json` の継承チェーンをどこから始めるかという問題。
vhost固有のdocsとトップレベルのdocsが両方存在する場合、
トップレベルの `defaults.json` をグローバルベースとして読むかどうか。

## 選択肢

**A) ファイルが見つかったdocs_dirのみを起点にする**
- vhostあり: `vhosts/<hostname>/docs/` チェーンのみ
- vhostなし: トップレベル `docs/` チェーンのみ

**B) トップレベルをグローバルベースとして先頭に加える**
- `docs/defaults.json` → `vhosts/<hostname>/docs/defaults.json` → ...

## 決定

**A** を採用。vhostが存在する場合、トップレベルの `defaults.json` はあらゆる階層において一切読まない。

## 理由

- Bは「トップレベルに共有リソースしか置かない」ルールと一見整合するが、
  defaults.jsonが静的ファイルと異なりコンテンツの挙動に影響するため、
  意図しない設定の混入リスクが高い
- 静的ファイルのフォールバックと違い、defaults.jsonの継承は複雑になりやすく、
  デバッグが困難になる
- 「vhostがあるならvhostで完結する」という単純なルールの方が運用しやすい
