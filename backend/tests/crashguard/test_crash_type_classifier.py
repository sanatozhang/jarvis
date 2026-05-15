"""Tests for crash_type_classifier."""
from __future__ import annotations
import pytest
from app.crashguard.services.crash_type_classifier import classify_crash_type


def test_anr_from_title():
    assert classify_crash_type("ANR in ai.plaud.android", "", {}) == "anr"
    assert classify_crash_type("Application Not Responding - MainActivity", "", {}) == "anr"


def test_anr_from_stack():
    stack = "android.os.Process.sendSignal\nandroid.os.Process.killProcess"
    assert classify_crash_type("Some crash", stack, {}) == "anr"


def test_freeze_from_title():
    assert classify_crash_type("App freeze detected", "", {}) == "freeze"
    assert classify_crash_type("卡顿 60s on HomeScreen", "", {}) == "freeze"
    assert classify_crash_type("Watchdog terminated app", "", {}) == "freeze"


def test_oom_from_title():
    assert classify_crash_type("OutOfMemoryError in bitmap", "", {}) == "oom"
    assert classify_crash_type("OOM crash on image load", "", {}) == "oom"


def test_native_crash_from_stack():
    stack = "SIGSEGV at 0x0000dead\n  #00 flutter::dart::..."
    assert classify_crash_type("Fatal signal", stack, {}) == "native_crash"
    stack2 = "EXC_BAD_ACCESS (SIGSEGV)"
    assert classify_crash_type("crash", stack2, {}) == "native_crash"


def test_default_crash():
    assert classify_crash_type("NullPointerException in foo", "at com.foo.Bar.baz(Bar.java:12)", {}) == "crash"


def test_anr_beats_default():
    """ANR title + normal stack → still anr."""
    assert classify_crash_type("ANR in Service", "java.lang.Thread.sleep", {}) == "anr"
