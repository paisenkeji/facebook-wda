#
# 说明:
#   本文件中的 AppiumSettings 枚举与 WebDriverAgent 服务端
#   `WebDriverAgentLib/Utilities/FBSettings.m` 中的 FB_SETTING_* 常量一一对应。
#   端点: GET/POST /session/$sessionId/appium/settings

__all__ = [
    'AppiumSettings',
    'AlertAction',
    'APPIUM_SETTINGS_SPEC',
    'AppiumSettingSpec',
    'validate_appium_settings',
]


def validate_appium_settings(settings: dict, strict: bool = True) -> dict:
    """校验并规范化 appium settings，避免把无效参数静默丢给服务端

    服务端对未识别的 key 是静默忽略的（不报错），客户端很容易因为拼错 key
    而误以为设置生效。这个函数在本地就把问题暴露出来。

    Args:
        settings: 待设置的字典，key 可以是 AppiumSettings 枚举或字符串
        strict: True 时遇到未知 key 直接抛 ValueError；False 时仅保留已知 key

    Returns:
        规范化后的 dict（key 统一为字符串，value 转成服务端期望的类型）

    Raises:
        ValueError: key 未知，或 value 类型不符合 spec
    """
    if not isinstance(settings, dict):
        raise TypeError("settings must be a dict, got %r" % type(settings))

    result = {}
    for key, value in settings.items():
        name = key.value if isinstance(key, AppiumSettings) else str(key)
        spec = APPIUM_SETTINGS_SPEC.get(name)
        if spec is None:
            if strict:
                raise ValueError(
                    "Unknown appium setting %r, valid keys: %s"
                    % (name, ", ".join(sorted(APPIUM_SETTINGS_SPEC))))
            continue

        if value is None:
            if not spec.clearable:
                raise ValueError(
                    "The setting %r cannot be cleared with None, only %s support it"
                    % (name, ", ".join(k for k, s in APPIUM_SETTINGS_SPEC.items() if s.clearable)))
            result[name] = None
            continue

        if spec.type == "bool":
            if isinstance(value, bool):
                result[name] = value
            elif isinstance(value, (int, float)) and value in (0, 1):
                result[name] = bool(value)
            elif isinstance(value, str) and value.lower() in ("true", "false", "yes", "no", "0", "1"):
                result[name] = value.lower() in ("true", "yes", "1")
            else:
                raise ValueError("The setting %r expects a bool, got %r" % (name, value))
        elif spec.type == "int":
            result[name] = int(value)
        elif spec.type == "float":
            result[name] = float(value)
        else:
            result[name] = value if isinstance(value, str) else str(value)
    return result


import enum
from typing import NamedTuple, Optional


