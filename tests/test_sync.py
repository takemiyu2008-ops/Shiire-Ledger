# 仕入台帳の E2E テスト（Playwright + Firebase RTDB モック）
#
# 実行: python3 tests/test_sync.py
#
# - 回帰: 同期未設定時はローカル専用で動き、Firebase への通信が一切ないこと
# - 文字認識: iPhone のショートカットが開く「#ocr=<文字>」で、検算が合えば自動登録・合わなければ確認画面。
#   貼り付けでの入力、崩れた金額の手直し（2つ打てば残りを補う）、月次の差し引き支払額、二重登録の警告
# - 解析: samples/ に本物の文字認識結果があれば、正解（truth.json）と突き合わせる（git 管理外なので無ければ省略）
# - 同期: モック RTDB（月ノード GET/PUT/if-match/412/shallow/空オブジェクト脱落を再現）に対して
#   2端末（別ブラウザコンテキスト）でマージ・オフライン復帰・競合を検証

import http.server
import json
import re
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PORT = 8767
FAKE_DB = 'https://mock-rtdb.asia-southeast1.firebasedatabase.app'
PASSCODE = 'test-pass-1234567890'

passed = []
failed = []
dialogs = []   # confirm / prompt の文言（すべて自動承諾）


def check(name, cond, detail=''):
    if cond:
        passed.append(name)
        print(f'  ok: {name}')
    else:
        failed.append(name)
        print(f'  NG: {name} {detail}')


# ---------- モック RTDB（月ノード） ----------

def fb_clean(v):
    """Firebase RTDB は空オブジェクト・空配列・空文字・null を保存しない仕様を再現する"""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            c = fb_clean(x)
            if c is not None:
                out[k] = c
        return out or None
    if isinstance(v, list):
        out = [fb_clean(x) for x in v]
        out = [x for x in out if x is not None]
        return out or None
    if v == '':
        return None
    return v


class MockRTDB:
    def __init__(self):
        self.months = {}   # ym → {'node': {...}, 'etag': str}
        self.counter = 0
        self.offline = False
        self.force_412 = 0
        self.put_count = 0

    def cors(self, extra=None):
        h = {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'GET, PUT, OPTIONS',
            'Access-Control-Allow-Headers': '*',
            'Access-Control-Expose-Headers': 'ETag',
            'Content-Type': 'application/json',
        }
        if extra:
            h.update(extra)
        return h

    def events(self, ym):
        m = self.months.get(ym)
        if not m or not m['node']:
            return []
        return (m['node'].get('data') or {}).get('events') or []

    def handle(self, route, request):
        if request.method == 'OPTIONS':
            route.fulfill(status=204, headers=self.cors())
            return
        if self.offline:
            route.abort()
            return
        m = re.match(r'.*firebasedatabase\.app/(.*?)\.json(\?.*)?$', request.url)
        path, query = m.group(1), (m.group(2) or '')
        segs = path.split('/')  # ['yoyaku', '<id>', 'm', '<ym>', ...]
        if request.method == 'GET':
            if len(segs) == 3 and 'shallow=true' in query:  # 月の一覧
                val = {ym: True for ym in self.months if self.months[ym]['node']} or None
                route.fulfill(status=200, headers=self.cors(), body=json.dumps(val))
                return
            ym = segs[3]
            month = self.months.get(ym)
            val = month['node'] if month else None
            for seg in segs[4:]:
                val = val.get(seg) if isinstance(val, dict) else None
            headers = self.cors()
            if request.headers.get('x-firebase-etag'):
                headers['ETag'] = month['etag'] if month else 'null-etag'
            route.fulfill(status=200, headers=headers, body=json.dumps(val))
        elif request.method == 'PUT':
            if 'print=silent' in request.url and request.headers.get('if-match') is not None:
                route.fulfill(status=400, headers=self.cors(), body='{"error":"Mixing print=silent and if-match is not supported"}')
                return
            ym = segs[3]
            month = self.months.setdefault(ym, {'node': None, 'etag': 'null-etag'})
            if self.force_412 > 0:
                self.force_412 -= 1
                route.fulfill(status=412, headers=self.cors({'ETag': month['etag']}), body='{}')
                return
            im = request.headers.get('if-match')
            if im is not None and im != month['etag']:
                route.fulfill(status=412, headers=self.cors({'ETag': month['etag']}), body='{}')
                return
            month['node'] = fb_clean(json.loads(request.post_data))
            self.counter += 1
            month['etag'] = f'etag-{self.counter}'
            self.put_count += 1
            route.fulfill(status=200, headers=self.cors(), body='null')
        else:
            route.fulfill(status=405, headers=self.cors(), body='{}')


