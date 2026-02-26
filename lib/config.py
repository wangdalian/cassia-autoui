"""
统一配置加载模块

支持从 JSON 文件加载配置，缺失字段用默认值填充。
环境变量 CASSIA_BASE_URL / CASSIA_AC_PASSWORD / CASSIA_LLM_API_KEY
可覆盖对应配置项。
"""

import json
import logging
import os

# 默认值
DEFAULTS = {
    "base_url": "http://YOUR_AC_IP",
    "browser_mode": "persistent",
    "ac_username": "admin",
    "ac_password": "",
    "ssh_credentials": [],
    "cdp_url": "http://localhost:9222",
    "auto_fetch_gateways": False,
    "gateway_macs": [],
    "shell_commands": [],
    "command_parsers": [],
    "timeout_page_load": 30000,
    "timeout_terminal_ready": 30000,
    "timeout_prompt_wait": 30000,
    "timeout_command_wait": 30000,
    "type_delay": 50,
    "devtools": False,
    "log_level": "INFO",
}

# 环境变量 -> 配置键的映射
_ENV_OVERRIDES = {
    "CASSIA_BASE_URL": "base_url",
    "CASSIA_AC_PASSWORD": "ac_password",
    "CASSIA_LLM_API_KEY": ("llm", "api_key"),
}


def load_config(path: str) -> dict:
    """
    从 JSON 文件加载配置。
    缺失的字段用 DEFAULTS 填充，环境变量可覆盖。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"配置文件不存在: {path}")

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 用默认值填充缺失字段
    config = {**DEFAULTS, **data}

    # 环境变量覆盖
    for env_key, config_key in _ENV_OVERRIDES.items():
        env_val = os.environ.get(env_key)
        if env_val:
            if isinstance(config_key, tuple):
                # 嵌套键，如 ("llm", "api_key")
                section, key = config_key
                if section not in config:
                    config[section] = {}
                config[section][key] = env_val
            else:
                config[config_key] = env_val

    return config


def apply_log_level(config: dict):
    """根据配置设置日志级别"""
    level_name = config.get("log_level", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.getLogger("cassia").setLevel(level)
