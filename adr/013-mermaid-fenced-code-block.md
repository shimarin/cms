# ADR-013: Mermaid fenced code blockの検出とテンプレート変数

## 状況

Markdownソース中のASCIIアートやフローチャートをMermaidダイアグラムに置き換えたいという
要望がある。しかしMermaid JSの配布方法・バージョン・CSS調整はサイトごとに方針が異なるため、
CMS本体に組み込むには責務が広すぎる。

## 決定

CMS本体はMermaidブロックの**検出とHTML出力**のみを担当し、JSの読み込み・初期化は
テンプレート/コンテンツ側に委ねる。

### CMS側の責務

1. fenced code blockの言語指定が `mermaid` の場合、Pygmentsに渡さず
   `<pre class="mermaid">（HTMLエスケープ済みの内容）</pre>` として出力する
2. Mermaidブロックを1つ以上含むページでは、テンプレート変数 `uses_mermaid` を `true` に設定する
3. Mermaidブロックを含まないページでは `uses_mermaid` キー自体を設定しない

### テンプレート/コンテンツ側の責務

- Mermaid JSをCDNで読むかローカル配布するか
- Mermaidのバージョン選定
- `mermaid.initialize(...)` の設定
- Mermaid図のCSS調整
- どのテンプレートで読み込むかの判断

## 実装

`_render_fence` 内で `info == "mermaid"` を判定し、マッチした場合は:

- `html.escape()` でコンテンツをエスケープして `<pre class="mermaid">` で囲む
- markdown-itのレンダーenv に `uses_mermaid = True` を記録する

`parse_markdown_document` でレンダーenv から `uses_mermaid` を読み取り、
返却するメタデータに含める。これによりテンプレート変数として自動的に利用可能になる。

## 根拠

- `<pre class="mermaid">` はMermaid JS公式が推奨する記法で、`mermaid.initialize({ startOnLoad: true })` でそのまま検出・変換される
- HTMLエスケープはXSS防止のために必須。Mermaid JSは自身でパース・描画するためエスケープ済みテキストを正しく処理できる
- `uses_mermaid` フラグにより、Mermaidを使わないページで無駄なJSをロードしない選択肢をテンプレート側に提供できる
- Pygmentsにも `mermaid` レキサーは存在しないため、従来は `TextLexer` でフォールバックされていた。専用処理への変更で既存の表示品質が下がることはない