# ---------- 文字認識の結果（ダミーの取引先・単価） ----------
# iPhone の「画像からテキストを抽出」は列ごとに並ぶ（品名の列 → 数量 → 単価 → 金額）。本物の出力と同じ形にしてある

# 納品書: 検算がすべて合う → 自動登録
OCR_CLEAN = """納品書
セブンイレブン
テスト 店様
合計金額
¥3,132
産地
摘要
m
玉葱
胡瓜
枝豆
（税込）
残数
数量
納品日
2026年10月1日
有限会社 テスト青果
〒100-0001
東京都千代田区1-1-1
TEL ： 03-0000-0000
単価
金額
4
10
2
￥150
¥180
¥250
¥600
¥1,800
¥500
¥0
¥0
小
消費税
合計
¥0
¥2,900
¥232
¥3,132"""

# 返品伝票: 年なし・金額の一部が逆さ読みで崩れている（茄子 1×230、値引き 1×1000）
OCR_GARBLED = """10月1日
セブンイレブン テスト店様
品名
返品伝票
（有）テスト青果
東京都千代田区1-1-1
TEL: 03-0000-0000
数量
単価
¥150
0CZ夫
ピーマン
茄子
値引き
3
1
1
000°レ
小計
消費税
合計
金額
¥450
0CZ夫
000'L
0夫
¥0
¥1,680
¥134
¥1,814"""

# iPhone のショートカットの実際の出方（2026-10-22 実機）をまねたもの:
# 1行ずつ並ぶ・¥ が「·」になる・1桁の数量は読めない（10 だけ読める）・伝票下の小計／税／合計は読めない・¥0 の列は「味味味」
OCR_IPHONE = """納品書
セブンイレブン
テスト店様
納品日
2026年10月3日
有限会社 テスト青果
TEL:
03-0000-0000

合計金額
·3,132
（税込）

産地
摘要
残数
数量
単価
金額

玉葱


¥150
·600

胡瓜

10
·180
¥1,800
m
枝豆


·250
·500




味味味味味味"""

# 1行ずつ並ぶ形（テキスト認識表示からの貼り付けなど）
OCR_ROWS = """納品書
有限会社 テスト青果
納品日 2026年10月2日
玉葱 4 ¥150 ¥600
胡瓜 10 ¥180 ¥1,800
小計 ¥2,400
消費税 ¥192
合計 ¥2,592"""


# ---------- テスト補助 ----------

def wait_until(page, fn, timeout=8.0, interval=0.15):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        page.wait_for_timeout(interval * 1000)
    return False


def on_dialog(d):
    dialogs.append(d.message)
    d.accept()


def open_app(browser, url, db=None, operator='篠原'):
    ctx = browser.new_context()
    if operator is not None:
        ctx.add_init_script(f"localStorage.setItem('shiire-operator-v1', '{operator}')")
    if db is not None:
        ctx.route('**/*.firebasedatabase.app/**', db.handle)
    page = ctx.new_page()
    page.on('dialog', on_dialog)
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(url)
    return ctx, page, errors


def connect(page):
    page.click('#syncChip')
    page.fill('#syncPasscode', PASSCODE)
    page.click('#syncConnect')
    page.wait_for_selector('.sync-chip.ok', timeout=8000)


ROW = '#itemsBody .item-row >> nth={} >> input[data-k="{}"]'


def manual_entry(page, supplier, kind='納品', date=None, item=('玉葱', 4, 150, 600), totals=(600, 48, 648)):
    page.click('nav.tabs button[data-tab="capture"]')
    page.click('#btnManual')
    page.select_option('#f-kind', kind)
    page.fill('#f-slipDate', date or today_iso())
    page.fill('#f-supplier', supplier)
    for k, v in zip(['name', 'qty', 'unitPrice', 'amount'], item):
        page.fill(ROW.format(0, k), str(v))
    for k, v in zip(['subtotal', 'tax', 'total'], totals):
        page.fill(f'#f-{k}', str(v))
    page.click('#btnSave')
    page.wait_for_selector('#detailOverlay.show', timeout=5000)
    page.click('#dClose')


