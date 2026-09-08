# coding: utf-8
"""
OpenCV + Vision 图像/文字识别接口封装

对应 WDA 分支 wda_cv_vision（基于 WebDriverAgent v16.12.3）新增的 9 个端点，
实现见服务端 ``WebDriverAgentLib/Commands/FBVisionCommands.m``。

端点一览::

    GET  /wda/cv/status          能力探测（无需 session）
    POST /wda/cv/snapshot        取截图，用于制作模板图
    POST /wda/vision/ocr         全屏文字识别
    POST /wda/vision/findText    按文字查找 + 可选点击
    POST /wda/vision/waitForText 轮询等待文字出现
    POST /wda/cv/matchImage      模板匹配找图 + 可选点击
    POST /wda/cv/waitForImage    轮询等待图片出现
    POST /wda/cv/findColor       找色 + 可选点击
    POST /wda/cv/waitForColor    轮询等待颜色出现

坐标系约定（与服务端一致）：

- 截图 / ``rect`` / ``region`` 使用**设备原始像素**坐标；
- 结果里的 ``x`` / ``y`` 是**逻辑点**坐标，可直接用于点击；
- ``scale`` = 像素 / 逻辑点，即 ``pixelX / scale == x``。

时间单位：``timeout`` / ``interval`` / ``duration`` 全部是**毫秒**（服务端内部才除以 1000）。

几个服务端行为，本地已提前拦截，避免静默出错:

- ``mode`` / ``method`` / ``colorSpace`` / ``level`` 写错会被服务端**静默退回默认值**；
- ``format`` 只认 ``"jpeg"``，写 ``"jpg"`` 会静默变成 png；
- ``region`` 的 width / height 必须为正，服务端**不会**退化成整屏而是直接报错；
- ``taps`` 会被 clamp 到 1..3，``timeout`` 上限 60s、``interval`` 上限 5s；
- ``ocr`` 不走通用点击逻辑，没有 tap 能力——要点文字请用 ``find_text``。

典型用法::

    import wdap
    c = wdap.Client()

    c.cv.status()                                   # {'cvAvailable': True, ...}
    c.cv.snapshot("screen.png")                     # 存图做模板
    c.cv.ocr(languages=["zh-Hans", "en-US"])        # 全屏识字
    c.cv.find_text("登录", tap=True)                # 找字并点击
    c.cv.wait_for_text("加载完成", timeout=10000)   # 等待文字出现
    c.cv.match_image("tpl.png", tap=True)           # 找图并点击（可直接传路径）
    c.cv.find_color("#FF5522", color_space="hsv")   # 找色
"""

import base64
import io
import os
import re
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union

from wdap.exceptions import WDAError, WDARequestError

try:
    from typing import Literal
except ImportError:  # pragma: no cover
    Literal = str

__all__ = [
    "CV",
    "CVResult",
    "CVMatch",
    "CVStatus",
    "Point",
    "Region",
    "CVMatchMode",
    "CVColorSpace",
    "CVMatchMethod",
    "CVLevel",
]

Point = NamedTuple("Point", [("x", float), ("y", float)])
Region = NamedTuple("Region", [("x", float), ("y", float),
                               ("width", float), ("height", float)])
Rect = NamedTuple("Rect", [("x", float), ("y", float),
                           ("width", float), ("height", float)])

_HEX_COLOR_RE = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_DATA_URI_RE = re.compile(r"^data:image/\w+;base64,", re.I)


class CVMatchMode:
    """findText / waitForText 的匹配模式

    服务端对取值大小写不敏感，且遇到非法值会**静默退回** ``contains``，
    这里在本地先校验，避免写错模式而毫无察觉。
    """
    CONTAINS = "contains"
    EXACT = "exact"
    REGEX = "regex"

    ALL = ("contains", "exact", "regex")


class CVColorSpace:
    """findColor / waitForColor 的色彩空间（只认 ``hsv``，其余一律 rgb）"""
    RGB = "rgb"
    HSV = "hsv"

    ALL = ("rgb", "hsv")


class CVMatchMethod:
    """matchImage / waitForImage 的匹配算法"""
    CCOEFF = "ccoeff"
    CCORR = "ccorr"
    SQDIFF = "sqdiff"

    ALL = ("ccoeff", "ccorr", "sqdiff")


class CVLevel:
    """文字识别精度"""
    FAST = "fast"
    ACCURATE = "accurate"

    ALL = ("fast", "accurate")


def _check_choice(name: str, value: Optional[str], choices: Sequence[str]) -> Optional[str]:
    """校验枚举型参数，统一转小写"""
    if value is None:
        return None
    lowered = str(value).strip().lower()
    if lowered not in choices:
        raise ValueError("%s must be one of %s, got %r" % (name, list(choices), value))
    return lowered


