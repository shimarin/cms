# CMS

Markdown→HTMLのオンデマンド変換をするCMS

## 使用ライブラリ

- starlette / uvicorn
- markdown-it-py / mdit-py-plugins
- jinja2
- pyyaml

## 起動

```bash
# 開発時（auto-reload有効）
python app.py

# 本番・systemd運用時（reload無効、uvicornアクセスログ抑制）
python app.py --no-reload --no-uvicorn-access-log

# または uvicorn を直接使う場合
uvicorn app:app --reload
```

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

### LLMクローラーへの対応

以下のUser-Agentを持つLLMクローラーがディレクトリにアクセスした場合、
HTMLレンダリングせず `index.md` をそのまま `text/markdown` で返す。

- `ClaudeBot` (Anthropic)
- `GPTBot` (OpenAI)
- `PerplexityBot`
- `meta-externalagent` (Meta)

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

## テンプレート

Jinja2テンプレートはvhost固有 → グローバル（`templates/`）の順で探索。

テンプレート内で使える変数:

| 変数 | 内容 |
|------|------|
| `body` | Markdownから変換されたHTML |
| フロントマター・defaults.jsonの全キー | そのまま展開 |

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

- `Last-Modified`: `.md` ファイルのmtime
- `ETag`: mtimeの整数値
- `Cache-Control: no-cache`

`If-None-Match` / `If-Modified-Since` による304レスポンスに対応。

## アクセスログ

Apache combined フォーマット。vhostごとに `logs/access_log` へ出力。
深夜0時に自動ローテーション、30日分保持。

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