def ocr_hash(page, text):
    """ショートカットが開く URL と同じ形でページへ文字を渡す"""
    page.evaluate("t => { location.hash = 'ocr=' + encodeURIComponent(t); }", text)


def get_store(page):
    return page.evaluate("JSON.parse(localStorage.getItem('shiire-v1'))")


def all_events(page):
    s = get_store(page) or {'months': {}}
    return [ev for m in s['months'].values() for ev in m.get('events', [])]


def records(page):
    return page.evaluate('Object.values(records).sort((a,b)=>a.createdAt<b.createdAt?-1:1).map(r => ({rid:r.rid, status:r.status, fields:r.fields, items:r.items, ym:r.ym, history:r.history.length}))')


def header_count(page):
    return page.text_content('#headerCount').strip()


def header_pay(page):
    return page.text_content('#headerPay').strip()


def trigger_sync(page):
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")


def today_iso():
    return time.strftime('%Y-%m-%d')


# ---------- テスト本体 ----------

def test_local_regression(browser, base_url):
    print('▼ 回帰: 同期未設定（素の index.html）はローカル専用で動く')
    ctx, page, errors = open_app(browser, base_url + '/index.html')
    firebase_requests = []
    page.on('request', lambda r: firebase_requests.append(r.url) if 'firebasedatabase.app' in r.url else None)

    check('チップが「同期未設定」', page.text_content('#syncChip') == '同期未設定')
    check('撮影タブがある', page.locator('nav.tabs button[data-tab="capture"]').text_content() == '撮影')
    check('撮影ボタンはショートカット「仕入伝票」を呼ぶ',
          page.get_attribute('#btnShoot', 'href') == 'shortcuts://run-shortcut?name=%E4%BB%95%E5%85%A5%E4%BC%9D%E7%A5%A8', page.get_attribute('#btnShoot', 'href'))
    check('起動時は一覧タブが空', 'まだ伝票がありません' in page.text_content('#recordList'))

    manual_entry(page, 'テスト青果')
    check('手入力の伝票が一覧に出る', 'テスト青果' in page.text_content('#recordList'))
    check('ヘッダーの今月の伝票が1', header_count(page) == '1')
    check('ヘッダーの今月の支払が648', header_pay(page) == '648', header_pay(page))
    recs = records(page)
    check('明細行が保存されている', recs[0]['items'][0]['name'] == '玉葱' and recs[0]['items'][0]['amount'] == 600, str(recs[0]['items']))
    check('登録イベントに by が付く', all(ev.get('by') == '篠原' for ev in all_events(page)))

    manual_entry(page, 'テスト青果', kind='返品', item=('ピーマン', 1, 150, 150), totals=(150, 12, 162))
    check('返品で支払が差し引かれる（648−162＝486）', header_pay(page) == '486', header_pay(page))
    check('返品は一覧でマイナス表示', '−162円' in page.text_content('#recordList'))
    page.click('#filterChips button:has-text("返品")')
    check('返品フィルタ', 'ピーマン' in page.text_content('#recordList') and '玉葱' not in page.text_content('#recordList'))
    page.click('#filterChips button:has-text("全部")')

    # 詳細 → 修正 → 取消 → 戻す
    page.click('#recordList .rec-item:has-text("玉葱")')
    page.wait_for_selector('#detailOverlay.show')
    check('詳細に検算 ✓', '検算 ✓' in page.text_content('#detailModal'))
    page.click('#dEdit')
    page.fill('#f-memo', '午前便')
    page.click('#detailSave')
    check('修正が反映', wait_until(page, lambda: records(page)[0]['fields']['memo'] == '午前便'))
    page.click('#dCancel')
    check('取消になる', wait_until(page, lambda: records(page)[0]['status'] == 'void'))
    check('取消は支払から外れる（−162）', header_pay(page) == '-162', header_pay(page))
    page.click('#dReopen')
    check('有効に戻る', wait_until(page, lambda: records(page)[0]['status'] == 'active'))
    check('履歴が4件（登録・修正・取消・戻す）', records(page)[0]['history'] == 4, str(records(page)[0]['history']))
    page.click('#dClose')

    # 検索（品名）
    page.fill('#searchBox', 'ピーマン')
    check('品名で検索', 'ピーマン' in page.text_content('#recordList') and '玉葱' not in page.text_content('#recordList'))
    page.fill('#searchBox', '')

    # 月次・CSV / バックアップ
    page.click('nav.tabs button[data-tab="monthly"]')
    check('月次に差し引き 486円', '486円' in page.text_content('#sumBody'), page.text_content('#sumBody')[:200])
    with page.expect_download() as dl:
        page.click('#csvSlips')
    check('伝票CSV が書き出される', dl.value.suggested_filename.startswith('仕入台帳_伝票_'))
    csv_text = Path(dl.value.path()).read_text(encoding='utf-8-sig')
    check('伝票CSV の返品はマイナス', ',-162,' in csv_text, csv_text[:300])
    with page.expect_download() as dl3:
        page.click('#csvItems')
    check('明細CSV が書き出される', dl3.value.suggested_filename.startswith('仕入台帳_明細_'))
    page.click('nav.tabs button[data-tab="settings"]')
    with page.expect_download() as dl2:
        page.click('#jsonExport')
    check('バックアップが書き出される', dl2.value.suggested_filename.startswith('仕入台帳バックアップ_'))

    page.reload()
    check('リロード後も残る', len(records(page)) == 2)
    check('Firebase への通信ゼロ', len(firebase_requests) == 0, str(firebase_requests[:3]))
    check('ページエラーなし（回帰）', not errors, str(errors[:2]))
    ctx.close()