class AppiumSettings(str, enum.Enum):
    """
    WDA 支持的全部 appium settings（对应服务端 FBSettingsHandler 的 settersMap/gettersMap）

    用法::

        c.appium_settings({AppiumSettings.SnapshotMaxDepth.value: 30})
        c.appium_settings()  # 读取当前全部设置

    通过 GET /appium/settings 读取时，服务端返回形如::

        {'shouldUseCompactResponses': True,
         'elementResponseAttributes': 'type,label',
         'mjpegServerScreenshotQuality': 25,
         'mjpegServerFramerate': 10,
         'mjpegScalingFactor': 100,
         'mjpegFixOrientation': False,
         'screenshotQuality': 1,
         'keyboardAutocorrection': 0,
         'keyboardPrediction': 0,
         'snapshotMaxDepth': 50,
         'snapshotMaxChildren': 2147483647,
         'waitForIdleTimeout': 10.0,
         'animationCoolOffTimeout': 2.0,
         'accessibilityDeadline': 120.0,
         'useFirstMatch': False,
         'boundElementsByIndex': False,
         'reduceMotion': False,
         'defaultActiveApplication': 'auto',
         'activeAppDetectionPoint': '64.00,64.00',
         'acceptAlertButtonSelector': '',
         'dismissAlertButtonSelector': '',
         'screenshotOrientation': 'auto',
         'maxTypingFrequency': 60,
         'respectSystemAlerts': False,
         'useClearTextShortcut': True,
         'defaultAlertAction': '',
         'autoClickAlertSelector': ''}

    注意: 服务端对未识别的 key 静默忽略，对值为 null 的 key 仅
    defaultAlertAction / acceptAlertButtonSelector / dismissAlertButtonSelector /
    autoClickAlertSelector 允许清空，其余一律跳过。
    """

    # --- 元素查找与 page source ---
    ShouldUseCompactResponses = "shouldUseCompactResponses"
    ElementResponseAttributes = "elementResponseAttributes"
    SnapshotMaxDepth = "snapshotMaxDepth"
    SnapshotMaxChildren = "snapshotMaxChildren"
    UseFirstMatch = "useFirstMatch"
    BoundElementsByIndex = "boundElementsByIndex"
    LimitXPathContextScope = "limitXPathContextScope"
    EnforceCustomSnapshots = "enforceCustomSnapshots"
    IncludeHittableInPageSource = "includeHittableInPageSource"
    IncludeNativeFrameInPageSource = "includeNativeFrameInPageSource"
    IncludeNativeAccessibilityElementInPageSource = "includeNativeAccessibilityElementInPageSource"
    IncludeMinMaxValueInPageSource = "includeMinMaxValueInPageSource"
    IncludeCustomActionsInPageSource = "includeCustomActionsInPageSource"

    # --- 截图与 MJPEG ---
    ScreenshotQuality = "screenshotQuality"
    ScreenshotOrientation = "screenshotOrientation"
    MjpegServerScreenshotQuality = "mjpegServerScreenshotQuality"
    MjpegServerFramerate = "mjpegServerFramerate"
    MjpegScalingFactor = "mjpegScalingFactor"
    MjpegFixOrientation = "mjpegFixOrientation"

    # --- 键盘 ---
    KeyboardAutocorrection = "keyboardAutocorrection"
    KeyboardPrediction = "keyboardPrediction"
    MaxTypingFrequency = "maxTypingFrequency"
    UseClearTextShortcut = "useClearTextShortcut"

    # --- 等待 / 动画 / 无障碍 ---
    WaitForIdleTimeout = "waitForIdleTimeout"
    AnimationCoolOffTimeout = "animationCoolOffTimeout"
    AccessibilityDeadline = "accessibilityDeadline"
    ReduceMotion = "reduceMotion"

    # --- 应用与弹窗 ---
    DefaultActiveApplication = "defaultActiveApplication"
    ActiveAppDetectionPoint = "activeAppDetectionPoint"
    DefaultAlertAction = "defaultAlertAction"
    AcceptAlertButtonSelector = "acceptAlertButtonSelector"
    DismissAlertButtonSelector = "dismissAlertButtonSelector"
    AutoClickAlertSelector = "autoClickAlertSelector"
    RespectSystemAlerts = "respectSystemAlerts"


class AppiumSettingSpec(NamedTuple):
    """单个 setting 的元数据（用于参数校验与文档生成）"""
    key: str
    type: str           # bool / int / float / str
    default: object     # 服务端默认值，None 表示随设备/系统变化
    clearable: bool     # 是否允许用 null 清空
    description: str


