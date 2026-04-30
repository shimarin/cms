# CMS

Markdown→HTMLのオンデマンド変換をするCMS

## 使用ライブラリ

- starlette / uvicorn
- markdown-it-py / mdit-py-plugins
- jinja2
- pyyaml
- Pillow

## 起動

```bash
# 開発時（auto-reload有効）
python app.py

# 本番・systemd運用時（reload無効、uvicornアクセスログ抑制）
python app.py --no-reload --file-logging

# UNIXドメインソケットで待受（リバースプロキシとの連携など）
python app.py --unix-socket /run/cms/app.sock --no-reload --file-logging

# または uvicorn を直接使う場合
uvicorn app:app --reload
```

`--unix-socket` を指定した場合、`--host` / `--port` は無視される。

## ディレクトリ構成

```
docs/          ドキュメントルート（デフォルトvhost兼フォールバック）
templates/     Jinja2テンプレート置き場（vhost間共通フォールバック）
  default.j2   デフォルトテンプレート
logs/          access_log / error_log
vhosts/
  example.com/
    docs/      vhost固有ドキュメントルート
    templates/ vhost固有テンプレート
    logs/
  www.foobar.com/
    ...
```

vhostを使わない場合はトップレベルの `docs/` と `templates/` だけ使えばよい。
vhostを運用する場合、トップレベルには全vhostで共有してよいリソース（画像など）のみ置くこと。

## vhostルーティング

`Host` ヘッダを見て `vhosts/<hostname>/` にマッチするディレクトリを探す。

- 一致するディレクトリがない場合はトップレベル `docs/` をフォールバックとして使用
- `www.` の付け外しでマッチできる場合は 301 リダイレクト
  - 例: `www.example.com` → `example.com`（`vhosts/example.com/` が存在する場合）
  - 例: `foobar.com` → `www.foobar.com`（`vhosts/www.foobar.com/` が存在する場合）

## ファイル配信

`.html` へのリクエストに対応する `.md` が存在する場合はMarkdown→HTML変換してレスポンス。
それ以外は静的ファイルとして配信。ファイルが見つからない場合はトップレベル `docs/` へフォールバック。

ディレクトリへのリクエストは `index.html` → `index.md` の順で探す。
vhostのディレクトリが存在するがindexがない場合はトップレベルへのフォールバックは行わない。

### 画像リサイズ・クロップ

JPEG / PNG / WebP 画像にはquery stringでリサイズ指定できる。
フォーマット変換は行わず、元画像と同じ形式で返す。

```text
/logo.png?w=320
/photo.jpg?w=800&h=450&fit=cover
/photo.jpg?width=1200&height=630&fit=contain
```

- `w` / `width`: 出力幅（1〜4096）
- `h` / `height`: 出力高さ（1〜4096）
- `fit=cover`: 指定サイズを埋めるように中央クロップ（`w` と `h` が必須）
- `fit=contain`: 指定枠内に収める。必要なら拡大もする
- `fit=inside`: 指定枠内に収める。拡大はしない

`fit` 省略時は、`w` と `h` の両方があれば `cover`、片方だけなら `inside`。

変換結果のサーバー側ディスクキャッシュは持たない。レスポンスには元画像のmtimeと
変換パラメータから作った `Last-Modified` / `ETag` を付与し、条件付きGETでは画像処理前に
`304 Not Modified` を返す。

### LLMクローラーへの対応

以下のUser-Agentを持つLLMクローラー・Eagerボットに対して `.md` を優先配信する。

- `ClaudeBot` (Anthropic)
- `GPTBot` (OpenAI)
- `PerplexityBot`
- `meta-externalagent` (Meta)
- `MJ12bot` など（Eagerクローラー）

**ディレクトリアクセス時:** HTMLレンダリングせず `index.md` をそのまま `text/markdown` で返す。

**`.html` アクセス時:** 対応する `.md` が存在する場合、その `.md` URL へ302リダイレクトする。
Markdown内のリンクは `.md` のまま（HTMLレンダリング時のみ `.html` に書き換え）なので、
一度 `.md` へ誘導されたクローラーは以降 `.md` のみを辿る。