def test_operator_gate(browser, base_url):
    print('▼ 操作者: 未設定なら選択モーダルが出て、選ぶまで記録できない')
    ctx, page, errors = open_app(browser, base_url + '/index.html', operator=None)
    check('起動時に操作者モーダルが表示', 'show' in page.get_attribute('#operatorOverlay', 'class'))
    check('4名のボタンが並ぶ', page.locator('#operatorBtns button').count() == 4)
    page.click('#operatorClose')
    page.click('nav.tabs button[data-tab="capture"]')
    page.click('#btnManual')
    page.fill('#f-supplier', 'テスト青果')
    page.click('#btnSave')
    check('未設定では記録されない', get_store(page) is None)
    check('記録しようとするとモーダルが開く', 'show' in page.get_attribute('#operatorOverlay', 'class'))
    page.click('#operatorBtns button:has-text("小宮山")')
    check('チップに名前が表示', page.text_content('#operatorChip') == '👤 小宮山')
    page.click('#btnSave')
    page.wait_for_selector('#detailOverlay.show')
    check('設定後は記録できる', len(records(page)) == 1)
    check('イベントに by が付く', all_events(page)[0].get('by') == '小宮山')
    check('ページエラーなし（操作者）', not errors, str(errors[:2]))
    ctx.close()


