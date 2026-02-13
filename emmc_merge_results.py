"""
合并网关结果文件

读取 emmc_results/ 目录下所有单网关 JSON 结果文件，
输出合并的 JSON 文件和 CSV 文件。

使用方式:
  python emmc_merge_results.py
"""

import csv
import glob
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "emmc_results")
GATEWAYS_DIR = os.path.join(RESULTS_DIR, "gateways")

# 合并输出文件（放在 emmc_results/ 根目录）
OUTPUT_JSON = os.path.join(RESULTS_DIR, "all_results.json")
OUTPUT_CSV = os.path.join(RESULTS_DIR, "all_results.csv")

# CSV 列的固定优先顺序（元数据字段在前）
PRIORITY_COLUMNS = [
    "mac", "name", "sn", "status", "uplink",
    "version", "containerVersion", "appVersion",
]

# 字段名 → CSV 列标题的映射（大写或大写开头）
COLUMN_HEADERS = {
    "mac": "MAC",
    "name": "Name",
    "sn": "SN",
    "status": "Status",
    "uplink": "Uplink",
    "version": "Version",
    "containerVersion": "ContainerVersion",
    "appVersion": "AppVersion",
    "devName": "DevName",
    "EST_TYP_A": "EST_TYP_A",
    "EST_TYP_B": "EST_TYP_B",
    "EOL_INFO": "EOL_INFO",
}


def main():
    if not os.path.isdir(GATEWAYS_DIR):
        print(f"[错误] 网关结果目录不存在: {GATEWAYS_DIR}")
        sys.exit(1)

    # 扫描 gateways/ 目录下所有单网关 JSON 文件
    json_files = sorted(glob.glob(os.path.join(GATEWAYS_DIR, "*.json")))

    if not json_files:
        print(f"[错误] 未找到任何网关结果文件（{GATEWAYS_DIR}/*.json）")
        sys.exit(1)

    print(f"找到 {len(json_files)} 个结果文件")

    # 读取所有结果
    all_results = []
    all_fields = set()
    for filepath in json_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
            all_results.append(data)
            all_fields.update(data.keys())
            print(f"  已读取: {os.path.basename(filepath)}")
        except (json.JSONDecodeError, IOError) as e:
            print(f"  [警告] 跳过无效文件 {os.path.basename(filepath)}: {e}")

    if not all_results:
        print("[错误] 没有有效的结果数据")
        sys.exit(1)

    # 生成有序列名：优先列 + 其余列按字母序
    priority_set = set(PRIORITY_COLUMNS)
    extra_columns = sorted(all_fields - priority_set)
    columns = [c for c in PRIORITY_COLUMNS if c in all_fields] + extra_columns

    # 输出合并 JSON
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n已输出合并 JSON: {OUTPUT_JSON}")

    # 输出合并 CSV（首列为从 1 开始的序号，标题大写开头）
    csv_columns = ["NO"] + columns
    # 将字段名映射为大写开头的标题（未在映射表中的字段首字母大写）
    csv_headers = {
        col: COLUMN_HEADERS.get(col, col[0].upper() + col[1:] if col else col)
        for col in csv_columns
    }
    with open(OUTPUT_CSV, "w", encoding="utf-8", newline="") as f:
        # 先写自定义标题行
        writer = csv.writer(f)
        writer.writerow([csv_headers[col] for col in csv_columns])
        # 再按字段顺序写数据行
        dict_writer = csv.DictWriter(f, fieldnames=csv_columns, extrasaction="ignore")
        for i, row in enumerate(all_results, 1):
            dict_writer.writerow({"NO": i, **row})
    print(f"已输出合并 CSV: {OUTPUT_CSV}")

    print(f"\n共合并 {len(all_results)} 条记录")


if __name__ == "__main__":
    main()
