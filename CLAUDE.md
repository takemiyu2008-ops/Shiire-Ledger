# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## プロジェクト概要

コンビニ店舗に届く**仕入先の納品書・返品伝票（印刷）**を台帳にするアプリ。
スタッフが iPhone のショートカットで伝票を撮影 → **iPhone の中の文字認識**で文字にする → アプリが行を組み立てて検算 → 合えば自動登録、合わなければ確認画面 → 全員で閲覧し、月ごとの差し引き支払額（納品−返品）を見る。

**ユーザーの方針: 伝票の写真を外部に送らない（Anthropic API などの AI サービスは使わない）。** 文字にした中身を Firebase で共有するのは了承済み（2026-10-22）。

予約台帳（`~/work/予約台帳/`）を複製して作り替えたもの。同期・イベント追記型の仕組みは同じ。予約台帳の Cloud Function による読み取りは外した。

- `index.html` 1ファイルのアプリ（GitHub Pages で公開。現金管理アプリ・予約台帳と同じ作り）
- `tools/ocr-mac.swift` … Mac で写真を Apple の文字認識にかける（iPhone のショートカットと同じ仕組み。解析の調整用）
- `tests/test_sync.py` … Playwright E2E（モック RTDB）。`samples/` があれば本物の認識結果でも解析を照合
- `samples/` … git 管理外。実際の納品書・返品伝票の写真、文字認識の出力 `ocr_*.txt`、正解 `truth.json`

タブは「一覧／撮影／月次／設定」。ヘッダーに今月（伝票日付の月）の差し引き支払額と伝票枚数、同期チップ、操作者チップ。

## アーキテクチャ

### 項目定義 `FIELDS`（単一の正）
`index.html` の `// FIELDS-BEGIN` 〜 `// FIELDS-END` の区間。**フォーム・読み取り用 JSON Schema・一覧・CSV・`tools/read-test.mjs` はすべてここから生成する**。項目を増減するときはこの配列だけを直す（Function の再デプロイは不要）。
- 伝票: `kind`（納品／返品・`options` で選択式）、`slipDate`、`supplier`、`subtotal`・`tax`・`total`、`memo`（`read:false`）
- 明細 `ITEM_FIELDS`: `origin`（産地欄の記号「m」等をそのまま）、`name`、`qty`、`unitPrice`、`amount`
- `options` 付きの項目は、読み取り結果が選択肢の語を含むかで振り分ける（「納品書」→納品、「返品伝票」→返品）

### 文字認識テキストの解析（`parseOcrText`）
- 入口: ショートカットが開く `…/index.html#ocr=<URL エンコードした文字>`（`takeOcrFromHash`。読んだらハッシュを消す）と、「撮影」タブへの貼り付け。撮影タブの「📷 伝票を撮る」は `shortcuts://run-shortcut?name=仕入伝票` でショートカットを呼ぶ（名前は `SHORTCUT_NAME`）
- Apple の文字認識は**列ごと**に返す（品名の列 → 数量の列 → 単価の列 → 金額の列。10/1 の実物で確認）。1行ずつの形も扱う
- 文字の種類で振り分ける: 品名（かな・漢字で数字なし）／数量（1〜4桁の整数）／¥金額（¥0 は空行として捨てる）／数字を含むが読めない語（逆さ読みの「0CZ夫」等）は「欠けた金額」null／英字1〜3字は産地の記号（行を特定できないので入れない）／ラベル・住所・電話・宛名は捨てる
- 小計・税・合計は「a＋b＝c」になる3つ組を後ろから探す。上部の「合計金額」は明細から除く
- 単価と金額の並び（列ごと末尾合わせ／先頭合わせ／交互）は、数量×単価＝金額 が最も多く合うものを選ぶ
- **推測しない**: 読めない値は空欄。補うのは 数量・単価・金額 のうち1つだけ欠けた行（`completeItem`）で、その行は要確認
- 自動登録（`AUTO_SAVE_WHEN_CLEAN`）: ショートカット経由・操作者設定済み・要確認の印なし（年の補完は除く）・検算 ✓・二重登録の疑いなし、のときだけ
- 入力フォームでは、1つだけ空の欄に計算値を placeholder で見せ、保存時に埋める（値を直接入れると、その欄に打った数字が後ろにつながるため）

### 検算（アプリ側の `slipChecks`。読み取り側には計算させない）
各行 数量×単価＝金額／明細合計（`rowAmount` の和）＝小計／小計×`TAX_RATE`(0.08) の切り捨てか四捨五入＝税／小計＋税＝合計／種別・合計の有無。
確認画面・詳細・一覧（「検算ずれ」バッジ・フィルタ）で使う。ずれがあっても保存は confirm の上で可（伝票自体の誤りもあるため）。

### 月次集計（`monthSummary`）
**伝票日付の月**で、`supplierKey`（「有限会社」「㈲」「(有)」や空白を除いた取引先名）ごとに 納品計・返品計（税込）を積む。
伝票の税込額は `slipTotal`（合計欄、無ければ 小計＋税）、符号は `signedTotal`（納品＋／返品−）。取消（`status:'void'`）は除く。
種別か合計が無い伝票は支払額に入れず、月次に警告を出す。

### データモデル（イベント追記型・月ノード）
- `store.months[YYYY-MM] = { version, gen, events[] }`。YYYY-MM は**登録した月**（create イベントの `at`）。集計は伝票日付の月で行う点に注意
- イベント `{ uid, rid, type:'create'|'edit'|'status', at, by, data }`。status は `active ⇄ void`
- レコードは `buildRecords(months)` でイベント列を at 順に畳み込んで導出する。削除はしない
- 二重登録の疑い（有効・同じ種別・伝票日付・取引先キー・合計）は保存時に confirm
- localStorage キーは `shiire-` 接頭辞（`shiire-v1` / `shiire-operator-v1` / `shiire-sync-v1`）

### クラウド同期（Firebase Realtime Database / REST のみ）
予約台帳と同じ（`syncMonth` の GET→merge→if-match PUT、`mergeMonth` の gen 全取り／uid 和集合、60秒の meta ポーリング）。
パス `/shiire/<storeId>/m/<YYYY-MM>`。`storeId = SHA-256('shiire-store:' + パスコード)`。Firebase は無料の Spark プランでよい（Functions なし）。
**予約台帳とは別の Firebase プロジェクト（既定 `shiire-sync`）**。RTDB のルールはプロジェクトに1つで、デプロイで丸ごと上書きされるため。

## 技術方針（ユーザーと合意済みの制約）

- 単一の `index.html` に HTML / CSS / JS をすべてインラインで書く。ビルドツール・外部依存・CDN は使わない（Firebase も SDK を使わず REST + fetch のみ）
- 伝票の写真はアプリにもサーバにも送らない。文字認識は iPhone（ショートカット「画像からテキストを抽出」）で行う
- UI は日本語。金額は円の整数
- `samples/` には取引先の実際の単価が入るため git 管理外。公開リポジトリ・画面写真・Issue に載せない

## 実行方法

```bash
open index.html                      # ブラウザで開くだけ（同期・読み取りは定数未設定なら無効）
python3 tests/test_sync.py           # E2E（Playwright。モック RTDB。samples/ があれば本物の認識結果も照合）
swift tools/ocr-mac.swift samples/伝票.jpg > samples/ocr_伝票.txt   # Mac で文字認識（解析の調整用）
firebase deploy --only database      # ルールの反映（SETUP.md 参照。Firebase コンソールに貼ってもよい）
```
