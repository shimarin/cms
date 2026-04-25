# ADR-006: LLMクローラーにはディレクトリインデックスをraw markdownで返す

## 状況

LLMがWebコンテンツを学習・参照する際、HTMLよりもMarkdownの方が
ノイズ（タグ・スタイル）が少なく処理しやすい。

## 決定

ClaudeBot・GPTBot・PerplexityBot・meta-externalagent のUser-Agentを持つ
クローラーがディレクトリにアクセスした場合、`index.md` を `text/markdown` で
そのまま返す。HTMLレンダリングは行わない。

## 理由

- HTMLレンダリングはブラウザ向けの処理であり、LLMには不要
- Markdownのリンクは `.md` のままなので、クローラーにとって正確な情報
- GoogleBotは検索インデックスへの影響があるため対象外とした

## トレードオフ

- User-Agentは偽装可能だが、意図的に偽装して `.md` を取得しようとする攻撃の実害はない
- `.html` への直接リクエストはLLMクローラーでもHTMLレンダリングを返す（URLを明示指定しているため）
