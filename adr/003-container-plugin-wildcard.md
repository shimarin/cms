# ADR-003: container_pluginをワイルドカードvalidateで1回だけ登録する

## 状況

mdit-py-pluginsのcontainer_pluginは本来クラス名ごとに1回ずつ登録する設計。
`:::warning`、`:::info` などを使うには事前に名前を登録する必要がある。

## 選択肢

**A) フロントマターまたはdefaults.jsonの `container_classes` で登録クラスを指定**

**B) カスタムvalidateで任意のクラス名を受け付けるよう1回だけ登録する**

## 決定

**B** を採用。`validate` に任意の非空文字列を受け付ける関数を渡し、
`render` でトークンの `info`（`:::` 直後のパラメータ）をそのままclass属性に使う。

```python
def _container_validate(params, markup):
    return bool(params.strip().split(None, 1)[0])

def _container_render(self, tokens, idx, options, env):
    if token.nesting == 1:
        token.attrSet("class", token.info.strip().split(None, 1)[0])
    return self.renderToken(tokens, idx, options, env)
```

## 理由

- 事前登録なしで任意のクラス名が使えた方がコンテンツ作成の自由度が高い
- XSSリスクはJinja2のautoescapeとは別レイヤーだが、class属性への挿入に限られ実害は小さい
- そもそもMarkdownを書く人間を信頼する運用前提のCMS
