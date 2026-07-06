#!/usr/bin/env python3
"""webapp/data/heliports.geojson の病院ヘリパッド・ヘリポートを分類する。

分類 (properties.cat):
  drheli : ドクターヘリ基地病院 — 全国の基地病院リスト(名称トークン+座標 20km 照合)
  er3    : 三次救急相当(推定)   — 大学病院・救命救急センター等の名称による推定
  er2    : 二次救急相当(推定)   — その他の病院ヘリパッド
  public : 公共用ヘリポート     — 東京・群馬・栃木・静岡・津市伊勢湾・若狭・大阪 等
  heliport: その他ヘリポート

⚠ 基地病院の指定状況・機数は年度により変動する。分類は概略であり、
  運航前に厚労省/各県の最新情報で確認すること。
"""
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, '..', 'webapp', 'data', 'heliports.geojson')

# ドクターヘリ基地病院 (2024-25 年頃の運航体制を基にした概略リスト)
# (日本語名, 照合トークン(英語名の部分一致), 概略 lat, lon)
DRHELI_BASES = [
    ('手稲渓仁会病院', ['Teine Keijinkai'], 43.122, 141.246),
    ('旭川赤十字病院', ['Asahikawa Red Cross', 'Red Cross Asahikawa'], 43.760, 142.357),
    ('市立函館病院', ['Hakodate Municipal', 'Hakodate City Hospital'], 41.819, 140.700),
    ('釧路孝仁会記念病院', ['Kojinkai'], 42.990, 144.397),
    ('八戸市立市民病院', ['Hachinohe City', 'Hachinohe Municipal'], 40.487, 141.500),
    ('青森県立中央病院', ['Aomori Prefectural Central'], 40.828, 140.769),
    ('岩手医科大学附属病院', ['Iwate Medical University'], 39.606, 141.070),
    ('仙台医療センター', ['Sendai Medical Center'], 38.277, 140.849),
    ('秋田赤十字病院', ['Akita Red Cross', 'Red Cross Akita'], 39.664, 140.152),
    ('山形県立中央病院', ['Yamagata Prefectural Central'], 38.298, 140.352),
    ('福島県立医科大学附属病院', ['Fukushima Medical University'], 37.789, 140.470),
    ('水戸済生会総合病院', ['Mito Saiseikai'], 36.402, 140.423),
    ('獨協医科大学病院', ['Dokkyo Medical University Hospital'], 36.437, 139.833),
    ('前橋赤十字病院', ['Maebashi Red Cross', 'Japan Red Cross Maebashi'], 36.359, 139.095),
    ('埼玉医科大学総合医療センター', ['Saitama Medical Center'], 35.920, 139.470),
    ('日本医科大学千葉北総病院', ['Chiba Hokuso'], 35.799, 140.117),
    ('君津中央病院', ['Kimitsu'], 35.330, 139.902),
    ('東海大学医学部付属病院', ['Tokai University Hospital'], 35.371, 139.273),
    ('山梨県立中央病院', ['Yamanashi Prefectural Central', 'Yamanashi Prefectural General'], 35.673, 138.562),
    ('佐久総合病院佐久医療センター', ['Saku Medical Center', 'Saku General'], 36.218, 138.468),
    ('信州大学医学部附属病院', ['Shinshu University'], 36.239, 137.973),
    ('新潟大学医歯学総合病院', ['Niigata University'], 37.918, 139.038),
    ('長岡赤十字病院', ['Nagaoka Red Cross', 'Red Cross Nagaoka'], 37.437, 138.838),
    ('富山県立中央病院', ['Toyama Prefectural Central'], 36.690, 137.239),
    ('石川県立中央病院', ['Ishikawa Prefectural Central'], 36.594, 136.617),
    ('福井県立病院', ['Fukui Prefectural'], 36.077, 136.246),
    ('岐阜大学医学部附属病院', ['Gifu University'], 35.463, 136.732),
    ('聖隷三方原病院', ['Seirei Mikatahara'], 34.793, 137.708),
    ('順天堂大学医学部附属静岡病院', ['Juntendo'], 35.048, 138.928),
    ('愛知医科大学病院', ['Aichi Medical University'], 35.182, 137.062),
    ('伊勢赤十字病院', ['Ise Red Cross', 'Red Cross Ise Hospital'], 34.494, 136.720),
    ('済生会滋賀県病院', ['Saiseikai Shiga'], 35.022, 135.964),
    ('大阪大学医学部附属病院', ['Osaka University'], 34.810, 135.526),
    ('公立豊岡病院', ['Toyooka'], 35.548, 134.815),
    ('兵庫県立加古川医療センター', ['Kakogawa Medical'], 34.748, 134.852),
    ('奈良県立医科大学附属病院', ['Nara Medical University'], 34.489, 135.792),
    ('和歌山県立医科大学附属病院', ['Wakayama Medical University'], 34.191, 135.190),
    ('鳥取大学医学部附属病院', ['Tottori University'], 35.436, 133.334),
    ('島根県立中央病院', ['Shimane Prefectural Central'], 35.363, 132.760),
    ('川崎医科大学附属病院', ['Kawasaki Medical'], 34.572, 133.790),
    ('広島大学病院', ['Hiroshima University'], 34.377, 132.472),
    ('山口大学医学部附属病院', ['Yamaguchi University'], 33.942, 131.272),
    ('徳島県立中央病院', ['Tokushima Prefectural Central'], 34.067, 134.548),
    ('香川大学医学部附属病院', ['Kagawa University'], 34.285, 134.128),
    ('愛媛大学医学部附属病院', ['Ehime University'], 33.790, 132.899),
    ('高知医療センター', ['Kochi Health Sciences', 'Kochi Medical Center'], 33.544, 133.574),
    ('久留米大学病院', ['Kurume University'], 33.310, 130.519),
    ('佐賀県医療センター好生館', ['Koseikan', 'Saga Prefectural Medical Center'], 33.234, 130.281),
    ('長崎医療センター', ['Nagasaki Medical Center'], 32.921, 129.966),
    ('熊本赤十字病院', ['Kumamoto Red Cross', 'Red Cross Kumamoto'], 32.819, 130.749),
    ('大分大学医学部附属病院', ['Oita University'], 33.170, 131.606),
    ('宮崎大学医学部附属病院', ['Miyazaki University'], 31.829, 131.414),
    ('鹿児島市立病院', ['Kagoshima City Hospital'], 31.596, 130.541),
    ('浦添総合病院', ['Urasoe'], 26.251, 127.719),
]

