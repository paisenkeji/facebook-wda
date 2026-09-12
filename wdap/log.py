# coding: utf-8
"""
WDA 运行日志查询接口（``/wda/log/*``）

对应 WDA 分支 ``wda_cv_vision`` 新增的 7 个端点，实现见服务端
``WebDriverAgentLib/Commands/FBLogCommands.m`` 与
``WebDriverAgentLib/Utilities/FBRunLogStore.{h,m}``。

它把 WDA **自己的运行日志**（HTTP 请求、异常、MJPEG 异常、监听器重建、
stderr 输出、上一次崩溃报告）暴露出来，用来回答"端口还在但没反应"、
"跑一小时整个 WDA 掉了"这类事后看不见的问题。

端点一览::

    GET  /wda/log/recent    最近 N 条（可按 level / category / since 过滤）
    GET  /wda/log/errors    只看 error 及以上
    GET  /wda/log/stats     各类计数器 + 捕获状态 + 上次崩溃
    GET  /wda/log/crash     上一次会话的崩溃报告（可顺带清除）
    GET  /wda/log/download  纯文本日志（text/plain，不是 JSON）
    POST /wda/log/clear     清空内存中的日志条目
    POST /wda/log/config    调整环形缓冲容量 / stderr 捕获开关

两个必须知道的服务端行为：

1. **全部路由都是 session-less + standalone**：路径里**不要**带
   ``/session/:id`` 前缀，本模块一律走 :attr:`Client.http`（不带 session）。
   standalone 表示它们绕开共享路由队列，所以即使主线程/队列卡死也能问到，
   这正是排查"卡死"时要用的通道。
2. **GET 的参数也必须放在 JSON body 里**：服务端 ``FBWebServer`` 构造
   ``request.arguments`` 时只做 ``JSONObjectWithData:request.body``，
   **不解析 query string**。所以 ``/wda/log/recent?limit=10`` 是无效的，
   客户端会帮你把参数塞进 body（本模块已处理，调用方无需关心）。

参数约束（服务端会**静默 clamp / 回退**，这里提前拦下来）：

- ``limit``：1..20000，超出被 clamp（默认 recent/errors=200，download=20000）；
- ``level``：只认 ``debug/info/warn/error/fatal``，
  写错会被服务端**静默当成 info**，所以本地直接抛 :class:`ValueError`；
- ``since``：只认整数（ ``NSNumber`` ），传别的类型服务端当 0 处理；
- ``category``：任意字符串，服务端不做校验（新分类无需声明）。

典型用法::

    import wdap
    c = wdap.Client()

    c.log.stats().http.requests            # 累计请求数
    for e in c.log.recent(limit=50).entries:
        print(e.time, e.level, e.message)

    # 只看崩溃与异常
    c.log.errors(category=wdap.log.LogCategory.EXCEPTION)

    # 增量跟随（内部用 since=上次 seq+1 轮询）
    for entry in c.log.follow(interval=1.0):
        print(entry.message)

    # 抓上一次进程是怎么死的
    rep = c.log.crash()
    if rep.previous_session_crashed:
        print(rep.report)

    c.log.save("wda.log")                  # 纯文本落盘
"""

from typing import Any, Dict, Iterator, List, Optional

from wdap.exceptions import WDAError, WDARequestError

__all__ = [
    "Log",
    "LogLevel",
    "LogCategory",
    "LogEntry",
    "LogSnapshot",
    "LogStats",
    "LogCrashReport",
    "LogConfig",
    "LogUnsupportedError",
]


class LogLevel(object):
    """/wda/log/recent 的 ``level`` 取值（服务端大小写不敏感，越往下越严重）"""

    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    FATAL = "fatal"

    ALL = (DEBUG, INFO, WARN, ERROR, FATAL)