def test_ocr_flow(browser, base_url):
    print('▼ 文字認識: ショートカット（#ocr=）→ 自動登録／二重登録の警告／崩れた返品伝票の手直し → 月次')
    url = base_url + '/synced.html'
    db = MockRTDB()
    ctx, page, errors = open_app(browser, url, db)
    connect(page)

    ocr_hash(page, OCR_CLEAN)
    check('検算 ✓ の納品書は自動で登録される', wait_until(page, lambda: len(records(page)) == 1), str(records(page)))
    recs = records(page)
    f = recs[0]['fields']
    check('項目が入る（納品・2026-10-01・取引先・合計3132）',
          f['kind'] == '納品' and f['slipDate'] == '2026-10-01' and f['supplier'] == '有限会社 テスト青果' and f['total'] == 3132, str(f))
    check('明細3行（胡瓜 10×180＝1800）', len(recs[0]['items']) == 3 and recs[0]['items'][1]['amount'] == 1800, str(recs[0]['items']))
    check('URL から文字が消える（閲覧履歴に単価を残さない）', page.evaluate('location.hash') == '')
    check('登録イベントに model=iphone-ocr', (all_events(page)[0]['data'].get('read') or {}).get('model') == 'iphone-ocr')
    check('クラウドに PUT される', wait_until(page, lambda: len(db.events(time.strftime('%Y-%m'))) == 1))
    page.wait_for_selector('#detailOverlay.show', timeout=5000)
    page.click('#dClose')

    # 同じ伝票がもう一度届く → 自動登録しない
    ocr_hash(page, OCR_CLEAN)
    page.wait_for_selector('#formCard:not([hidden])', timeout=5000)
    check('二重登録の疑いがあれば自動登録しない', len(records(page)) == 1)
    n_dialogs = len(dialogs)
    page.click('#btnSave')
    page.wait_for_selector('#detailOverlay.show', timeout=5000)
    check('保存しようとすると二重登録の警告', any('二重登録' in m for m in dialogs[n_dialogs:]), str(dialogs[n_dialogs:]))
    page.click('#dCancel')   # 二重登録は取消にしておく
    page.click('#dClose')

    # 崩れた返品伝票を貼り付け
    page.click('nav.tabs button[data-tab="capture"]')
    page.fill('#ocrText', OCR_GARBLED)
    page.click('#btnParse')
    page.wait_for_selector('#formCard:not([hidden])', timeout=5000)
    check('貼り付けでも入力欄ができる（返品）', page.input_value('#f-kind') == '返品')
    check('年なしの日付は年を補完して要確認', '年を補完' in page.text_content('#f-slipDate >> xpath=..'))
    check('取引先 (有)テスト青果', page.input_value('#f-supplier') == '(有)テスト青果', page.input_value('#f-supplier'))
    check('1行目（読めた行）は無印', 'flag' not in (page.get_attribute('#itemsBody .item-row >> nth=0', 'class') or ''))
    check('2行目（金額が崩れた行）は要確認', 'flag' in (page.get_attribute('#itemsBody .item-row >> nth=1', 'class') or ''))
    check('崩れた金額は空欄（推測しない）', page.input_value(ROW.format(1, 'unitPrice')) == '' and page.input_value(ROW.format(1, 'amount')) == '')
    check('産地欄の記号が無いので印も無い', page.input_value(ROW.format(0, 'origin')) == '')
    check('小計・税・合計は読めている', page.input_value('#f-subtotal') == '1680' and page.input_value('#f-total') == '1814')
    page.fill(ROW.format(1, 'unitPrice'), '230')
    check('単価を打つと金額欄に 数量×単価 の見本が出る', page.get_attribute(ROW.format(1, 'amount'), 'placeholder') == '230' and page.input_value(ROW.format(1, 'amount')) == '')
    page.fill(ROW.format(2, 'amount'), '1000')
    check('金額を打つと単価欄に 金額÷数量 の見本が出る', page.get_attribute(ROW.format(2, 'unitPrice'), 'placeholder') == '1000')
    check('直すと検算 ✓', wait_until(page, lambda: '検算 ✓' in page.text_content('#formBody .checkbox')), page.text_content('#formBody .checkbox'))
    page.fill('#f-slipDate', '2026-10-01')
    page.click('#btnSave')
    page.wait_for_selector('#detailOverlay.show', timeout=5000)
    hen = [r for r in records(page) if r['fields']['kind'] == '返品'][0]
    check('保存時に見本の値で埋まる（茄子 230・値引き 1000）',
          [(i['unitPrice'], i['amount']) for i in hen['items']] == [(150, 450), (230, 230), (1000, 1000)], str(hen['items']))
    page.click('#dClose')

    page.click('nav.tabs button[data-tab="monthly"]')
    page.select_option('#sumMonth', '2026-10')
    sb = page.text_content('#sumBody')
    check('月次: 3132−1814＝1,318円（取消した二重登録は除く）', '1,318円' in sb and '納品 1枚' in sb and '返品 1枚' in sb, sb[:200])
    check('月次: 「有限会社 テスト青果」と「(有)テスト青果」が同じ取引先', page.locator('#sumBody .sum-table tbody tr').count() == 1)
    page.evaluate('window.__printed = 0; window.print = () => { window.__printed++; }; 0')
    page.click('#printMonthly')
    pa = page.text_content('#printArea')
    check('印刷: 印刷ダイアログが呼ばれる', page.evaluate('window.__printed') == 1)
    check('印刷: 見出し・差し引き 1,318円・伝票2枚', '月次のまとめ　2026年10月' in pa and '1,318円' in pa and page.locator('#printArea tbody tr').count() >= 4, pa[:200])
    check('印刷: 取消した伝票は除き、その旨を書く', '取消した伝票 1枚は含めていません' in pa)
    check('印刷: 画面には出ない', not page.locator('#printArea').is_visible())
    page.emulate_media(media='print')
    check('印刷: 印刷時は印刷用だけが見える', page.locator('#printArea').is_visible() and not page.locator('main').is_visible())
    page.emulate_media(media='screen')

    # 1行ずつ並ぶ形
    r = page.evaluate('t => parseOcrText(t)', OCR_ROWS)
    r2 = page.evaluate('t => parseOcrText(t)', '納品書\n玉葱\n人参\n4\n3\n味150\n味150\n味600\n味450\n味味味味\n味\n小計\n¥1,050\n¥84\n¥1,134')
    check('「味」と読み違えた ¥ を直し、「味味味」は空行として捨てる',
          [(i['name'], i['qty'], i['unitPrice'], i['amount']) for i in r2['items']] == [('玉葱', 4, 150, 600), ('人参', 3, 150, 450)] and r2['total'] == 1134, str(r2))
    r3 = page.evaluate('t => parseOcrText(t)', OCR_IPHONE)
    check('iPhone の出方: 「·」を ¥ に・数量は 金額÷単価・小計と税は合計金額と一致を確かめて入れる',
          [(i['name'], i['qty'], i['unitPrice'], i['amount']) for i in r3['items']] == [('玉葱', 4, 150, 600), ('胡瓜', 10, 180, 1800), ('枝豆', 2, 250, 500)]
          and (r3['subtotal'], r3['tax'], r3['total']) == (2900, 232, 3132), str(r3))
    r4 = page.evaluate('t => parseOcrText(t)', OCR_IPHONE.replace('·3,132', '·3,200'))
    check('合計金額と合わなければ小計・税は空欄のまま', r4['subtotal'] is None and r4['tax'] is None and r4['total'] == 3200, str(r4))
    check('1行ずつの形も解析できる', [(i['name'], i['qty'], i['unitPrice'], i['amount']) for i in r['items']] == [('玉葱', 4, 150, 600), ('胡瓜', 10, 180, 1800)]
          and (r['subtotal'], r['tax'], r['total']) == (2400, 192, 2592) and r['slipDate'] == '2026年10月2日', str(r))
    check('ページエラーなし（文字認識）', not errors, str(errors[:2]))
    ctx.close()

    print('▼ 文字認識: 操作者が未設定なら自動登録せず、名前を選ばせる')
    ctx, page, errors = open_app(browser, base_url + '/index.html', operator=None)
    page.click('#operatorClose')
    ocr_hash(page, OCR_CLEAN)
    page.wait_for_selector('#formCard:not([hidden])', timeout=5000)
    check('操作者未設定では自動登録しない', get_store(page) is None)
    check('操作者モーダルが開く', 'show' in page.get_attribute('#operatorOverlay', 'class'))
    check('ページエラーなし（操作者未設定）', not errors, str(errors[:2]))
    ctx.close()


