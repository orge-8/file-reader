# -*- coding: utf-8 -*-
"""宿主级加载门禁：用官方 MaiBot 源码的 PluginLoader 加载本插件并核对组件注册。

为什么需要它：`tests/smoke_test.py` 用的是自建 fakehost，走不到宿主的两道硬闸——
  1. ManifestValidator 按 host_version / SDK 版本校验 min/max_version 区间；
  2. `PluginLoader._validate_sdk_plugin_contract()` 要求 on_load / on_unload /
     on_config_update 都被覆写，且 `get_config_reload_subscriptions()` 可调用。
这两道闸只在真 Host 上跑得出来，本地 fakehost 永远绿（runtime-gotchas §67）。

用法（用装了官方最新 maibot-plugin-sdk 的解释器）：
    python tests/host_load_probe.py <MaiBot 源码根目录>
环境变量 `MAIBOT_HOST_ROOT` 可替代位置参数。
找不到 Host 时脚本以退出码 0 跳过（本地无实例不该算失败）。

只加载本插件（临时 plugins 目录里只放本插件的引用），不触碰其它插件。
退出码：0 = PASS/跳过，2 = 未发现插件，3 = 加载失败，4 = 组件数不符。
"""
import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

PLUGIN_ID = "org.mai-mai.file-reader"
EXPECTED = {"COMMAND": 3, "TOOL": 1, "HOOK_HANDLER": 4}

PLUGIN_DIR = Path(__file__).resolve().parent.parent


def resolve_host_root() -> Path | None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("host_root_pos", nargs="?", default="")
    parser.add_argument("--host-root", default="")
    known, _ = parser.parse_known_args()
    for raw in (known.host_root, os.environ.get("MAIBOT_HOST_ROOT", ""), known.host_root_pos):
        if not raw:
            continue
        candidate = Path(raw)
        if (candidate / "src" / "plugin_runtime").is_dir():
            return candidate.resolve()
    return None


def read_host_version(host_root: Path) -> str:
    pyproject = host_root / "pyproject.toml"
    if pyproject.is_file():
        matched = re.search(
            r'^\s*version\s*=\s*"([^"]+)"',
            pyproject.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
        if matched:
            return matched.group(1)
    return "unknown"


def main() -> int:
    host_root = resolve_host_root()
    if host_root is None:
        print("[SKIP] 未找到 MaiBot 源码根目录（可用位置参数或 MAIBOT_HOST_ROOT 指定）")
        return 0

    host_version = read_host_version(host_root)
    import importlib.metadata as im

    sdk_version = im.version("maibot-plugin-sdk")
    print(f"[host] {host_root}")
    print(f"[host] version = {host_version} / SDK = {sdk_version}")

    sys.path.insert(0, str(host_root))
    from src.plugin_runtime.runner.plugin_loader import PluginLoader  # noqa: E402

    # 严格区间校验（force=False）：manifest 声明必须真实覆盖当前 Host/SDK
    loader = PluginLoader(host_version=host_version, force_plugin_compatibility=False)

    with tempfile.TemporaryDirectory() as td:
        staging = Path(td) / "plugins"
        staging.mkdir()
        link = staging / PLUGIN_DIR.name
        linked = False
        try:
            os.symlink(str(PLUGIN_DIR), str(link), target_is_directory=True)
            # Windows 无开发者模式时 symlink「创建成功」却是悬空重解析点，
            # 必须验证可用性再用（实测 is_dir()=False 的假成功）
            linked = link.is_dir() and (link / "plugin.py").exists()
            if not linked:
                os.remove(str(link))
        except OSError:
            linked = False
        if not linked:
            # junction（mklink /J）无需特权；输出是 GBK，用字节模式收
            import subprocess

            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(PLUGIN_DIR)],
                check=True, capture_output=True,
            )
        candidates, duplicates = loader.discover_candidates([str(staging)])
        print(f"[discover] {len(candidates)} 个候选，重复 id {len(duplicates)} 个")

        if PLUGIN_ID not in candidates:
            print(f"[FAIL] 未发现目标插件 {PLUGIN_ID}")
            print("failed:", json.dumps(loader.failed_plugins, ensure_ascii=False, indent=2))
            return 2

        meta = loader.load_candidate(PLUGIN_ID, candidates[PLUGIN_ID])
        if meta is None:
            print("[FAIL] load_candidate 返回 None（manifest 闸或 SDK 契约闸未过）")
            print("failed:", json.dumps(loader.failed_plugins, ensure_ascii=False, indent=2))
            return 3

    print(f"[loaded] {meta.plugin_id} v{meta.version} type={meta.plugin_type}")
    print(f"[loaded] capabilities = {meta.capabilities_required}")
    print(f"[loaded] instance = {type(meta.instance).__name__}")

    components = meta.instance.get_components()
    counts = dict(Counter(component["type"] for component in components))
    print(f"[components] {counts}")
    for component in components:
        handler = component["metadata"].get("handler_name")
        print(f"  [{component['type']}] {component['name']}  (handler={handler})")
    print(f"[config_reload_subs] {meta.instance.get_config_reload_subscriptions()}")

    if counts != EXPECTED:
        print(f"[FAIL] 组件数与预期不符：得到 {counts}，预期 {EXPECTED}")
        return 4

    print("[PASS] 宿主加载门禁通过：manifest 兼容 + SDK 契约 + 组件注册全部符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
