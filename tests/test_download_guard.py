#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""URL 下载安全守卫的 pytest 用例（v1.2.0，防 SSRF 回归）。

为什么单独放这一份
------------------
`run_gates.py` 的 pytest 步骤只收集 `plugin_dir/tests`，而插件的综合功能自测
`test_file_reader.py` 在**插件根目录**——门禁看不到它。所以这里把"下载校验"
这条安全契约按 pytest 形态再固一份，让门禁能真正跑到：
`tests/` 目录为空会被记成 SKIP（"未收集到用例"），SKIP 不等于通过。

威胁模型：待下载的 URL 来自**消息正文**。适配器把 file 段降级成
`[文件] x.docx，大小: 9547，链接: <URL>`，插件从这段文本里抠出 URL 去下载——
也就是 URL 完全由发消息的人控制。

校验层的完整用例同时存在于根目录 `test_file_reader.py`（可独立运行的综合套件）；
本文件是守护同一契约的 pytest 投影，两者都改才算改完。
"""

import asyncio
import socket
import sys
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = PLUGIN_DIR / "tests"
for _p in (str(PLUGIN_DIR), str(TESTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import plugin as plugin_mod  # noqa: E402

ALLOWED = plugin_mod.DEFAULT_DOWNLOAD_HOSTS.split(",")


def _resolver(*ips):
    """注入 DNS 解析结果，让用例脱机可跑。"""
    return lambda host, port: list(ips)


# ─── 1. 拒绝矩阵：内网 / 云元数据 / 非白名单 URL ─────────────────────

@pytest.mark.parametrize(
    "url, reason",
    [
        # reviewer 报告的两条原始 URL
        ("http://127.0.0.1:3001/x", "host_not_allowed"),
        ("http://169.254.169.254/latest/meta-data/", "host_not_allowed"),
        # 只比域名字符串挡不住的那些等价写法
        ("http://2130706433/", "host_not_allowed"),
        ("http://0x7f000001/", "host_not_allowed"),
        ("http://127.1/", "host_not_allowed"),
        ("http://[::1]/x", "host_not_allowed"),
        # 其它内网段与保留地址
        ("http://10.0.0.5/x", "host_not_allowed"),
        ("http://172.16.5.5/x", "host_not_allowed"),
        ("http://192.168.1.1/x", "host_not_allowed"),
        ("http://localhost:3001/x", "host_not_allowed"),
        # 白名单之外
        ("http://attacker.example/x", "host_not_allowed"),
        # 协议与凭据
        ("file:///etc/passwd", "bad_scheme"),
        ("ftp://ftn.qq.com/x", "bad_scheme"),
        ("http://user:pw@ftn.qq.com/x", "userinfo_not_allowed"),
        ("", "empty_url"),
    ],
)
def test_internal_and_offlist_urls_are_rejected(url, reason):
    with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
        plugin_mod.validate_download_target(url, policy="whitelist", allowed_hosts=ALLOWED)
    assert excinfo.value.reason == reason


# ─── 2. 域名白名单：边界安全 ─────────────────────────────────────────

@pytest.mark.parametrize(
    "host, allowed, expected",
    [
        ("tjc-download.ftn.qq.com", ["ftn.qq.com"], True),   # 子域放行
        ("ftn.qq.com", ["ftn.qq.com"], True),                # 本体放行
        ("eviltfn.qq.com", ["ftn.qq.com"], False),           # 后缀绕过必须拒
        ("ftn.qq.com.evil.com", ["ftn.qq.com"], False),      # 前缀伪装必须拒
        ("", ["ftn.qq.com"], False),
        ("ftn.qq.com", ["https://ftn.qq.com:443/path"], True),  # 条目写成整条 URL 也认
        ("a.ftn.qq.com", [".ftn.qq.com"], True),             # 条目带前导点也认
    ],
)
def test_allowlist_suffix_matching_is_boundary_safe(host, allowed, expected):
    assert plugin_mod.host_in_allowlist(host, allowed) is expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("a.com, b.com;c.com\nd.com", ["a.com", "b.com", "c.com", "d.com"]),
        (["a.com", "a.com", " b.com "], ["a.com", "b.com"]),
        ("", []),
        (None, []),
    ],
)
def test_allowlist_parsing(raw, expected):
    assert plugin_mod.parse_allowed_hosts(raw) == expected


# ─── 3. 公网判定 ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1", "10.0.0.1", "172.16.5.5", "192.168.1.1",
        "169.254.169.254",  # 云元数据
        "0.0.0.0", "224.0.0.1",  # 组播：is_global 会误判为公网，必须显式排掉
        "::1", "fe80::1",
        "::ffff:127.0.0.1", "::ffff:10.0.0.1",  # IPv4-mapped 要拆开判
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_rejected(ip):
    assert plugin_mod.is_public_ip(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "93.184.216.34", "2001:4860:4860::8888"])
def test_public_addresses_are_accepted(ip):
    assert plugin_mod.is_public_ip(ip) is True


# ─── 4. 关键：校验必须落在"解析结果"上，而不是域名字符串 ─────────────

@pytest.mark.parametrize("bad_ip", ["127.0.0.1", "169.254.169.254", "10.1.2.3", "::1"])
def test_allowlisted_host_resolving_to_internal_ip_is_rejected(bad_ip):
    """白名单域名 + DNS 指向内网 = 必须拒绝。

    这是"只挡域名"和"真防护"的分界线：攻击者自控 DNS 就能把白名单域名的解析结果
    指向内网，字符串比对毫无察觉。
    """
    with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
        plugin_mod.validate_download_target(
            "https://tjc-download.ftn.qq.com/ftn_handler/x?fname=a",
            policy="whitelist",
            allowed_hosts=ALLOWED,
            resolver=_resolver(bad_ip),
        )
    assert excinfo.value.reason == "non_public_address"


def test_mixed_resolution_with_any_internal_ip_is_rejected():
    """DNS 轮询返回公网+内网混合结果时，只要有一个落在内网就拒绝。"""
    with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
        plugin_mod.validate_download_target(
            "https://tjc-download.ftn.qq.com/x",
            policy="whitelist",
            allowed_hosts=ALLOWED,
            resolver=_resolver("93.184.216.34", "10.0.0.1"),
        )
    assert excinfo.value.reason == "non_public_address"


def test_allowlisted_host_resolving_to_public_ip_is_allowed():
    target, host = plugin_mod.validate_download_target(
        "https://tjc-download.ftn.qq.com/x?fname=a",
        policy="whitelist",
        allowed_hosts=ALLOWED,
        resolver=_resolver("93.184.216.34"),
    )
    assert host == "tjc-download.ftn.qq.com"
    assert target.startswith("https://tjc-download.ftn.qq.com/")


def test_dns_failure_is_rejected_not_silently_allowed():
    def failing_resolver(host, port):
        raise socket.gaierror("名称解析失败")

    with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
        plugin_mod.validate_download_target(
            "https://tjc-download.ftn.qq.com/x",
            policy="whitelist",
            allowed_hosts=ALLOWED,
            resolver=failing_resolver,
        )
    assert excinfo.value.reason == "dns_failed"


# ─── 5. 策略语义与 fail-safe ────────────────────────────────────────

def test_empty_allowlist_rejects_everything():
    with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
        plugin_mod.validate_download_target(
            "https://ftn.qq.com/x", policy="whitelist", allowed_hosts="", resolver=_resolver("93.184.216.34")
        )
    assert excinfo.value.reason == "allowlist_empty"


def test_public_policy_allows_offlist_host_but_still_blocks_private():
    target, host = plugin_mod.validate_download_target(
        "http://my-cdn.example/x", policy="public", allowed_hosts="", resolver=_resolver("93.184.216.34")
    )
    assert host == "my-cdn.example"
    with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
        plugin_mod.validate_download_target(
            "http://my-cdn.example/x", policy="public", allowed_hosts="", resolver=_resolver("127.0.0.1")
        )
    assert excinfo.value.reason == "non_public_address"


def test_off_policy_keeps_scheme_and_userinfo_checks():
    """policy=off 只是放开域名/IP，协议与内嵌凭据这两条永不放开。"""
    plugin_mod.validate_download_target("http://127.0.0.1:3001/x", policy="off", allowed_hosts="")
    for url, reason in (("file:///etc/passwd", "bad_scheme"), ("http://u:p@127.0.0.1/x", "userinfo_not_allowed")):
        with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
            plugin_mod.validate_download_target(url, policy="off", allowed_hosts="")
        assert excinfo.value.reason == reason


@pytest.mark.parametrize("raw", ["pbulic", "", "  ", "PUBLIC1", None, "任意中文"])
def test_unknown_policy_falls_back_to_strictest(raw):
    """策略名写错必须退回 whitelist——配置笔误不能变成校验绕过。"""
    assert plugin_mod.normalize_download_policy(raw) == "whitelist"


def test_unknown_policy_still_rejects_offlist_host():
    with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
        plugin_mod.validate_download_target(
            "http://attacker.example/x", policy="pbulic", allowed_hosts=ALLOWED, resolver=_resolver("93.184.216.34")
        )
    assert excinfo.value.reason == "host_not_allowed"


def test_private_hosts_escape_hatch_requires_two_explicit_optins():
    """逃生口要同时满足"主机在白名单"+"显式允许私网"两个条件才生效。"""
    target, _ = plugin_mod.validate_download_target(
        "http://localhost:3001/download/f.docx",
        policy="whitelist",
        allowed_hosts="localhost",
        allow_private_hosts=True,
        resolver=_resolver("127.0.0.1"),
    )
    assert target.endswith("/download/f.docx")
    # 只开 flag、不把主机加进白名单 → 仍然拒绝
    with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
        plugin_mod.validate_download_target(
            "http://localhost:3001/x",
            policy="whitelist",
            allowed_hosts=ALLOWED,
            allow_private_hosts=True,
            resolver=_resolver("127.0.0.1"),
        )
    assert excinfo.value.reason == "host_not_allowed"


# ─── 6. 日志脱敏 ────────────────────────────────────────────────────

def test_download_url_logging_redacts_query_credentials():
    """QQ 下载链接的 query 里带 fname/签名等凭据，日志不能整条留下。"""
    redacted = plugin_mod.redact_url(
        "https://tjc-download.ftn.qq.com/ftn_handler/abc?fname=secret&sig=deadbeef"
    )
    assert "secret" not in redacted and "deadbeef" not in redacted
    assert "tjc-download.ftn.qq.com/ftn_handler/abc" in redacted
    assert redacted.endswith("?<已隐去>")


# ─── 7. 连接钉定（v1.2.1，防 DNS 重绑定） ───────────────────────────
#
# review 指出：校验时解析一次 DNS、httpx 连接时**再解析一次**，两次解析之间
# 存在重绑定窗口——download_url_policy=public 时，攻击者自持 DNS 的域名可以
# 先解析到公网 IP 过检、连接时再解析回内网 IP。
# 修复：校验通过的 IP 经 collect_ips 带出，请求 URL 直接换成该 IP
# （Host 头/SNI/证书校验保留原主机名），连接阶段不再有第二次解析。

def test_build_pinned_request_rewrites_host_keeps_host_header_and_sni():
    url, extra = plugin_mod.build_pinned_request(
        "https://tjc-download.ftn.qq.com/ftn_handler/abc?fname=a",
        "tjc-download.ftn.qq.com",
        "93.184.216.34",
    )
    assert url == "https://93.184.216.34/ftn_handler/abc?fname=a", url
    assert extra["headers"]["host"] == "tjc-download.ftn.qq.com"
    # https 必须带 sni_hostname：连 IP 但按原主机名做 TLS，证书校验不降级
    assert extra["extensions"]["sni_hostname"] == "tjc-download.ftn.qq.com"


def test_build_pinned_request_keeps_explicit_port_and_supports_ipv6():
    url, extra = plugin_mod.build_pinned_request(
        "http://my-cdn.example:8080/x", "my-cdn.example", "93.184.216.34"
    )
    assert url == "http://93.184.216.34:8080/x", url
    assert extra["headers"]["host"] == "my-cdn.example:8080"

    url6, _extra6 = plugin_mod.build_pinned_request(
        "http://my-cdn.example/x", "my-cdn.example", "2001:4860:4860::8888"
    )
    assert url6 == "http://[2001:4860:4860::8888]/x", url6


def test_build_pinned_request_noop_without_ip():
    """policy=off 不解析、无从钉定——必须原样返回（旧行为）。"""
    url, extra = plugin_mod.build_pinned_request("http://127.0.0.1:3001/x", "127.0.0.1", None)
    assert url == "http://127.0.0.1:3001/x"
    assert extra == {}


def test_validate_collects_validated_ips_only_on_success():
    """collect_ips 只在**校验通过**时带出解析结果；拒绝路径一个 IP 都不能漏出去。"""
    collected: list[str] = []
    plugin_mod.validate_download_target(
        "https://tjc-download.ftn.qq.com/x",
        policy="whitelist",
        allowed_hosts=ALLOWED,
        resolver=_resolver("93.184.216.34", "2001:4860:4860::8888"),
        collect_ips=collected,
    )
    assert collected == ["93.184.216.34", "2001:4860:4860::8888"], collected

    collected_bad: list[str] = []
    with pytest.raises(plugin_mod._UrlRejectedError) as excinfo:
        plugin_mod.validate_download_target(
            "https://tjc-download.ftn.qq.com/x",
            policy="whitelist",
            allowed_hosts=ALLOWED,
            resolver=_resolver("127.0.0.1"),
            collect_ips=collected_bad,
        )
    assert excinfo.value.reason == "non_public_address"
    assert collected_bad == [], f"拒绝路径不得带出任何 IP: {collected_bad}"


class _RecordingStreamResponse:
    def __init__(self, status_code: int, headers: dict | None = None, body: bytes = b""):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._body = body

    @property
    def is_redirect(self) -> bool:
        return self.status_code in (301, 302, 303, 307, 308) and "location" in self.headers

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    async def aiter_bytes(self, chunk_size: int = 65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]


class _RecordingClient:
    """记录 (url, headers, extensions) 的 httpx 替身——断言"请求打到哪个 IP"必须看出站记录。"""

    def __init__(self, routes: dict[str, tuple[int, dict, bytes]]):
        self.routes = routes
        self.requests: list[tuple[str, dict, dict]] = []

    def stream(self, method: str, url: str, **kwargs):
        self.requests.append(
            (url, dict(kwargs.get("headers") or {}), dict(kwargs.get("extensions") or {}))
        )
        status, headers, body = self.routes.get(url, (404, {}, b""))
        resp = _RecordingStreamResponse(status, headers, body)

        class _Ctx:
            async def __aenter__(self):
                return resp

            async def __aexit__(self, *exc):
                return False

        return _Ctx()

    async def aclose(self) -> None:
        return None


def _make_plugin():
    """用 fakehost 造一个默认配置的插件实例（不跑完整生命周期，按需 on_load）。"""
    from fakehost import FakeHost, bind_context, build_context, get_default_config, load_plugin_module

    module = load_plugin_module(PLUGIN_DIR)
    plugin = module.create_plugin()
    host = FakeHost("org.mai-mai.file-reader")
    ctx = build_context("org.mai-mai.file-reader", rpc_call=host.rpc_call)
    bind_context(plugin, ctx, get_default_config(type(plugin).config_model))
    return module, plugin


def _patch_resolver(plugin, resolver):
    """在被测模块命名空间替换 resolve_host_ips（load_plugin_module 每次出新模块对象，
    异常类/全局名都必须从被测方法自己的 __globals__ 取——test_file_reader 踩过的坑）。"""
    under_test = type(plugin)._download.__globals__
    original = under_test["resolve_host_ips"]
    under_test["resolve_host_ips"] = resolver
    return lambda: under_test.__setitem__("resolve_host_ips", original)


def test_download_goes_to_validated_ip_not_hostname():
    """端到端：出站请求的主机必须是校验时用的那个 IP——域名不参与连接，重绑定窗口关闭。

    白名单条目故意用**域名**（而非 IP 字面量）并打桩 resolve_host_ips：
    若钉定失效（把原始 URL 直接交给 httpx），请求 URL 会是 cdn.example.com，
    这条用例就红——"看实际发包记录"的判据才立得住。
    """
    module, plugin = _make_plugin()

    async def drive():
        await plugin.on_load()
        restore = _patch_resolver(plugin, lambda host, port: ["93.184.216.34"])
        try:
            plugin.config.reader.download_url_policy = "whitelist"
            plugin.config.reader.download_allowed_hosts = "cdn.example.com"
            client = _RecordingClient({"http://93.184.216.34/f/a.docx": (200, {}, b"PINNED")})

            async def factory():
                return client

            plugin._get_http_client = factory  # type: ignore[method-assign]
            data = await plugin._download("http://cdn.example.com/f/a.docx", 4096)
            assert data == b"PINNED", data
            assert client.requests, "应当发出一次请求"
            url, headers, _ext = client.requests[0]
            assert url == "http://93.184.216.34/f/a.docx", f"请求应钉到已校验 IP: {client.requests}"
            assert headers.get("host") == "cdn.example.com", f"Host 头应保留原主机名: {headers}"
        finally:
            restore()
            await plugin.on_unload()

    asyncio.run(drive())


def test_redirect_hop_is_pinned_to_its_own_validated_ip():
    """重定向每一跳独立「校验 → 钉定」：第二跳连接的是它自己校验过的 IP。"""
    module, plugin = _make_plugin()

    def _two_hosts(host, port):
        # 注意必须返回 **list**：validate 会迭代解析结果逐个判公网，返回字符串会被逐字符拆开
        return {"cdn.example.com": ["93.184.216.34"], "mirror.example.com": ["8.8.4.4"]}[host]

    async def drive():
        await plugin.on_load()
        restore = _patch_resolver(plugin, _two_hosts)
        try:
            plugin.config.reader.download_url_policy = "whitelist"
            plugin.config.reader.download_allowed_hosts = "cdn.example.com,mirror.example.com"
            plugin.config.reader.download_max_redirects = 2
            client = _RecordingClient(
                {
                    "http://93.184.216.34/old": (302, {"location": "http://mirror.example.com/new"}, b""),
                    "http://8.8.4.4/new": (200, {}, b"HOP2"),
                }
            )

            async def factory():
                return client

            plugin._get_http_client = factory  # type: ignore[method-assign]
            data = await plugin._download("http://cdn.example.com/old", 4096)
            assert data == b"HOP2", data
            urls = [u for u, _h, _e in client.requests]
            assert urls == ["http://93.184.216.34/old", "http://8.8.4.4/new"], (
                f"每一跳都应连各自校验过的 IP: {client.requests}"
            )
        finally:
            restore()
            await plugin.on_unload()

    asyncio.run(drive())