## Markdown変換

- パーサー: markdown-it-py (commonmark + table)
- プラグイン: front_matter, footnote
- サイト内リンクの `.md` は自動的に `.html` に変換

### テーブル

GFM形式のパイプテーブルをサポート:

```markdown
| 項目 | 内容 |
|------|------|
| 社名 | 例株式会社 |
```

### コンテナ構文

```
::: classname
内容（入れ子可）
:::
```

`<div class="classname">` としてレンダリング。入れ子に対応。
使用するクラス名はフロントマターまたは `defaults.json` の `container_classes` に列挙する必要がある:

```yaml
container_classes:
  - section
  - service-grid
  - service-card
```

## フロントマターとdefaults.json

各Markdownファイルのフロントマター（YAML）に変数を記述するとJinja2テンプレートに渡される。

```yaml
---
title: ページタイトル
description: ページの説明
template: custom.j2   # 省略時は default.j2
timezone: Asia/Tokyo  # 省略時はシステムTZ
date: 2026-04-25      # 記事の公開日（省略するとドラフト扱い）
rss: /blog/feed.xml   # RSSフィードURL（省略時はLink:ヘッダなし）
sitemap: /sitemap.xml # サイトマップURL（省略時はLink:ヘッダなし）
---
```

各ディレクトリに `defaults.json` を置くと、そのディレクトリ以下のページのデフォルト値として継承される。
上位ディレクトリから下位へ継承され、下位が優先。フロントマターが最優先。

```
docs/defaults.json          ← 全体のデフォルト
docs/blog/defaults.json     ← blog/ 以下のデフォルト（上書き）
docs/blog/post.md           ← フロントマター（最優先）
```

`defaults.json` への外部HTTPアクセスは403を返す。

vhostディレクトリが存在する場合、トップレベルの `defaults.json` は一切読まない（vhostスコープのみ）。

### 本文・mtime からのメタデータ自動補完

`defaults.json` またはフロントマターで以下のフラグを有効にすると、フロントマターの欠けた値を本文・ファイル属性から補える。

```json
{
  "h1_as_title": true,
  "mtime_as_date": true
}
```

- `h1_as_title: true` — フロントマターに `title` がない場合、本文先頭のH1をタイトルとして採用し、本文HTMLからは取り除く。タイトルはインライン装飾（リンク・強調等）を除去したプレーンテキスト化される。`title` がフロントマター等で明示されている場合は何もしない。
- `mtime_as_date: true` — フロントマターに `date` がない場合、Markdownファイルのmtimeを `date` として採用する。`timezone` 設定に従ったタイムゾーン付き datetime になる。`date` が明示されている場合は従来通りそれを優先。mtimeはファイル編集で変わるため、厳密な公開日ではなく更新日時寄りになる点に注意。

両フラグは通常ページ表示・`index_of()` ・`feed.xml` ・`sitemap.xml` のいずれでも同じ抽出結果が使われる。

## テンプレート

Jinja2テンプレートはvhost固有 → グローバル（`templates/`）の順で探索。

テンプレート内で使える変数:

| 変数 | 内容 |
|------|------|
| `body` | Markdownから変換されたHTML |
| `url_path` | リクエストのパス（例: `/article/foo.html`） |
| `page_dir` | ページが属するディレクトリ（例: `/article/`） |
| `site_url` | スキーム＋ホスト（例: `https://www.example.com`） |
| フロントマター・defaults.jsonの全キー | そのまま展開 |

`site_url` はリバースプロキシ配下で `X-Forwarded-Proto` / `X-Forwarded-Host` / `CF-Visitor` を信頼済みプロキシからの接続時のみ参照して動的生成する。
defaults.json またはフロントマターで `site_url` を明示指定するとその値が優先される。

### client_ip_match_any 関数

クライアントIPアドレスがリストに含まれているかを判定するテンプレート関数。
IPv4/IPv6の単一アドレスとCIDR表記の両方に対応。