#: 全部 settings 的元数据表，key 与 AppiumSettings 成员值一致
APPIUM_SETTINGS_SPEC = {
    s.key: s
    for s in (
        AppiumSettingSpec("shouldUseCompactResponses", "bool", True, False,
                          "元素响应是否只返回精简字段（不含完整属性树）"),
        AppiumSettingSpec("elementResponseAttributes", "str", "type,label", False,
                          "逗号分隔的元素属性白名单，仅在精简响应时生效"),
        AppiumSettingSpec("snapshotMaxDepth", "int", 50, False,
                          "page source 快照的最大层级深度，越大越慢"),
        AppiumSettingSpec("snapshotMaxChildren", "int", None, False,
                          "page source 快照中每个节点的最大子节点数"),
        AppiumSettingSpec("useFirstMatch", "bool", False, False,
                          "查找元素时命中第一个即返回，可显著提速"),
        AppiumSettingSpec("boundElementsByIndex", "bool", False, False,
                          "按索引绑定元素，同一查询多次结果保持一致"),
        AppiumSettingSpec("limitXPathContextScope", "bool", False, False,
                          "限制 XPath 查询上下文范围以提速"),
        AppiumSettingSpec("enforceCustomSnapshots", "bool", False, False,
                          "强制使用自定义快照逻辑"),
        AppiumSettingSpec("includeHittableInPageSource", "bool", False, False,
                          "page source 中输出 hittable 属性"),
        AppiumSettingSpec("includeNativeFrameInPageSource", "bool", False, False,
                          "page source 中输出原生 frame"),
        AppiumSettingSpec("includeNativeAccessibilityElementInPageSource", "bool", False, False,
                          "page source 中输出原生无障碍元素信息"),
        AppiumSettingSpec("includeMinMaxValueInPageSource", "bool", False, False,
                          "page source 中输出 min/max value"),
        AppiumSettingSpec("includeCustomActionsInPageSource", "bool", False, False,
                          "page source 中输出自定义 actions"),
        AppiumSettingSpec("screenshotQuality", "int", 1, False,
                          "截图质量（1=lossless .. 3=low，服务端用 XCTest 枚举）"),
        AppiumSettingSpec("screenshotOrientation", "str", "auto", False,
                          "截图方向: auto / portrait / landscape"),
        AppiumSettingSpec("mjpegServerScreenshotQuality", "int", 25, False,
                          "MJPEG 推流截图质量 1..100"),
        AppiumSettingSpec("mjpegServerFramerate", "int", 10, False,
                          "MJPEG 推流帧率"),
        AppiumSettingSpec("mjpegScalingFactor", "int", 100, False,
                          "MJPEG 推流缩放百分比 1..100"),
        AppiumSettingSpec("mjpegFixOrientation", "bool", False, False,
                          "MJPEG 推流是否修正方向"),
        AppiumSettingSpec("keyboardAutocorrection", "bool", False, False,
                          "是否关闭键盘自动纠错（服务端按 enabled/disabled 处理）"),
        AppiumSettingSpec("keyboardPrediction", "bool", False, False,
                          "是否关闭键盘联想预测"),
        AppiumSettingSpec("maxTypingFrequency", "int", 60, False,
                          "每秒最大输入字符数，调小可规避输入丢字"),
        AppiumSettingSpec("useClearTextShortcut", "bool", True, False,
                          "清空输入框时是否使用全选删除快捷键"),
        AppiumSettingSpec("waitForIdleTimeout", "float", 10.0, False,
                          "等待应用空闲的超时时间（秒）"),
        AppiumSettingSpec("animationCoolOffTimeout", "float", 2.0, False,
                          "动画冷却时间（秒）"),
        AppiumSettingSpec("accessibilityDeadline", "float", 120.0, False,
                          "无障碍快照获取的最长等待（秒）"),
        AppiumSettingSpec("reduceMotion", "bool", False, False,
                          "是否开启减弱动效"),
        AppiumSettingSpec("defaultActiveApplication", "str", "auto", False,
                          "默认活跃应用，'auto' 为系统前台应用"),
        AppiumSettingSpec("activeAppDetectionPoint", "str", "64.00,64.00", False,
                          "判定活跃应用的屏幕探测点，格式 'x,y'（逻辑点）"),
        AppiumSettingSpec("defaultAlertAction", "str", "", True,
                          "弹窗默认动作: accept / dismiss，空字符串表示不自动处理"),
        AppiumSettingSpec("acceptAlertButtonSelector", "str", "", True,
                          "弹窗确认按钮的 class chain 选择器"),
        AppiumSettingSpec("dismissAlertButtonSelector", "str", "", True,
                          "弹窗取消按钮的 class chain 选择器"),
        AppiumSettingSpec("autoClickAlertSelector", "str", "", True,
                          "自动点击弹窗按钮的 class chain 选择器，置空则关闭监控"),
        AppiumSettingSpec("respectSystemAlerts", "bool", False, False,
                          "是否让系统级弹窗也参与自动处理"),
    )
}


class AlertAction(str, enum.Enum):
    ACCEPT = "accept"
    DISMISS = "dismiss"


# default_alert_accept_selector = "**/XCUIElementTypeButton[`label IN {'允许','好','仅在使用应用期间','暂不'}`]"
# default_alert_dismiss_selector = "**/XCUIElementTypeButton[`label IN {'不允许','暂不'}`]"
