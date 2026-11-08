"""日本株デイトレード自動売買ボット。

構成:
  config.py      設定の読み込み
  feed.py        株価データ（過去データの再生 / Yahoo / 将来は立花証券）
  broker.py      発注（仮想の売買 / 立花証券 e支店）
  risk.py        株数の計算と、注文前のリスクチェック
  timemachine.py 注文前の確率の見積もり
  engine.py      売買の本体（戦略は backtest.py と同じコードを使う）
  run.py         起動（python3 -m bot.run）
  review.py      成績の要約（python3 -m bot.review）
"""
