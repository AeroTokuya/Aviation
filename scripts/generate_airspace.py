#!/usr/bin/env python3
"""webapp/data/airspace.geojson を生成する。

収録クラス:
  CTR  管制圏(概略)   : TWR 周波数を持つ空港の半径 5NM 円 (実際の範囲は空港ごとに告示)
  INFO 情報圏(概略)   : RDO(リモート/AFIS) 周波数を持つ空港の半径 5NM 円
  RSTR 飛行回避(概略) : 原子力関係施設の上空 (半径約 4km / 概ね 2.2NM)
  TCA  進入管制区(概略): 大規模空港の APP 周波数を持つ空港の半径 24NM 円

⚠ すべて概略値。正確な水平・垂直範囲は AIP Japan / 告示で必ず照合すること。
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, '..', 'webapp', 'data')

# 原子力関係施設 (概略位置)。上空は飛行回避が求められる (半径約4km)。
NUCLEAR_SITES = [
    ('泊発電所', 43.036, 140.512),
    ('東通原子力発電所', 41.188, 141.390),
    ('六ヶ所再処理工場', 40.965, 141.330),
    ('女川原子力発電所', 38.401, 141.500),
    ('福島第一原子力発電所', 37.421, 141.033),
    ('福島第二原子力発電所', 37.316, 141.026),
    ('柏崎刈羽原子力発電所', 37.429, 138.596),
    ('東海第二発電所', 36.466, 140.607),
    ('浜岡原子力発電所', 34.623, 138.143),
    ('志賀原子力発電所', 37.061, 136.726),
    ('敦賀発電所', 35.674, 136.079),
    ('美浜発電所', 35.703, 135.963),
    ('大飯発電所', 35.541, 135.652),
    ('高浜発電所', 35.522, 135.504),
    ('島根原子力発電所', 35.538, 132.999),
    ('伊方発電所', 33.491, 132.311),
    ('玄海原子力発電所', 33.516, 129.837),
    ('川内原子力発電所', 31.833, 130.190),
]


def feat(lat, lon, props):
    return {
        'type': 'Feature',
        'properties': {'type': 'airspace', **props},
        'geometry': {'type': 'Point', 'coordinates': [round(lon, 5), round(lat, 5)]},
    }


def main():
    with open(os.path.join(DATA, 'airports.geojson')) as f:
        airports = json.load(f)

    feats = []
    n_ctr = n_info = n_tca = 0
    for a in airports['features']:
        p = a['properties']
        lon, lat = a['geometry']['coordinates']
        freqs = p.get('freqs') or {}
        name = p.get('name') or p.get('ident', '')
        if 'TWR' in freqs:
            n_ctr += 1
            feats.append(feat(lat, lon, {
                'class': 'CTR', 'name': f'{name} 管制圏', 'apt': p.get('ident'),
                'radius_nm': 5, 'alt': 'SFC～(告示高度)', 'freq': f"TWR {freqs['TWR']}",
            }))
        elif 'RDO' in freqs or 'AFIS' in freqs:
            n_info += 1
            feats.append(feat(lat, lon, {
                'class': 'INFO', 'name': f'{name} 情報圏', 'apt': p.get('ident'),
                'radius_nm': 5, 'alt': 'SFC～(告示高度)', 'freq': f"RDO {freqs.get('RDO', freqs.get('AFIS'))}",
            }))
        if p.get('class') == 'large airport' and 'APP' in freqs:
            n_tca += 1
            feats.append(feat(lat, lon, {
                'class': 'TCA', 'name': f'{name} 進入管制区(概略)', 'apt': p.get('ident'),
                'radius_nm': 24, 'alt': '(告示による)', 'freq': f"APP {freqs['APP']}",
            }))

    for name, lat, lon in NUCLEAR_SITES:
        feats.append(feat(lat, lon, {
            'class': 'RSTR', 'name': f'{name} 上空(飛行回避)',
            'radius_nm': 2.2, 'alt': 'SFC～', 'note': '原子力関係施設。上空の飛行は回避すること。',
        }))

    gj = {
        'type': 'FeatureCollection',
        'meta': {
            'dataset': 'airspace',
            'note': ('管制圏/情報圏/進入管制区は空港周波数データ(OurAirports)からの概略導出(円近似)。'
                     '原子力施設は概略位置。正確な範囲・高度は必ず AIP Japan / 告示で照合すること。'),
            'classes': {'CTR': n_ctr, 'INFO': n_info, 'TCA': n_tca, 'RSTR': len(NUCLEAR_SITES)},
        },
        'features': feats,
    }
    out = os.path.join(DATA, 'airspace.geojson')
    with open(out, 'w') as f:
        json.dump(gj, f, ensure_ascii=False, separators=(',', ':'))
    print(f'wrote {out}: CTR={n_ctr} INFO={n_info} TCA={n_tca} RSTR={len(NUCLEAR_SITES)} total={len(feats)}')


if __name__ == '__main__':
    main()
