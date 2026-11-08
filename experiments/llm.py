"""実験用の Claude API 呼び出し。

- 同じ質問の答えはディスクに保存し、再実行しても二重に課金しない
- 使ったトークンと料金（ドル）を記録する
- 出力は JSON スキーマで形を固定する

API キー: 環境変数 ANTHROPIC_API_KEY。この作業環境では、ネットワークの中継（プロキシ）が
登録済みのキーを差し込むので、コード側はダミーの値で動く。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import anthropic

CACHE = Path(__file__).resolve().parent / "cache"
# 1M トークンあたりのドル（入力, 出力）。2026-10 時点の公式価格
PRICE = {
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}
_lock = threading.Lock()


def client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    proxy = os.environ.get("HTTPS_PROXY")
    if key or not proxy:
        return anthropic.Anthropic(max_retries=4)
    # キーはプロキシが差し込む（コードやログにキーを残さない）
    return anthropic.Anthropic(
        api_key="sk-ant-placeholder", max_retries=4,
        http_client=anthropic.DefaultHttpxClient(proxy=proxy, verify="/root/.ccr/ca-bundle.crt"))


def ask(cl, model: str, system: str, user: str, schema: dict, effort: str = "medium",
        tag: str = "default") -> dict:
    """1回の質問。結果は {"data": {...} or None, "usage": {...}, "stop": str}。"""
    h = hashlib.sha256(json.dumps([model, system, user, schema, effort]).encode()).hexdigest()[:24]
    path = CACHE / tag / model / f"{h}.json"
    if path.exists():
        return json.loads(path.read_text())
    resp = cl.messages.create(
        model=model, max_tokens=4000, system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    )
    data = None
    if resp.stop_reason not in ("refusal", "max_tokens"):
        text = next((b.text for b in resp.content if b.type == "text"), None)
        data = json.loads(text) if text else None
    pin, pout = PRICE[model]
    u = resp.usage
    usage = {"in": u.input_tokens, "out": u.output_tokens,
             "usd": (u.input_tokens * pin + u.output_tokens * pout) / 1e6}
    out = {"data": data, "usage": usage, "stop": resp.stop_reason}
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False))
    return out


def ask_many(model, system, prompts: list[str], schema, effort="medium", tag="default",
             workers: int = 8, budget_usd: float = 30.0, progress: bool = True) -> list[dict]:
    """まとめて質問する。累計の料金が budget_usd を超えたら、残りは聞かずに止める。"""
    cl = client()
    results: list[dict | None] = [None] * len(prompts)
    spent = 0.0
    stop = threading.Event()

    def one(i):
        if stop.is_set():
            return i, None
        return i, ask(cl, model, system, prompts[i], schema, effort, tag)

    with ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(one, i) for i in range(len(prompts))]
        for n, f in enumerate(as_completed(futs), 1):
            i, r = f.result()
            results[i] = r
            if r:
                spent += r["usage"]["usd"]
                if spent > budget_usd:
                    stop.set()
            if progress and n % 50 == 0:
                print(f"  {model}: {n}/{len(prompts)} 件 / 累計 ${spent:.2f}", flush=True)
    print(f"  {model}: 完了 / 今回の料金 ${spent:.2f}（保存済みの答えの分も含む）")
    return results
