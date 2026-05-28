# Skills Leaderboard

每日追踪 [skills.sh](https://www.skills.sh/) Top30 排行榜变化。

## 功能

- 每天自动爬取 skills.sh 排行榜
- 保存 Top30 JSON 快照（含安装量、每周趋势）
- 对比昨天生成 diff（排名变化、新进榜、掉榜）
- 生成 Markdown 日报

## 数据结构

```
data/
├── latest.json           # 最新快照（含 diff）
├── dates.json            # 日期索引
├── snapshots/
│   └── 2026-05-28.json   # 每日快照
└── reports/
    └── 2026-05-28.md     # 每日报告
```

## 本地运行

```bash
pip install -r requirements.txt
python scripts/daily.py
```

指定日期：
```bash
python scripts/daily.py --date 2026-05-28
```

## 自动运行

GitHub Actions 每天北京时间 10:00 自动运行，也可手动触发。
