# ADR-009: RFC 8288 Link ヘッダーで RSS フィードを提示する

## 状況

ADR-006 の方針によりLLMクローラーには Markdown を返すが、Markdown には
HTML の `<link rel="alternate" type="application/rss+xml">` に相当する
RSS フィードへの誘導手段がない。クローラーやリンク対応クライアントに対して
コンテンツタイプに依存しない形で関連フィードを示す方法が必要だった。

## 決定

`defaults.json` またはフロントマターに `rss:` キーで RSS フィード URL を
指定できるようにし、ページレスポンスに RFC 8288 準拠の `Link:` ヘッダーを付与する。

```
Link: </blog/feed.xml>; rel="alternate"; type="application/rss+xml"
Link: </sitemap.xml>; rel="sitemap"
```

`rss:` と `sitemap:` を両方設定した場合は1つの `Link:` ヘッダーにカンマ区切りで結合する。

```
Link: </blog/feed.xml>; rel="alternate"; type="application/rss+xml", </sitemap.xml>; rel="sitemap"
```

指定できるキーと生成される `rel`:

| キー | `rel` | 用途 |
|------|-------|------|
| `rss` | `alternate` + `type="application/rss+xml"` | RSSフィード |
| `sitemap` | `sitemap` | サイトマップ |

- `render_md_file()` が返す HTML レスポンス（200 および 304）
- LLMクローラー向け Markdown `FileResponse`（ディレクトリ `index.md`・直接 `.md` アクセス）

`rss:` と `sitemap:` の両方が未設定の場合は `Link:` ヘッダーを付与しない。

304 レスポンスにも同じ `Link:` ヘッダーを含める。これは RFC 7232 §4.1 の
「表現メタデータは 304 に含めてよい」という方針と、キャッシュ再検証時でも
クライアントがフィード URL を取得できるようにする意図による。

## 理由

- HTTP ヘッダーはコンテンツタイプに依存しないため、Markdown レスポンスでも機能する
- `rel="alternate"` + `type="application/rss+xml"` は RSS の標準的な関係型であり、
  HTML の `<link>` と意味的に等価
- `rss:` という専用キーにしたのは、汎用の `links:` よりシンプルで
  テンプレート変数との衝突リスクが低いため。将来より汎用的な `http_links:` 等が
  必要になっても `rss:` と併存できる

## トレードオフ

- クローラーが `Link:` ヘッダーを実際に解釈するかは実装依存だが、付与しないよりは情報が増える
- `defaults.json` に書くことでディレクトリ配下全ページに一括適用できる反面、
  RSS と無関係なページ（トップページ等）にも付与される可能性がある。
  必要であればフロントマターで上書き（`rss: null` 等）することで制御できる
