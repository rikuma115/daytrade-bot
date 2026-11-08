"""スマホへの通知。

ntfy（無料のプッシュ通知サービス）を使う。スマホに ntfy アプリを入れて、
トピック名（環境変数 NTFY_TOPIC）を購読すると、ボットからの通知が届く。
トピック名を知っている人は誰でも通知を読めるので、推測されにくい名前にして公開しない。
Discord の Webhook（環境変数 DISCORD_WEBHOOK_URL）にも対応。
通知に失敗しても、売買は止めない。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request


def send(text: str, title: str = "デイトレボット", urgent: bool = False) -> list[str]:
    """通知を送る。失敗した送信先のエラーを返す（空なら成功）。"""
    errors = []
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        # 共有の回線から送ると一時的に制限（429）されることがあるので、間隔をあけて最大3回送る
        for wait in (0, 15, 45):
            time.sleep(wait)
            try:
                req = urllib.request.Request(
                    f"https://ntfy.sh/{topic}", data=text.encode("utf-8"),
                    headers={"Title": title.encode("utf-8").decode("latin-1"),
                             "Priority": "urgent" if urgent else "default",
                             "Tags": "warning" if urgent else "chart_with_upwards_trend"})
                urllib.request.urlopen(req, timeout=10).close()
                break
            except urllib.error.HTTPError as e:
                if e.code != 429 or wait == 45:
                    errors.append(f"ntfy: {e}")
                    break
            except Exception as e:  # noqa: BLE001
                errors.append(f"ntfy: {e}")
                break
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if url:
        try:
            req = urllib.request.Request(url, data=json.dumps({"content": text}).encode(),
                                         headers={"Content-Type": "application/json", "User-Agent": "daytrade-bot"})
            urllib.request.urlopen(req, timeout=10).close()
        except Exception as e:  # noqa: BLE001
            errors.append(f"discord: {e}")
    return errors


if __name__ == "__main__":
    import sys
    errs = send(" ".join(sys.argv[1:]) or "テスト通知です。これが届けば設定は完了です。")
    print("送信しました" if not errs else errs)
