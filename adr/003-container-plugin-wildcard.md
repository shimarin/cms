# ADR-003: コンテナ構文の実装方式（独自block ruleへの変更）

## 状況

`:::classname` … `:::` を `<div class="classname">` にレンダリングするコンテナ構文が必要。
mdit-py-plugins の `container_plugin` は本来クラス名ごとに1回ずつ登録する設計。

## これまでの試行

**試行A) ワイルドカードvalidateで1回だけ登録**（当初実装）

任意の非空文字列を受け付ける `validate` 関数を渡して1つのルールで全クラス名を処理。
→ 入れ子コンテナで開閉マッチが崩れる問題が発生。各ルールが独立して閉じ `:::` を探すため、
外側のコンテナが内側の `:::` を誤って閉じタグと認識してしまう。
また `params.strip().split(None, 1)[0]` が閉じタグ（空params）で `IndexError`。

**試行B) クラスごとに `container_plugin` を別登録**

`container_classes` で指定されたクラスを個別に登録。
→ 依然として各ルールが独立動作するため入れ子崩れは解消されず。

## 決定

**C) CMS独自の block rule `_make_container_rule` を実装**（現在の実装）

単一のルールで全クラス名を処理し、depth カウンタで開閉を追跡する。

```python
depth = 1
while nextLine < endLine:
    # 開き ::: classname → depth+1
    # 閉じ ::: (空params) → depth-1
    # depth == 0 → auto_closed = True; break
```

使用するclass名はフロントマターまたは `defaults.json` の `container_classes` で指定する（選択肢Aのアプローチ）。
`container_classes` が空の場合、コンテナルール自体を登録しない（2-pass でfirst-pass時に抽出）。

## 理由

- 任意クラス名ワイルドカードは入れ子で破綻するため、クラス登録は必須
- 複数ルール登録より単一ルール+depthカウンタの方が入れ子を正確に処理できる
- 2-pass（frontmatter抽出 → make_md → レンダリング）で登録クラスの事前把握が可能
- XSSリスク: class属性への挿入に限られ、Markdownを書く人を信頼する運用前提