```jinja2
{# defaults.json の internal_client_ips リストにマッチするIPからはGAタグを出さない #}
{% if site_url == "https://www.example.com" and not client_ip_match_any(internal_client_ips | default([])) %}
  <!-- Google Analytics tag -->
{% endif %}
```

`defaults.json` 側の設定例:

```json
{
  "internal_client_ips": [
    "127.0.0.1",
    "::1",
    "192.0.2.10",
    "2001:db8::/32"
  ]
}
```

- 無効なエントリは無視してサイト全体には影響しない
- クライアントIPの解決は `get_client_ip()` 基準（信頼済みプロキシ対応）
- 通常ページテンプレートとカスタムエラーテンプレートの両方で利用可能
- テンプレート内で1回でも呼ばれると、レスポンスに適切な `Vary:` ヘッダーが
  自動的に付与される（IP判定に影響しうる `CF-Connecting-IP` /
  `X-Forwarded-For` を共有キャッシュへ通知する）。詳細は ADR-010 参照
- **セキュリティ境界・認可・非公開情報の出し分けには使わないこと。** 主に
  解析タグの抑制など軽微な出し分け用途を想定している。

### カスタムエラーページ

`templates/{status_code}.j2`（例: `404.j2`、`403.j2`）が存在する場合、
`.html` へのリクエストに対してそのテンプレートでエラーページを生成する。
テンプレート内で `{{ status_code }}` が使える。
`.html` 以外へのリクエスト（画像・`.md` など）はプレーンテキストでエラーを返す。

### index_of関数

テンプレート内でディレクトリをスキャンして記事一覧を取得できる。

```jinja2
{% for article in index_of("/blog") %}
  <a href="{{ article.url }}">{{ article.title }}</a>
  {{ article.date.strftime("%Y-%m-%d") }}
{% endfor %}

{# 最新3件だけ #}
{% for article in index_of("/blog")[:3] %}
  ...
{% endfor %}

{# index.mdも含める場合 #}
{% for article in index_of("/blog", include_index=True) %}
  ...
{% endfor %}
```

- `date:` のないファイルはドラフトとして除外される（デフォルト: `index.md` も除外）
- 結果は `date` 降順ソート済み
- サブディレクトリを再帰的にスキャンする
- `index_of()` を呼んだMarkdown→HTMLレスポンスは、配下ドキュメントの追加・更新を
  常に反映するため `Cache-Control: no-store` になり、`Last-Modified` / `ETag` は
  付与されない。詳細は ADR-011 参照

#### dateフィールドの書式

YAMLが自動解釈できる以下の形式をサポート:

```yaml
date: 2026-04-25               # 日付のみ
date: 2026-04-25 10:00:00      # 日時（タイムゾーンなし→timezone設定で補完）
date: 2026-04-25 10:00:00+09:00
date: 2026-04-25T10:00:00+09:00
```

`2026/04/25` のようなスラッシュ区切りは非サポート（ドラフト扱いになる）。

## RSSフィードとサイトマップ

### feed.xml

任意のディレクトリに対して `feed.xml` でRSSフィードを生成できる。

- `/feed.xml` → サイト全体
- `/blog/feed.xml` → `/blog/` 以下の記事
- `date:` のないファイル（ドラフト）と `index.md` は除外
- 存在しないディレクトリへのリクエストは404

チャンネルタイトル・説明はそのディレクトリの `defaults.json` の `title:` / `description:` を使用。

### sitemap.xml

`/sitemap.xml` でサイトマップを生成する。

- `date:` のあるページのみ掲載（ドラフト除外）
- `index.md` も含む
- `lastmod` はファイルのmtime

## HTTPキャッシュ

Markdown→HTMLレスポンスには以下のヘッダを付与:

- `Last-Modified`: `.md` ファイルと採用テンプレートのmtime最大値
- `ETag`: mtime最大値の整数値
- `Cache-Control: no-cache`
- `Link: <url>; rel="alternate"; type="application/rss+xml"` — `rss` が設定されている場合
- `Link: <url>; rel="sitemap"` — `sitemap` が設定されている場合

