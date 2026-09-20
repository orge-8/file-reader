#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文件读取插件冒烟测试（FakeHost，不启动 MaiBot、不联网）。

跑法（须用装了 maibot-plugin-sdk 的 venv）：
    python tests/smoke_test.py

退出码 0 = 全部通过。

为什么这个文件必须存在
----------------------
`run_gates.py` 只有看到 `tests/smoke_test.py` 才会执行冒烟步骤；缺了它门禁只跑静态
结构检查，输出"门禁全绿"却**从未加载过插件**。v1.1.0 就是这样过审的：
`check_plugin.py` 36 PASS，而真正的功能自测（根目录 `test_file_reader.py`，
不在 pytest 收集范围内）当时已经红了。

这里刻意放进去两类断言：
1. **组件清单**（数量 + 类型 + 处理器名）——装饰器被辅助方法"插队"时组件会静默绑到
   错误的函数上：名字还在、类型还在，只有 handler 变了。只查存在性抓不到。
2. **review 整改项的防回归**：manifest 仓库地址/版本/依赖声明、URL 下载 SSRF 守卫、
   风险配置告警。这些都是"改错了不会报错、只会静默失效"的类型，必须靠断言守住。
"""

import ast
import asyncio
import json
import logging
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent
for path in (str(PLUGIN_DIR), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import fakehost  # noqa: E402,F401  （缺 fakehost 时要立刻报错，而不是静默跳过检查）
from fakehost import (  # noqa: E402
    FakeHost,
    bind_context,
    build_context,
    get_default_config,
    load_plugin_module,
)

PLUGIN_ID = "org.mai-mai.file-reader"

#: 组件清单：(组件名, 类型, 处理器方法名)。总数必须完全一致。
EXPECTED_COMPONENTS = {
    # ── Hook：入站文件检测（before / after 双阶段）+ 上下文注入
    "detect_file_early": ("HOOK_HANDLER", "on_file_message_early"),
    "detect_file": ("HOOK_HANDLER", "on_file_message"),
    "inject_file_context": ("HOOK_HANDLER", "inject_file_context"),
    # ── Tool
    "search_file": ("TOOL", "search_file"),
    # ── Command（组件名 ASCII，用户敲的是 pattern 里的中文，所以方法名 != 组件名）
    "file_status": ("COMMAND", "cmd_status"),
    "clear_file": ("COMMAND", "cmd_clear_file"),
}

#: manifest 里必须声明的运行时依赖。pdfminer.six 是 review 项：
#: PDF 没有标准库降级路径，描述里又把它列为核心能力，漏声明会导致"装完开箱读不了 PDF"。
REQUIRED_DEPENDENCIES = {"numpy", "httpx", "pdfminer.six"}

#: 应当被安全策略拒绝的 URL——全部来自"群里谁都能发出来"的消息正文。
INTERNAL_URLS = [
    "http://127.0.0.1:3001/x",
    "http://169.254.169.254/latest/meta-data/",
    "http://192.168.1.1/x",
    "http://10.0.0.5/admin",
    "http://[::1]/x",
    "http://2130706433/",  # 十进制的 127.0.0.1
    "http://0x7f000001/",  # 十六进制的 127.0.0.1
    "http://localhost:3001/x",
    "file:///etc/passwd",
    "http://user:pw@ftn.qq.com/x",
]

#: 白名单域名 302 到云元数据——重定向方向最危险的一条
_METADATA_REDIRECT = {"status": 302, "headers": {"location": "http://169.254.169.254/latest/meta-data/"}}


class Runner:
    """极简测试夹具：装载插件、注入假上下文与配置。"""

    def __init__(self, config_overrides=None, returns=None):
        self.module = load_plugin_module(PLUGIN_DIR)
        self.plugin = self.module.create_plugin()
        self.host = FakeHost(PLUGIN_ID, returns=returns)
        self.ctx = build_context(PLUGIN_ID, rpc_call=self.host.rpc_call)
        config = get_default_config(type(self.plugin).config_model)
        _apply_overrides(config, config_overrides or {})
        bind_context(self.plugin, self.ctx, config)


def _apply_overrides(config: dict, overrides: dict) -> None:
    """按 "a.b.c" 路径覆盖默认配置。"""
    for dotted, value in overrides.items():
        parts = dotted.split(".")
        node = config
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value


def _fake_http_client(routes: dict) -> object:
    """httpx.AsyncClient 替身：按 URL 返回预设响应，并记录**实际发出的请求**。

    routes 形如 ``{url: {"status": 302, "headers": {...}, "body": b"..."}}``；
    未命中的 URL 一律 404。

    记录 ``requested`` 是这套用例的关键——要断言的是"内网地址从未被请求"，
    而不是只看返回值（返回 None 可能是因为拒绝，也可能是因为请求先失败了）。
    """

    class _Resp:
        def __init__(self, spec: dict):
            self.status_code = int(spec.get("status", 200))
            self.headers = dict(spec.get("headers") or {})
            self._body = spec.get("body") or b""

        @property
        def is_redirect(self) -> bool:
            return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        async def aiter_bytes(self, chunk_size: int = 65536):
            for i in range(0, len(self._body), chunk_size):
                yield self._body[i : i + chunk_size]

    class _StreamContext:
        def __init__(self, resp: _Resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def __init__(self) -> None:
            self.requested: list[str] = []

        def stream(self, method: str, url: str, **kwargs):
            self.requested.append(url)
            return _StreamContext(_Resp(routes.get(url, {"status": 404})))

        async def aclose(self) -> None:
            return None

    return _Client()


def _bind_fake_client(plugin, client) -> None:
    """把假客户端接到插件的 _get_http_client 上（它是个 async 工厂）。"""

    async def factory():
        return client

    plugin._get_http_client = factory  # type: ignore[method-assign]


class _LogCapture(logging.Handler):
    """挂到插件 logger 上收日志（启动自检的输出走的就是它）。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record) -> None:
        try:
            # 用 getMessage() 就够——它内部已经做过 `msg % args`。
            # 之前这里写成 `getMessage() % record.args`，等于格式化两次：第二次抛异常后
            # 退回 str(record.msg)，拿到的只是未填参的模板（日志断言因此永远不匹配）。
            self.lines.append(record.getMessage())
        except Exception:  # noqa: BLE001
            self.lines.append(str(record.msg))