class LogCategory(object):
    """日志分类取值

    与服务端 ``FBRunLogCategory*`` 一一对应。分类本身是**纯字符串**，
    服务端允许出现这里没列出的值，所以本类只作常量速查，不做校验。
    """

    #: 8100 HTTP 路由层的异常与慢请求
    HTTP = "http"
    #: 逃逸出路由 / 回调的 OC 异常
    EXCEPTION = "exception"
    #: 9100 MJPEG 推流循环
    MJPEG = "mjpeg"
    #: 监听器 / socket 重建
    SOCKET = "socket"
    #: 捕获到的 stderr 行（如 XCTest 的 NSLog）
    STDERR = "stderr"
    #: 崩溃信号处理
    CRASH = "crash"
    #: 启动 / 会话 / 生命周期
    LIFECYCLE = "lifecycle"

    ALL = (HTTP, EXCEPTION, MJPEG, SOCKET, STDERR, CRASH, LIFECYCLE)


#: 服务端对 limit 的硬上限（FBLogMaximumRecentLimit）
MAX_LIMIT = 20000
#: recent / errors 的服务端默认条数（FBLogDefaultRecentLimit）
DEFAULT_LIMIT = 200
#: download 的服务端默认条数
DEFAULT_DOWNLOAD_LIMIT = MAX_LIMIT


class LogUnsupportedError(WDAError):
    """设备上运行的 WDA 没有编译 /wda/log/* 支持

    服务端返回 ``status=110 / unknown command / Unhandled endpoint`` 时抛出，
    含义是这条路由**根本没有注册**，通常是设备上装的 WDA 不是用
    ``wda_cv_vision`` 这份源码编译的，或源码更新后没有重新编译部署。
    """


#: 110 = FBCommandStatus 的 unknown command
_UNKNOWN_COMMAND_STATUS = 110

_LOG_UNSUPPORTED_HINT = (
    "当前设备上的 WDA 没有注册 %s 这条路由。\n"
    "这不是调用姿势问题，而是设备上运行的 WDA 二进制不含运行日志支持，常见原因：\n"
    "  1. 设备上的 WDA 不是用 wda_cv_vision 这份源码编译的（例如用了 tidevice/官方预编译包）；\n"
    "  2. 源码更新后没有重新编译并部署到设备（xcodebuild test / tidevice xctest）；\n"
    "  3. 部署的 bundle id 指向了设备上残留的旧 WDA App。\n"
    "验证方法：curl http://<device>:8100/wda/log/stats —— 带日志支持的构建会返回 JSON，"
    "而不是 unknown command。"
)


def _raise_if_unsupported(path: str, err: Exception) -> None:
    """把 "unknown command" 翻译成一眼能看懂的错误"""
    if not isinstance(err, WDARequestError):
        return
    if err.status != _UNKNOWN_COMMAND_STATUS:
        return
    value = err.value if isinstance(err.value, dict) else {}
    if "Unhandled endpoint" not in str(value.get("message", "")):
        return
    raise LogUnsupportedError(_LOG_UNSUPPORTED_HINT % path)


def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
    """去掉值为 None 的键，避免把 null 传给服务端"""
    return {k: v for k, v in data.items() if v is not None}


def _check_limit(limit: Optional[int], default: int) -> Optional[int]:
    if limit is None:
        return None
    value = int(limit)
    if not 1 <= value <= MAX_LIMIT:
        raise ValueError("limit must be in 1..%d, got %r (server clamps it)"
                         % (MAX_LIMIT, limit))
    return value


def _check_level(level: Optional[str]) -> Optional[str]:
    """校验 level。

    服务端 ``FBRunLogLevelFromName`` 对未知名字**静默回退成 info**，
    那会让人以为"没有更严重的日志"，所以这里直接报错。
    """
    if level is None:
        return None
    lowered = str(level).strip().lower()
    if lowered not in LogLevel.ALL:
        raise ValueError("level must be one of %s, got %r (unknown names are "
                         "silently treated as 'info' by the server)"
                         % (list(LogLevel.ALL), level))
    return lowered


def _check_since(since: Optional[int]) -> Optional[int]:
    """``since`` 是 seq 下界（含），服务端只认 NSNumber，其余类型当 0 处理"""
    if since is None:
        return None
    value = int(since)
    if value < 0:
        raise ValueError("since must be >= 0, got %r" % since)
    return value


