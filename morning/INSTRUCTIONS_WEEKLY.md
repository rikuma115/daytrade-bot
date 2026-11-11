# 毎週の振り返り（Claude 用の手順）

毎週金曜の引け後に、予約された作業として実行する。目的は、記録から学んで、翌週からの銘柄選びを良くすること。

## 守ること
- 書くのは `morning/LESSONS.md` と `morning/weekly/<日付>.md` だけ。お金の設定（config.toml）やプログラムは変更しない
- 読むのは集計の出力と `morning/LESSONS.md` だけ（記録のファイルを直接読み込まない）
- 少ない件数で結論を出さない。10件未満は【仮説】のまま

## 手順
1. 準備と集計
   ```bash
   git pull -q --rebase origin main
   pip install -q -r requirements.txt
   python -m morning.learn
   ```
2. 集計と `morning/LESSONS.md` を読み、メモを更新する
   - 新しく見えた傾向を【仮説】として足す。10件以上で同じ傾向なら「確かめられたこと」に移す
   - 集計で否定された項目は消す
   - 朝の判断の手順書（INSTRUCTIONS.md）自体は変えない
3. `morning/weekly/<今日の日付>.md` に、ユーザー向けの短い報告を書く（10行以内、やさしい日本語、専門用語を避ける）
   - 今週の成績（Claude の候補と候補全体の比較）
   - 学んだこと（1〜3個）
   - 本番のお金を入れる基準の進み具合（何個満たしたか）
4. 保存する（GitHub に送ると、報告がスマホに届く）
   ```bash
   git add morning/LESSONS.md morning/weekly
   git commit -m "毎週の振り返り <今日の日付>"
   git pull --rebase -q origin main && git push -q origin main
   ```
5. 最後の返答は1行だけ
