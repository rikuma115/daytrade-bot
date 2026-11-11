"""学習用の集計。毎週の振り返りで Claude が読むのは、この出力だけ（プランの利用枠の節約のため）。

  python -m morning.learn

出す内容:
  1. 全体の成績（朝の買い候補 / 寄り付き後に残した銘柄 / 候補全体 / 避けた銘柄）
  2. 何が効いているか（確信度別、寄り付きの窓の大きさ別、開示の種類別、株価帯別）
  3. 本番のお金を入れる基準（GO_LIVE.md）を満たしているか
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PICKS = ROOT / "picks"
BRIEFS = ROOT / "morning" / "briefs"

KIND_WORDS = [("上方修正", "上方修正"), ("下方修正", "下方修正"), ("業績予想", "業績予想の修正"),
              ("増配", "増配"), ("配当", "配当"), ("自己株式", "自社株買い"), ("公開買付", "TOB"),
              ("提携", "提携"), ("受注", "受注"), ("決算短信", "決算")]

# 本番の基準（GO_LIVE.md と同じ値）
MIN_DAYS = 30
MIN_KEEP = 40


def _stat(s) -> str:
    if len(s) == 0:
        return "0件"
    return f"{len(s)}件 平均{s.mean() * 100:+.2f}% 勝率{(s > 0).mean():.0%}"


def _perm_p(all_r, sub_r, n=20000, seed=0) -> float:
    import numpy as np
    k = len(sub_r)
    if k == 0 or k >= len(all_r):
        return float("nan")
    rng = np.random.default_rng(seed)
    a = np.asarray(all_r)
    sims = np.array([a[rng.choice(len(a), k, replace=False)].mean() for _ in range(n)])
    return float((sims >= np.mean(sub_r)).mean())


def main():
    import pandas as pd
    res_p = PICKS / "results.csv"
    if not res_p.exists():
        print("まだ集計できる日がありません")
        return
    r = pd.read_csv(res_p, dtype={"code": str})
    # 開示の題名を付ける
    titles = {}
    for f in BRIEFS.glob("20*.candidates.json"):
        date = f.name[:10]
        for c in json.loads(f.read_text(encoding="utf-8")):
            titles[(date, c["code"])] = c.get("titles", "")
    r["titles"] = [titles.get((d, c), "") for d, c in zip(r["date"].astype(str), r["code"])]
    r["kind"] = r["titles"].map(lambda t: next((k for w, k in KIND_WORDS if w in t), "その他"))
    days = r["date"].nunique()
    L, A = r[r["action"] == "long"], r[r["action"] == "avoid"]

    print(f"# 学習用の集計（{r['date'].min()}〜{r['date'].max()}、{days}日）")
    print("成績は「始値で買って終値で売った場合」のコスト込みの損益率")
    print()
    print("## 1. 全体")
    print(f"- 朝の買い候補: {_stat(L['ret'])}")
    print(f"- 候補全体: {_stat(r['ret'])}")
    print(f"- 避けた銘柄: {_stat(A['ret'])}")
    op = PICKS / "open_results.csv"
    o = pd.read_csv(op, dtype={"code": str}) if op.exists() else pd.DataFrame(columns=["decision", "ret", "date"])
    K, D = o[o["decision"] == "keep"], o[o["decision"] == "drop"]
    print(f"- 寄り付き後に残した（見直し時刻から終値）: {_stat(K['ret'])}")
    print(f"- 寄り付き後に外した（同上）: {_stat(D['ret'])}")
    print()
    print("## 2. 何が効いているか（件数が10件未満のものは偶然の可能性が高い）")
    if len(L):
        print("- 朝の確信度別: " + " / ".join(
            f"{lo}〜{hi - 1}: {_stat(L[(L['confidence'] >= lo) & (L['confidence'] < hi)]['ret'])}"
            for lo, hi in ((0, 60), (60, 70), (70, 101))))
    bins = [(-1, -0.02, "窓-2%未満"), (-0.02, 0.02, "窓±2%"), (0.02, 0.05, "窓+2〜5%"), (0.05, 9, "窓+5%以上")]
    print("- 寄り付きの窓別（候補全体）: " + " / ".join(
        f"{lab}: {_stat(r[(r['gap'] > lo) & (r['gap'] <= hi)]['ret'])}" for lo, hi, lab in bins))
    if len(L):
        print("- 寄り付きの窓別（朝の買い候補）: " + " / ".join(
            f"{lab}: {_stat(L[(L['gap'] > lo) & (L['gap'] <= hi)]['ret'])}" for lo, hi, lab in bins))
    print("- 開示の種類別（候補全体 → うち買い候補）:")
    for k, g in r.groupby("kind"):
        print(f"  - {k}: {_stat(g['ret'])} → {_stat(g[g['action'] == 'long']['ret'])}")
    print()
    print("## 3. 本番のお金を入れる基準（GO_LIVE.md）")
    half = sorted(o["date"].unique())
    first = K[K["date"].isin(half[: len(half) // 2])]["ret"]
    second = K[K["date"].isin(half[len(half) // 2:])]["ret"]
    pool = pd.concat([K["ret"], D["ret"]])
    p = _perm_p(pool, K["ret"]) if len(K) else float("nan")
    checks = [
        (f"記録が{MIN_DAYS}営業日以上", days >= MIN_DAYS, f"{days}日"),
        (f"残した銘柄が{MIN_KEEP}件以上", len(K) >= MIN_KEEP, f"{len(K)}件"),
        ("残した銘柄の平均がコスト込みでプラス", len(K) > 0 and K["ret"].mean() > 0,
         f"{K['ret'].mean() * 100:+.2f}%" if len(K) else "-"),
        ("残した銘柄が候補全体の平均を上回る", len(K) > 0 and K["ret"].mean() > r["ret"].mean(),
         f"{K['ret'].mean() * 100 if len(K) else 0:+.2f}% vs {r['ret'].mean() * 100:+.2f}%"),
        ("前半・後半ともプラス", len(first) > 0 and len(second) > 0 and first.mean() > 0 and second.mean() > 0,
         f"{first.mean() * 100 if len(first) else 0:+.2f}% / {second.mean() * 100 if len(second) else 0:+.2f}%"),
        ("偶然ではなさそう（p < 0.10）", p < 0.10, f"p={p:.2f}"),
    ]
    for name, ok, val in checks:
        print(f"- {'○' if ok else '×'} {name}（{val}）")
    print(f"→ {'全て満たした。少額での本番を検討できる' if all(c[1] for c in checks) else 'まだ本番には進まない'}")


if __name__ == "__main__":
    main()
