# ADR-006: LLMクローラーにはディレクトリインデックスをraw markdownで返す

## 状況

LLMがWebコンテンツを学習・参照する際、HTMLよりもMarkdownの方が
ノイズ（タグ・スタイル）が少なく処理しやすい。

## 決定

ClaudeBot・GPTBot・ChatGPT-User・PerplexityBot・meta-externalagent のUser-Agentを持つ
クローラーがディレクトリにアクセスした場合、`index.md` を `text/markdown` で
そのまま返す。HTMLレンダリングは行わない。

また、MJ12bot のような積極的（Eager）なクローラーも同じ枠に含める。
これらはLLMクローラーではないが、アクセス量が多く負荷になりやすいため、
HTMLよりも構造がシンプルなMarkdownを返すことでダウンロードサイズを減らし
トラフィックを抑制することを意図している。

**方針:** LLMクローラーと同様に「Markdownを与えてよいEagerなボット」は
`LLM_CRAWLERS` タプルにまとめて管理する。

## 理由

- HTMLレンダリングはブラウザ向けの処理であり、LLMには不要
- Markdownのリンクは `.md` のままなので、クローラーにとって正確な情報
- GoogleBotは検索インデックスへの影響があるため対象外とした
- Eagerなボットへは軽量なレスポンスを返すことでサーバー負荷・帯域を節約できる

## トレードオフ

- User-Agentは偽装可能だが、意図的に偽装して `.md` を取得しようとする攻撃の実害はない
- `.html` への直接リクエストはLLMクローラーでもHTMLレンダリングを返す（URLを明示指定しているため）
- `LLM_CRAWLERS` という名称は厳密にはLLM専用ではないが、主用途がLLMクローラー対応であるため据え置く