両方設定されている場合は1つの `Link:` ヘッダーにカンマ区切りで結合する。

`If-None-Match` / `If-Modified-Since` による304レスポンスにも同じヘッダセット（`Link` を含む）を付与する。

テンプレート内で `index_of()` が1回でも呼ばれた場合、そのレスポンスは
`Cache-Control: no-store` になり、`Last-Modified` / `ETag` は付与しない。
条件付きGETでも304を返さず、常に本文を再生成する。

画像リサイズレスポンスには以下のヘッダを付与:

- `Last-Modified`: 元画像ファイルのmtime
- `ETag`: 元画像のmtimeナノ秒・サイズ・正規化済み変換パラメータ
- `Cache-Control: public, no-cache`

`If-None-Match` / `If-Modified-Since` が一致した場合は、画像をデコード・変換せずに
`304 Not Modified` を返す。

## アクセスログ

Apache combined フォーマット。vhostごとに `logs/access_log` へ出力。
深夜0時に自動ローテーション、30日分保持。
ただしローテーション時点でファイルサイズが閾値未満の場合はスキップする（デフォルト: 1MB）。
閾値は `--log-rotation-min-bytes` 引数で変更可能（0を指定すると常にローテーション）。

ファイルへのアクセスログ出力は `--file-logging` を指定した場合のみ有効になる。
uvicornアクセスログを出力している場合（デフォルト）はファイルには書き込まない。

リバースプロキシ経由の場合のクライアントIP解決順:

1. `CF-Connecting-IP`（Cloudflare Tunnel）
2. `X-Forwarded-For` の左端
3. `REMOTE_ADDR`

`REMOTE_ADDR` がプライベートアドレス/ローカルホストの場合のみ上記ヘッダを信頼する。

## APIエンドポイント

`/api/` 以下は静的ファイルを一切配信しないAPIエンドポイント専用の名前空間。

設定ファイルは `docs/api/settings.json`（vhost対応）。
このファイルへの外部HTTPアクセスは `/api/` ブロックにより不可能。

### /api/inquiry

問い合わせフォーム用エンドポイント。

**GET** → XSRFトークンを発行してJSONで返す

```json
{"token": "..."}
```

**POST** → トークン検証後メール送信

- `X-XSRF-Token` ヘッダにGETで取得したトークンを付ける
- トークンは使い捨て・有効期限1時間
- リクエストボディはJSON（フィールド自由）

```json
{"name": "山田", "email": "yamada@example.com", "message": "問い合わせ内容"}
```

#### settings.json の構造

```json
{
  "smtp": {
    "host": "smtp.example.com",
    "port": 587,
    "use_tls": true,
    "username": "user@example.com",
    "password": "secret",
    "from": "noreply@example.com"
  },
  "inquiry": {
    "to": "admin@example.com",
    "default_subject": "お問い合わせ",
    "template": "inquiry_mail.j2",
    "honeypot": "website"
  }
}
```

| キー | 説明 |
|------|------|
| `smtp.from` | 差出人アドレス。省略時は `smtp.username` |
| `inquiry.default_subject` | 件名のデフォルト値 |
| `inquiry.template` | メール本文のJinja2テンプレート名（省略時はJSONをpretty-print） |
| `inquiry.honeypot` | ハニーポットフィールド名。省略時はチェックなし |

#### メールテンプレート

テンプレートの先頭行が `Subject:` で始まる場合、その内容が件名になる。

```jinja2
Subject: {{ name }}からのお問い合わせ

お名前: {{ name }}
メッセージ:
{{ message }}
```

テンプレート変数にはPOSTされたJSONの全フィールドが展開される。

#### ハニーポット

`inquiry.honeypot` に指定したフィールド名に値が入っていた場合、
メール送信せず `{"ok": true}` を返す（ボットに検知を気づかせない）。
HTMLフォーム側でそのフィールドをCSSで非表示にしておく。
