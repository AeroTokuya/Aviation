# -*- coding: utf-8 -*-
"""見積書（ＺＥＲＯの家 外構工事）をもとに、三鷹商工会 中小企業等産業活性化補助金の
申請書（様式第１号の１・様式第２号の１）へ内容を記入する生成スクリプト。"""
import os
from docx import Document
from docx.oxml.ns import qn

SRC = "/root/.claude/uploads/f35ad963-9a8d-58b5-9ea2-35e768a3f491"
OUT = "/home/user/Aviation/申請書"
Y11 = os.path.join(SRC, "cbf9ceb1-youshiki11.docx")   # 様式第１号の１ 交付申請書
Y21 = os.path.join(SRC, "ce2f02bf-youshiki21.docx")   # 様式第２号の１ 事業計画書

EA_FONT = "ＭＳ 明朝"


def set_ea(run, size=None):
    run.font.name = EA_FONT
    rPr = run._element.get_or_add_rPr()
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = rPr.makeelement(qn("w:rFonts"), {})
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:eastAsia"), EA_FONT)
    if size is not None:
        run.font.size = size


def check_box(paragraph):
    """段落先頭の □ を ☑ に置き換える。"""
    for run in paragraph.runs:
        if "□" in run.text:
            run.text = run.text.replace("□", "☑", 1)
            return True
    return False


def add_para(cell, text, size=None):
    p = cell.add_paragraph()
    r = p.add_run(text)
    set_ea(r, size)
    return p


# ============================================================
# 様式第２号の１ 事業計画書
# ============================================================
d = Document(Y21)

# 業種区分：サービス業 に☑
cell = d.tables[2].rows[0].cells[1]
for p in cell.paragraphs:
    if p.text.strip() == "□　サービス業":
        check_box(p)

# 業種：Ｋ 宿泊業・飲食サービス業 に ○
c = d.tables[3].rows[0].cells[2]
for p in c.paragraphs:
    for run in p.runs:
        if run.text.strip() == "Ｋ":
            run.text = "○Ｋ"

# 事業完了予定日（令和９年１月末日を目標）
row3 = d.tables[4].rows[3]
comp_cell = row3.cells[1]
for p in comp_cell.paragraphs:
    for run in p.runs:
        run.text = ""
comp_cell.paragraphs[0].add_run("交付決定日～令和　９　年　　１　月　末日")
for run in comp_cell.paragraphs[0].runs:
    set_ea(run)

# 経営計画の記述（各セルの見出し下に追記）
tbl = d.tables[4]

gaiyou = [
    "当施設は、三鷹市内で宿泊施設（住宅宿泊事業・民泊）を運営しています。宿泊者の多くは"
    "公共交通機関を利用して来訪しますが、最寄駅から施設まで、また周辺の観光・生活拠点への"
    "二次交通（ラストワンマイル）の移動手段が乏しく、宿泊者の回遊性・利便性の面で課題を"
    "抱えています。",
    "加えて、道路に面した敷地外周には老朽化した既存のブロック塀（ＣＢフェンス）・門扉・"
    "引戸が残存しており、景観面・防災面・安全面でも改善が求められています。宿泊者へ提供"
    "できる移動体験（モビリティ）が限られていることが、施設としての付加価値向上の妨げと"
    "なっています。",
]
for t in gaiyou:
    add_para(tbl.rows[0].cells[0], t)

torikumi = [
    "道路に面した既存のブロック塀（ＣＢフェンス）、門扉、引戸および既存土間を撤去し、"
    "抜根・整地のうえ土間コンクリートを打設する外構工事を行います。整備したスペースに、"
    "シェアモビリティ「ＬＵＵＰ（ループ）」のポート（電動キックボード・電動アシスト自転車の"
    "シェアリングステーション）を設置し、宿泊者および近隣住民が利用できる移動拠点を"
    "整備します。",
    "本申請の補助対象経費は、株式会社清水工務店による「ＺＥＲＯの家 外構工事 内訳明細書」"
    "に基づく外構整備工事一式（税込 540,000円）です。",
    "〔主な工事内容〕既存ＣＢフェンス・門扉・引戸撤去、既存土間撤去、抜根、"
    "土間コンクリート打設、発生材処分費、搬入・搬出等雑費、諸経費　ほか一式",
]
for t in torikumi:
    add_para(tbl.rows[1].cells[0], t)

kouka = [
    "・宿泊者に対し、駅から施設・周辺観光地までのラストワンマイルを担う新たな移動手段を"
    "提供し、宿泊体験の質と施設の付加価値を向上させます。",
    "・ＬＵＵＰポートは宿泊者に限らず近隣ユーザーも利用可能であり、地域全体に新たな"
    "モビリティのＵＸ（移動体験）を提供します。",
    "・老朽化したブロック塀の撤去により、景観・防災・安全性が向上します。",
    "・〔数値目標（例）〕ポート設置後１年間で、宿泊者の当該モビリティ利用率　○○％／"
    "月間利用回数　○○回／これに伴う稼働率・売上　前年比　○○％増　を目指します。",
]
for t in kouka:
    add_para(tbl.rows[2].cells[0], t)

out21 = os.path.join(OUT, "様式第２号の１_事業計画書.docx")
d.save(out21)
print("saved:", out21)

# ============================================================
# 様式第１号の１ 交付申請書
# ============================================================
d = Document(Y11)

# チェックする誓約・同意事項（全申請者共通のもの）
common_ids = [
    "本申請書及び添付書類の記載内容に偽りはありません",
    "申請日時点で市内に事業所を有し",
    "営業に関して必要な許認可等を取得しています",
    "本事業を営むに当たり、関連する法令",
    "性風俗関連特殊営業を行う者に該当しません",
    "暴力団、暴力団員、暴力団関係者）に該当しません",
    "同一の対象経費について重複して補助金の交付を受けておらず",
    "必要な報告をし、又は現地調査等に応じます",
    "偽りその他不正の手段により補助金の交付を受けたとき",
    "少なくとも１年以上継続して事業を営む意思があります",
    "正当な理由なく廃止、譲渡、処分等は行いません",
    "帳簿及び領収書等の証拠書類を保存し",
]
# チェックする添付書類（既存事業者としての標準的な提出書類）
attach_ids = [
    "① 三鷹商工会中小企業等産業活性化補助金事業計画書",
    "② 申請枠・対象経費チェックシート",
    "③ 補助対象経費の見積書等の写し",
    "④ 補助対象経費の内容が分かる書類",
    "⑤-１ 確定申告書",
    "⑥ 三鷹市内に事業所があることが分かる書類",
    "⑦ 営業に関する許認可証の写し",
]
targets = common_ids + attach_ids
for p in d.paragraphs:
    for key in targets:
        if key in p.text and "□" in p.text:
            check_box(p)
            break

out11 = os.path.join(OUT, "様式第１号の１_交付申請書.docx")
d.save(out11)
print("saved:", out11)