# --------------------------------------------------------------------------- #
# 结果封装
# --------------------------------------------------------------------------- #
class LogEntry(object):
    """/wda/log/recent 里的一条日志"""

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw or {}

    @property
    def raw(self) -> Dict[str, Any]:
        return self._raw

    @property
    def seq(self) -> int:
        """自增序号，可用作 :meth:`Log.recent` 的 ``since`` 做增量拉取"""
        return int(self._raw.get("seq", 0))

    @property
    def time(self) -> str:
        """ISO8601 时间戳字符串（设备本地时区）"""
        return self._raw.get("time") or ""

    @property
    def uptime_sec(self) -> float:
        """WDA 进程启动到该条日志的秒数"""
        return float(self._raw.get("uptimeSec", 0.0) or 0.0)

    @property
    def level(self) -> str:
        """``debug`` / ``info`` / ``warn`` / ``error`` / ``fatal``"""
        return self._raw.get("level") or ""

    @property
    def category(self) -> str:
        """分类名，见 :class:`LogCategory`"""
        return self._raw.get("category") or ""

    @property
    def thread(self) -> str:
        """产生该条日志的线程名"""
        return self._raw.get("thread") or ""

    @property
    def message(self) -> str:
        return self._raw.get("message") or ""

    def format(self) -> str:
        """单行文本，与 /wda/log/download 的格式一致"""
        return "%s %s [%s] %s" % (self.time, self.level.upper(),
                                  self.category, self.message)

    def __str__(self):
        return self.format()

    def __repr__(self):
        return "<LogEntry #%s %s %s %s>" % (self.seq, self.level,
                                            self.category,
                                            self.message[:40])


class LogSnapshot(object):
    """/wda/log/recent 与 /wda/log/errors 的返回"""

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw or {}

    @property
    def raw(self) -> Dict[str, Any]:
        return self._raw

    @property
    def entries(self) -> List[LogEntry]:
        """命中的日志，**按时间从旧到新**（服务端 ``oldest first``）"""
        return [LogEntry(item) for item in (self._raw.get("entries") or [])]

    @property
    def returned(self) -> int:
        """本次返回的条数"""
        return int(self._raw.get("returned", 0))

    @property
    def total(self) -> int:
        """WDA 启动以来记录过的总条数（含已因容量被挤掉的）"""
        return int(self._raw.get("total", 0))

    @property
    def stored(self) -> int:
        """当前内存里还留着的条数"""
        return int(self._raw.get("stored", 0))

    @property
    def capacity(self) -> int:
        """环形缓冲容量"""
        return int(self._raw.get("capacity", 0))

    @property
    def runner_uptime_seconds(self) -> float:
        return float(self._raw.get("runnerUptimeSeconds", 0.0) or 0.0)

    @property
    def filters(self) -> Dict[str, Any]:
        """服务端实际生效的过滤条件（minLevel / category / since / limit）"""
        return dict(self._raw.get("filters") or {})

    @property
    def capture(self) -> Dict[str, Any]:
        """stderr 捕获状态：stderrRequested / stderrActive / capturedLines"""
        return dict(self._raw.get("capture") or {})

    @property
    def last_seq(self) -> int:
        """最后一条的 seq；没有条目时返回 0，可 +1 后作为下次的 ``since``"""
        entries = self.entries
        return entries[-1].seq if entries else 0

    def __len__(self):
        return self.returned

    def __iter__(self) -> Iterator[LogEntry]:
        return iter(self.entries)

    def __repr__(self):
        return ("<LogSnapshot returned=%d stored=%d/%d uptime=%.0fs>" % (
            self.returned, self.stored, self.capacity,
            self.runner_uptime_seconds))