# ═══════════════════════════════════════════════ 各项检查


def check_manifest_matches_reality() -> list[str]:
    """manifest 必须与仓库/代码现实一致（review 阻断项 2、3）。

    这几条错了都不会抛异常，只会让用户装完发现"链接不是这个仓库""读不了 PDF"——
    正是 review 抓住的那两个问题，所以必须显式断言守住。
    """
    failures: list[str] = []
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))

    repository = (manifest.get("urls") or {}).get("repository", "")
    if repository != "https://github.com/orge-8/file-reader":
        failures.append(
            f"urls.repository 必须指向实际仓库 https://github.com/orge-8/file-reader，实际 {repository!r}"
        )

    version = str(manifest.get("version") or "")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        failures.append(f"version 必须是三段式语义版本，实际 {version!r}")

    declared = {
        item.get("name")
        for item in (manifest.get("dependencies") or [])
        if item.get("type") == "python_package"
    }
    missing = sorted(REQUIRED_DEPENDENCIES - declared)
    if missing:
        failures.append(
            f"manifest 缺少必需依赖声明：{missing}"
            "（PDF 无标准库降级路径，pdfminer.six 必须声明，否则装完读不了 PDF）"
        )

    # dependencies 条目是严格字段白名单，多写一个字段真机会整包拒载
    for index, item in enumerate(manifest.get("dependencies") or []):
        extra = set(item) - {"type", "name", "version_spec", "id", "version"}
        if extra:
            failures.append(f"dependencies[{index}] 含未声明字段 {sorted(extra)}，真机会拒载")

    # 声明了的依赖必须真被代码用到
    parser_src = (PLUGIN_DIR / "file_parser.py").read_text(encoding="utf-8")
    plugin_src = (PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8")
    store_src = (PLUGIN_DIR / "vector_store.py").read_text(encoding="utf-8")
    if "pdfminer" not in parser_src:
        failures.append("file_parser.py 里找不到 pdfminer，manifest 却声明了它")
    for package, marker in (("numpy", "import numpy"), ("httpx", "import httpx")):
        if marker not in plugin_src and marker not in store_src:
            failures.append(f"manifest 声明了 {package}，但代码里找不到 {marker}")

    # 描述里承诺的能力必须有实现路径
    if "pdf" not in str(manifest.get("description", "")).lower():
        failures.append("manifest 描述里应体现 PDF 是支持的核心格式之一")
    return failures


def check_component_inventory(runner: Runner) -> list[str]:
    """组件清单：数量 + 类型 + 处理器名。专抓装饰器绑错人。"""
    components = runner.plugin.get_components()
    found = {item["name"]: item for item in components}
    failures: list[str] = []

    if len(components) != len(EXPECTED_COMPONENTS):
        failures.append(f"组件总数应为 {len(EXPECTED_COMPONENTS)}，实际 {len(components)}")
    missing = sorted(set(EXPECTED_COMPONENTS) - set(found))
    if missing:
        failures.append(f"缺少组件：{missing}")
    unexpected = sorted(set(found) - set(EXPECTED_COMPONENTS))
    if unexpected:
        failures.append(f"多出未预期的组件：{unexpected}")

    for name, (expected_type, expected_handler) in EXPECTED_COMPONENTS.items():
        item = found.get(name)
        if item is None:
            continue
        if item.get("type") != expected_type:
            failures.append(f"{name}: 类型应为 {expected_type}，实际 {item.get('type')}")
        handler = (item.get("metadata") or {}).get("handler_name")
        if handler != expected_handler:
            failures.append(
                f"{name}: handler 应为 {expected_handler}，实际 {handler!r}"
                "（装饰器与 def 之间插了辅助方法时会这样）"
            )
    return failures


def check_command_patterns() -> list[str]:
    """命令的中文触发词必须真能匹配到——组件名是 ASCII，匹配全靠 pattern。"""
    module = load_plugin_module(PLUGIN_DIR)
    cases = [
        ("cmd_status", "/file_status", "／文件状态", "/文件读取状态"),
        ("cmd_clear_file", "/clear_file", "／清文件", "/清理文件"),
    ]
    failures: list[str] = []
    for method_name, *patterns in cases:
        method = getattr(module.FileReaderPlugin, method_name, None)
        if method is None:
            failures.append(f"找不到命令方法 {method_name}")
            continue
        info = getattr(method, "__maibot_component_info__", None)
        if info is None or not info.command_pattern:
            failures.append(f"{method_name} 没有 command_pattern")
            continue
        for pattern in patterns:
            if not re.fullmatch(info.command_pattern, pattern):
                failures.append(f"{method_name} 的正则匹配不上 {pattern!r}")

    # 命令不能误吞普通聊天
    status = getattr(module.FileReaderPlugin.cmd_status, "__maibot_component_info__")
    for ordinary in ("你好呀", "/help", "文件状态是什么", "今天天气不错"):
        if re.fullmatch(status.command_pattern, ordinary):
            failures.append(f"cmd_status 误匹配普通文本 {ordinary!r}")
    return failures


def check_no_undefined_self_calls() -> list[str]:
    """扫出"被调用但类里没定义"的 ``self._xxx``。

    `check_plugin.py` 只做静态结构检查，抓不到这种漏改——别的插件栽过：重构时漏回
    一个方法，每次都抛 AttributeError，而测试桩正好打在那个方法上，测试全绿。
    """
    failures: list[str] = []
    tree = ast.parse((PLUGIN_DIR / "plugin.py").read_text(encoding="utf-8"))
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        defined = {
            node.name
            for node in cls.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assigned = {
            node.attr
            for node in ast.walk(cls)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and isinstance(node.ctx, ast.Store)
            and node.attr.startswith("_")
        }
        called = {
            node.func.attr
            for node in ast.walk(cls)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr.startswith("_")
        }
        for name in sorted(called - defined - assigned):
            failures.append(f"{cls.name} 调用了未定义的 self.{name}()")
    return failures


def check_lifecycle_and_unload_cleanup() -> list[str]:
    """on_load / on_unload 必须干净收尾：后台任务取消、临时文件清掉、客户端关闭。"""
    failures: list[str] = []
    runner = Runner()
    plugin = runner.plugin
    asyncio.run(plugin.on_load())

    if getattr(plugin, "_cleanup_task", None) is None:
        failures.append("on_load 没有启动清理循环任务")

    # 塞一个假的重试队列条目（带临时文件），卸载后必须连带清掉
    fd, tmp = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "wb") as handle:
        handle.write(b"x")
    plugin._embed_retry_queue.append({
        "session_id": "smoke", "conversation_id": "smoke", "stream_id": "smoke",
        "name": "x.txt", "tmp_path": tmp, "attempts": 1, "next_ts": 0.0,
    })

    asyncio.run(plugin.on_unload())
    if plugin._embed_retry_queue:
        failures.append("卸载后重试队列没有清空")
    if Path(tmp).exists():
        failures.append(f"卸载后重试队列的临时文件没有删除：{tmp}")
    if plugin._http_client is not None:
        failures.append("卸载后 httpx 单例没有释放")
    if plugin._bg_tasks:
        failures.append("卸载后仍有未回收的后台任务")
    return failures


def check_ssrf_guard_blocks_internal_targets() -> list[str]:
    """URL 下载必须拦下内网/云元数据地址，而且**一个包都不发**（review 阻断项 1）。

    这是本次整改的核心。URL 来自消息正文（`链接: ...` 由发消息的人完全控制），
    旧版直接 GET——等于任何群成员都能让插件拉内网接口，并把响应解析进 LLM 上下文。
    """
    failures: list[str] = []
    runner = Runner()
    plugin = runner.plugin
    # 被测模块自己的命名空间：fakehost 是**另行加载**插件模块的，
    # 直接 `import plugin` 拿到的是另一份模块对象，异常类不是同一个，except 接不住。
    under_test = type(plugin)._download.__globals__
    UrlRejectedError = under_test["_UrlRejectedError"]

    async def drive() -> None:
        await plugin.on_load()
        try:
            # ① 首跳即被拒：每个内网 URL 都必须拒绝，且零发包
            for url in INTERNAL_URLS:
                client = _fake_http_client({})
                _bind_fake_client(plugin, client)
                try:
                    data = await plugin._download(url, 4096)
                except UrlRejectedError:
                    pass
                except Exception as exc:  # noqa: BLE001
                    failures.append(f"{url!r} 抛了非预期异常 {type(exc).__name__}: {exc}")
                else:
                    failures.append(f"{url!r} 应被安全策略拒绝，实际返回 {data!r}")
                if client.requested:
                    failures.append(f"{url!r} 被拒绝却仍然发出了请求：{client.requested}")

            # ② 重定向：白名单域名 302 到云元数据，第二跳必须被拦，且元数据地址从未被请求
            public_host = "93.184.216.34"  # 公网 IP 字面量，不触发真实 DNS
            plugin.config.reader.download_url_policy = "whitelist"
            plugin.config.reader.download_allowed_hosts = public_host
            plugin.config.reader.download_max_redirects = 2
            redirect_client = _fake_http_client({
                f"http://{public_host}/f/a.docx": _METADATA_REDIRECT,
            })
            _bind_fake_client(plugin, redirect_client)
            try:
                await plugin._download(f"http://{public_host}/f/a.docx", 4096)
                failures.append("白名单域名 302 到云元数据时应被拒绝")
            except UrlRejectedError:
                pass
            except Exception as exc:  # noqa: BLE001
                failures.append(f"重定向用例抛了非预期异常 {type(exc).__name__}: {exc}")
            if any("169.254.169.254" in url for url in redirect_client.requested):
                failures.append(f"云元数据地址被真实请求了：{redirect_client.requested}")

            # ③ 反向验证：白名单内的正常下载必须还能用（别把功能一起改坏）
            ok_client = _fake_http_client({f"http://{public_host}/ok": {"status": 200, "body": b"OK"}})
            _bind_fake_client(plugin, ok_client)
            data = await plugin._download(f"http://{public_host}/ok", 4096)
            if data != b"OK":
                failures.append(f"白名单内正常下载应当成功，实际 {data!r}")
        finally:
            await plugin.on_unload()

    asyncio.run(drive())
    return failures


def check_self_report_warns_on_risky_config() -> list[str]:
    """风险配置必须在启动自检里"喊出来"：policy=off / 允许私网 / 关 TLS 校验。

    这类配置改了不会报错，只会在出事后才被想起来。且非法策略名必须
    **退回最严格的 whitelist**——配置笔误不能变成校验绕过。
    """
    failures: list[str] = []
    module = load_plugin_module(PLUGIN_DIR)
    if module.normalize_download_policy("pbulic") != "whitelist":
        failures.append("非法策略名没有退回 whitelist（配置笔误会变成校验绕过）")
    if module.normalize_download_policy("") != "whitelist":
        failures.append("空策略名没有退回 whitelist")

    capture = _LogCapture()
    # 日志记录挂在哪个 logger 名上取决于 SDK 的 PluginContext 实现，别去猜名字：
    # 直接挂到 `plugin` 这个父 logger 上，`plugin.file-reader` 与
    # `plugin.org.mai-mai.file-reader` 两种命名的记录都会向上冒泡到这里。
    targets = [logging.getLogger("plugin"), logging.getLogger(f"plugin.{PLUGIN_ID}")]
    previous_levels = [(target, target.level) for target in targets]
    for target in targets:
        target.addHandler(capture)
        target.setLevel(logging.DEBUG)
    try:
        runner = Runner(config_overrides={
            "reader.download_url_policy": "off",
            "reader.download_allow_private_hosts": True,
            "reader.insecure_download": True,
        })
        asyncio.run(runner.plugin.on_load())
        asyncio.run(runner.plugin.on_unload())
    finally:
        for target, level in previous_levels:
            target.removeHandler(capture)
            target.setLevel(level)

    joined = "\n".join(capture.lines)
    for keyword in ("download_url_policy=off", "download_allow_private_hosts", "insecure_download"):
        if keyword not in joined:
            failures.append(f"风险配置 {keyword} 没有在启动自检里告警")
    return failures


def check_messages_are_not_rewritten(runner: Runner) -> list[str]:
    """文件检测 hook 是**只读**的：必须放行消息且不改写内容。

    它靠"另起后台任务解析入库"工作；一旦改写 raw_message / processed_plain_text，
    或返回 modified_kwargs，就把别人的消息内容吃掉了。
    """
    failures: list[str] = []
    message = {
        "session_id": "smoke-1",
        "message_id": "smoke-msg-1",
        "processed_plain_text": "今天天气不错",
        "text": "今天天气不错",
        "raw_message": [{"type": "text", "data": "今天天气不错"}],
    }
    result = asyncio.run(
        runner.plugin.on_file_message(hook_name="chat.receive.after_process", message=message)
    )
    if not isinstance(result, dict) or result.get("action") != "continue":
        failures.append(f"非文件消息应原样放行，实际返回 {result!r}")
    elif result.get("modified_kwargs"):
        failures.append("文件检测 hook 不该改写消息内容")

    # 没有入库文件的会话不应注入任何东西。
    # 注意 hook 的契约是**恒定**返回 {"action": "continue"}，只有真注入时才附加
    # modified_kwargs——所以断言点是"没有 modified_kwargs"，不是"返回 None"。
    injected = asyncio.run(runner.plugin.inject_file_context(items=[], session_id="不存在的会话"))
    if injected is None:
        failures.append("钩子返回了 None；SKIP/OBserve 钩子应当返回 {'action': 'continue'}")
    elif injected.get("modified_kwargs"):
        failures.append(f"空会话不该注入内容，实际 {injected!r}")
    return failures


def check_status_command_replies(runner: Runner) -> list[str]:
    """命令必须显式发回执——命令返回值不会自动发到群里。"""
    failures: list[str] = []
    runner.host.calls.clear()
    ok, reply, level = asyncio.run(runner.plugin.cmd_status(stream_id="smoke-1"))
    if not ok or not reply:
        failures.append("cmd_status 未返回可用回执")
    if not runner.host.sent_texts:
        failures.append("cmd_status 没有显式发送回执")
    status_text = runner.host.sent_texts[-1] if runner.host.sent_texts else ""
    for keyword in ("文件读取插件状态", "NapCat 兜底"):
        if keyword not in status_text:
            failures.append(f"状态回执缺少 {keyword!r}")
    if level != 2:
        failures.append(f"发送成功时 intercept_level 应为 2，实际 {level}")
    return failures


# ═══════════════════════════════════════════════ 运行

_runner_cache: dict[str, Runner] = {}


def _shared_runner() -> Runner:
    """多个检查共用一份已加载的插件实例，省去重复装载。

    配置保持默认（whitelist 策略）——冒烟测试完全离线，不能因为网络抖动变偶发失败，
    所以下载相关用例一律用 IP 字面量或桩客户端，绝不碰真实 DNS。
    """
    if "runner" not in _runner_cache:
        runner = Runner()
        asyncio.run(runner.plugin.on_load())
        _runner_cache["runner"] = runner
    return _runner_cache["runner"]


CHECKS = [
    ("manifest 与现实一致（仓库/版本/依赖声明）", check_manifest_matches_reality),
    ("自调方法都有定义（防漏改）", check_no_undefined_self_calls),
    ("组件清单（数量/类型/处理器名）", lambda: check_component_inventory(_shared_runner())),
    ("命令中文触发词正则", check_command_patterns),
    ("生命周期加载与卸载收尾", check_lifecycle_and_unload_cleanup),
    ("SSRF 守卫：内网/云元数据一律拒且零发包", check_ssrf_guard_blocks_internal_targets),
    ("风险配置在启动自检里告警", check_self_report_warns_on_risky_config),
    ("文件检测 hook 只读不改写消息", lambda: check_messages_are_not_rewritten(_shared_runner())),
    ("file_status 命令回执", lambda: check_status_command_replies(_shared_runner())),
]


def main() -> int:
    failures: dict[str, list[str]] = {}
    for name, check in CHECKS:
        try:
            problems = check() or []
        except Exception:  # noqa: BLE001
            problems = ["检查本身抛异常：\n" + traceback.format_exc(limit=4)]
        failures[name] = problems
        print(f"{'PASS' if not problems else 'FAIL'}  {name}")
        for problem in problems:
            print(f"      · {problem}")

    bad = {name: items for name, items in failures.items() if items}
    print("-" * 60)
    if bad:
        print(f"冒烟未通过：{len(bad)}/{len(CHECKS)} 项有问题")
        return 1
    print(f"冒烟全部通过：{len(CHECKS)}/{len(CHECKS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
