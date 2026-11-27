"""Read the focused macOS application and window without changing focus or prompting."""

import ctypes as C
import sys
from contextlib import ExitStack
from dataclasses import dataclass

AX = "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
CG = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
CF = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
BROWSERS = {"Google Chrome", "Safari", "Chromium", "Microsoft Edge", "Brave Browser", "Firefox", "Arc"}


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

    def matches(self, expect: str) -> bool:
        if not self.app:
            return False
        if (self.app in BROWSERS or self.document.startswith(("http://", "https://")) or
                expect.startswith(("http://", "https://"))):
            return self.document.startswith(("http://", "https://")) and self.document == expect
        return bool(self.app) and self.app.casefold() == expect.casefold()


def current() -> Window:
    if sys.platform != "darwin":
        raise OSError("Live input needs macOS.")
    ax, cf = C.CDLL(AX), C.CDLL(CF)
    ptr, integer = C.c_void_p, C.c_int
    system = _function(ax, "AXUIElementCreateSystemWide", ptr)
    copy_attr = _function(ax, "AXUIElementCopyAttributeValue", integer, ptr, ptr, C.POINTER(ptr))
    release = _function(cf, "CFRelease", None, ptr)
    new_string = _function(cf, "CFStringCreateWithCString", ptr, ptr, C.c_char_p, C.c_uint32)
    type_id = _function(cf, "CFGetTypeID", C.c_ulong, ptr)
    string_type = _function(cf, "CFStringGetTypeID", C.c_ulong)()
    url_type = _function(cf, "CFURLGetTypeID", C.c_ulong)()
    url_string = _function(cf, "CFURLGetString", ptr, ptr)
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
            if element and copy_attr(element, name, C.byref(value)) == 0:
                return owned(value)
            return None

        def string(ref):
            if ref and type_id(ref) == url_type:
                ref = url_string(ref)
            if not ref or type_id(ref) != string_type:
                return ""
            buf = C.create_string_buffer(16384)
            if not read_string(ref, buf, len(buf), utf8):
                raise OSError("Could not read the front window.")
            return buf.value.decode("utf-8")

        app = attr(owned(system()), "AXFocusedApplication")
        window = attr(app, "AXFocusedWindow")
        if not window:
            raise OSError("Could not read the front window. Check Accessibility in jev-brain doctor.")
        field = attr(app, "AXFocusedUIElement")
        return Window(string(attr(app, "AXTitle")), string(attr(window, "AXTitle")),
                      string(attr(window, "AXDocument")), string(attr(field, "AXSubrole")))
