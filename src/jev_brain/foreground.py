"""Read the focused macOS application and window without changing focus or prompting."""

import ctypes as C
import sys
import time
from collections.abc import Sequence
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
    try:
        recording = _function(C.CDLL(CG), "CGPreflightScreenCaptureAccess", C.c_bool)()
    except (AttributeError, OSError):
        recording = False
    return accessibility(), recording


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
    pool = _function(objc, "objc_autoreleasePoolPush", ptr)()
    try:
        app = get_app(cls(b"NSRunningApplication"), selector(b"runningApplicationWithProcessIdentifier:"), pid)
        bundle = get_object(app, selector(b"bundleIdentifier"))
        value = get_bytes(bundle, selector(b"UTF8String"))
        if not value:
            raise OSError("Could not identify the front application.")
        return value.decode("utf-8")
    finally:
        _function(objc, "objc_autoreleasePoolPop", None, ptr)(pool)


class TargetUnavailable(ValueError):
    def __init__(self, outcome="low confidence"):
        super().__init__(outcome)
        self.outcome = outcome


def unique_target(root, label, children, attribute):
    # Count every exposed reference, including overlapping elements. Repeated or
    # cyclic subtrees may cause a conservative refusal at the traversal limit.
    pending = [root]
    visited = 0
    match = None
    while pending:
        ref = pending.pop()
        visited += 1
        descendants = children(ref)
        if descendants is None:  # AX explicitly reported unsupported or no value.
            descendants = ()
        if not isinstance(descendants, Sequence) or isinstance(descendants, (str, bytes, bytearray)):
            raise TargetUnavailable()
        if visited + len(pending) + len(descendants) > 512:
            raise TargetUnavailable()
        pending.extend(descendants)
        names = (attribute(ref, "AXTitle"), attribute(ref, "AXDescription"))
        if any(name is not None and not isinstance(name, str) for name in names):
            raise TargetUnavailable()
        if label in names:
            if match is not None:
                raise TargetUnavailable()
            match = ref
    if match is None:
        raise TargetUnavailable("target not found")
    return match


def control_metadata(ref, label, attribute):
    metadata = tuple(attribute(ref, key) for key in
                     ("AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXPlaceholderValue"))
    if (any(v is not None and not isinstance(v, str) for v in metadata)
            or not metadata[0] or not metadata[0].startswith("AX") or label not in metadata[2:4]):
        raise TargetUnavailable()
    return tuple(value or "" for value in metadata)


def target_metadata(approved, label, deadline):
    return _read(approved, label, deadline)[1]


def current() -> Window:
    return _read()[0]


def _read(approved=None, label=None, deadline=None):
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

        def check_time():
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError

        def attr(element, key):
            check_time()
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
        metadata = None
        if label is not None:
            if observed != approved:
                raise ValueError("Foreground changed before target lookup.")
            copy_children = _function(ax, "AXUIElementCopyAttributeValues", integer, ptr, ptr,
                                      C.c_long, C.c_long, C.POINTER(ptr))
            array_type = _function(cf, "CFArrayGetTypeID", C.c_ulong)()
            element_type = _function(ax, "AXUIElementGetTypeID", C.c_ulong)()
            count = _function(cf, "CFArrayGetCount", C.c_long, ptr)
            item = _function(cf, "CFArrayGetValueAtIndex", ptr, ptr, C.c_long)
            key = owned(new_string(None, b"AXChildren", utf8))
            if not key:
                raise OSError("Could not read accessibility children.")

            def children(ref):
                check_time()
                array = ptr()
                status = copy_children(ref, key, 0, 513, C.byref(array))
                if status in (-25205, -25212):
                    return []
                if status or not array or type_id(owned(array)) != array_type or count(array) > 512:
                    raise ValueError("Incomplete accessibility tree.")
                children_refs = [item(array, i) for i in range(count(array))]
                if any(not child or type_id(child) != element_type for child in children_refs):
                    raise ValueError("Unreadable accessibility child.")
                return children_refs

            def attribute(ref, name):
                return string(attr(ref, name))

            match = unique_target(window, label, children, attribute)
            owner = attr(match, "AXWindow")
            if not owner or not equal(owner, window):
                raise TargetUnavailable()
            metadata = control_metadata(match, label, attribute)
        for owner, name, original in ((desktop, "AXFocusedApplication", app), (app, "AXFocusedWindow", window),
                                      (app, "AXFocusedUIElement", field)):
            latest = attr(owner, name)
            if not latest or not equal(latest, original):
                raise OSError("Focus changed during observation.")
        return observed, metadata