def test_parser_real_samples(browser, base_url):
    samples = ROOT / 'samples'
    pairs = [('ocr_nouhin_2026-10-01.txt', 'nouhin_2026-10-01.jpg'), ('ocr_henpin_2026-10-01.txt', 'henpin_2026-10-01.jpg'),
             ('ocr_iphone_nouhin_2026-10-01.txt', 'nouhin_2026-10-01.jpg')]  # 最後は iPhone のショートカットの実際の出力
    if not (samples / 'truth.json').exists() or not all((samples / a).exists() for a, _ in pairs):
        print('▼ 本物の伝票での解析: samples/ が無いので省略')
        return
    print('▼ 本物の伝票での解析（Mac の文字認識の出力 → parseOcrText → 正解と照合）')
    truth = json.loads((samples / 'truth.json').read_text(encoding='utf-8'))['slips']
    ctx, page, errors = open_app(browser, base_url + '/index.html')
    for ocr, jpg in pairs:
        t = truth[jpg]
        r = page.evaluate('t => parseOcrText(t)', (samples / ocr).read_text(encoding='utf-8'))
        rows = [(i['name'], i['qty'], i['unitPrice'], i['amount']) for i in r['items']]
        want = [(i['name'], i['qty'], i['unitPrice'], i['amount']) for i in t['items']]
        exact = sum(1 for a, b in zip(rows, want) if a == b)
        print(f'    {ocr}: 行 {exact}/{len(want)} が完全一致・{r["notes"]}')
        check(f'{ocr}: 小計・税・合計', (r['subtotal'], r['tax'], r['total']) == (t['subtotal'], t['tax'], t['total']))
        check(f'{ocr}: 品名の数', len(rows) == len(want))
        if jpg.startswith('nouhin'):
            check(f'{ocr}: 全行が正解と一致', exact == len(want))
        # 誤った値を入れていないこと（読めない値は空欄のまま）
        wrong = [(a, b) for a, b in zip(rows, want) if any(x is not None and x != y for x, y in zip(a[1:], b[1:]))]
        check(f'{ocr}: 数字を誤って入れた行が無い', not wrong, str(wrong[:3]))
    ctx.close()