class LogStats(object):
    """/wda/log/stats 的返回：计数器总览

    排查"跑一段时间就掉"时最有价值的是这几项：
    ``listener_restarts``（监听器被重建过）、``mjpeg.exceptions``、
    ``http.non_success_responses``、``crash.previous_session_crashed``。
    """

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw or {}

    @property
    def raw(self) -> Dict[str, Any]:
        return self._raw

    @property
    def runner_uptime_seconds(self) -> float:
        return float(self._raw.get("runnerUptimeSeconds", 0.0) or 0.0)

    @property
    def log(self) -> Dict[str, Any]:
        """installed / totalRecorded / stored / capacity / suppressedEntries"""
        return dict(self._raw.get("log") or {})

    @property
    def http(self) -> Dict[str, Any]:
        """requests / nonSuccessResponses / slowRequests /
        slowestRequestSeconds / slowestRequest"""
        return dict(self._raw.get("http") or {})

    @property
    def mjpeg(self) -> Dict[str, Any]:
        """exceptions / screenshotFailurePeak"""
        return dict(self._raw.get("mjpeg") or {})

    @property
    def capture(self) -> Dict[str, Any]:
        """stderrRequested / stderrActive / capturedLines / oversizedLines"""
        return dict(self._raw.get("capture") or {})

    @property
    def crash(self) -> Dict[str, Any]:
        """previousSessionCrashed / previousSessionReportBytes / reportPath"""
        return dict(self._raw.get("crash") or {})

    @property
    def exceptions_by_name(self) -> Dict[str, int]:
        """按分类统计的异常次数"""
        return dict(self._raw.get("exceptionsByName") or {})

    @property
    def listener_restarts(self) -> int:
        """监听器被重建的次数（>0 说明端口层的 socket 异常过）"""
        return int(self._raw.get("listenerRestarts", 0))

    @property
    def previous_session_crashed(self) -> bool:
        return bool(self.crash.get("previousSessionCrashed", False))

    def __repr__(self):
        http = self.http
        return ("<LogStats uptime=%.0fs requests=%s errors=%s "
                "listenerRestarts=%s prevCrash=%s>" % (
                    self.runner_uptime_seconds, http.get("requests", 0),
                    http.get("nonSuccessResponses", 0),
                    self.listener_restarts, self.previous_session_crashed))


class LogCrashReport(object):
    """/wda/log/crash 的返回：上一次会话的崩溃报告"""

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw or {}

    @property
    def raw(self) -> Dict[str, Any]:
        return self._raw

    @property
    def previous_session_crashed(self) -> bool:
        """上一次会话确实死于信号或未捕获异常"""
        return bool(self._raw.get("previousSessionCrashed", False))

    @property
    def report(self) -> str:
        """崩溃报告全文（信号名 / 栈顶若干帧），没有时为空串"""
        return self._raw.get("report") or ""

    @property
    def report_bytes(self) -> int:
        return int(self._raw.get("previousSessionReportBytes", 0))

    @property
    def report_path(self) -> str:
        """设备上当前会话写崩溃尾部的文件路径"""
        return self._raw.get("reportPath") or ""

    @property
    def cleared(self) -> bool:
        """本次请求是否顺带清掉了报告"""
        return bool(self._raw.get("cleared", False))

    def __bool__(self):
        return self.previous_session_crashed

    def __repr__(self):
        return ("<LogCrashReport crashed=%s bytes=%d>" % (
            self.previous_session_crashed, self.report_bytes))


class LogConfig(object):
    """/wda/log/config 的返回：当前日志配置"""

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw or {}

    @property
    def raw(self) -> Dict[str, Any]:
        return self._raw

    @property
    def capacity(self) -> int:
        """环形缓冲容量"""
        return int(self._raw.get("capacity", 0))

    @property
    def stored_entries(self) -> int:
        return int(self._raw.get("storedEntries", 0))

    @property
    def stderr_capture_requested(self) -> bool:
        return bool(self._raw.get("stderrCaptureRequested", False))

    @property
    def stderr_capture_active(self) -> bool:
        """捕获线程是否还活着（请求了但没起来说明回滚过）"""
        return bool(self._raw.get("stderrCaptureActive", False))

    def __repr__(self):
        return ("<LogConfig capacity=%d stored=%d stderr=%s(active=%s)>" % (
            self.capacity, self.stored_entries,
            self.stderr_capture_requested, self.stderr_capture_active))


