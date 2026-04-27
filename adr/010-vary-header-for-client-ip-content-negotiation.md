# ADR-010: client_ip_match_any 利用時の Vary ヘッダー自動付与

## 状況

テンプレートで `client_ip_match_any()` を呼び出してコンテンツの出し分けを
する場合、共有キャッシュ（CDN・リバースプロキシ）が異なるクライアントへ
誤ったバリアントを返さないよう、レスポンスに適切な `Vary:` ヘッダーを
付与する必要がある。

しかしテンプレート作者は「IPアドレス判定がどのリクエストヘッダーに依存
しているか」を知らない（`get_client_ip()` の内部実装事項）。テンプレート
側で `Vary: X-Forwarded-For` などと書かせるのは責任分離として理不尽である。

## 検討した代替案

### 案A: テンプレート側に `add_vary_field()` を提供

テンプレートが `add_vary_field('X-Forwarded-For')` を明示的に呼ぶ方式。
判定にマッチした時だけVaryを付け、マッチしなかった時はキャッシュ可能に
できる柔軟さがある。
**却下理由**: テンプレート作者にCMS内部のヘッダー優先順位（CF-Connecting-IP
→ X-Forwarded-For）を意識させるのは責任分離違反。フィールド名を間違えると
キャッシュバリアント管理が壊れる。

### 案B: `mark_response_uncacheable()` をマクロから呼ぶ

```jinja2
{% if client_ip_match_any(['10.0.0.0/8']) %}
  {%- set _ = mark_response_uncacheable() -%}
{% endif %}
```

テンプレートは「キャッシュ不可フラグ」だけ立て、CMSが具体的なヘッダーへ
変換する。
**却下理由**: テンプレート作者がフラグ立て忘れする可能性が残る。

### 案C: Jinja2 マクロで `{% call %}` ブロック化

```jinja2
{% macro if_client_ip_match_any(patterns) %}
  {% if client_ip_match_any(patterns) %}
    {%- set _ = mark_response_uncacheable() -%}
    {{ caller() }}
  {% endif %}
{% endmacro %}

{% call if_client_ip_match_any(['10.0.0.0/8']) %}
  内部向けコンテンツ
{% endcall %}
```

副作用をマクロに閉じ込められて美しい。
**却下理由**: マクロの提供責務がテンプレート制作側に発生する。CMS側で
自動付与したい。

### 案D: Jinja2 Extension（カスタムタグ）

```jinja2
{% if_client_ip ['10.0.0.0/8'] %}
  内部向けコンテンツ
{% endif_client_ip %}
```

CMS側で `Environment` に Extension を登録すればテンプレートはimport不要。
**却下理由**: パーサー実装が必要で実装コストが高い。リクエストごとの状態
（vary_fields セット）を Extension に橋渡しする設計も追加で必要。

### 採用案: 「呼んだら自動でVary」

`client_ip_match_any()` が呼ばれた時点で、CMSが「IP判定に影響しうる
リクエストヘッダー」を自動的に Vary フィールドへ追加する。

## 決定

`_get_client_ip_and_vary(request)` を内部関数として導入し、`(ip, vary_fields)`
を返す。`vary_fields` は **判定に影響しうるヘッダーすべて** を含む：

- `CF-Connecting-IP` 採用時: `("CF-Connecting-IP",)`
- `X-Forwarded-For` 採用時: `("CF-Connecting-IP", "X-Forwarded-For")`
  （CF-Connecting-IPが追加されると判定結果が変わるため）
- 信頼済みプロキシで両ヘッダー欠落: `("CF-Connecting-IP", "X-Forwarded-For")`
- 信頼外（直接接続）: `()`（プロキシヘッダーを参照しないため）

`make_client_ip_match_any(request, vary_fields)` はオプションの `set` を
受け取り、呼ばれるたびに上記タプルの中身を `update()` する。

`render_md_file()` / `render_error()` はリクエストごとに `vary_fields` セット
を生成してテンプレート描画に渡し、レンダリング後にセットが空でなければ
レスポンスへ `Vary:` ヘッダーをセットする。

## 理由

- テンプレート作者は何も意識せずに `client_ip_match_any()` を使える
- 「実際に使ったヘッダー」ではなく「判定に影響しうるヘッダー」をすべてVary
  に含めることで、後続リクエストで別ヘッダーが追加されたときのキャッシュ
  バリアント混同を防げる
- 「呼んだ時点で常にVary追加」は安全寄りの倒し方。マッチしなかった時に
  Varyを付けないとキャッシュ可能性は上がるが、テンプレート側に追加の
  責務（マッチした時だけフラグを立てる）が発生する。本CMSのユースケース
  では、IP出し分けは解析タグの抑制など軽微な用途が中心であり、過剰なVary
  によるキャッシュヒット率低下は許容できる

## トレードオフ・既知の制約

### 304 レスポンスには Vary が付かない

`render_md_file()` は ETag / Last-Modified による条件付きGETで、テンプレート
描画前に 304 を返す早期リターンがある。テンプレートが何を参照するかは描画
してみないとわからないため、304 にテンプレート由来の Vary を付与する手段が
現状ない。

共有キャッシュは保存済みレスポンスのメタデータ更新時に 304 の Vary を参照
する可能性があるため、厳密には不整合が起こり得る。

### セキュリティ境界としての利用は推奨しない

`client_ip_match_any()` は次のような用途を想定している：

- 解析タグ（GA等）の出し分け
- 内部用デバッグ情報の表示

**非公開情報・パーソナル情報・認可境界の出し分けには使わないこと。** 上記
304 の制約と、`X-Forwarded-For` ベースの判定を共有キャッシュが完璧に扱う
保証がないため、リスクが残る。
