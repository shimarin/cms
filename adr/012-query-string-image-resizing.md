# ADR-012: 画像リサイズはquery stringで指定し304再検証に寄せる

## 状況

このCMSで配信する画像に対して、HTML制作側から幅・高さ・クロップ方法を指定して
自動リサイズできるようにしたい。

当初は、変換仕様をURLパスに含める案とquery stringで指定する案があった。
また、元画像のmtimeをURLに含めれば `Cache-Control: immutable` に近い強いキャッシュを
使いやすいが、このCMSでは画像リンクをHTML制作側が手で考えるため、制作時に元画像のmtimeを
知る手段がない。

対象サイトは小規模であり、CDNとの厳密な役割分担やorigin側の変換結果ディスクキャッシュは、
実運用で必要性が見えてから検討する。

## 選択肢

**A) パスで変換仕様を指定する**

例: `/photo.jpg/w800-h450-cover.jpg`

CDNのキャッシュキーとしては扱いやすく、フォーマット変換もURLとして自然に表現できる。
一方で、既存の静的ファイルパス解決との衝突を避ける設計が必要で、HTML制作側が手書きするには
記法がやや重い。今回はフォーマット変換も不要なため、利点が小さい。

**B) query stringで変換仕様を指定する**

例: `/photo.jpg?w=800&h=450&fit=cover`

HTML制作側が手書きしやすく、既存の静的ファイル配信の入口に小さく追加できる。
CDNではquery string付きURLのキャッシュ扱いが設定依存になるが、小規模サイトではまず実運用で
観察してから詰めればよい。

**C) 元画像mtimeをURLに含める**

例: `/photo.jpg?w=800&h=450&fit=cover&v=1777500000`

長期キャッシュしやすいが、HTML制作側がmtimeを知る必要がある。
テンプレートヘルパーでURLを生成する設計なら有効だが、今回は手書きリンクを前提にするため採用しない。

**D) origin側に変換結果のディスクキャッシュを持つ**

CDNが期待通りに再検証しない場合でもoriginのCPU負荷を抑えられる。
ただしキャッシュファイルのキー設計、削除方針、容量管理が必要になる。
対象サイトの規模に対して初期実装としては重い。

## 決定

**B** を採用し、mtime入りURLとorigin側ディスクキャッシュは採用しない。

- 対象形式は JPEG / PNG / WebP
- 指定は `w` / `width`, `h` / `height`, `fit`
- `fit` は `cover`, `contain`, `inside` のみ
- フォーマット変換はしない
- 変換結果のサーバー側ディスクキャッシュは持たない
- レスポンスは `Cache-Control: public, no-cache`
- `Last-Modified` は元画像ファイルのmtime
- `ETag` は元画像のmtimeナノ秒・サイズ・正規化済み変換パラメータから作る
- `If-None-Match` / `If-Modified-Since` が一致した場合は、画像をデコード・変換せず
  `304 Not Modified` を返す

## 制作運用への含意

HTML制作側は通常の画像URLにquery stringを付けるだけでよい。

```text
/photo.jpg?w=800&h=450&fit=cover
```

mtimeをURLに含めないため、ブラウザやCDNに長期immutableキャッシュを期待する設計ではない。
一方で、クライアントまたはCDNが条件付きGETを送る場合、originは安く304を返せる。

CDNがquery string付き画像を保存するか、originへの再検証時に `If-None-Match` /
`If-Modified-Since` を付けるかはCDN設定に依存する。運用時にはCDNのキャッシュ状態ヘッダと
originログを見て、必要ならディスクキャッシュや短い `max-age` の導入を再検討する。
