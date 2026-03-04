"""
检查网关结果中采集失败的记录

扫描 emmc_results/gateways/ 目录下的 JSON 文件，
找出 EST_TYP_A 和 EST_TYP_B 均为空的网关（eMMC 数据未采集到），
输出失败 MAC 列表到 emmc_results/failed_macs.json，
可直接复制到 config.json 的 gateway_macs 中进行重试。

使用方式:
  python emmc_check_failed.py
"""

import glob
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "emmc_results")
GATEWAYS_DIR = os.path.join(RESULTS_DIR, "gateways")
OUTPUT_FILE = os.path.join(RESULTS_DIR, "failed_macs.json")


def main():
    if not os.path.isdir(GATEWAYS_DIR):
        print(f"[错误] 网关结果目录不存在: {GATEWAYS_DIR}")
        sys.exit(1)

    json_files = sorted(glob.glob(os.path.join(GATEWAYS_DIR, "*.json")))
    if not json_files:
        print(f"[错误] 未找到任何网关结果文件（{GATEWAYS_DIR}/*.json）")
        sys.exit(1)

    print(f"扫描 {len(json_files)} 个结果文件...")

    failed_macs = []
    for filepath in json_files:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"  [警告] 无法读取 {os.path.basename(filepath)}: {e}")
            continue

        mac = data.get("mac", "")
        est_a = (data.get("EST_TYP_A") or "").strip()
        est_b = (data.get("EST_TYP_B") or "").strip()

        if not est_a and not est_b:
            failed_macs.append(mac)
            print(f"  [失败] {mac} — EST_TYP_A/B 均为空")

    if failed_macs:
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(failed_macs, f, ensure_ascii=False, indent=4)
        print(f"\n共 {len(failed_macs)} 台失败网关，已输出到: {OUTPUT_FILE}")
    else:
        print("\n所有网关均采集成功，无失败记录")


if __name__ == "__main__":
    main()