# 公共用ヘリポート (名称の完全一致。部分一致だとホテル屋上等を誤判定する)
PUBLIC_HELIPORTS = [
    'Tokyo Heliport', 'Gunma Heliport', 'Tochigi Heliport', 'Shizuoka Heliport',
    'Tsu City Isewan Heliport', 'Wakasa Heliport', 'Osaka Heliport',
]

ER3_KEYWORDS = ['University', 'Medical School', 'Medical College', 'Emergency', 'Critical Care']


def dist_km(a_lat, a_lon, b_lat, b_lon):
    dx = (a_lon - b_lon) * 111.32 * math.cos(math.radians((a_lat + b_lat) / 2))
    dy = (a_lat - b_lat) * 110.57
    return math.hypot(dx, dy)


def main():
    with open(PATH) as f:
        gj = json.load(f)

    # 再実行安全: 前回追加した概略地物を除去してから分類し直す
    feats = [f for f in gj['features'] if f['properties'].get('added_by') != 'classify_hospitals']
    gj['features'] = feats
    matched_bases = set()
    n = {'drheli': 0, 'er3': 0, 'er2': 0, 'public': 0, 'heliport': 0}

    for feat in feats:
        p = feat['properties']
        name = str(p.get('name') or '')
        lon, lat = feat['geometry']['coordinates']
        if p.get('type') == 'hospital':
            cat = None
            for jp, tokens, blat, blon in DRHELI_BASES:
                if any(t.lower() in name.lower() for t in tokens) and dist_km(lat, lon, blat, blon) < 20:
                    cat = 'drheli'
                    p['name_jp'] = jp
                    matched_bases.add(jp)
                    break
            if not cat:
                cat = 'er3' if any(k.lower() in name.lower() for k in ER3_KEYWORDS) else 'er2'
            p['cat'] = cat
            n[cat] += 1
        else:
            is_pub = name.strip().lower() in [t.lower() for t in PUBLIC_HELIPORTS]
            p['cat'] = 'public' if is_pub else 'heliport'
            n[p['cat']] += 1

    # 照合できなかった基地病院は概略座標で追加
    added = 0
    for jp, tokens, blat, blon in DRHELI_BASES:
        if jp in matched_bases:
            continue
        added += 1
        feats.append({
            'type': 'Feature',
            'properties': {
                'type': 'hospital', 'cat': 'drheli', 'name': jp, 'name_jp': jp,
                'added_by': 'classify_hospitals', 'approx': True,
                'note': '概略位置(基地病院リストから追加)。ヘリパッド位置は要確認。',
            },
            'geometry': {'type': 'Point', 'coordinates': [round(blon, 5), round(blat, 5)]},
        })
        n['drheli'] += 1
        print('  + 追加(未照合):', jp)

    gj.setdefault('meta', {})['classified'] = (
        'cat: drheli(ドクターヘリ基地病院・概略リスト照合)/er3(三次救急相当・名称推定)/'
        'er2(二次救急相当・推定)/public(公共用ヘリポート)/heliport。'
        '基地病院の指定・機数は変動するため要確認。')
    with open(PATH, 'w') as f:
        json.dump(gj, f, ensure_ascii=False, separators=(',', ':'))
    print('counts:', n, '| 基地照合:', len(matched_bases), '/', len(DRHELI_BASES), '| 追加:', added)


if __name__ == '__main__':
    main()