# --------------------------------------------------------------------------- #
# 参数归一化
# --------------------------------------------------------------------------- #
def _normalize_region(
    region: Optional[Union[dict, Sequence, Region]]
) -> Optional[Dict[str, float]]:
    """把区域参数统一成服务端要求的**源图像素坐标** dict

    Args:
        region: None（整屏）/ dict{x,y,width,height} / (x, y, width, height)

    Raises:
        ValueError: width / height 缺失或非正数

    Note:
        width 与 height **必须同时给出且大于 0**。服务端拿到零尺寸区域会直接
        报错 ``The search region is outside of the source image bounds``，
        而不会退化成整屏搜索——这里提前校验，省一次往返。

        区域超出图像边界时服务端同样报上述错误，不会自动裁剪。
    """
    if region is None:
        return None
    if isinstance(region, dict):
        keys = {str(k).lower(): v for k, v in region.items()}
        value = {
            "x": keys.get("x", 0),
            "y": keys.get("y", 0),
            "width": keys.get("width", keys.get("w", 0)),
            "height": keys.get("height", keys.get("h", 0)),
        }
    elif isinstance(region, (list, tuple)) and len(region) == 4:
        x, y, w, h = region
        value = {"x": x, "y": y, "width": w, "height": h}
    else:
        raise TypeError(
            "region must be None, a dict or a 4-elements sequence (x, y, width, height), got %r"
            % (region,))

    if float(value["width"]) <= 0 or float(value["height"]) <= 0:
        raise ValueError(
            "region requires positive width and height in source image pixels, got %r"
            % (value,))
    return value


def _normalize_color(color: Union[str, Sequence]) -> Union[str, list]:
    """校验并归一化颜色参数

    Args:
        color: "#RRGGBB" / "#RGB" / "RRGGBB" 字符串，或 [r, g, b] 序列
    """
    if isinstance(color, str):
        if not _HEX_COLOR_RE.match(color.strip()):
            raise ValueError(
                "color must be a '#RRGGBB' or '#RGB' string, got %r" % color)
        return color.strip()
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        r, g, b = (int(color[0]), int(color[1]), int(color[2]))
        for name, value in (("red", r), ("green", g), ("blue", b)):
            if not 0 <= value <= 255:
                raise ValueError(
                    "color channel %s must be in 0..255, got %s" % (name, value))
        return [r, g, b]
    raise TypeError(
        "color must be a hex string or a [r, g, b] sequence, got %r" % (color,))


def _to_base64(template: Union[str, bytes, "os.PathLike", Any]) -> str:
    """把模板图转成服务端要求的 base64 字符串

    Args:
        template: 支持以下类型
            - str: 图片文件路径，或已是 base64 字符串（含 data URI 亦可）
            - bytes: 图片二进制内容
            - os.PathLike: 图片文件路径
            - PIL.Image.Image: Pillow 图片对象
            - 带 read() 的文件对象
    """
    # 文件对象
    if hasattr(template, "read"):
        return base64.b64encode(template.read()).decode()

    # Pillow 图片
    if hasattr(template, "save") and hasattr(template, "size"):
        buff = io.BytesIO()
        template.save(buff, format="PNG")
        return base64.b64encode(buff.getvalue()).decode()

    if isinstance(template, (bytes, bytearray)):
        return base64.b64encode(bytes(template)).decode()

    if isinstance(template, os.PathLike):
        template = os.fspath(template)

    if isinstance(template, str):
        # 优先当文件路径处理
        if os.path.isfile(template):
            with open(template, "rb") as fp:
                return base64.b64encode(fp.read()).decode()
        value = _DATA_URI_RE.sub("", template.strip())
        # 校验是否像 base64
        if re.match(r"^[A-Za-z0-9+/\r\n]+={0,2}$", value):
            return value
        raise ValueError(
            "template is neither an existing file path nor a valid base64 string: %r"
            % template[:64])

    raise TypeError("Unsupported template type: %r" % type(template))


def _clean(data: Dict[str, Any]) -> Dict[str, Any]:
    """去掉值为 None 的键，避免把 null 传给服务端"""
    return {k: v for k, v in data.items() if v is not None}


# --------------------------------------------------------------------------- #
# 结果封装
# --------------------------------------------------------------------------- #
class CVUnsupportedError(WDAError):
    """设备上运行的 WDA 不是带 CV/Vision 的构建

    服务端返回 ``status=110 / unknown command / Unhandled endpoint`` 时抛出，
    含义是这条路由**根本没有注册**，而不是调用姿势不对——通常是设备上装的
    WDA 不是用 ``wda_cv_vision`` 这份源码编译的（或没重新编译部署）。
    """

#: 110 = FBCommandStatus 的 unknown command
_UNKNOWN_COMMAND_STATUS = 110