# --------------------------------------------------------------------------- #
# 接口集合
# --------------------------------------------------------------------------- #
class Log(object):
    """运行日志接口集合，通过 ``client.log`` 访问

    所有接口都**不需要 session**（服务端注册的是 ``withoutSession`` +
    ``standalone`` 路由），因此在 session 都没建立起来的时候也能用。
    """

    def __init__(self, client):
        self._client = client

    # ------------------------------------------------------------------ #
    # 内部：统一发请求（GET 的参数走 body）
    # ------------------------------------------------------------------ #
    def _get(self, path: str, timeout: Optional[float] = None, **params) -> Any:
        """GET：参数必须放在 JSON body 里（服务端不解析 query string）"""
        data = _clean(params) or None
        try:
            return self._client.http.get(path, data=data, timeout=timeout).value
        except WDARequestError as err:
            _raise_if_unsupported(path, err)
            raise

    def _post(self, path: str, timeout: Optional[float] = None, **params) -> Any:
        data = _clean(params) or None
        try:
            return self._client.http.post(path, data=data, timeout=timeout).value
        except WDARequestError as err:
            _raise_if_unsupported(path, err)
            raise

    # ------------------------------------------------------------------ #
    # 1. recent / errors
    # ------------------------------------------------------------------ #
    def recent(self,
               limit: Optional[int] = None,
               level: Optional[str] = None,
               category: Optional[str] = None,
               since: Optional[int] = None,
               timeout: Optional[float] = None) -> LogSnapshot:
        """取最近的运行日志

        Args:
            limit: 最多返回几条，1..20000（服务端默认 200）
            level: 最低级别，``debug``/``info``/``warn``/``error``/``fatal``
            category: 只取某一分类，见 :class:`LogCategory`
            since: 只取 seq >= since 的条目，用于增量拉取
            timeout: 单次请求超时（秒）

        Returns:
            LogSnapshot: ``.entries`` 是从旧到新的 :class:`LogEntry` 列表

        Example::

            snap = c.log.recent(limit=100, level="warn")
            for e in snap.entries:
                print(e.format())
        """
        value = self._get("/wda/log/recent",
                          limit=_check_limit(limit, DEFAULT_LIMIT),
                          level=_check_level(level),
                          category=category,
                          since=_check_since(since),
                          timeout=timeout)
        return LogSnapshot(value)

    def errors(self,
               limit: Optional[int] = None,
               category: Optional[str] = None,
               timeout: Optional[float] = None) -> LogSnapshot:
        """只取 error 及以上的日志（等价于 ``recent(level="error")``）

        Example::

            c.log.errors(category=wdap.log.LogCategory.EXCEPTION)
        """
        value = self._get("/wda/log/errors",
                          limit=_check_limit(limit, DEFAULT_LIMIT),
                          category=category,
                          timeout=timeout)
        return LogSnapshot(value)

    # ------------------------------------------------------------------ #
    # 2. stats / crash
    # ------------------------------------------------------------------ #
    def stats(self, timeout: Optional[float] = None) -> LogStats:
        """各类计数器总览（请求数 / 失败数 / 慢请求 / MJPEG 异常 / 监听器重建 / 上次崩溃）

        Example::

            s = c.log.stats()
            print(s.http["requests"], s.listener_restarts, s.previous_session_crashed)
        """
        return LogStats(self._get("/wda/log/stats", timeout=timeout))

    def crash(self,
              clear: bool = False,
              timeout: Optional[float] = None) -> LogCrashReport:
        """取**上一次**会话的崩溃报告

        Args:
            clear: 取完顺带清掉，避免下次重复看到

        Returns:
            LogCrashReport: ``bool(report)`` 为真表示上次确实崩过

        Example::

            rep = c.log.crash(clear=True)
            if rep.previous_session_crashed:
                print(rep.report)
        """
        value = self._get("/wda/log/crash", clear=bool(clear), timeout=timeout)
        return LogCrashReport(value)

    # ------------------------------------------------------------------ #
    # 3. download（纯文本，不是 JSON）
    # ------------------------------------------------------------------ #
    def download(self,
                 limit: Optional[int] = None,
                 level: Optional[str] = None,
                 timeout: Optional[float] = None) -> str:
        """纯文本日志（``text/plain``），一行一条

        这个端点返回的不是 WDA 的标准 JSON 信封，所以不走通用请求通道，
        直接拿原始响应体。

        Example::

            text = c.log.download(limit=2000)
        """
        data = _clean({
            "limit": _check_limit(limit, DEFAULT_DOWNLOAD_LIMIT),
            "level": _check_level(level),
        }) or None
        try:
            resp = self._client._fetch_raw("GET", "/wda/log/download",
                                           data=data, timeout=timeout)
        except WDARequestError as err:
            _raise_if_unsupported("/wda/log/download", err)
            raise
        return resp.text

    def save(self,
             path: str,
             limit: Optional[int] = None,
             level: Optional[str] = None,
             timeout: Optional[float] = None) -> str:
        """把 :meth:`download` 的结果写到本地文件，返回写入的路径

        Example::

            c.log.save("wda-run.log", limit=5000)
        """
        text = self.download(limit=limit, level=level, timeout=timeout)
        with open(path, "w", encoding="utf-8", newline="") as fp:
            fp.write(text)
        return path

    # ------------------------------------------------------------------ #
    # 4. clear / config
    # ------------------------------------------------------------------ #
    def clear(self, timeout: Optional[float] = None) -> None:
        """清空内存中的日志条目（计数器与 seq 不重置）

        注意这也会清掉还没看的崩溃尾部以外的历史，排查完再调。
        """
        self._post("/wda/log/clear", timeout=timeout)

    def config(self,
               capacity: Optional[int] = None,
               stderr_capture: Optional[bool] = None,
               timeout: Optional[float] = None) -> LogConfig:
        """读取或修改日志配置（不给参数就是纯查询）

        Args:
            capacity: 环形缓冲容量，1..20000。调小会立刻丢弃超出的旧条目
            stderr_capture: 是否捕获 WDA 的 stderr（XCTest 的 NSLog 等）。
                打开后日志量会明显变大

        Returns:
            LogConfig: 修改后的生效值

        Example::

            c.log.config(capacity=5000, stderr_capture=True)
        """
        value = self._post("/wda/log/config",
                           capacity=_check_limit(capacity, DEFAULT_LIMIT),
                           stderrCapture=(None if stderr_capture is None
                                          else bool(stderr_capture)),
                           timeout=timeout)
        return LogConfig(value)

    # ------------------------------------------------------------------ #
    # 5. 便捷封装
    # ------------------------------------------------------------------ #
    def available(self) -> bool:
        """探测设备上的 WDA 是否注册了 /wda/log/* 路由

        与 :meth:`wdap.cv.CV.status` 不同，这里没有专门的能力端点，
        用最轻的 ``/wda/log/stats`` 试一次；路由不存在会返回
        ``unknown command``（status=110），这里转成 ``False``。
        """
        try:
            self.stats(timeout=5.0)
            return True
        except LogUnsupportedError:
            return False

    def follow(self,
               interval: float = 1.0,
               level: Optional[str] = None,
               category: Optional[str] = None,
               limit: Optional[int] = None) -> Iterator[LogEntry]:
        """增量跟随日志（生成器，按 seq 增量拉取，Ctrl-C / break 退出）

        Args:
            interval: 两次拉取之间的间隔（秒）
            level: 最低级别
            category: 分类过滤
            limit: 每次最多拉几条（服务端默认 200）

        Example::

            for entry in c.log.follow(interval=0.5, level="warn"):
                print(entry.format())

        Yields:
            LogEntry
        """
        import time

        since: Optional[int] = None
        seen: Optional[int] = None
        while True:
            snap = self.recent(limit=limit, level=level,
                               category=category, since=since)
            entries = snap.entries
            if seen is not None:
                # since 是"含下界"，同一条可能被重复带回一次
                entries = [e for e in entries if e.seq > seen]
            for entry in entries:
                seen = entry.seq
                yield entry
            if entries:
                since = seen + 1 if seen is not None else since
            elif seen is None and snap.stored:
                # 首次拉取没命中（例如过滤条件太严），从当前尾部开始
                since = None
            time.sleep(interval)