def test_sync_scenarios(browser, base_url):
    url = base_url + '/synced.html'
    db = MockRTDB()
    ym = time.strftime('%Y-%m')

    print('▼ A: 端末1の記録が端末2に伝わる')
    ctx1, page1, err1 = open_app(browser, url, db)
    connect(page1)
    manual_entry(page1, 'テスト青果 A')
    check('A: PUT が月ノードに到達', wait_until(page1, lambda: len(db.events(ym)) == 1))
    check('A: ノードは meta/data の2層', db.months[ym]['node'].get('meta', {}).get('rev') and 'events' in db.months[ym]['node']['data'])
    check('A: パスは /shiire/', page1.evaluate("monthUrl('2026-10').includes('/shiire/')"))

    ctx2, page2, err2 = open_app(browser, url, db, operator='橋本')
    connect(page2)
    check('A: 端末2が接続時に受信', wait_until(page2, lambda: header_count(page2) == '1'))
    check('A: 入力者名が端末2へ伝わる', all(ev.get('by') == '篠原' for ev in all_events(page2)))

    page1.click('#recordList .rec-item >> nth=0')
    page1.click('#dCancel')
    page1.click('#dClose')
    check('A: 端末1の取消が PUT される', wait_until(page1, lambda: len(db.events(ym)) == 2))
    trigger_sync(page2)
    check('A: 端末2が visibilitychange で受信', wait_until(page2, lambda: records(page2)[0]['status'] == 'void'))

    print('▼ B: 両端末オフラインで同じ伝票の別項目を修正 → 復帰後に両方生きる')
    db.offline = True
    for pg, key, val in [(page1, '#f-supplier', 'テスト青果 B'), (page2, '#f-memo', '午後便')]:
        pg.click('#filterChips button:has-text("取消")')
        pg.click('#recordList .rec-item >> nth=0')
        pg.click('#dEdit')
        pg.fill(key, val)
        pg.click('#detailSave')
        pg.click('#dClose')
    page1.wait_for_selector('.sync-chip.error, .sync-chip.offline', timeout=8000)
    db.offline = False

    def both_converged():
        trigger_sync(page1)
        trigger_sync(page2)
        r1, r2 = records(page1), records(page2)
        return (r1 and r2 and r1[0]['fields']['supplier'] == 'テスト青果 B' and r1[0]['fields']['memo'] == '午後便'
                and r2[0]['fields']['supplier'] == 'テスト青果 B' and r2[0]['fields']['memo'] == '午後便')
    check('B: 両端末で両方の修正が生きる', wait_until(page2, both_converged, interval=0.5),
          f"p1={records(page1)[0]['fields']} p2={records(page2)[0]['fields']}")
    check('B: イベント4件・uid 集合が一致',
          len(all_events(page1)) == 4 and {e['uid'] for e in all_events(page1)} == {e['uid'] for e in all_events(page2)})

    print('▼ C: PUT の 412 競合 → 再取得マージで成功する')
    before = db.put_count
    db.force_412 = 1
    manual_entry(page1, 'テスト青果 C')
    check('C: 412 後にリトライ PUT が成功', wait_until(page1, lambda: db.put_count > before and len(db.events(ym)) == 5))
    check('C: チップが同期済みに戻る', wait_until(page1, lambda: 'sync-chip ok' in page1.get_attribute('#syncChip', 'class')))

    print('▼ D: 空値の脱落（RTDB 仕様）込みで往復しても壊れない')
    trigger_sync(page2)
    check('D: 端末2が受信してもクラッシュしない', wait_until(page2, lambda: len(all_events(page2)) == 5))
    page2.click('#filterChips button:has-text("全部")')
    check('D: 端末2の一覧（有効）に1件', page2.locator('#recordList .rec-item').count() == 1)

    print('▼ E: 同月バックアップ復元（gen 更新）→ 他端末が追従する')
    gen_before = (db.months[ym]['node']['data'].get('gen') or 0)
    page1.evaluate("""() => {
      const ym = Object.keys(store.months)[0];
      const m = store.months[ym];
      m.events = m.events.slice(0, 1);  // 1件目だけの復元を模擬
      m.gen = Date.now();
      save(); renderAll();
    }""")
    check('E: クラウドが新世代になる', wait_until(page1, lambda: (db.months[ym]['node']['data'].get('gen') or 0) > gen_before and len(db.events(ym)) == 1))
    trigger_sync(page2)
    check('E: 端末2も追従（1件）', wait_until(page2, lambda: len(all_events(page2)) == 1))

    print('▼ F: 両端末に既存データがある状態で接続 → 和集合で統合')
    dbf = MockRTDB()
    ctxa, pagea, erra = open_app(browser, url, dbf)
    manual_entry(pagea, '接続前 A')
    connect(pagea)
    check('F: 端末Aの初期アップロード', wait_until(pagea, lambda: len(dbf.events(ym)) == 1))
    ctxb, pageb, errb = open_app(browser, url, dbf, operator='橋本')
    manual_entry(pageb, '接続前 B', kind='返品', item=('人参', 1, 150, 150), totals=(150, 12, 162))
    connect(pageb)
    check('F: 端末Bで統合（2枚）', wait_until(pageb, lambda: header_count(pageb) == '2'), header_count(pageb))
    check('F: 端末Bの支払 648−162＝486', header_pay(pageb) == '486', header_pay(pageb))

    def a_has_two():
        trigger_sync(pagea)
        return header_count(pagea) == '2'
    check('F: 端末Aにも伝播', wait_until(pagea, a_has_two, interval=0.4), header_count(pagea))

    print('▼ G: 古い月の読み込み（shallow 一覧）')
    old_ym = '2024-01'
    dbf.months[old_ym] = {'node': {'meta': {'rev': 'x', 'at': 1}, 'data': {'version': 1, 'gen': 0, 'events': [
        {'uid': 'old-1', 'rid': 'old-r', 'type': 'create', 'at': '2024-01-05T01:00:00.000Z', 'by': '篠原',
         'data': {'fields': {'kind': '納品', 'slipDate': '2024-01-05', 'supplier': '去年の八百屋', 'total': 1000}, 'items': [{'name': '白菜', 'qty': 1}]}}]}}, 'etag': 'etag-old'}
    pagea.click('nav.tabs button[data-tab="settings"]')
    pagea.click('#loadMonthBtn')
    pagea.wait_for_selector('#oldMonthBox:not([hidden])', timeout=5000)
    check('G: 候補に古い月だけが出る', [o.get_attribute('value') for o in pagea.locator('#oldMonthSelect option').all()] == [old_ym])
    pagea.select_option('#oldMonthSelect', old_ym)
    pagea.click('#oldMonthLoad')
    check('G: 古い月が読み込まれる', wait_until(pagea, lambda: any(r['fields']['supplier'] == '去年の八百屋' for r in records(pagea))))
    check('G: 今月の記録も残っている', any(r['fields']['supplier'] == '接続前 A' for r in records(pagea)))
    check('G: 月次の月一覧に 2024年01月 が出る', '2024-01' in [o.get_attribute('value') for o in pagea.locator('#sumMonth option').all()])

    for name, errs in [('端末1', err1), ('端末2', err2), ('端末A', erra), ('端末B', errb)]:
        check(f'ページエラーなし（{name}）', not errs, str(errs[:2]))
    ctx1.close(); ctx2.close(); ctxa.close(); ctxb.close()


