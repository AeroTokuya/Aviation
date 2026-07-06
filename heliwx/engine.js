/* HeliWX ブラウザ版 推定エンジン — app/analysis.py の JavaScript 移植。
 *
 * サーバ版と同じアルゴリズム:
 * - 視程: ターゲットのコントラスト減衰比 + Koschmieder の法則
 * - シーリング: 標高既知ターゲットの雲遮蔽判定
 * - 雲量: 空領域の色解析 (低彩度 = 雲)
 *
 * サーバ版との差分: 画角ズレ自動補正 (位相相関) は未実装。
 * デモ画像は基準画像と完全に位置が合っているため影響しない。
 */
"use strict";

const HeliWXEngine = (() => {
  const KOSCHMIEDER_K = 3.912;
  const RATIO_CLEAR = 0.85;
  const RATIO_OBSCURED = 0.15;
  const VISIBILITY_CAP_KM = 50.0;
  const LOW_LIGHT_THRESHOLD = 40.0;
  const OKTA_LABELS = [[0, "SKC"], [2, "FEW"], [4, "SCT"], [7, "BKN"], [8, "OVC"]];

  /* ---------- 基本画像処理 (Float64Array ベース) ---------- */

  // ImageData (RGBA) → グレースケール。cv2.COLOR_BGR2GRAY と同じ係数
  function toGray(imageData) {
    const { data, width, height } = imageData;
    const g = new Float64Array(width * height);
    for (let i = 0, p = 0; i < g.length; i++, p += 4) {
      // uint8 への丸めまで cv2 に合わせる
      g[i] = Math.round(0.299 * data[p] + 0.587 * data[p + 1] + 0.114 * data[p + 2]);
    }
    return { g, w: width, h: height };
  }

  function mean(arr) {
    let s = 0;
    for (let i = 0; i < arr.length; i++) s += arr[i];
    return arr.length ? s / arr.length : 0;
  }

  // numpy.median 互換 (偶数長は中央2値の平均)
  function median(arr) {
    if (!arr.length) return 0;
    const a = Float64Array.from(arr).sort();
    const m = a.length >> 1;
    return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2;
  }

  // bbox = [x, y, w, h]。numpy スライスと同様に画像端でクリップする
  function roi(img, bbox) {
    const [bx, by, bw, bh] = bbox;
    const x0 = Math.max(0, bx), y0 = Math.max(0, by);
    const x1 = Math.min(img.w, bx + bw), y1 = Math.min(img.h, by + bh);
    const w = Math.max(0, x1 - x0), h = Math.max(0, y1 - y0);
    const out = new Float64Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) out[y * w + x] = img.g[(y0 + y) * img.w + (x0 + x)];
    }
    return { g: out, w, h };
  }

  // BORDER_REFLECT_101 (gfedcb|abcdefgh|gfedcba) のインデックス
  function refl(i, n) {
    if (n === 1) return 0;
    if (i < 0) return -i;
    if (i >= n) return 2 * n - i - 2;
    return i;
  }

  // 3x3 ガウシアン (cv2.GaussianBlur ksize=3, sigma=0 → [.25, .5, .25] separable)
  function gaussBlur3(img) {
    const { g, w, h } = img;
    const tmp = new Float64Array(w * h);
    const out = new Float64Array(w * h);
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        tmp[y * w + x] =
          0.25 * g[y * w + refl(x - 1, w)] + 0.5 * g[y * w + x] + 0.25 * g[y * w + refl(x + 1, w)];
      }
    }
    for (let y = 0; y < h; y++) {
      for (let x = 0; x < w; x++) {
        out[y * w + x] =
          0.25 * tmp[refl(y - 1, h) * w + x] + 0.5 * tmp[y * w + x] + 0.25 * tmp[refl(y + 1, h) * w + x];
      }
    }
    return { g: out, w, h };
  }

  // Sobel 3x3 → 勾配強度 sqrt(gx^2 + gy^2)
  function gradientMagnitude(img) {
    const blur = gaussBlur3(img);
    const { g, w, h } = blur;
    const out = new Float64Array(w * h);
    for (let y = 0; y < h; y++) {
      const ym = refl(y - 1, h) * w, y0 = y * w, yp = refl(y + 1, h) * w;
      for (let x = 0; x < w; x++) {
        const xm = refl(x - 1, w), xp = refl(x + 1, w);
        const a = g[ym + xm], b = g[ym + x], c = g[ym + xp];
        const d = g[y0 + xm], f = g[y0 + xp];
        const p = g[yp + xm], q = g[yp + x], r = g[yp + xp];
        const gx = (c + 2 * f + r) - (a + 2 * d + p);
        const gy = (p + 2 * q + r) - (a + 2 * b + c);
        out[y0 + x] = Math.sqrt(gx * gx + gy * gy);
      }
    }
    return out;
  }

  function contrastMetric(roiImg, exposureNorm) {
    if (!roiImg.g.length) return 0;
    const grad = gradientMagnitude(roiImg);
    let s = 0;
    for (let i = 0; i < grad.length; i++) s += grad[i] * grad[i];
    return Math.sqrt(s / grad.length) / Math.max(exposureNorm, 1.0);
  }

  function noiseFloorMetric(roiImg, exposureNorm) {
    if (!roiImg.g.length) return 0;
    return (1.2 * median(gradientMagnitude(roiImg))) / Math.max(exposureNorm, 1.0);
  }

  function targetContrastRatio(refGray, curGray, bbox, skyBbox) {
    const adjusted = (gray) => {
      const norm = mean(gray.g);
      const m = contrastMetric(roi(gray, bbox), norm);
      if (!skyBbox) return m;
      const noise = noiseFloorMetric(roi(gray, skyBbox), norm);
      return Math.sqrt(Math.max(m * m - noise * noise, 0));
    };
    const refM = adjusted(refGray);
    const curM = adjusted(curGray);
    if (refM <= 1e-9) return 1.0;
    return Math.min(Math.max(curM / refM, 0), 1);
  }

  /* ---------- 推定ロジック ---------- */

  function koschmiederVisibility(distanceKm, ratio) {
    if (ratio >= 0.999 || ratio <= 1e-4) return null;
    return (KOSCHMIEDER_K * distanceKm) / -Math.log(ratio);
  }

  const round1 = (v) => Math.round(v * 10) / 10;
  const round3 = (v) => Math.round(v * 1000) / 1000;

  // Python round() の偶数丸め (シーリングの 50ft 丸めで一致させる)
  function bankersRound(v) {
    const fl = Math.floor(v);
    const diff = v - fl;
    if (diff > 0.5) return fl + 1;
    if (diff < 0.5) return fl;
    return fl % 2 === 0 ? fl : fl + 1;
  }

  function estimateVisibility(targets, ratios) {
    const results = [];
    const perTarget = [];
    let maxClearDist = 0;
    let minObscuredDist = null;

    targets.forEach((t, i) => {
      const r = ratios[i];
      const visible = r >= RATIO_OBSCURED;
      let est = null;
      if (r >= RATIO_OBSCURED && r <= RATIO_CLEAR) {
        est = koschmiederVisibility(t.distance_km, r);
        if (est != null) perTarget.push(est);
      }
      results.push({
        name: t.name,
        distance_km: t.distance_km,
        elevation_ft_msl: t.elevation_ft_msl ?? null,
        contrast_ratio: round3(r),
        visible,
        visibility_estimate_km: est ? round1(est) : null,
      });
      if (r > RATIO_CLEAR) maxClearDist = Math.max(maxClearDist, t.distance_km);
      if (r < RATIO_OBSCURED && (minObscuredDist === null || t.distance_km < minObscuredDist)) {
        minObscuredDist = t.distance_km;
      }
    });

    if (perTarget.length) {
      let vis = median(perTarget);
      vis = Math.max(vis, maxClearDist);
      if (vis > VISIBILITY_CAP_KM) return [VISIBILITY_CAP_KM, true, results];
      return [round1(vis), false, results];
    }
    if (minObscuredDist === null) {
      if (maxClearDist > 0) return [round1(maxClearDist), true, results];
      return [null, false, results];
    }
    if (maxClearDist > 0) {
      return [round1((maxClearDist + minObscuredDist) / 2), false, results];
    }
    return [round1(minObscuredDist / 2), false, results];
  }

  function estimateCeiling(results, cameraElevationFt, visibilityKm) {
    const notes = [];
    const elevTargets = results.filter((r) => r.elevation_ft_msl != null);
    if (!elevTargets.length) {
      return [null, false, ["標高付きターゲットが未設定のためシーリング推定不可"]];
    }
    const cloudObscured = (r) => {
      if (r.visible) return false;
      if (visibilityKm == null) return true;
      const rPred = Math.exp((-KOSCHMIEDER_K * r.distance_km) / visibilityKm);
      if (rPred <= 0.02) return false;
      return r.contrast_ratio < 0.4 * rPred;
    };
    const visibleElevs = elevTargets.filter((r) => r.visible).map((r) => r.elevation_ft_msl);
    const obscuredElevs = elevTargets.filter(cloudObscured).map((r) => r.elevation_ft_msl);

    if (!obscuredElevs.length) {
      const maxElev = Math.max(...elevTargets.map((r) => r.elevation_ft_msl));
      notes.push(`標高 ${Math.round(maxElev)}ft MSL まで雲遮蔽なし (それ以上は判定不能)`);
      return [null, true, notes];
    }
    const lowestObscured = Math.min(...obscuredElevs);
    const visibleBelow = visibleElevs.filter((e) => e < lowestObscured);
    let baseMsl;
    if (visibleBelow.length) {
      baseMsl = (Math.max(...visibleBelow) + lowestObscured) / 2;
    } else {
      baseMsl = lowestObscured * 0.75 + cameraElevationFt * 0.25;
    }
    const ceilingAgl = Math.max(baseMsl - cameraElevationFt, 0);
    return [bankersRound(ceilingAgl / 50) * 50, false, notes];
  }

  // 空領域の色解析による雲量 (BGR→HSV 相当を RGBA から直接計算)
  function estimateCloudCover(imageData, skyBbox) {
    if (!skyBbox) return [null, null];
    const { data, width, height } = imageData;
    const [bx, by, bw, bh] = skyBbox;
    const x0 = Math.max(0, bx), y0 = Math.max(0, by);
    const x1 = Math.min(width, bx + bw), y1 = Math.min(height, by + bh);
    if (x1 <= x0 || y1 <= y0) return [null, null];
    let cloud = 0, total = 0;
    for (let y = y0; y < y1; y++) {
      for (let x = x0; x < x1; x++) {
        const p = (y * width + x) * 4;
        const r = data[p], g = data[p + 1], b = data[p + 2];
        const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
        const sat = mx === 0 ? 0 : (mx - mn) / mx;
        const val = mx / 255;
        if (sat < 0.25 && val > 0.25) cloud++;
        total++;
      }
    }
    const oktas = Math.round((cloud / total) * 8);
    let label = "OVC";
    for (const [upper, name] of OKTA_LABELS) {
      if (oktas <= upper) { label = name; break; }
    }
    return [oktas, label];
  }

  function flightCategory(visibilityKm, ceilingFtAgl, ceilingIsUnlimited) {
    if (visibilityKm == null) return "UNKNOWN";
    const ceil = ceilingIsUnlimited ? null : ceilingFtAgl;
    const visCat = visibilityKm > 8 ? 3 : visibilityKm >= 5 ? 2 : visibilityKm >= 1.6 ? 1 : 0;
    const ceilCat = ceil == null ? 3 : ceil > 3000 ? 3 : ceil >= 1000 ? 2 : ceil >= 500 ? 1 : 0;
    return ["LIFR", "IFR", "MVFR", "VFR"][Math.min(visCat, ceilCat)];
  }

  /* ---------- 総合推定 (analysis.analyze 相当) ---------- */

  function analyze(refImageData, curImageData, targets, cameraElevationFt = 0, skyBbox = null) {
    const refGray = toGray(refImageData);
    const curGray = toGray(curImageData);

    if (mean(curGray.g) < LOW_LIGHT_THRESHOLD) {
      return {
        visibility_km: null,
        visibility_is_lower_bound: false,
        ceiling_ft_agl: null,
        ceiling_is_unlimited: false,
        cloud_cover_oktas: null,
        cloud_cover_label: null,
        flight_category: "UNKNOWN",
        targets: [],
        notes: ["夜間・低照度のため推定できません (日中の画像でのみ有効)"],
      };
    }

    const ordered = [...targets].sort((a, b) => a.distance_km - b.distance_km);
    const ratios = ordered.map((t) => targetContrastRatio(refGray, curGray, t.bbox, skyBbox));

    const [visKm, visLb, results] = estimateVisibility(ordered, ratios);
    const [ceiling, unlimited, notes] = estimateCeiling(results, cameraElevationFt, visKm);
    const [oktas, coverLabel] = estimateCloudCover(curImageData, skyBbox);

    if (ceiling != null && oktas != null && oktas < 5) {
      notes.push(`雲量 ${coverLabel} (BKN 未満) のため正式なシーリングには該当しない可能性`);
    }

    return {
      visibility_km: visKm,
      visibility_is_lower_bound: visLb,
      ceiling_ft_agl: ceiling,
      ceiling_is_unlimited: unlimited,
      cloud_cover_oktas: oktas,
      cloud_cover_label: coverLabel,
      flight_category: flightCategory(visKm, ceiling, unlimited),
      targets: results,
      notes,
    };
  }

  return { analyze };
})();
