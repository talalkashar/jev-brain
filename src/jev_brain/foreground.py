"""Read the focused macOS application and window without changing focus or prompting."""

import ctypes as C
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from urllib.parse import urlsplit

AX = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
CG = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
# Unknown bundles cannot opt themselves in by supplying a familiar AXTitle.
NATIVE = {"com.apple.TextEdit": "TextEdit", "com.apple.Notes": "Notes",
          "com.apple.finder": "Finder", "com.apple.iCal": "Calendar"}
BROWSERS = frozenset({
    "com.apple.Safari", "com.apple.SafariTechnologyPreview", "com.google.Chrome",
    "com.google.Chrome.canary", "org.chromium.Chromium", "com.microsoft.edgemac",
    "com.brave.Browser", "org.mozilla.firefox", "org.mozilla.firefoxdeveloperedition",
    "org.mozilla.nightly", "company.thebrowser.Browser", "com.vivaldi.Vivaldi",
})


def _function(lib, name, result, *args):
    fn = getattr(lib, name)
    fn.restype = result
    fn.argtypes = list(args)
    return fn


def permissions():
    if sys.platform != "darwin":
        return False, False
    accessibility = _function(C.CDLL(AX), "AXIsProcessTrusted", C.c_bool)
    recording = _function(C.CDLL(CG), "CGPreflightScreenCaptureAccess", C.c_bool)
    return accessibility(), recording()


@dataclass(frozen=True)
class Window:
    app: str
    title: str
    document: str
    field_role: str
    bundle: str = ""
    pid: int = 0
    field_subrole: str = ""
    field_label: str = ""
    placeholder: str = ""
    description: str = ""

    def matches(self, expect: str) -> bool:
        if not (self.app.strip() and self.title.strip() and self.field_role.startswith("AX") and self.pid > 0):
            return False
        if self.bundle in BROWSERS:
            try:
                url = urlsplit(self.document)
                return (url.scheme in ("http", "https") and bool(url.hostname)
                        and not url.username and not url.password and self.document == expect)
            except ValueError:
                return False
        return (NATIVE.get(self.bundle) == self.app and self.app.casefold() == expect.casefold()
                and not self.document.startswith(("http://", "https://")))


def _bundle_id(pid):
    # NSRunningApplication binds the bundle identifier to the PID of the AX app.
    C.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc = C.CDLL("/usr/lib/libobjc.A.dylib")
    ptr = C.c_void_p
    cls = _function(objc, "objc_getClass", ptr, C.c_char_p)
    selector = _function(objc, "sel_registerName", ptr, C.c_char_p)
    get_app = C.CFUNCTYPE(ptr, ptr, ptr, C.c_int)(("objc_msgSend", objc))
    get_object = C.CFUNCTYPE(ptr, ptr, ptr)(("objc_msgSend", objc))
    get_bytes = C.CFUNCTYPE(C.c_char_p, ptr, ptr)(("objc_msgSend", objc))
    app = get_app(cls(b"NSRunningApplication"), selector(b"runningApplicationWithProcessIdentifier:"), pid)
    bundle = get_object(app, selector(b"bundleIdentifier"))
    value = get_bytes(bundle, selector(b"UTF8String"))
    if not value:
        raise OSError("Could not identify the front application.")
    return value.decode("utf-8")


def current() -> Window:
    if sys.platform != "darwin":
        raise OSError("Live input needs macOS.")
    ax, cf = C.CDLL(AX), C.CDLL(CF)
    ptr, integer = C.c_void_p, C.c_int
    system = _function(ax, "AXUIElementCreateSystemWide", ptr)
    copy_attr = _function(ax, "AXUIElementCopyAttributeValue", integer, ptr, ptr, C.POINTER(ptr))
    get_pid = _function(ax, "AXUIElementGetPid", integer, ptr, C.POINTER(integer))
    equal = _function(cf, "CFEqual", C.c_bool, ptr, ptr)
    release = _function(cf, "CFRelease", None, ptr)
    new_string = _function(cf, "CFStringCreateWithCString", ptr, ptr, C.c_char_p, C.c_uint32)
    type_id = _function(cf, "CFGetTypeID", C.c_ulong, ptr)
    string_type = _function(cf, "CFStringGetTypeID", C.c_ulong)()
    url_type = _function(cf, "CFURLGetTypeID", C.c_ulong)()
    url_string = _function(cf, "CFURLGetString", ptr, ptr)
    string_length = _function(cf, "CFStringGetLength", C.c_long, ptr)
    read_string = _function(cf, "CFStringGetCString", C.c_bool, ptr, ptr, C.c_long, C.c_uint32)
    utf8 = 0x08000100
    with ExitStack() as refs:
        def owned(ref):
            if ref:
                refs.callback(release, ref)
            return ref

        def attr(element, key):
            value = ptr()
            name = owned(new_string(None, key.encode("ascii"), utf8))
            if not element or not name:
                raise OSError("Could not read accessibility metadata.")
            status = copy_attr(element, name, C.byref(value))
            if status == 0 and value:
                return owned(value)
            if status in (-25205, -25212):  # AXAttributeUnsupported, AXNoValue
                return None
            raise OSError("Could not read accessibility metadata.")

        def string(ref):
            if not ref:
                return ""
            if type_id(ref) == url_type:
                ref = url_string(ref)
            if not ref or type_id(ref) != string_type:
                raise OSError("Unreadable accessibility string.")
            buf = C.create_string_buffer(16384)
            if not read_string(ref, buf, len(buf), utf8):
                raise OSError("Could not read the front window.")
            text = buf.value.decode("utf-8")
            if len(text.encode("utf-16-le")) // 2 != string_length(ref):
                raise OSError("Incomplete accessibility string.")
            return text

        desktop = owned(system())
        app = attr(desktop, "AXFocusedApplication")
        window = attr(app, "AXFocusedWindow")
        field = attr(app, "AXFocusedUIElement")
        pid = integer()
        if not (window and field) or get_pid(app, C.byref(pid)) or pid.value <= 0:
            raise OSError("Could not identify the front window or focused control.")
        observed = Window(
            string(attr(app, "AXTitle")), string(attr(window, "AXTitle")), string(attr(window, "AXDocument")),
            string(attr(field, "AXRole")), _bundle_id(pid.value), pid.value, string(attr(field, "AXSubrole")),
            string(attr(field, "AXTitle")), string(attr(field, "AXPlaceholderValue")),
            string(attr(field, "AXDescription")),
        )
        for owner, name, original in ((desktop, "AXFocusedApplication", app), (app, "AXFocusedWindow", window),
                                      (app, "AXFocusedUIElement", field)):
            latest = attr(owner, name)
            if not latest or not equal(latest, original):
                raise OSError("Focus changed during observation.")
        return observed