def main():
    srv_dir = tempfile.mkdtemp(prefix='shiire-test-')
    src = (ROOT / 'index.html').read_text(encoding='utf-8')
    pat_db = re.compile(r"const SYNC_DB_URL = '[^']*';")
    assert pat_db.search(src), 'SYNC_DB_URL 定数が見つかりません'
    local = pat_db.sub("const SYNC_DB_URL = '';", src)
    synced = pat_db.sub(f"const SYNC_DB_URL = '{FAKE_DB}';", src)
    (Path(srv_dir) / 'index.html').write_text(local, encoding='utf-8')
    (Path(srv_dir) / 'synced.html').write_text(synced, encoding='utf-8')

    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=srv_dir, **kw)
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', PORT), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f'http://127.0.0.1:{PORT}'

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            test_local_regression(browser, base_url)
            test_operator_gate(browser, base_url)
            test_ocr_flow(browser, base_url)
            test_parser_real_samples(browser, base_url)
            test_sync_scenarios(browser, base_url)
            browser.close()
    finally:
        httpd.shutdown()
        shutil.rmtree(srv_dir, ignore_errors=True)

    print(f'\n結果: {len(passed)} passed / {len(failed)} failed')
    if failed:
        print('失敗:', *failed, sep='\n  - ')
        sys.exit(1)


if __name__ == '__main__':
    main()
