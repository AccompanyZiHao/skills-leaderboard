"""
skills.sh Top30 每日排行榜爬虫

功能：
1. 爬取 skills.sh 首页 HTML
2. 从页面嵌入的 JSON 数据中提取完整排行榜（排名连续、包含周趋势）
3. 保存 JSON 快照
4. 对比昨天生成 diff（排名变化、新进榜、掉榜）
5. 生成 Markdown 日报

数据来源：skills.sh 使用 Next.js，在 <script> 标签中嵌入了 initialSkills 数组，
包含所有 skill 的精确安装量和每周安装趋势，比解析 HTML 元素更准确完整。

用法：
    python scripts/daily.py                  # 使用今天日期
    python scripts/daily.py --date 2026-05-28  # 指定日期
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# 项目根目录（scripts/ 的上级）
PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"
REPORTS_DIR = DATA_DIR / "reports"
LATEST_FILE = DATA_DIR / "latest.json"
DATES_FILE = DATA_DIR / "dates.json"

# skills.sh 首页地址
SKILLS_URL = "https://www.skills.sh/"

# 请求头，模拟浏览器访问
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

# 只取前 30 条
TOP_N = 30


def fetch_html() -> str:
    """
    请求 skills.sh 首页，返回 HTML 内容

    失败时抛出异常，由调用方处理
    """
    print(f"正在请求 {SKILLS_URL} ...")
    resp = requests.get(SKILLS_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    print(f"请求成功，状态码: {resp.status_code}，HTML 长度: {len(resp.text)}")
    return resp.text


def extract_initial_skills(html: str) -> list[dict]:
    """
    从 HTML 的 <script> 标签中提取 initialSkills 数组

    skills.sh 使用 Next.js RSC 格式，数据嵌入在如下结构中：
        self.__next_f.push([1,"45:...initialSkills:[...]..."])
    需要先提取 push 中的字符串内容，解码转义，再解析 JSON。

    每条数据包含：source, skillId, name, installs, weeklyInstalls, isOfficial

    返回按安装量排序的完整 skill 列表
    """
    soup = BeautifulSoup(html, "html.parser")

    # 查找包含 initialSkills 的 script 标签
    for s in soup.find_all("script"):
        text = s.string or ""
        if "initialSkills" not in text:
            continue

        # Next.js RSC 格式: self.__next_f.push([1,"内容"])
        # 先定位 push 调用
        push_match = re.search(r'self\.__next_f\.push\(\[1,"', text)
        if not push_match:
            continue

        # 提取引号内的字符串内容
        # 从 push([1," 后面开始，到 "]) 结束
        str_start = text.find('"', push_match.end() - 1) + 1
        str_end = text.rfind('"])')
        if str_end == -1:
            # 尝试其他结束标记
            str_end = text.rfind('")')
        if str_end == -1:
            continue

        inner = text[str_start:str_end]
        # 解码转义：\" -> "，\\ -> \
        inner = inner.replace('\\"', '"').replace('\\\\', '\\')

        # 从解码后的内容中定位 initialSkills 数组
        idx = inner.find("initialSkills")
        if idx == -1:
            continue

        bracket_start = inner.find("[", idx)
        if bracket_start == -1:
            continue

        # 手动匹配方括号，提取完整的 JSON 数组
        depth = 0
        end = bracket_start
        for i in range(bracket_start, min(bracket_start + 200000, len(inner))):
            if inner[i] == "[":
                depth += 1
            elif inner[i] == "]":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break

        raw = inner[bracket_start:end]

        try:
            skills = json.loads(raw)
            print(f"从 script 标签提取到 {len(skills)} 个 skill")
            return skills
        except json.JSONDecodeError as e:
            print(f"JSON 解析失败: {e}", file=sys.stderr)
            continue

    print("未找到 initialSkills 数据", file=sys.stderr)
    return []


def parse_top_skills(all_skills: list[dict]) -> list[dict]:
    """
    从完整 skill 列表中提取 Top N，构建统一的数据结构

    每条数据包含：rank, id, name, source, installs, weeklyInstalls, isOfficial, url, description
    """
    top30 = []
    for i, sk in enumerate(all_skills[:TOP_N]):
        source = sk.get("source", "")
        skill_id = sk.get("skillId", "")
        name = sk.get("name", skill_id)

        skill_data = {
            "rank": i + 1,
            "id": f"{source}/{skill_id}",
            "name": name,
            "source": source,
            "installs": sk.get("installs", 0),
            "weeklyInstalls": sk.get("weeklyInstalls", []),
            "isOfficial": sk.get("isOfficial", False),
            "url": f"https://skills.sh/{source}/{skill_id}",
            "description": "",
        }
        top30.append(skill_data)

    print(f"构建 Top{TOP_N} 完成，共 {len(top30)} 条")
    return top30


def fetch_descriptions(top30: list[dict]) -> list[dict]:
    """
    并行请求每个 skill 的详情页，提取 description

    详情页的 script 标签中嵌入了含 description 字段的 JSON 数据
    """
    import concurrent.futures

    def fetch_one(skill: dict) -> str:
        """请求单个 skill 详情页，返回描述文本"""
        try:
            resp = requests.get(skill["url"], headers=HEADERS, timeout=15)
            if not resp.ok:
                return ""
            soup = BeautifulSoup(resp.text, "html.parser")

            # 从 meta description 标签提取（最可靠）
            meta = soup.find("meta", attrs={"name": "description"})
            if meta and meta.get("content"):
                desc = meta["content"].strip()
                # 截断过长的描述，保留前 200 字符
                if len(desc) > 200:
                    desc = desc[:197] + "..."
                return desc
        except Exception:
            pass
        return ""

    # 并行请求 30 个详情页
    print("正在获取 Top30 的描述信息...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        descriptions = list(executor.map(fetch_one, top30))

    # 填充描述
    for skill, desc in zip(top30, descriptions):
        skill["description"] = desc

    success_count = sum(1 for d in descriptions if d)
    print(f"描述获取完成: {success_count}/{len(top30)} 成功")
    return top30


def load_yesterday_snapshot(date_str: str) -> list[dict] | None:
    """
    加载昨天的快照数据

    返回 None 表示没有昨天的数据（第一次运行）
    """
    yesterday = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    snapshot_path = SNAPSHOTS_DIR / f"{yesterday}.json"

    if not snapshot_path.exists():
        print(f"昨天的快照不存在: {yesterday}")
        return None

    with open(snapshot_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    print(f"加载昨天快照: {yesterday}，共 {len(data)} 条")
    return data


def compute_diff(today: list[dict], yesterday: list[dict] | None) -> dict:
    """
    对比今天和昨天的数据，生成 diff

    包含：新进榜、掉榜、排名变化（含安装量增量）
    """
    if yesterday is None:
        # 第一次运行，没有历史数据
        return {
            "newEntries": [],
            "dropped": [],
            "rankChanges": [],
        }

    # 建立 id → 数据 的映射
    today_map = {item["id"]: item for item in today}
    yesterday_map = {item["id"]: item for item in yesterday}

    today_ids = set(today_map.keys())
    yesterday_ids = set(yesterday_map.keys())

    # 新进榜：今天有，昨天没有
    new_entries = []
    for skill_id in today_ids - yesterday_ids:
        item = today_map[skill_id]
        new_entries.append({
            "name": item["name"],
            "source": item["source"],
            "rank": item["rank"],
            "installs": item["installs"],
        })

    # 按排名排序
    new_entries.sort(key=lambda x: x["rank"])

    # 掉榜：昨天有，今天没有
    dropped = []
    for skill_id in yesterday_ids - today_ids:
        item = yesterday_map[skill_id]
        dropped.append({
            "name": item["name"],
            "source": item["source"],
            "yesterdayRank": item["rank"],
            "yesterdayInstalls": item["installs"],
        })

    # 按昨天排名排序
    dropped.sort(key=lambda x: x["yesterdayRank"])

    # 排名变化：两天都有的
    rank_changes = []
    for skill_id in today_ids & yesterday_ids:
        today_item = today_map[skill_id]
        yesterday_item = yesterday_map[skill_id]
        change = yesterday_item["rank"] - today_item["rank"]  # 正数=上升，负数=下降
        installs_diff = today_item["installs"] - yesterday_item["installs"]

        rank_changes.append({
            "id": skill_id,
            "name": today_item["name"],
            "source": today_item["source"],
            "yesterday": yesterday_item["rank"],
            "today": today_item["rank"],
            "change": change,
            "installsDiff": installs_diff,
            "todayInstalls": today_item["installs"],
        })

    # 按排名变化幅度排序（变化最大的排前面）
    rank_changes.sort(key=lambda x: abs(x["change"]), reverse=True)

    print(f"Diff: 新进榜 {len(new_entries)}，掉榜 {len(dropped)}，排名变化 {len(rank_changes)}")
    return {
        "newEntries": new_entries,
        "dropped": dropped,
        "rankChanges": rank_changes,
    }


def save_snapshot(top30: list[dict], date_str: str) -> None:
    """保存每日 JSON 快照"""
    snapshot_path = SNAPSHOTS_DIR / f"{date_str}.json"
    with open(snapshot_path, "w", encoding="utf-8") as f:
        json.dump(top30, f, ensure_ascii=False, indent=2)
    print(f"快照已保存: {snapshot_path}")


def save_latest(top30: list[dict], diff: dict, date_str: str) -> None:
    """更新 latest.json"""
    data = {
        "date": date_str,
        "top30": top30,
        "diff": diff,
    }
    with open(LATEST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"最新数据已更新: {LATEST_FILE}")


def update_dates(date_str: str) -> None:
    """更新日期索引"""
    dates = []
    if DATES_FILE.exists():
        with open(DATES_FILE, "r", encoding="utf-8") as f:
            dates = json.load(f)

    if date_str not in dates:
        dates.append(date_str)
        dates.sort()  # 按日期排序

    with open(DATES_FILE, "w", encoding="utf-8") as f:
        json.dump(dates, f, ensure_ascii=False, indent=2)
    print(f"日期索引已更新: {len(dates)} 个日期")


def format_installs(num: int) -> str:
    """将安装量整数格式化为可读字符串，如 1.7M、471.2K"""
    if num >= 1_000_000:
        return f"{num / 1_000_000:.1f}M"
    elif num >= 1_000:
        return f"{num / 1_000:.1f}K"
    else:
        return str(num)


def format_installs_diff(num: int) -> str:
    """格式化安装量增量，带正负号"""
    sign = "+" if num > 0 else ""
    abs_num = abs(num)
    if abs_num >= 1_000_000:
        return f"{sign}{num / 1_000_000:.1f}M"
    elif abs_num >= 1_000:
        return f"{sign}{num / 1_000:.0f}K"
    else:
        return f"{sign}{num}"


def generate_report(top30: list[dict], diff: dict, date_str: str) -> str:
    """
    生成 Markdown 日报

    返回报告内容字符串
    """
    lines = []
    lines.append(f"# Skills.sh Top30 日报 - {date_str}")
    lines.append("")

    # Top30 完整榜单
    lines.append("## Top 30 排行榜")
    lines.append("")
    lines.append("| 排名 | 变化 | 名称 | 来源 | 安装量 | 增量 |")
    lines.append("|------|------|------|------|--------|------|")

    # 建立 id → rankChange 的映射
    change_map = {rc["id"]: rc for rc in diff["rankChanges"]}
    new_ids = {e["name"] for e in diff["newEntries"]}

    for item in top30:
        # 判断状态
        if item["name"] in new_ids:
            change_str = "🆕"
            diff_str = ""
        elif item["id"] in change_map:
            rc = change_map[item["id"]]
            if rc["change"] > 0:
                change_str = f"↑{rc['change']}"
            elif rc["change"] < 0:
                change_str = f"↓{abs(rc['change'])}"
            else:
                change_str = "→"
            diff_str = format_installs_diff(rc["installsDiff"]) if rc["installsDiff"] != 0 else ""
        else:
            change_str = "→"
            diff_str = ""

        installs_str = format_installs(item["installs"])
        official = " ✓" if item.get("isOfficial") else ""
        lines.append(
            f"| {item['rank']} | {change_str} | {item['name']}{official} | "
            f"{item['source']} | {installs_str} | {diff_str} |"
        )

    lines.append("")

    # 新进榜
    if diff["newEntries"]:
        lines.append("## 新进榜")
        lines.append("")
        for entry in diff["newEntries"]:
            lines.append(f"- **{entry['name']}**（{entry['source']}）"
                        f"直接进入第 {entry['rank']} 名，安装量 {format_installs(entry['installs'])}")
        lines.append("")

    # 掉榜
    if diff["dropped"]:
        lines.append("## 掉榜")
        lines.append("")
        for entry in diff["dropped"]:
            lines.append(f"- **{entry['name']}**（{entry['source']}）"
                        f"昨天第 {entry['yesterdayRank']} 名，掉出 Top30")
        lines.append("")

    # 排名变化 Top5（变化最大的）
    big_changes = [rc for rc in diff["rankChanges"] if rc["change"] != 0][:5]
    if big_changes:
        lines.append("## 排名变化 Top5")
        lines.append("")
        lines.append("| 名称 | 昨天排名 | 今天排名 | 变化 | 安装量增量 |")
        lines.append("|------|---------|---------|------|----------|")
        for rc in big_changes:
            direction = "↑" if rc["change"] > 0 else "↓"
            diff_str = format_installs_diff(rc["installsDiff"])
            lines.append(
                f"| {rc['name']} | {rc['yesterday']} | {rc['today']} | "
                f"{direction}{abs(rc['change'])} | {diff_str} |"
            )
        lines.append("")

    report = "\n".join(lines)
    return report


def save_report(report: str, date_str: str) -> None:
    """保存 Markdown 日报"""
    report_path = REPORTS_DIR / f"{date_str}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"日报已保存: {report_path}")


def main():
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="skills.sh Top30 每日爬虫")
    parser.add_argument("--date", help="指定日期（YYYY-MM-DD），默认今天", default=None)
    args = parser.parse_args()

    # 确定日期
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    print(f"=== 开始运行 {date_str} 的爬虫 ===")

    # 确保目录存在
    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # 1. 爬取 HTML
    try:
        html = fetch_html()
    except requests.RequestException as e:
        print(f"错误：请求 skills.sh 失败 - {e}", file=sys.stderr)
        sys.exit(1)

    # 2. 从 script 标签提取完整排行榜数据
    all_skills = extract_initial_skills(html)
    if not all_skills:
        print("错误：未提取到任何 skill 数据", file=sys.stderr)
        sys.exit(1)

    # 3. 构建 Top30
    top30 = parse_top_skills(all_skills)
    if len(top30) < TOP_N:
        print(f"警告：只解析到 {len(top30)} 条数据，预期 {TOP_N} 条")

    # 4. 获取每个 skill 的描述（并行请求详情页）
    top30 = fetch_descriptions(top30)

    # 5. 加载昨天的快照并计算 diff
    yesterday = load_yesterday_snapshot(date_str)
    diff = compute_diff(top30, yesterday)

    # 6. 保存快照
    save_snapshot(top30, date_str)

    # 7. 更新 latest.json
    save_latest(top30, diff, date_str)

    # 8. 更新日期索引
    update_dates(date_str)

    # 9. 生成并保存日报
    report = generate_report(top30, diff, date_str)
    save_report(report, date_str)

    print(f"\n=== {date_str} 完成 ===")
    print(f"Top30 数据: {len(top30)} 条")
    print(f"新进榜: {len(diff['newEntries'])}，掉榜: {len(diff['dropped'])}")
    print(f"快照: data/snapshots/{date_str}.json")
    print(f"日报: data/reports/{date_str}.md")


if __name__ == "__main__":
    main()
