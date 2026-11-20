"""見張り役。1日3回、予定どおり動いたかを確認し、失敗していたら代わりの処理をして必ず通知する。

  python -m morning.health --stage morning   7:47  朝の注目銘柄があるか（なければ空で保存 → ボットも起動）
  python -m morning.health --stage midday    10:07 ボットが動いているか（動いていなければ起動し直す）
  python -m morning.health --stage evening   16:07 1日のまとめができたか（なければ代わりに集計して通知）

通知は alerts/<日付>-<stage>.md を GitHub に保存して送る（保存されると GitHub がスマホに送る）。
確認は GitHub 上の最新の状態（origin/main）で行う。休場日は何もしない。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path

from morning.gather import JST, is_trading_day

ROOT = Path(__file__).resolve().parent.parent


def git(*args, check=True) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check).stdout


def on_main(path: str) -> str | None:
    out = subprocess.run(["git", "show", f"origin/main:{path}"], cwd=ROOT, capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


def push(files: list[str], msg: str):
    git("add", *files)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
        return
    git("commit", "-q", "-m", msg)
    for _ in range(4):
        if subprocess.run(["git", "pull", "--rebase", "-q", "origin", "main"], cwd=ROOT).returncode == 0 and \
           subprocess.run(["git", "push", "-q", "origin", "main"], cwd=ROOT).returncode == 0:
            return
    raise RuntimeError("GitHub に保存できませんでした")


def alert(d: dt.date, stage: str, text: str, extra: list[str] | None = None):
    p = ROOT / "alerts" / f"{d}-{stage}.md"
    p.parent.mkdir(exist_ok=True)
    p.write_text(text, encoding="utf-8")
    push([str(p.relative_to(ROOT))] + (extra or []), f"見張り {d} {stage}")


def bot_started(d: dt.date, part: str) -> bool:
    hb = on_main("state/heartbeat.json")
    if not hb:
        return False
    h = json.loads(hb)
    return h.get("date") == str(d) and part in h.get("parts", [])


def bot_running(d: dt.date, now: dt.datetime) -> bool:
    """今、ボットが動いているはずの時間帯に、実際に動いているか（起動済みで、予定より早く終わっていない）。"""
    hb = on_main("state/heartbeat.json")
    if not hb:
        return False
    h = json.loads(hb)
    if h.get("date") != str(d):
        return False
    part = "前場" if now.strftime("%H:%M") < "11:35" else "後場"
    planned_end = "11:35" if part == "前場" else "15:40"
    ended = h.get("ended", {}).get(part)
    return part in h.get("parts", []) and (ended is None or ended >= planned_end)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True, choices=["morning", "midday", "evening"])
    a = ap.parse_args()
    now = dt.datetime.now(JST)
    d = now.date()
    if not is_trading_day(d):
        print(f"{d} 休場日")
        return
    git("fetch", "-q", "origin", "main")
    git("pull", "-q", "--rebase", "origin", "main", check=False)
    picks = f"picks/{d}.json"

    if a.stage == "morning":
        if on_main(picks):
            # 開示の候補が少なすぎる日は、資料づくりの不具合を疑って知らせる（古い一覧を使っていた事故の再発防止）
            import re
            brief = on_main(f"morning/briefs/{d}.md") or ""
            m = re.search(r"開示（前の営業日 15:25 〜 今朝）: (\d+)社", brief)
            n = int(m.group(1)) if m else -1
            if 0 <= n < 3:   # 決算シーズン外は10社前後になるのが普通。0〜2社は不具合を疑う
                alert(d, "morning-check", f"⚠️ {d} 今朝の開示の候補が{n}社しかありませんでした。資料づくりの不具合の可能性があるので調べます。")
                print(f"注意: 開示の候補が{n}社")
                return
            print(f"OK: 朝の注目銘柄あり（開示の候補 {n}社）")
            return
        (ROOT / picks).write_text(json.dumps({
            "date": str(d), "market_view": "朝の作業が7:45までに終わらなかったため、今日は選定なし（見張り役が代わりに保存）",
            "picks": []}, ensure_ascii=False), encoding="utf-8")
        alert(d, "morning", f"⚠️ {d} 朝の注目銘柄の作業が終わっていませんでした。今日は選定なしで進め、ボットは起動します。原因は調べて直します。",
              [picks])
        print("代替: 空の注目銘柄を保存し、通知した")

    elif a.stage == "midday":
        if bot_running(d, now):
            print("OK: ボット稼働中")
            return
        # 注目銘柄のファイルを保存し直すと、それを合図にボットが起動する
        p = ROOT / picks
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {
            "date": str(d), "market_view": "朝の作業が未完了のため選定なし（見張り役が代わりに保存）", "picks": []}
        data["restarted_at"] = now.strftime("%H:%M")
        p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        alert(d, "midday", f"⚠️ {d} ボットが動いていませんでした（{now:%H:%M} 確認。起動しなかったか、途中で止まっていた）。今から起動し直します。",
              [picks])
        print("代替: ボットを起動し直し、通知した")

    elif a.stage == "evening":
        if on_main(f"alerts/{d}-summary.md") or bot_started(d, "完了"):
            print("OK: 1日のまとめあり")
            return
        from morning import picks as pk
        try:
            summary = pk.evaluate()
            extra = pk.evaluate_open()
        except Exception as e:  # noqa: BLE001
            summary, extra = f"（成績の集計に失敗: {type(e).__name__}）", ""
        text = (f"📒 {d} 1日のまとめ（見張り役が代わりに作成）\n"
                f"ボットの引け後の処理が確認できませんでした。原因は調べて直します。\n\n{summary}\n{extra}")
        alert(d, "evening", text, ["picks"])
        print("代替: 成績を集計して通知した")


if __name__ == "__main__":
    main()