_CV_UNSUPPORTED_HINT = (
    "当前设备上的 WDA 没有注册 %s 这条路由。\n"
    "这不是调用姿势问题，而是设备上运行的 WDA 二进制不含 CV/Vision 支持，常见原因：\n"
    "  1. 设备上的 WDA 不是用 wda_cv_vision 这份源码编译的（例如用了 tidevice/官方预编译包）；\n"
    "  2. 源码更新后没有重新编译并部署到设备（xcodebuild test / tidevice xctest）；\n"
    "  3. 部署的 bundle id 指向了设备上残留的旧 WDA App。\n"
    "验证方法：curl http://<device>:8100/status 看 build 信息，"
    "或 curl http://<device>:8100/wda/cv/status —— 带 CV 的构建会返回 "
    "{'cvAvailable': ..., 'visionAvailable': ...} 而不是 unknown command。"
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
    raise CVUnsupportedError(_CV_UNSUPPORTED_HINT % path) from err


class CVMatch(object):
    """单条命中结果

    文字类结果额外有 ``text`` / ``confidence`` / ``char_boxes`` / ``normalized_rect``，
    图像/颜色类结果额外有 ``score`` / ``matched_scale``。
    """

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw or {}

    @property
    def raw(self) -> Dict[str, Any]:
        """原始响应字典"""
        return self._raw

    @property
    def x(self) -> float:
        """逻辑点坐标 X，可直接用于点击"""
        return self._raw.get("x", 0.0)

    @property
    def y(self) -> float:
        """逻辑点坐标 Y，可直接用于点击"""
        return self._raw.get("y", 0.0)

    @property
    def pixel_x(self) -> float:
        """截图像素坐标 X"""
        return self._raw.get("pixelX", 0.0)

    @property
    def pixel_y(self) -> float:
        """截图像素坐标 Y"""
        return self._raw.get("pixelY", 0.0)

    @property
    def center(self) -> Point:
        """逻辑点中心坐标"""
        return Point(self.x, self.y)

    @property
    def pixel_center(self) -> Point:
        """像素中心坐标"""
        return Point(self.pixel_x, self.pixel_y)

    @property
    def rect(self) -> Rect:
        """像素坐标系下的矩形"""
        r = self._raw.get("rect") or {}
        return Rect(r.get("x", 0.0), r.get("y", 0.0),
                    r.get("width", 0.0), r.get("height", 0.0))

    @property
    def normalized_rect(self) -> Optional[Rect]:
        """归一化到 0..1 的矩形（仅文字识别接口返回）"""
        r = self._raw.get("normalizedRect")
        if not r:
            return None
        return Rect(r.get("x", 0.0), r.get("y", 0.0),
                    r.get("width", 0.0), r.get("height", 0.0))

    @property
    def score(self) -> Optional[float]:
        """匹配得分 / 文字置信度"""
        value = self._raw.get("score")
        return None if value is None else float(value)

    @property
    def confidence(self) -> Optional[float]:
        """文字识别置信度（仅文字接口）"""
        value = self._raw.get("confidence")
        return None if value is None else float(value)

    @property
    def text(self) -> Optional[str]:
        """识别到的文字（仅文字接口）"""
        return self._raw.get("text")

    @property
    def char_boxes(self) -> List[Dict[str, Any]]:
        """逐字包围盒（char_boxes=True 时才有内容）"""
        return self._raw.get("charBoxes") or []

    @property
    def matched_scale(self) -> Optional[float]:
        """命中时模板被缩放的比例（仅 matchImage 系列）"""
        value = self._raw.get("scale")
        return None if value is None else float(value)

    @property
    def tapped(self) -> bool:
        """这一条是否被点击过"""
        return bool(self._raw.get("tapped", False))

    def __repr__(self):
        if self.text is not None:
            return "<CVMatch text=%r center=(%.1f, %.1f) confidence=%s>" % (
                self.text, self.x, self.y, self.confidence)
        return "<CVMatch score=%s center=(%.1f, %.1f) rect=%s>" % (
            self.score, self.x, self.y, self.rect)


class CVStatus(object):
    """/wda/cv/status 的返回：当前 WDA 包是否具备图像能力"""

    def __init__(self, raw: Dict[str, Any]):
        self._raw = raw or {}

    @property
    def raw(self) -> Dict[str, Any]:
        return self._raw

    @property
    def cv_available(self) -> bool:
        """是否带 OpenCV（找图 / 找色）"""
        return bool(self._raw.get("cvAvailable", False))

    @property
    def vision_available(self) -> bool:
        """是否支持 Vision 文字识别"""
        return bool(self._raw.get("visionAvailable", False))

    @property
    def opencv_version(self) -> str:
        """OpenCV 版本号，未接入时为空串"""
        return self._raw.get("opencvVersion") or ""

    @property
    def scale(self) -> float:
        """设备屏幕缩放比（像素 / 逻辑点）"""
        return float(self._raw.get("scale", 1.0))

    @property
    def available(self) -> bool:
        """是否具备任意一项图像能力（找图找色 或 文字识别）"""
        return self.cv_available or self.vision_available

    def __bool__(self):
        # 注意：这里只表示"至少有一样能力"。要调 match_image/find_color
        # 必须看 cv_available，要调 ocr/find_text 必须看 vision_available
        return self.available

    def __repr__(self):
        return ("<CVStatus cv=%s vision=%s opencv=%r scale=%s>" % (
            self.cv_available, self.vision_available,
            self.opencv_version, self.scale))


class CVResult(object):
    """查找类接口的统一返回

    - ``found``: 是否命中（``count > 0``）
    - ``results``: 命中列表，元素为 :class:`CVMatch`
    - 未命中 / 等待超时都返回 HTTP 200，用 ``found`` 判断即可
    """

    def __init__(self, value: Union[Dict[str, Any], Any]):
        raw = dict(value or {})
        self._raw = raw
        self.results: List[CVMatch] = [CVMatch(item) for item in raw.get("results") or []]

    # --- 基础字段 ---
    @property
    def raw(self) -> Dict[str, Any]:
        """原始响应字典"""
        return self._raw

    @property
    def scale(self) -> float:
        """像素 / 逻辑点的换算比例"""
        return float(self._raw.get("scale", 1.0))

    @property
    def image_size(self) -> Tuple[float, float]:
        """截图原始像素尺寸 (width, height)"""
        size = self._raw.get("imageSize") or {}
        return (float(size.get("width", 0.0)), float(size.get("height", 0.0)))

    @property
    def count(self) -> int:
        return int(self._raw.get("count", len(self.results)))

    @property
    def found(self) -> bool:
        return bool(self._raw.get("found", self.count > 0))

    @property
    def tapped(self) -> bool:
        """是否执行了点击"""
        return bool(self._raw.get("tapped", False))

    @property
    def waited(self) -> Optional[float]:
        """实际等待的毫秒数（仅 wait* 接口返回）"""
        value = self._raw.get("waited")
        return None if value is None else float(value)

    # --- 结果访问 ---
    @property
    def first(self) -> Optional[CVMatch]:
        """第一条结果，没有则 None"""
        return self.results[0] if self.results else None

    @property
    def tapped_item(self) -> Optional[CVMatch]:
        """被点击的那一条结果"""
        for item in self.results:
            if item.tapped:
                return item
        return None

    @property
    def texts(self) -> List[str]:
        """所有结果的文字（图片/颜色结果会被跳过）"""
        return [item.text for item in self.results if item.text is not None]

    def center(self, index: int = 0) -> Optional[Point]:
        """第 index 条结果的逻辑点中心坐标"""
        if index >= len(self.results):
            return None
        return self.results[index].center

    # --- 调试图 ---
    @property
    def debug_image(self) -> Optional[bytes]:
        """debug=True 时返回的标注图（JPEG 二进制），否则 None"""
        data = self._raw.get("debugImage")
        if not data:
            return None
        return base64.b64decode(data)

    def save_debug_image(self, path: str) -> bool:
        """保存标注图，没有则返回 False"""
        data = self.debug_image
        if not data:
            return False
        with open(path, "wb") as fp:
            fp.write(data)
        return True

    # --- 魔法方法 ---
    def __bool__(self):
        return self.found

    def __len__(self):
        return len(self.results)

    def __iter__(self):
        return iter(self.results)

    def __getitem__(self, index):
        return self.results[index]

    def __repr__(self):
        return ("<CVResult found=%s count=%d scale=%s waited=%s>" % (
            self.found, self.count, self.scale, self.waited))


# --------------------------------------------------------------------------- #
# CV 客户端
# --------------------------------------------------------------------------- #
class CV(object):
    """CV / Vision 接口集合，通过 ``client.cv`` 访问

    除 :meth:`status` 之外，所有接口都需要已建立的 session。
    """

    #: wait* 接口的服务端超时上限（毫秒）。超出会被服务端静默截断到 60s
    MAX_TIMEOUT_MS = 60000.0
    #: wait* 接口的服务端轮询间隔上限（毫秒）。超出会被截断到 5s
    MAX_INTERVAL_MS = 5000.0
    #: 单次点击的连击次数上限，服务端会 clamp 到 1..3
    MAX_TAPS = 3

    def __init__(self, client):
        self._client = client

    # ------------------------------------------------------------------ #
    # 通用参数拼装
    # ------------------------------------------------------------------ #
    @staticmethod
    def _common(region=None,
                index: Optional[int] = None,
                tap: Optional[bool] = None,
                duration: Optional[float] = None,
                taps: Optional[int] = None,
                debug: Optional[bool] = None) -> Dict[str, Any]:
        """查找类接口共有的参数

        只有走 ``respondForMatches`` 的接口（findText / waitForText /
        matchImage / waitForImage / findColor / waitForColor）才认 ``index`` /
        ``tap`` / ``duration`` / ``taps``；``ocr`` 走的是独立的
        ``handleRecognizeText``，传这些会被**静默忽略**，所以 :meth:`ocr` 不调用本方法。
        """
        if taps is not None and not 1 <= int(taps) <= CV.MAX_TAPS:
            raise ValueError("taps must be in 1..%d, got %r" % (CV.MAX_TAPS, taps))
        return _clean({
            "region": _normalize_region(region),
            "index": index,
            "tap": tap,
            "duration": duration,
            "taps": taps,
            "debug": debug,
        })

    @staticmethod
    def _wait(timeout: Optional[float], interval: Optional[float]) -> Dict[str, Any]:
        """wait* 接口的轮询参数

        Args:
            timeout: 最长等待时间，**毫秒**（服务端 ``/1000.0`` 后 clamp 到 0..60s）
            interval: 两次截图间隔，**毫秒**（服务端 clamp 到 0.02..5s）

        默认值与服务端一致：timeout=5000ms、interval=300ms。
        """
        for name, value, upper in (("timeout", timeout, CV.MAX_TIMEOUT_MS),
                                   ("interval", interval, CV.MAX_INTERVAL_MS)):
            if value is None:
                continue
            if value < 0:
                raise ValueError("%s must be >= 0 ms, got %r" % (name, value))
            if value > upper:
                raise ValueError("%s must be <= %s ms (server clamps it), got %r"
                                 % (name, upper, value))
        return _clean({"timeout": timeout, "interval": interval})

    @staticmethod
    def _text_common(level: Optional[str],
                     languages: Optional[Sequence[str]],
                     language_correction: Optional[bool],
                     minimum_text_height: Optional[float],
                     char_boxes: Optional[bool]) -> Dict[str, Any]:
        """文字识别类接口共有的参数

        ``minimum_text_height`` 是相对图高的比例 0..1，服务端会 clamp 到单位区间。
        ``languages`` 只接受字符串数组，空数组按"未指定"处理（系统自动判定）。
        """
        if minimum_text_height is not None:
            if not 0.0 <= float(minimum_text_height) <= 1.0:
                raise ValueError("minimum_text_height must be in 0..1, got %r"
                                 % minimum_text_height)
        langs = [str(item) for item in languages] if languages else None
        return _clean({
            "level": _check_choice("level", level, CVLevel.ALL),
            "languages": langs,
            "languageCorrection": language_correction,
            "minimumTextHeight": minimum_text_height,
            "charBoxes": char_boxes,
        })

    @staticmethod
    def _image_common(template: Union[str, bytes, Any],
                      threshold: Optional[float],
                      method: Optional[str],
                      max_results: Optional[int],
                      scale_min: Optional[float],
                      scale_max: Optional[float],
                      scale_steps: Optional[int],
                      use_mask: Optional[bool]) -> Dict[str, Any]:
        """matchImage / waitForImage 共有的参数

        多尺度匹配需要同时满足 ``scale_steps >= 2`` 且 ``scale_max > scale_min > 0``，
        否则服务端只在原始尺寸上匹配一次（ scales = [1.0] ）。
        缩放步数服务端上限为 32。
        """
        if threshold is not None and not 0.0 <= float(threshold) <= 1.0:
            raise ValueError("threshold must be in 0..1, got %r" % threshold)
        if max_results is not None and int(max_results) < 1:
            raise ValueError("max_results must be >= 1, got %r" % max_results)
        return _clean({
            "template": _to_base64(template),
            "threshold": threshold,
            "method": _check_choice("method", method, CVMatchMethod.ALL),
            "maxResults": max_results,
            "scaleMin": scale_min,
            "scaleMax": scale_max,
            "scaleSteps": scale_steps,
            "useMask": use_mask,
        })

    @staticmethod
    def _color_common(color: Union[str, Sequence],
                      tolerance: Optional[float],
                      color_space: Optional[str],
                      min_area: Optional[int],
                      max_results: Optional[int]) -> Dict[str, Any]:
        """findColor / waitForColor 共有的参数"""
        if tolerance is not None and not 0 <= int(tolerance) <= 255:
            raise ValueError("tolerance must be in 0..255, got %r" % tolerance)
        if max_results is not None and int(max_results) < 1:
            raise ValueError("max_results must be >= 1, got %r" % max_results)
        if min_area is not None and int(min_area) < 0:
            raise ValueError("min_area must be >= 0, got %r" % min_area)
        return _clean({
            "color": _normalize_color(color),
            "tolerance": tolerance,
            "colorSpace": _check_choice("colorSpace", color_space, CVColorSpace.ALL),
            "minArea": min_area,
            "maxResults": max_results,
        })

    def _post(self, path: str, data: Dict[str, Any], timeout: Optional[float] = None) -> CVResult:
        try:
            value = self._client._session_http.post(path, data=data, timeout=timeout).value
        except WDARequestError as err:
            _raise_if_unsupported(path, err)
            raise
        return CVResult(value)

    # ------------------------------------------------------------------ #
    # 1. 能力探测
    # ------------------------------------------------------------------ #
    def status(self) -> CVStatus:
        """探测当前 WDA 构建是否具备 OpenCV / Vision 能力（无需 session）

        Returns:
            CVStatus: ``cv_available`` 为真才可用找图/找色接口

        Example::

            if c.cv.status().cv_available:
                c.cv.match_image("tpl.png", tap=True)

        Raises:
            CVUnsupportedError: 设备上的 WDA 未编译 CV 支持（路由不存在）
        """
        try:
            value = self._client.http.get("/wda/cv/status").value
        except WDARequestError as err:
            _raise_if_unsupported("/wda/cv/status", err)
            raise
        return CVStatus(value)

    # ------------------------------------------------------------------ #
    # 2. 取截图（制作模板）
    # ------------------------------------------------------------------ #
    def snapshot_raw(self,
                     format: str = "png",
                     quality: Optional[float] = None,
                     max_width: Optional[float] = None) -> Dict[str, Any]:
        """取当前截图，返回完整响应 value（含 format / scale / imageSize / returnedSize / data）

        服务端只认 ``"jpeg"`` 这一个非 png 取值，写 ``"jpg"`` 会被静默当成 png，
        这里做了归一化。
        """
        fmt = _check_choice("format", format, ("png", "jpeg", "jpg"))
        if fmt == "jpg":
            fmt = "jpeg"
        if quality is not None and not 0.0 <= float(quality) <= 1.0:
            raise ValueError("quality must be in 0..1, got %r" % quality)
        data = _clean({
            "format": fmt,
            "quality": quality,
            "maxWidth": max_width,
        })
        try:
            return self._client._session_http.post("/wda/cv/snapshot", data=data).value
        except WDARequestError as err:
            _raise_if_unsupported("/wda/cv/snapshot", err)
            raise

    def snapshot(self,
                 path: Optional[str] = None,
                 format: str = "png",
                 quality: Optional[float] = None,
                 max_width: Optional[float] = None) -> bytes:
        """取当前设备的**原始分辨率**截图（不带状态栏合成、不缩放）

        Args:
            path: 可选，保存到该文件
            format: "png"（无损，适合做模板，默认）或 "jpeg"/"jpg"
            quality: 仅 jpeg 生效，0..1，服务端默认 0.9
            max_width: 大于 0 时按比例缩放到该宽度，减小传输体积；
                       注意返回的 ``returnedSize`` 会与 ``imageSize`` 不同

        Returns:
            图片二进制内容

        Example::

            c.cv.snapshot("screen.png")          # 存下来裁剪模板
            data = c.cv.snapshot(max_width=400)  # 缩略图
        """
        value = self.snapshot_raw(format=format, quality=quality, max_width=max_width)
        content = base64.b64decode(value["data"])
        if path:
            with open(path, "wb") as fp:
                fp.write(content)
        return content

    # ------------------------------------------------------------------ #
    # 3. OCR
    # ------------------------------------------------------------------ #
    def ocr(self,
            region: Optional[Union[dict, Sequence]] = None,
            level: str = CVLevel.ACCURATE,
            languages: Optional[Sequence[str]] = None,
            language_correction: Optional[bool] = None,
            minimum_text_height: Optional[float] = None,
            char_boxes: Optional[bool] = None,
            debug: Optional[bool] = None) -> CVResult:
        """识别当前屏幕的全部文字（不做过滤，不点击）

        服务端实现是 ``handleRecognizeText``，**不经过**通用的
        ``respondForMatches``，所以本方法没有 ``index`` / ``tap`` / ``duration`` /
        ``taps`` 参数——要"找到并点击"请用 :meth:`find_text`。

        Args:
            region: 只识别该像素区域，可显著提速
            level: "accurate"（准但慢，默认）或 "fast"
            languages: BCP-47 语言列表，如 ["zh-Hans", "en-US"]；
                       留空则由系统自动判定
            language_correction: 关闭后可拿到未经校正的原文，适合验证码/数字串
            minimum_text_height: 相对图高的最小文字高度 0..1，过滤噪点
            char_boxes: 是否返回逐字包围盒，用于点某个具体字
            debug: 额外返回标注图

        Returns:
            CVResult，结果按从上到下、从左到右排序

        Example::

            for item in c.cv.ocr(languages=["zh-Hans"]):
                print(item.text, item.center)
        """
        data = _clean({"region": _normalize_region(region), "debug": debug})
        data.update(self._text_common(level, languages, language_correction,
                                      minimum_text_height, char_boxes))
        return self._post("/wda/vision/ocr", data)

    # ------------------------------------------------------------------ #
    # 4. findText
    # ------------------------------------------------------------------ #
    def find_text(self,
                  text: str,
                  mode: str = CVMatchMode.CONTAINS,
                  region: Optional[Union[dict, Sequence]] = None,
                  index: Optional[int] = None,
                  tap: Optional[bool] = None,
                  duration: Optional[float] = None,
                  taps: Optional[int] = None,
                  debug: Optional[bool] = None,
                  level: Optional[str] = None,
                  languages: Optional[Sequence[str]] = None,
                  language_correction: Optional[bool] = None,
                  minimum_text_height: Optional[float] = None,
                  char_boxes: Optional[bool] = None) -> CVResult:
        """按文字查找，可选点击

        Args:
            text: 要查找的文字（必填）
            mode: "contains" 包含 / "exact" 完全相等 / "regex" 正则（忽略大小写）
            index: 命中第 N 个结果时点击，默认 0；越界会报服务端错误
            tap: 是否点击 index 对应结果的中心
            duration: 手指按下持续时间（毫秒），500+ 即长按
            taps: 连击次数 1..3
            其余参数同 :meth:`ocr`

        Returns:
            CVResult，未命中时 found=False（HTTP 仍为 200）
        """
        if not isinstance(text, str) or not text:
            raise ValueError("text is required and must be a non-empty string")
        data = self._common(region=region, index=index, tap=tap,
                            duration=duration, taps=taps, debug=debug)
        data.update(self._text_common(level, languages, language_correction,
                                      minimum_text_height, char_boxes))
        data["text"] = text
        data["mode"] = _check_choice("mode", mode, CVMatchMode.ALL)
        return self._post("/wda/vision/findText", data)

    # ------------------------------------------------------------------ #
    # 5. waitForText
    # ------------------------------------------------------------------ #
    def wait_for_text(self,
                      text: str,
                      timeout: float = 5000,
                      interval: float = 300,
                      mode: str = CVMatchMode.CONTAINS,
                      region: Optional[Union[dict, Sequence]] = None,
                      index: Optional[int] = None,
                      tap: Optional[bool] = None,
                      duration: Optional[float] = None,
                      taps: Optional[int] = None,
                      debug: Optional[bool] = None,
                      level: Optional[str] = None,
                      languages: Optional[Sequence[str]] = None,
                      language_correction: Optional[bool] = None,
                      minimum_text_height: Optional[float] = None,
                      char_boxes: Optional[bool] = None) -> CVResult:
        """轮询等待文字出现

        Args:
            timeout: 最长等待时间（毫秒），上限 60000
            interval: 两次截图之间的间隔（毫秒）
            其余参数同 :meth:`find_text`

        Returns:
            CVResult，超时未命中时 found=False（不报错）
        """
        if not isinstance(text, str) or not text:
            raise ValueError("text is required and must be a non-empty string")
        data = self._common(region=region, index=index, tap=tap,
                            duration=duration, taps=taps, debug=debug)
        data.update(self._text_common(level, languages, language_correction,
                                      minimum_text_height, char_boxes))
        data["text"] = text
        data["mode"] = _check_choice("mode", mode, CVMatchMode.ALL)
        data.update(self._wait(timeout, interval))
        # 留足 HTTP 超时余量（wait* 跑在服务端独立队列上）
        return self._post("/wda/vision/waitForText", data,
                          timeout=max(60.0, timeout / 1000.0 + 30.0))

    # ------------------------------------------------------------------ #
    # 6. matchImage
    # ------------------------------------------------------------------ #
    def match_image(self,
                    template: Union[str, bytes, Any],
                    threshold: Optional[float] = None,
                    method: Optional[str] = None,
                    max_results: Optional[int] = None,
                    region: Optional[Union[dict, Sequence]] = None,
                    scale_min: Optional[float] = None,
                    scale_max: Optional[float] = None,
                    scale_steps: Optional[int] = None,
                    use_mask: Optional[bool] = None,
                    index: Optional[int] = None,
                    tap: Optional[bool] = None,
                    duration: Optional[float] = None,
                    taps: Optional[int] = None,
                    debug: Optional[bool] = None) -> CVResult:
        """模板匹配找图，可选点击

        Args:
            template: 模板图，可为文件路径 / bytes / base64 字符串 / PIL.Image / 文件对象
            threshold: 匹配阈值 0..1，越高越严格，默认 0.8
            method: "ccoeff" / "ccorr" / "sqdiff"，默认 ccoeff
            max_results: 最多返回几个结果，按得分降序，默认 5
            region: 限定搜索区域（像素坐标），显著提速
            scale_min / scale_max: 多尺度匹配的缩放区间，默认 1.0
            scale_steps: 缩放步数。只在 ``>= 2`` 且 ``scale_max > scale_min > 0``
                         时才真的做多尺度，否则等价于只匹配原尺寸；
                         服务端上限 32
            use_mask: 用模板 alpha 通道作掩膜；仅对 sqdiff / ccorr 生效，
                      与 ccoeff 同时指定会自动降级为 ccorr
            index / tap / duration / taps / debug: 通用参数

        Returns:
            CVResult，结果的 ``matched_scale`` 是命中时模板被缩放的比例
        """
        data = self._common(region=region, index=index, tap=tap,
                            duration=duration, taps=taps, debug=debug)
        data.update(self._image_common(template, threshold, method, max_results,
                                       scale_min, scale_max, scale_steps, use_mask))
        return self._post("/wda/cv/matchImage", data)

    # ------------------------------------------------------------------ #
    # 7. waitForImage
    # ------------------------------------------------------------------ #
    def wait_for_image(self,
                       template: Union[str, bytes, Any],
                       timeout: float = 5000,
                       interval: float = 300,
                       threshold: Optional[float] = None,
                       method: Optional[str] = None,
                       max_results: Optional[int] = None,
                       region: Optional[Union[dict, Sequence]] = None,
                       scale_min: Optional[float] = None,
                       scale_max: Optional[float] = None,
                       scale_steps: Optional[int] = None,
                       use_mask: Optional[bool] = None,
                       index: Optional[int] = None,
                       tap: Optional[bool] = None,
                       duration: Optional[float] = None,
                       taps: Optional[int] = None,
                       debug: Optional[bool] = None) -> CVResult:
        """轮询等待图片出现，参数 = :meth:`match_image` + timeout / interval"""
        data = self._common(region=region, index=index, tap=tap,
                            duration=duration, taps=taps, debug=debug)
        data.update(self._image_common(template, threshold, method, max_results,
                                       scale_min, scale_max, scale_steps, use_mask))
        data.update(self._wait(timeout, interval))
        return self._post("/wda/cv/waitForImage", data,
                          timeout=max(60.0, timeout / 1000.0 + 30.0))

    # ------------------------------------------------------------------ #
    # 8. findColor
    # ------------------------------------------------------------------ #
    def find_color(self,
                   color: Union[str, Sequence],
                   tolerance: Optional[float] = None,
                   color_space: Optional[str] = None,
                   region: Optional[Union[dict, Sequence]] = None,
                   min_area: Optional[int] = None,
                   max_results: Optional[int] = None,
                   index: Optional[int] = None,
                   tap: Optional[bool] = None,
                   duration: Optional[float] = None,
                   taps: Optional[int] = None,
                   debug: Optional[bool] = None) -> CVResult:
        """按颜色 + 容差查找色块质心，可选点击

        Args:
            color: "#RRGGBB" / "#RGB" 字符串，或 [r, g, b] 数组（必填）
            tolerance: 每个通道允许的最大偏差 0..255，默认 10
            color_space: "rgb"（快但对亮度敏感）或 "hsv"（对光照鲁棒），默认 rgb
            min_area: 色块最小像素面积，更小的忽略，默认 1
            max_results: 最多返回几个结果，按面积降序，默认 5
            index / tap / duration / taps / debug: 通用参数

        Note:
            ``score`` 是色块在外接矩形中的填充率（越接近 1 越实心），不是匹配置信度；
            中心点是色块质心，不是外接矩形几何中心。
        """
        data = self._common(region=region, index=index, tap=tap,
                            duration=duration, taps=taps, debug=debug)
        data.update(self._color_common(color, tolerance, color_space,
                                       min_area, max_results))
        return self._post("/wda/cv/findColor", data)

    # ------------------------------------------------------------------ #
    # 9. waitForColor
    # ------------------------------------------------------------------ #
    def wait_for_color(self,
                       color: Union[str, Sequence],
                       timeout: float = 5000,
                       interval: float = 300,
                       tolerance: Optional[float] = None,
                       color_space: Optional[str] = None,
                       region: Optional[Union[dict, Sequence]] = None,
                       min_area: Optional[int] = None,
                       max_results: Optional[int] = None,
                       index: Optional[int] = None,
                       tap: Optional[bool] = None,
                       duration: Optional[float] = None,
                       taps: Optional[int] = None,
                       debug: Optional[bool] = None) -> CVResult:
        """轮询等待颜色出现，参数 = :meth:`find_color` + timeout / interval"""
        data = self._common(region=region, index=index, tap=tap,
                            duration=duration, taps=taps, debug=debug)
        data.update(self._color_common(color, tolerance, color_space,
                                       min_area, max_results))
        data.update(self._wait(timeout, interval))
        return self._post("/wda/cv/waitForColor", data,
                          timeout=max(60.0, timeout / 1000.0 + 30.0))

    # ------------------------------------------------------------------ #
    # 便捷方法
    # ------------------------------------------------------------------ #
    def tap_text(self, text: str, mode: str = CVMatchMode.CONTAINS,
                 index: int = 0,
                 duration: Optional[float] = None,
                 taps: Optional[int] = None,
                 region: Optional[Union[dict, Sequence]] = None) -> bool:
        """找文字并点击，返回是否真的点了"""
        return self.find_text(text, mode=mode, index=index, tap=True,
                              duration=duration, taps=taps, region=region).tapped

    def tap_image(self, template: Union[str, bytes, Any],
                  threshold: Optional[float] = None,
                  index: int = 0,
                  duration: Optional[float] = None,
                  taps: Optional[int] = None,
                  region: Optional[Union[dict, Sequence]] = None,
                  scale_min: Optional[float] = None,
                  scale_max: Optional[float] = None,
                  scale_steps: Optional[int] = None) -> bool:
        """找图并点击，返回是否真的点了"""
        return self.match_image(template, threshold=threshold, index=index, tap=True,
                                duration=duration, taps=taps, region=region,
                                scale_min=scale_min, scale_max=scale_max,
                                scale_steps=scale_steps).tapped

    def tap_color(self, color: Union[str, Sequence],
                  tolerance: Optional[float] = None,
                  color_space: Optional[str] = None,
                  index: int = 0,
                  duration: Optional[float] = None,
                  taps: Optional[int] = None,
                  region: Optional[Union[dict, Sequence]] = None) -> bool:
        """找色并点击，返回是否真的点了"""
        return self.find_color(color, tolerance=tolerance, color_space=color_space,
                               index=index, tap=True, duration=duration,
                               taps=taps, region=region).tapped

    def __repr__(self):
        return "<CV client=%r>" % (self._client,)
